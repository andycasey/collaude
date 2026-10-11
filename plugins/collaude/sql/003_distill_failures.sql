-- collaude schema, version 003: count failed distillation attempts.
--
-- A thread the extractor keeps failing on (bad JSON, timeouts) is retried a few
-- times and then given up, so it can't block every later thread.

alter table collaude.sources add column distill_failures smallint not null default 0;

grant update (distill_failures) on collaude.sources to collaude_member;
