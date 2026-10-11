-- collaude schema, version 006: folders and connector routes can belong to several groups.
--
-- A folder's .collaude.json may list several groups, and one Slack channel, mail
-- folder or repo may feed several groups. Memories, sessions, messages and routes
-- now carry `group_ids`, the groups they are shared with, and row-level security
-- lets you read a row when it is shared with any group of yours.
--
-- `group_id` stays, as the first of `group_ids`, so clients from before this
-- version keep working: a row written with only `group_id` is shared with that
-- one group, and a row written with only `group_ids` gets `group_id` filled in.

-- Helpers -------------------------------------------------------------------

create function collaude.my_group_ids() returns text[]
  language sql stable security definer
  set search_path = pg_catalog, collaude
  as $$ select coalesce(array_agg(g order by g), '{}') from collaude.my_groups() g $$;

-- Keep group_id and group_ids in step, drop repeats, and refuse unknown groups.
-- Security definer so the check sees groups the writer is not a member of.
create function collaude.fill_group_ids() returns trigger
  language plpgsql security definer
  set search_path = pg_catalog, collaude
  as $$
begin
  if new.group_ids is null or cardinality(new.group_ids) = 0 then
    new.group_ids := array[new.group_id];
  end if;
  new.group_ids := array(select g from unnest(new.group_ids) with ordinality t (g, i)
                         where g is not null group by g order by min(i));
  if new.group_id is null or not new.group_id = any (new.group_ids) then
    new.group_id := new.group_ids[1];
  end if;
  if exists (select 1 from unnest(new.group_ids) g where g not in (select id from collaude.groups)) then
    raise exception 'unknown group in %', new.group_ids using errcode = '23503';
  end if;
  return new;
end
$$;

revoke all on function collaude.my_group_ids() from public;
revoke all on function collaude.fill_group_ids() from public;
grant execute on function collaude.my_group_ids() to collaude_member;

-- Columns -------------------------------------------------------------------

alter table collaude.memories         add column group_ids text[];
alter table collaude.sessions         add column group_ids text[];
alter table collaude.sources          add column group_ids text[];
alter table collaude.connector_routes add column group_ids text[];

update collaude.memories         set group_ids = array[group_id];
update collaude.sessions         set group_ids = array[group_id];
update collaude.sources          set group_ids = array[group_id];
update collaude.connector_routes set group_ids = array[group_id];

alter table collaude.memories
  alter column group_ids set not null,
  add constraint memories_group_ids_check check (cardinality(group_ids) > 0 and group_id = any (group_ids));
alter table collaude.sessions
  alter column group_ids set not null,
  add constraint sessions_group_ids_check check (cardinality(group_ids) > 0 and group_id = any (group_ids));
alter table collaude.sources
  alter column group_ids set not null,
  add constraint sources_group_ids_check check (cardinality(group_ids) > 0 and group_id = any (group_ids));
alter table collaude.connector_routes
  alter column group_ids set not null,
  add constraint routes_group_ids_check check (cardinality(group_ids) > 0 and group_id = any (group_ids));

-- Named to sort before memories_before_update, so that trigger sees group_ids filled in.
create trigger fill_group_ids before insert or update on collaude.memories
  for each row execute function collaude.fill_group_ids();
create trigger fill_group_ids before insert or update on collaude.sessions
  for each row execute function collaude.fill_group_ids();
create trigger fill_group_ids before insert or update on collaude.sources
  for each row execute function collaude.fill_group_ids();
create trigger fill_group_ids before insert or update on collaude.connector_routes
  for each row execute function collaude.fill_group_ids();

create index memories_group_ids_idx on collaude.memories using gin (group_ids);
create index sources_group_ids_idx on collaude.sources using gin (group_ids);

-- A memory can only be superseded by one that everyone who sees it can also see.
-- Checked only when superseded_by changes, so sharing an old memory with another
-- group later doesn't trip over its history.
create or replace function collaude.memories_before_update() returns trigger
  language plpgsql
  as $$
begin
  if new.superseded_by is not null and new.superseded_by is distinct from old.superseded_by then
    if new.superseded_by = new.id then
      raise exception 'a memory cannot supersede itself';
    end if;
    if not exists (select 1 from collaude.memories m
                   where m.id = new.superseded_by and new.group_ids <@ m.group_ids) then
      raise exception 'superseding memory % is not shared with every group of memory % (%)',
        new.superseded_by, new.id, array_to_string(new.group_ids, ', ');
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

-- Access control ------------------------------------------------------------

-- Who may share a row with a set of groups: anyone who belongs to all of them, or,
-- for what a connector brings in, a member of one of them when a route the
-- connector's creator set up already feeds all of them.
create function collaude.can_share(p_groups text[]) returns boolean
  language sql stable security definer
  set search_path = pg_catalog, collaude
  as $$
    select cardinality(p_groups) > 0
       and (p_groups <@ collaude.my_group_ids()
            or (p_groups && collaude.my_group_ids()
                and exists (select 1 from collaude.connector_routes r where r.group_ids @> p_groups)))
  $$;

revoke all on function collaude.can_share(text[]) from public;
grant execute on function collaude.can_share(text[]) to collaude_member;

drop policy sessions_read on collaude.sessions;
drop policy sessions_write on collaude.sessions;
create policy sessions_read on collaude.sessions for select to collaude_member
  using (group_ids && (select collaude.my_group_ids()));
create policy sessions_write on collaude.sessions for insert to collaude_member
  with check (author = session_user and group_ids <@ (select collaude.my_group_ids()));

drop policy memories_read on collaude.memories;
drop policy memories_write on collaude.memories;
drop policy memories_status on collaude.memories;
create policy memories_read on collaude.memories for select to collaude_member
  using (group_ids && (select collaude.my_group_ids()));
create policy memories_write on collaude.memories for insert to collaude_member
  with check (author = session_user
              and collaude.can_share(group_ids)
              and (session_id is null
                   or exists (select 1 from collaude.sessions s
                              where s.id = session_id and s.author = session_user)));
create policy memories_status on collaude.memories for update to collaude_member
  using (group_ids && (select collaude.my_group_ids()))
  with check (group_ids && (select collaude.my_group_ids()));

drop policy routes_read on collaude.connector_routes;
drop policy routes_create on collaude.connector_routes;
drop policy routes_progress on collaude.connector_routes;
drop policy routes_delete on collaude.connector_routes;
create policy routes_read on collaude.connector_routes for select to collaude_member
  using (group_ids && (select collaude.my_group_ids()));
create policy routes_create on collaude.connector_routes for insert to collaude_member
  with check (added_by = session_user
              and group_ids <@ (select collaude.my_group_ids())
              and collaude.connector_creator(connector_id) = session_user);
create policy routes_progress on collaude.connector_routes for update to collaude_member
  using (group_ids && (select collaude.my_group_ids()));
-- Deleting a whole route takes it from every group it feeds, so only someone in all
-- of them may; anyone else uses unroute_source() to take out their own groups.
create policy routes_delete on collaude.connector_routes for delete to collaude_member
  using (group_ids <@ (select collaude.my_group_ids()));

drop policy sources_read on collaude.sources;
drop policy sources_write on collaude.sources;
drop policy sources_distill on collaude.sources;
create policy sources_read on collaude.sources for select to collaude_member
  using (group_ids && (select collaude.my_group_ids()));
create policy sources_write on collaude.sources for insert to collaude_member
  with check (ingested_by = session_user
              and exists (select 1 from collaude.connector_routes r
                          where r.connector_id = sources.connector_id
                            and r.source_key = sources.source_key
                            and r.group_ids @> sources.group_ids));
create policy sources_distill on collaude.sources for update to collaude_member
  using (group_ids && (select collaude.my_group_ids()));

create or replace function collaude.my_connectors() returns setof text
  language sql stable security definer
  set search_path = pg_catalog, collaude
  as $$
    select distinct r.connector_id from collaude.connector_routes r
    where r.group_ids && collaude.my_group_ids()
  $$;

create or replace function collaude.claim_ingest(p_connector text, p_min_interval interval, p_lease interval)
  returns boolean
  language plpgsql security definer
  set search_path = pg_catalog, collaude
  as $$
begin
  if not exists (select 1 from collaude.connector_routes r
                 where r.connector_id = p_connector and r.group_ids && collaude.my_group_ids()) then
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

-- Routing -------------------------------------------------------------------

-- Feed a channel, folder or repo into one more group. Only the connector's creator,
-- and only into a group of theirs. If the source already feeds other groups, the
-- new group also gets the messages already ingested within the backfill window,
-- and the memories made only from those messages.
-- Returns 'added' (a new route), 'joined' (a group added to a route) or 'already'.
create function collaude.route_source(p_connector text, p_key text, p_label text, p_group text, p_backfill interval)
  returns text
  language plpgsql security definer
  set search_path = pg_catalog, collaude
  as $$
declare
  r collaude.connector_routes;
begin
  if collaude.connector_creator(p_connector) is distinct from session_user then
    raise exception 'only the creator of connector % can route it', p_connector using errcode = '42501';
  end if;
  if not p_group = any (collaude.my_group_ids()) then
    raise exception 'you are not in group %', p_group using errcode = '42501';
  end if;
  select * into r from collaude.connector_routes
   where connector_id = p_connector and source_key = p_key for update;
  if not found then
    insert into collaude.connector_routes (connector_id, source_key, source_label, group_ids, backfill_from, added_by)
    values (p_connector, p_key, p_label, array[p_group], now() - p_backfill, session_user);
    return 'added';
  end if;
  if p_group = any (r.group_ids) then
    return 'already';
  end if;
  update collaude.connector_routes set group_ids = group_ids || p_group
   where connector_id = p_connector and source_key = p_key;
  update collaude.sources set group_ids = group_ids || p_group
   where connector_id = p_connector and source_key = p_key
     and source_time >= now() - p_backfill and not p_group = any (group_ids);
  update collaude.memories m set group_ids = m.group_ids || p_group
   where not p_group = any (m.group_ids)
     and exists (select 1 from collaude.memory_sources ms join collaude.sources s on s.id = ms.source_id
                 where ms.memory_id = m.id and s.connector_id = p_connector and s.source_key = p_key)
     and not exists (select 1 from collaude.memory_sources ms join collaude.sources s on s.id = ms.source_id
                     where ms.memory_id = m.id and not p_group = any (s.group_ids));
  return 'joined';
end
$$;

-- Stop feeding sources of a connector into your groups: p_source is a source key or
-- label (null for all of them), p_group one group (null for every group of yours).
-- A route left feeding no group is removed. Ingested messages and memories stay.
-- Returns how many routes changed.
create function collaude.unroute_source(p_connector text, p_source text, p_group text)
  returns integer
  language plpgsql security definer
  set search_path = pg_catalog, collaude
  as $$
declare
  r collaude.connector_routes;
  mine text[] := collaude.my_group_ids();
  leaving text[];
  n integer := 0;
begin
  for r in select * from collaude.connector_routes
            where connector_id = p_connector
              and (p_source is null or source_key = p_source or source_label = p_source)
            for update loop
    leaving := array(select g from unnest(r.group_ids) g
                     where g = any (mine) and (p_group is null or g = p_group));
    continue when cardinality(leaving) = 0;
    if r.group_ids <@ leaving then
      delete from collaude.connector_routes where connector_id = r.connector_id and source_key = r.source_key;
    else
      update collaude.connector_routes
         set group_ids = array(select g from unnest(r.group_ids) g where not g = any (leaving))
       where connector_id = r.connector_id and source_key = r.source_key;
    end if;
    n := n + 1;
  end loop;
  return n;
end
$$;

revoke all on function collaude.route_source(text, text, text, text, interval) from public;
revoke all on function collaude.unroute_source(text, text, text) from public;
grant execute on function collaude.route_source(text, text, text, text, interval) to collaude_member;
grant execute on function collaude.unroute_source(text, text, text) to collaude_member;
