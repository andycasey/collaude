-- collaude schema, version 002: external sources (Slack, email, ...).
--
-- Connectors and their channel/folder -> group routes live in the database, so
-- any collaborator who holds the connector's credentials locally can run the
-- ingest. Credentials themselves never touch the database.
--
-- Raw messages land in `sources`; a distillation pass turns quiet threads into
-- memories and links each memory to the messages it came from.

create table collaude.connectors (
  id           text primary key check (id ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
  kind         text not null,                 -- slack | imap | ...
  settings     jsonb not null default '{}',   -- non-secret settings only
  enabled      boolean not null default true,
  created_by   name not null default session_user references collaude.members (role_name),
  created_at   timestamptz not null default now(),
  lease_holder name,
  lease_until  timestamptz,
  last_run_at  timestamptz,
  last_run_by  name,
  last_error   text
);

-- One channel / mail folder feeds exactly one group.
create table collaude.connector_routes (
  connector_id   text not null references collaude.connectors (id) on delete cascade,
  source_key     text not null,                -- Slack channel id, IMAP folder name, ...
  source_label   text,                         -- '#clump-rotation', 'lists/stars'
  group_id       text not null references collaude.groups (id),
  backfill_from  timestamptz not null default now() - interval '14 days',
  cursor         text,                         -- connector-specific high-water mark
  last_synced_at timestamptz,
  added_by       name not null default session_user references collaude.members (role_name),
  added_at       timestamptz not null default now(),
  primary key (connector_id, source_key)
);

create table collaude.sources (
  id            bigint generated always as identity primary key,
  connector_id  text not null references collaude.connectors (id),
  source_key    text not null,
  group_id      text not null references collaude.groups (id),
  origin        text not null,                 -- slack | email | ...
  external_id   text not null,                 -- Slack ts, Message-ID, ...
  thread_key    text not null,
  container     text,                          -- channel or list label
  author_name   text,
  author_handle text,                          -- email address, Slack user id
  source_time   timestamptz not null,
  permalink     text,
  subject       text,
  content       text not null,
  ingested_by   name not null default session_user references collaude.members (role_name),
  ingested_at   timestamptz not null default now(),
  distilled_at  timestamptz,
  search        tsvector generated always as (
                  setweight(to_tsvector('english', coalesce(subject, '')), 'A') ||
                  setweight(to_tsvector('english', content), 'B') ||
                  setweight(to_tsvector('simple', coalesce(author_name, '')), 'C')
                ) stored,
  unique (connector_id, external_id)
);

create index sources_search_idx on collaude.sources using gin (search);
create index sources_pending_idx on collaude.sources (connector_id, source_time) where distilled_at is null;
create index sources_thread_idx on collaude.sources (connector_id, thread_key, source_time);

create table collaude.memory_sources (
  memory_id bigint not null references collaude.memories (id),
  source_id bigint not null references collaude.sources (id),
  primary key (memory_id, source_id)
);

-- When the underlying discussion happened (memories distilled from old messages).
alter table collaude.memories add column source_time timestamptz;

-- Leases --------------------------------------------------------------------
-- A lease, not an advisory lock, so this works through transaction-mode poolers.

create function collaude.claim_ingest(p_connector text, p_min_interval interval, p_lease interval)
  returns boolean
  language plpgsql security definer
  set search_path = pg_catalog, collaude
  as $$
begin
  if not exists (select 1 from collaude.connector_routes r
                 where r.connector_id = p_connector and r.group_id in (select collaude.my_groups())) then
    raise exception 'you are not in any group fed by connector %', p_connector using errcode = '42501';
  end if;
  update collaude.connectors c
     set lease_holder = session_user, lease_until = now() + p_lease
   where c.id = p_connector and c.enabled
     and (c.lease_until is null or c.lease_until < now())
     and (c.last_run_at is null or c.last_run_at < now() - p_min_interval);
  return found;
end
$$;

create function collaude.finish_ingest(p_connector text, p_error text)
  returns void
  language sql security definer
  set search_path = pg_catalog, collaude
  as $$
    update collaude.connectors
       set lease_holder = null, lease_until = null, last_run_at = now(), last_run_by = session_user,
           last_error = p_error
     where id = p_connector and lease_holder = session_user
  $$;

revoke all on function collaude.claim_ingest(text, interval, interval) from public;
revoke all on function collaude.finish_ingest(text, text) from public;
grant execute on function collaude.claim_ingest(text, interval, interval) to collaude_member;
grant execute on function collaude.finish_ingest(text, text) to collaude_member;

-- Access control ------------------------------------------------------------

-- Helpers that read across the two tables without re-entering their policies
-- (a policy on connectors that queries connector_routes, and vice versa, recurses).
create function collaude.my_connectors() returns setof text
  language sql stable security definer
  set search_path = pg_catalog, collaude
  as $$
    select distinct r.connector_id from collaude.connector_routes r
    where r.group_id in (select collaude.my_groups())
  $$;

create function collaude.connector_creator(p_connector text) returns name
  language sql stable security definer
  set search_path = pg_catalog, collaude
  as $$ select created_by from collaude.connectors where id = p_connector $$;

revoke all on function collaude.my_connectors() from public;
revoke all on function collaude.connector_creator(text) from public;
grant execute on function collaude.my_connectors() to collaude_member;
grant execute on function collaude.connector_creator(text) to collaude_member;

alter table collaude.connectors       enable row level security;
alter table collaude.connector_routes enable row level security;
alter table collaude.sources          enable row level security;
alter table collaude.memory_sources   enable row level security;

create policy connectors_read on collaude.connectors for select to collaude_member
  using (created_by = session_user or id in (select collaude.my_connectors()));
create policy connectors_create on collaude.connectors for insert to collaude_member
  with check (created_by = session_user);
create policy connectors_edit on collaude.connectors for update to collaude_member
  using (created_by = session_user);
create policy connectors_delete on collaude.connectors for delete to collaude_member
  using (created_by = session_user);

create policy routes_read on collaude.connector_routes for select to collaude_member
  using (group_id in (select collaude.my_groups()));
-- Only the connector's creator can point it at a group, and only at their own groups.
create policy routes_create on collaude.connector_routes for insert to collaude_member
  with check (added_by = session_user
              and group_id in (select collaude.my_groups())
              and collaude.connector_creator(connector_id) = session_user);
create policy routes_progress on collaude.connector_routes for update to collaude_member
  using (group_id in (select collaude.my_groups()));
-- Any member of the group can disconnect a channel from it.
create policy routes_delete on collaude.connector_routes for delete to collaude_member
  using (group_id in (select collaude.my_groups()));

create policy sources_read on collaude.sources for select to collaude_member
  using (group_id in (select collaude.my_groups()));
create policy sources_write on collaude.sources for insert to collaude_member
  with check (ingested_by = session_user
              and exists (select 1 from collaude.connector_routes r
                          where r.connector_id = sources.connector_id
                            and r.source_key = sources.source_key
                            and r.group_id = sources.group_id));
create policy sources_distill on collaude.sources for update to collaude_member
  using (group_id in (select collaude.my_groups()));

create policy memory_sources_read on collaude.memory_sources for select to collaude_member
  using (exists (select 1 from collaude.memories m where m.id = memory_id));
create policy memory_sources_write on collaude.memory_sources for insert to collaude_member
  with check (exists (select 1 from collaude.memories m where m.id = memory_id and m.author = session_user)
              and exists (select 1 from collaude.sources s where s.id = source_id));

grant select on collaude.connectors, collaude.connector_routes, collaude.sources, collaude.memory_sources
  to collaude_member;
grant insert on collaude.connectors, collaude.connector_routes, collaude.sources, collaude.memory_sources
  to collaude_member;
grant update (settings, enabled) on collaude.connectors to collaude_member;
grant delete on collaude.connectors, collaude.connector_routes to collaude_member;
grant update (cursor, last_synced_at) on collaude.connector_routes to collaude_member;
grant update (distilled_at) on collaude.sources to collaude_member;
