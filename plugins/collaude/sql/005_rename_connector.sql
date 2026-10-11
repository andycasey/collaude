-- collaude schema, version 005: rename a connector.
--
-- Routes and ingested messages refer to a connector by its id, so a rename moves
-- them all in one transaction. Only the creator may rename, and not while a run
-- holds the lease. The old id is remembered in settings.previous_ids, so other
-- machines that stored credentials under it can pick up the new name.

create function collaude.rename_connector(p_old text, p_new text)
  returns void
  language plpgsql security definer
  set search_path = pg_catalog, collaude
  as $$
declare
  c collaude.connectors;
begin
  select * into c from collaude.connectors where id = p_old for update;
  if not found or c.created_by <> session_user then
    raise exception 'only the creator of connector % can rename it', p_old using errcode = '42501';
  end if;
  if c.lease_until > now() then
    raise exception 'connector % is running (lease held by %); try again when it finishes', p_old, c.lease_holder
      using errcode = '55006';
  end if;
  insert into collaude.connectors (id, kind, settings, enabled, created_by, created_at, last_run_at, last_run_by,
                                   last_error)
  values (p_new, c.kind,
          c.settings || jsonb_build_object('previous_ids', coalesce(c.settings -> 'previous_ids', '[]') || to_jsonb(p_old)),
          c.enabled, c.created_by, c.created_at, c.last_run_at, c.last_run_by, c.last_error);
  update collaude.connector_routes set connector_id = p_new where connector_id = p_old;
  update collaude.sources set connector_id = p_new where connector_id = p_old;
  delete from collaude.connectors where id = p_old;
end
$$;

revoke all on function collaude.rename_connector(text, text) from public;
grant execute on function collaude.rename_connector(text, text) to collaude_member;
