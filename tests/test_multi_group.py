"""A folder, a memory or a connector route shared with several groups."""

import io
import json
import os
import secrets
import shutil
import sys

import pytest

from helpers import ADMIN_DSN, cl, entry, needs_db

TRANSCRIPT = [
    entry("user", "Why does the light curve of TIC 1234 dip every 13.7 days? It looks like a planet."),
    entry("assistant", [{"type": "text", "text": "The 13.7 d period matches the TESS orbit. The dips line up "
                                                 "with momentum dumps, so they are instrumental, not a planet."}]),
    entry("user", "Great, so mask the momentum dumps before the BLS search. " + "More context. " * 60),
]


def test_project_groups(tmp_path):
    assert cl.project_groups({"group": "g1"}) == ["g1"]
    assert cl.project_groups({"groups": ["g1", "g2", "g1", " "]}) == ["g1", "g2"]
    assert cl.project_groups({"groups": "g1"}) == ["g1"]
    assert cl.project_groups({"groups": ["g2"], "group": "g1"}) == ["g2"]  # "groups" wins
    assert cl.project_groups({}) == [] and cl.project_groups({"groups": 3}) == []
    (tmp_path / ".collaude.json").write_text('{"groups": ["g1", "g2"]}')
    assert cl.project_groups(cl.find_project(tmp_path)[1]) == ["g1", "g2"]
    (tmp_path / ".collaude.json").write_text('{"groups": []}')
    assert cl.find_project(tmp_path) is None


def test_supersedable_needs_every_group_of_the_old_memory():
    existing = [{"id": 1, "group_ids": ["g1"]}, {"id": 2, "group_ids": ["g1", "g2"]}, {"id": 3, "group_ids": ["g2"]}]
    assert cl._supersedable(existing, ["g1"]) == {1}
    assert cl._supersedable(existing, ["g1", "g2"]) == {1, 2, 3}


def add(dsn, groups, title, body="body text", **kw):
    with cl.connect(dsn) as conn:
        m = {"title": title, "body": body, "kind": "finding", "tags": [], "confidence": "medium", **kw}
        new_id = cl.insert_memory(conn, groups, m, None, "manual", None)
        conn.commit()
        return new_id


@needs_db
def test_a_memory_shared_with_two_groups(db):
    import psycopg

    mid = add(db["carol"], ["g1", "g2", "g1"], "Shared finding about bolometers")
    for who in ("alice", "bob", "carol"):
        with cl.connect(db[who]) as conn:
            hits = cl.search_memories(conn, "bolometers", None)
            assert [h["id"] for h in hits] == [mid] and hits[0]["group_ids"] == ["g1", "g2"]
            assert hits[0]["group_id"] == "g1"
    with cl.connect(db["carol"]) as conn:
        assert [h["id"] for h in cl.search_memories(conn, "bolometers", ["g2"])] == [mid]
        assert [h["id"] for h in cl.fetch_recent(conn, ["g1"], 50)].count(mid) == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # alice isn't in g2
        add(db["alice"], ["g1", "g2"], "Alice sharing into bob's group")
    with cl.connect(db["bob"]) as conn:  # anyone it is shared with can curate it
        assert cl.set_status(conn, mid, "retracted", note="wrong detector")
        conn.commit()


@needs_db
def test_old_clients_write_one_group(db):
    import psycopg

    with cl.connect(db["alice"]) as conn:  # how a client before 0.4 inserts
        row = conn.execute("insert into collaude.memories (group_id, kind, title, body) "
                           "values ('g1', 'finding', 'from an old client', 'x') returning group_ids").fetchone()
        assert row == (["g1"],)
        conn.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("insert into collaude.memories (group_id, kind, title, body) "
                         "values ('g2', 'finding', 'into a group of bob''s', 'x')")
    with cl.connect(db["admin"]) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute("insert into collaude.memories (group_ids, author, kind, title, body) "
                     "values ('{g1,nope}', %s, 'finding', 'unknown group', 'x')", (f"t{db['tag']}_alice",))


@needs_db
def test_supersede_only_by_a_memory_every_reader_can_see(db):
    import psycopg

    shared = add(db["carol"], ["g1", "g2"], "Threshold is 0.5 everywhere")
    narrow = add(db["alice"], ["g1"], "Threshold is 0.3 for g1's pipeline")
    with cl.connect(db["alice"]) as conn, pytest.raises(psycopg.errors.RaiseException, match="every group"):
        cl.set_status(conn, shared, "superseded", superseded_by=narrow)
    wider = add(db["carol"], ["g1", "g2"], "Threshold is 0.3 everywhere", supersedes=shared)
    add(db["carol"], ["g1", "g2"], "g1's pipeline follows the shared threshold", supersedes=narrow)
    with cl.connect(db["bob"]) as conn:
        assert conn.execute("select status, superseded_by from collaude.memories where id = %s",
                            (shared,)).fetchone() == ("superseded", wider)


def _capture(db, who, proj, tmp_path, monkeypatch, title):
    transcript = tmp_path / f"{who}-{title[:4]}.jsonl"
    transcript.write_text("".join(TRANSCRIPT))
    fake = tmp_path / "fake_extractor.py"
    fake.write_text("import sys, json\nsys.stdin.read()\nprint(json.dumps({'memories': [{"
                    f"'title': {title!r}, 'body': 'Mask momentum dumps.', 'kind': 'gotcha'}}]}}))\n")
    monkeypatch.setattr(cl, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("COLLAUDE_DSN", db[who])
    monkeypatch.setenv("COLLAUDE_EXTRACT_CMD", f"{sys.executable} {fake}")
    session_id = f"sess-{secrets.token_hex(4)}"
    cl.main(["capture-worker", json.dumps({"session_id": session_id, "transcript_path": str(transcript),
                                           "cwd": str(proj)})])
    with cl.connect(db["admin"]) as conn:
        return conn.execute("select m.group_ids, s.group_ids from collaude.memories m "
                            "join collaude.sessions s on s.id = m.session_id where m.session_id = %s",
                            (session_id,)).fetchall()


@needs_db
def test_capture_in_a_folder_shared_with_two_groups(db, tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".collaude.json").write_text(json.dumps({"groups": ["g1", "g2"]}))

    assert _capture(db, "carol", proj, tmp_path, monkeypatch, "Carol's momentum dumps") == [(["g1", "g2"],) * 2]
    # Alice is only in g1, so her sessions here are shared with g1 alone.
    assert _capture(db, "alice", proj, tmp_path, monkeypatch, "Alice's momentum dumps") == [(["g1"],) * 2]

    only_g2 = tmp_path / "g2-only"
    only_g2.mkdir()
    (only_g2 / ".collaude.json").write_text('{"group": "g2"}')
    assert _capture(db, "alice", only_g2, tmp_path, monkeypatch, "Not stored") == []
    assert "waiting: you are not in this group" in (tmp_path / "state" / "collaude.log").read_text()

    monkeypatch.setenv("COLLAUDE_DSN", db["bob"])  # bob, in g2, starts a session here
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(proj), "source": "startup"})))
    capsys.readouterr()
    cl.main(["hook", "session-start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert 'belongs to groups "g1", "g2"' in ctx
    assert "Carol's momentum dumps" in ctx and "Alice's momentum dumps" not in ctx


@needs_db
def test_init_and_search_scope_with_several_groups(db, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLAUDE_DSN", db["alice"])
    with pytest.raises(SystemExit, match="not a member of 'g2'"):
        cl.main(["init", "g1", "g2"])

    monkeypatch.setenv("COLLAUDE_DSN", db["carol"])
    cl.main(["init", "g1", "g2"])
    assert json.loads((tmp_path / ".collaude.json").read_text()) == {"groups": ["g1", "g2"]}
    assert "groups g1, g2" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="already exists"):
        cl.main(["init", "g1"])
    (tmp_path / ".collaude.json").write_text('{"groups": ["g1", "g2"], "checkpoint_minutes": 30}')
    cl.main(["init", "g2", "--force"])  # replaces the groups, keeps the other settings
    assert json.loads((tmp_path / ".collaude.json").read_text()) == {"group": "g2", "checkpoint_minutes": 30}

    cl.main(["add", "--title", "Cryostat warms up on Mondays", "--body", "b", "-g", "g1", "-g", "g2"])
    assert "in groups g1, g2" in capsys.readouterr().out
    cl.main(["add", "--title", "Cryostat log is in the g1 drive", "--body", "b", "-g", "g1"])
    cl.main(["search", "cryostat"])  # this folder's group, g2
    out = capsys.readouterr().out
    assert "warms up on Mondays" in out and "g1 drive" not in out
    cl.main(["search", "cryostat", "-g", "g2", "-g", "g1"])
    out = capsys.readouterr().out
    assert "warms up on Mondays" in out and "g1 drive" in out
    cl.main(["status"])
    assert ".collaude.json -> group g2" in capsys.readouterr().out


@needs_db
def test_upgrade_shares_existing_rows_with_their_group(tmp_path, monkeypatch):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    dbname = f"collaude_upgrade_{secrets.token_hex(3)}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"create database {dbname}")
    admin = make_conninfo(**{**conninfo_to_dict(ADMIN_DSN), "dbname": dbname})
    try:
        old_sql = tmp_path / "sql"
        old_sql.mkdir()
        for f in sorted(cl.SQL_DIR.glob("*.sql")):
            if f.stem < "006":
                shutil.copy(f, old_sql)
        monkeypatch.setattr(cl, "SQL_DIR", old_sql)
        cl.main(["admin", "--admin-dsn", admin, "migrate"])
        with cl.connect(admin) as conn:
            conn.execute("insert into collaude.groups (id, name) values ('g1', 'g1')")
            conn.execute("insert into collaude.members (role_name) values ('someone')")
            conn.execute("insert into collaude.sessions (id, author, group_id) values ('s1', 'someone', 'g1')")
            conn.execute("insert into collaude.memories (group_id, author, kind, title, body, session_id) "
                         "values ('g1', 'someone', 'finding', 'old memory', 'x', 's1')")
            conn.execute("insert into collaude.connectors (id, kind, created_by) values ('c', 'slack', 'someone')")
            conn.execute("insert into collaude.connector_routes (connector_id, source_key, group_id, added_by) "
                         "values ('c', 'C1', 'g1', 'someone')")
            conn.execute("insert into collaude.sources (connector_id, source_key, group_id, origin, external_id, "
                         "thread_key, source_time, content, ingested_by) "
                         "values ('c', 'C1', 'g1', 'slack', 'e1', 't1', now(), 'hello', 'someone')")
            conn.commit()
        monkeypatch.undo()
        cl.main(["admin", "--admin-dsn", admin, "migrate"])
        with cl.connect(admin) as conn:
            for table in ("sessions", "memories", "connector_routes", "sources"):
                assert conn.execute(f"select group_id, group_ids from collaude.{table}").fetchall() == \
                    [("g1", ["g1"])], table
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
            c.execute(f"drop database {dbname} with (force)")
