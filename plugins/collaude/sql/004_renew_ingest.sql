-- collaude schema, version 004: renew an ingest lease while the run is still working.
--
-- A long run (a first backfill, many threads to distil) can outlast its lease.
-- The runner renews before each step; if the lease has lapsed it stops, so two
-- machines never distil the same threads.

create function collaude.renew_ingest(p_connector text, p_lease interval)
  returns boolean
  language plpgsql security definer
  set search_path = pg_catalog, collaude
  as $$
begin
  update collaude.connectors
     set lease_until = now() + p_lease
   where id = p_connector and lease_holder = session_user and lease_until > now();
  return found;
end
$$;

revoke all on function collaude.renew_ingest(text, interval) from public;
grant execute on function collaude.renew_ingest(text, interval) to collaude_member;
