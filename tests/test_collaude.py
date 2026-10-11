"""Tests for collaude.

Unit tests run anywhere. Database tests need a disposable Postgres server and an
admin connection string:

    COLLAUDE_TEST_ADMIN_DSN=postgresql://postgres:pw@localhost:5432/postgres \
        uv run --with pytest --with 'psycopg[binary]' pytest tests
"""

import contextlib
import io
import json
import secrets
import sys

import pytest

from helpers import cl, entry, needs_db


TRANSCRIPT = [
    json.dumps({"type": "mode", "mode": "normal"}) + "\n",
    entry("user", "Why do the TESS light curves for sector 14 show a dip every 13.7 days?"),
    entry("assistant", [{"type": "thinking", "thinking": "hmm"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "python fit.py", "description": "Fit"}}]),
    entry("user", [{"type": "tool_result", "content": "period=13.70 d, depth=0.3%\n<system-reminder>ignore</system-reminder>"}]),
    entry("assistant", [{"type": "text", "text": "The 13.7 d dip is the spacecraft momentum dump, not astrophysical. "
                         "Masking momentum dumps removes it entirely. " * 12}]),
    entry("assistant", [{"type": "text", "text": "subagent chatter"}], isSidechain=True),
]
QUOTE = "The 13.7 d dip is the spacecraft momentum dump, not astrophysical."


# --- unit tests ----------------------------------------------------------------


def test_condense_keeps_dialogue_and_drops_noise():
    text = cl.condense_transcript(TRANSCRIPT)
    assert "USER: Why do the TESS light curves" in text
    assert "TOOL Bash: python fit.py  # Fit" in text
    assert "RESULT: period=13.70 d" in text
    assert "system-reminder" not in text and "ignore" not in text
    assert "hmm" not in text and "subagent chatter" not in text


def test_condense_drops_injected_user_turns():
    text = cl.condense_transcript(TRANSCRIPT + [
        entry("user", [{"type": "text", "text": "Base directory for this skill: ..."}], isMeta=True),
        entry("user", "This session is being continued. Summary: ...", isCompactSummary=True),
    ])
    assert "Base directory" not in text and "Summary" not in text


def test_redact():
    s = cl.redact("export PGPASSWORD=hunter2hunter2 and postgresql://bob:s3cret@db/x and key sk-ant-abcdefghijklmnopqrstuvwxyz")
    assert "hunter2" not in s and "s3cret" not in s and "abcdefghijklmnop" not in s
    assert "postgresql://bob:[REDACTED]@db/x" in s


def test_parse_memories_validates():
    raw = "Sure!\n" + json.dumps({"memories": [
        {"title": "Momentum dumps cause 13.7 d dip", "body": "Mask them.", "kind": "gotcha",
         "tags": ["TESS", " "], "confidence": "high", "evidence_quote": QUOTE, "supersedes": 3},
        {"title": "Invented", "body": "x", "kind": "nonsense", "confidence": "sure",
         "evidence_quote": "not in the transcript", "supersedes": 999},
        {"title": "", "body": "dropped"},
    ]})
    out = cl.parse_memories(raw, cl.condense_transcript(TRANSCRIPT), existing_ids={3})
    assert len(out) == 2
    assert out[0]["evidence_quote"] == QUOTE and out[0]["supersedes"] == 3 and out[0]["tags"] == ["tess"]
    assert out[1] == {**out[1], "kind": "other", "confidence": "medium", "evidence_quote": None, "supersedes": None}


def test_off_record():
    assert not cl.is_off_record(TRANSCRIPT)
    assert cl.is_off_record(TRANSCRIPT + [entry("user", "this is #offrecord please")])
    # A mention inside tool output does not count.
    assert not cl.is_off_record(TRANSCRIPT + [entry("user", [{"type": "tool_result", "content": "#offrecord"}])])
    # Nor does text Claude Code injects as a user turn: a loaded skill or a compaction summary.
    assert not cl.is_off_record(TRANSCRIPT + [entry("user", [{"type": "text", "text": "write #offrecord"}], isMeta=True)])
    assert not cl.is_off_record(TRANSCRIPT + [entry("user", "Summary: #offrecord skips capture",
                                                    isCompactSummary=True, isVisibleInTranscriptOnly=True)])


def test_find_project(tmp_path):
    (tmp_path / ".collaude.json").write_text('{"group": "g1"}')
    (tmp_path / "a" / "b").mkdir(parents=True)
    root, cfg = cl.find_project(tmp_path / "a" / "b")
    assert root == tmp_path.resolve() and cfg["group"] == "g1"


# --- database tests ------------------------------------------------------------


def add(dsn, group, title, body="body text", **kw):
    with cl.connect(dsn) as conn:
        m = {"title": title, "body": body, "kind": kw.pop("kind", "finding"), "tags": kw.pop("tags", []),
             "confidence": "medium", **kw}
        new_id = cl.insert_memory(conn, group, m, None, "manual", None)
        conn.commit()
        return new_id


@needs_db
def test_groups_isolate(db):
    import psycopg

    a = add(db["alice"], "g1", "Alice finding about spectra")
    b = add(db["bob"], "g2", "Bob finding about spectra")
    with cl.connect(db["alice"]) as conn:
        seen = {m["id"] for m in cl.fetch_recent(conn, None, 100)}
        assert a in seen and b not in seen
        assert [r[0] for r in conn.execute("select role_name from collaude.members order by 1")] == \
            sorted([f"t{db['tag']}_alice", f"t{db['tag']}_carol"])
    with cl.connect(db["carol"]) as conn:
        seen = {m["id"] for m in cl.fetch_recent(conn, None, 100)}
        assert {a, b} <= seen
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        add(db["alice"], "g2", "Alice writing into bob's group")


@needs_db
def test_authorship_cannot_be_forged_and_rows_are_append_only(db):
    import psycopg

    with cl.connect(db["alice"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("insert into collaude.memories (group_id, author, kind, title, body) "
                         "values ('g1', %s, 'finding', 'forged', 'x')", (f"t{db['tag']}_carol",))
    mid = add(db["alice"], "g1", "Something to protect")
    for stmt in ("delete from collaude.memories where id = %s",
                 "update collaude.memories set title = 'changed' where id = %s",
                 "update collaude.memories set author = 'postgres' where id = %s"):
        with cl.connect(db["carol"]) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt, (mid,))


@needs_db
def test_supersede_records_who_and_stays_in_group(db):
    import psycopg

    old = add(db["alice"], "g1", "Pipeline uses threshold 0.5")
    new = add(db["carol"], "g1", "Pipeline threshold should be 0.3", supersedes=old)
    other = add(db["bob"], "g2", "Unrelated")
    with cl.connect(db["alice"]) as conn:
        row = conn.execute("select status, superseded_by, status_changed_by from collaude.memories where id = %s",
                           (old,)).fetchone()
        assert row == ("superseded", new, f"t{db['tag']}_carol")
    with cl.connect(db["carol"]) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            cl.set_status(conn, new, "superseded", superseded_by=other)


@needs_db
def test_search_ranks_relevant_first(db):
    add(db["alice"], "g1", "Gaia DR3 parallax zero-point offset",
        "Apply the Lindegren zero-point correction before computing distances.", tags=["gaia", "astrometry"])
    add(db["alice"], "g1", "Coffee machine on 4th floor is broken", "Use the 3rd floor.")
    with cl.connect(db["carol"]) as conn:
        hits = cl.search_memories(conn, "gaia parallax offset", ["g1"])
        assert hits and hits[0]["title"].startswith("Gaia DR3")
        assert not cl.search_memories(conn, "gaia", ["g2"])
        assert cl.search_memories(conn, "distances", None)[0]["title"].startswith("Gaia")


@needs_db
def test_capture_end_to_end(db, tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".collaude.json").write_text(json.dumps({"group": "g1", "share_excerpts": True}))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("".join(TRANSCRIPT))
    fake = tmp_path / "fake_extractor.py"
    fake.write_text("import sys, json\nsys.stdin.read()\nprint(json.dumps({'memories': [{"
                    f"'title': 'TESS 13.7 d dip is a momentum dump', 'body': 'Mask momentum dumps.', "
                    f"'kind': 'gotcha', 'tags': ['tess'], 'confidence': 'high', 'evidence_quote': {QUOTE!r}, "
                    "'supersedes': None}]}))\n")
    monkeypatch.setattr(cl, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("COLLAUDE_DSN", db["alice"])
    monkeypatch.setenv("COLLAUDE_EXTRACT_CMD", f"{sys.executable} {fake}")
    session_id = f"sess-{secrets.token_hex(4)}"
    payload = json.dumps({"session_id": session_id, "transcript_path": str(transcript), "cwd": str(proj),
                          "hook_event_name": "SessionEnd"})

    cl.main(["capture-worker", payload])
    cl.main(["capture-worker", payload])  # nothing new: must not duplicate

    with cl.connect(db["carol"]) as conn:
        rows = conn.execute("""select m.title, m.author, m.evidence_quote, m.extractor, s.cwd, s.transcript_path
                               from collaude.memories m join collaude.sessions s on s.id = m.session_id
                               where m.session_id = %s""", (session_id,)).fetchall()
        assert len(rows) == 1
        title, author, quote, extractor, cwd, tpath = rows[0]
        assert author == f"t{db['tag']}_alice" and quote == QUOTE and cwd == str(proj)
        assert tpath == str(transcript) and extractor.startswith("collaude-")
        excerpt = conn.execute("select content from collaude.excerpts where session_id = %s", (session_id,)).fetchone()
        assert "momentum dump" in excerpt[0]
    with cl.connect(db["bob"]) as conn:  # not in g1
        assert not conn.execute("select 1 from collaude.memories where session_id = %s", (session_id,)).fetchall()
        assert not conn.execute("select 1 from collaude.excerpts").fetchall()

    # The next session in this folder is told about it.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(proj), "source": "startup"})))
    capsys.readouterr()
    cl.main(["hook", "session-start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "TESS 13.7 d dip is a momentum dump" in ctx and 'group "g1"' in ctx


@needs_db
def test_capture_respects_off_record(db, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".collaude.json").write_text('{"group": "g1"}')
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("".join(TRANSCRIPT + [entry("user", "#offrecord")]))
    monkeypatch.setattr(cl, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("COLLAUDE_DSN", db["alice"])
    monkeypatch.setenv("COLLAUDE_EXTRACT_CMD", "false")  # would fail if called
    session_id = f"sess-{secrets.token_hex(4)}"
    cl.main(["capture-worker", json.dumps({"session_id": session_id, "transcript_path": str(transcript),
                                           "cwd": str(proj)})])
    assert "#offrecord" in (tmp_path / "state" / "collaude.log").read_text()
    with cl.connect(db["alice"]) as conn:
        assert not conn.execute("select 1 from collaude.memories where session_id = %s", (session_id,)).fetchall()


@needs_db
def test_capture_holds_no_transaction_while_extracting(db, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".collaude.json").write_text('{"group": "g1"}')
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("".join(TRANSCRIPT))
    # The extractor reports how many of alice's collaude connections sit idle in a transaction.
    fake = tmp_path / "fake_extractor.py"
    fake.write_text(f"""import json, sys, psycopg
sys.stdin.read()
with psycopg.connect({db['admin']!r}) as c:
    n = c.execute("select count(*) from pg_stat_activity where application_name = 'collaude' "
                  "and usename = %s and state like 'idle in transaction%%'", ({f"t{db['tag']}_alice"!r},)).fetchone()[0]
print(json.dumps({{'memories': [{{'title': f'idle transactions: {{n}}', 'body': 'b', 'kind': 'other'}}]}}))
""")
    monkeypatch.setattr(cl, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("COLLAUDE_DSN", db["alice"])
    monkeypatch.setenv("COLLAUDE_EXTRACT_CMD", f"{sys.executable} {fake}")
    session_id = f"sess-{secrets.token_hex(4)}"
    cl.main(["capture-worker", json.dumps({"session_id": session_id, "transcript_path": str(transcript),
                                           "cwd": str(proj)})])
    with cl.connect(db["alice"]) as conn:
        titles = [r[0] for r in conn.execute("select title from collaude.memories where session_id = %s",
                                             (session_id,))]
    assert titles == ["idle transactions: 0"]


def test_parse_memories_tolerates_odd_shapes():
    one = {"title": "Momentum dumps cause 13.7 d dip", "body": "Mask them.", "kind": "gotcha"}
    out = cl.parse_memories(json.dumps([{**one, "tags": "TESS, systematics"}, one]), "", set())
    assert len(out) == 2 and out[0]["tags"] == ["tess", "systematics"]
    assert cl.parse_memories("```json\n" + json.dumps({"memories": [one]}) + "\n```", "", set())[0]["kind"] == "gotcha"
    assert cl.parse_memories('{"memories": {"title": "not a list"}}', "", set()) == []
    with pytest.raises(ValueError):
        cl.parse_memories("I can't help with that.", "", set())


def test_checkpoint_capture_runs_at_most_once_per_interval(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".collaude.json").write_text('{"group": "g1", "checkpoint_minutes": 20}')
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("".join(TRANSCRIPT))
    monkeypatch.setattr(cl, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("COLLAUDE_DSN", "postgresql://unused")
    monkeypatch.delenv("COLLAUDE_CHECKPOINT_MINUTES", raising=False)
    spawned = []
    monkeypatch.setattr(cl.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
    clock = [1_000_000.0]
    monkeypatch.setattr(cl.time, "time", lambda: clock[0])

    def reply(minutes_later):
        clock[0] += minutes_later * 60
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(proj), "hook_event_name": "Stop"})))
        cl.main(["hook", "capture", "--checkpoint"])
        return len(spawned)

    assert reply(0) == 0    # first reply starts the clock
    assert reply(5) == 0
    assert reply(16) == 1   # 21 minutes in
    assert reply(1) == 1
    assert reply(20) == 2
    assert spawned[0][-2] == "capture-worker"

    monkeypatch.setenv("COLLAUDE_CHECKPOINT_MINUTES", "0")  # off
    assert reply(60) == 2


def test_session_start_survives_a_bad_recent_on_start(tmp_path, monkeypatch, capsys):
    (tmp_path / ".collaude.json").write_text('{"group": "g1", "recent_on_start": "lots"}')
    monkeypatch.setattr(cl, "SECRETS_FILE", tmp_path / "secrets.json")
    monkeypatch.setattr(cl, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("COLLAUDE_DSN", "postgresql://example/none")
    seen = []
    monkeypatch.setattr(cl, "connect", lambda **kw: contextlib.nullcontext())
    monkeypatch.setattr(cl, "fetch_recent", lambda conn, groups, n: seen.append(n) or [])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    cl.main(["hook", "session-start"])  # must not raise
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert seen == [8] and 'group "g1"' in ctx


@needs_db
def test_admin_errors_are_explained(db, capsys):
    tag = db["tag"]
    run = lambda *a: cl.main(["admin", "--admin-dsn", db["admin"], *a])
    with pytest.raises(SystemExit, match="already exists; pick another with --role"):
        run("add-user", "alice@other.org", "--role", f"t{tag}_alice")
    with pytest.raises(SystemExit, match="already a member"):
        run("add-user", "alice@example.org", "--role", f"t{tag}_alice2")
    with pytest.raises(SystemExit, match="no such group"):
        run("add-user", "dave@example.org", "--role", f"t{tag}_dave", "--group", "nope")
    with pytest.raises(SystemExit, match="no group 'nope'"):
        run("add-member", "alice@example.org", "nope")
