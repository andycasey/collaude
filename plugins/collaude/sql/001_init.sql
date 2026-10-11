-- collaude schema, version 001.
--
-- Plain PostgreSQL (>= 14). Nothing here depends on a particular host: the same
-- file runs on Supabase, a self-managed server, RDS, etc.
--
-- Security model
--   * Every collaborator is a real Postgres LOGIN role that is a member of
--     `collaude_member`. Identity comes from the connection (session_user), so
--     authorship cannot be forged by the client.
--   * Groups are the unit of sharing. Row-level security limits every read and
--     write to the groups the connected role belongs to.
--   * Memories are append-only for members: they can insert, and change only
--     `status` / `superseded_by`. Nothing is deleted except by an admin.
--   * The admin (the role that runs migrations and owns the tables) bypasses
--     RLS as table owner.

create schema if not exists collaude;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'collaude_member') then
    create role collaude_member nologin;
  end if;
end
$$;

-- People -------------------------------------------------------------------

create table collaude.members (
  role_name    name primary key,
  email        text unique,
  display_name text,
  active       boolean not null default true,
  created_at   timestamptz not null default now()
);

create table collaude.groups (
  id          text primary key check (id ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
  name        text not null,
  description text,
  created_at  timestamptz not null default now()
);

create table collaude.memberships (
  group_id  text not null references collaude.groups (id) on delete cascade,
  role_name name not null references collaude.members (role_name) on delete cascade,
  added_at  timestamptz not null default now(),
  primary key (group_id, role_name)
);

-- Provenance ---------------------------------------------------------------

-- One row per Claude Code session that produced memories.
create table collaude.sessions (
  id              text primary key,          -- Claude Code session id
  author          name not null default session_user references collaude.members (role_name),
  group_id        text not null references collaude.groups (id),
  host            text,
  cwd             text,
  repo            text,                      -- git remote URL
  branch          text,
  commit_sha      text,
  transcript_path text,                      -- path on the author's machine
  client_version  text,
  created_at      timestamptz not null default now()
);

-- Optional condensed transcript chunks, uploaded only when a project opts in
-- (share_excerpts in .collaude.json).
create table collaude.excerpts (
  id          bigint generated always as identity primary key,
  session_id  text not null references collaude.sessions (id),
  author      name not null default session_user references collaude.members (role_name),
  captured_at timestamptz not null default now(),
  content     text not null
);

-- Memories -----------------------------------------------------------------

create function collaude.tags_text(text[]) returns text
  language sql immutable parallel safe
  as $$ select coalesce(array_to_string($1, ' '), '') $$;

create table collaude.memories (
  id                bigint generated always as identity primary key,
  group_id          text not null references collaude.groups (id),
  author            name not null default session_user references collaude.members (role_name),
  created_at        timestamptz not null default now(),
  kind              text not null check (kind in
                      ('finding', 'dead_end', 'decision', 'method', 'data_note', 'gotcha', 'question', 'other')),
  title             text not null check (length(title) between 3 and 200),
  body              text not null check (length(body) <= 4000),
  tags              text[] not null default '{}',
  confidence        text not null default 'medium' check (confidence in ('low', 'medium', 'high')),
  status            text not null default 'active' check (status in ('active', 'superseded', 'retracted')),
  superseded_by     bigint references collaude.memories (id),
  status_changed_by name,
  status_changed_at timestamptz,
  status_note       text,                    -- why it was superseded/retracted
  evidence_quote    text,                    -- verbatim from the transcript
  origin            text not null default 'claude-code',  -- claude-code | manual | slack | email | ...
  session_id        text references collaude.sessions (id),
  extractor         text,                    -- model/prompt version that wrote it
  search            tsvector generated always as (
                      setweight(to_tsvector('english', title), 'A') ||
                      setweight(to_tsvector('english', collaude.tags_text(tags)), 'A') ||
                      setweight(to_tsvector('english', body), 'B')
                    ) stored
);

create index memories_search_idx on collaude.memories using gin (search);
create index memories_group_created_idx on collaude.memories (group_id, created_at desc);
create index memories_session_idx on collaude.memories (session_id);

-- Record who changed a memory's status, and keep supersession inside a group.
create function collaude.memories_before_update() returns trigger
  language plpgsql
  as $$
begin
  if new.superseded_by is not null then
    if new.superseded_by = new.id then
      raise exception 'a memory cannot supersede itself';
    end if;
    if not exists (select 1 from collaude.memories m
                   where m.id = new.superseded_by and m.group_id = new.group_id) then
      raise exception 'superseding memory % is not in group %', new.superseded_by, new.group_id;
    end if;
  end if;
  if new.status is distinct from old.status
     or new.superseded_by is distinct from old.superseded_by
     or new.status_note is distinct from old.status_note then
    new.status_changed_by := session_user;
    new.status_changed_at := now();
  end if;
  return new;
end
$$;

create trigger memories_before_update
  before update on collaude.memories
  for each row execute function collaude.memories_before_update();

-- Access control -----------------------------------------------------------

create function collaude.my_groups() returns setof text
  language sql stable security definer
  set search_path = pg_catalog, collaude
  as $$
    select m.group_id
    from collaude.memberships m
    join collaude.members u using (role_name)
    where m.role_name = session_user and u.active
  $$;

revoke all on function collaude.my_groups() from public;

alter table collaude.members     enable row level security;
alter table collaude.groups      enable row level security;
alter table collaude.memberships enable row level security;
alter table collaude.sessions    enable row level security;
alter table collaude.excerpts    enable row level security;
alter table collaude.memories    enable row level security;

create policy groups_read on collaude.groups for select to collaude_member
  using (id in (select collaude.my_groups()));

create policy memberships_read on collaude.memberships for select to collaude_member
  using (group_id in (select collaude.my_groups()));

-- You can see yourself and the people you share a group with.
create policy members_read on collaude.members for select to collaude_member
  using (role_name = session_user
         or role_name in (select role_name from collaude.memberships));

create policy sessions_read on collaude.sessions for select to collaude_member
  using (group_id in (select collaude.my_groups()));
create policy sessions_write on collaude.sessions for insert to collaude_member
  with check (author = session_user and group_id in (select collaude.my_groups()));

create policy excerpts_read on collaude.excerpts for select to collaude_member
  using (exists (select 1 from collaude.sessions s where s.id = session_id));
create policy excerpts_write on collaude.excerpts for insert to collaude_member
  with check (author = session_user
              and exists (select 1 from collaude.sessions s
                          where s.id = session_id and s.author = session_user));

create policy memories_read on collaude.memories for select to collaude_member
  using (group_id in (select collaude.my_groups()));
create policy memories_write on collaude.memories for insert to collaude_member
  with check (author = session_user
              and group_id in (select collaude.my_groups())
              and (session_id is null
                   or exists (select 1 from collaude.sessions s
                              where s.id = session_id and s.author = session_user)));
create policy memories_status on collaude.memories for update to collaude_member
  using (group_id in (select collaude.my_groups()))
  with check (group_id in (select collaude.my_groups()));

grant usage on schema collaude to collaude_member;
grant execute on function collaude.my_groups() to collaude_member;
grant select on all tables in schema collaude to collaude_member;
grant insert on collaude.sessions, collaude.excerpts, collaude.memories to collaude_member;
grant update (status, superseded_by, status_note) on collaude.memories to collaude_member;
