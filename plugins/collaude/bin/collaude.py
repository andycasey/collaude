#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2"]
# ///
"""collaude: shared, searchable team memory for Claude Code, stored in PostgreSQL.

The database is plain Postgres. Point COLLAUDE_DSN (or `collaude setup`) at any
server: Supabase, a self-managed instance, RDS, etc.
"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
SCRIPT = Path(__file__).resolve()
PLUGIN_ROOT = SCRIPT.parent.parent
SQL_DIR = PLUGIN_ROOT / "sql"
PROMPT_FILE = PLUGIN_ROOT / "prompts" / "extract.md"
MESSAGES_PROMPT_FILE = PLUGIN_ROOT / "prompts" / "extract_messages.md"

CONFIG_DIR = Path(os.environ.get("COLLAUDE_CONFIG_DIR", Path.home() / ".config" / "collaude"))
STATE_DIR = Path(os.environ.get("COLLAUDE_STATE_DIR", Path.home() / ".local" / "state" / "collaude"))
CONFIG_FILE = CONFIG_DIR / "config.json"
SECRETS_FILE = CONFIG_DIR / "secrets.json"
PROJECT_FILE = ".collaude.json"

EXTRACT_MODEL = os.environ.get("COLLAUDE_MODEL", "haiku")
EXTRACTOR_ID = f"collaude-{VERSION}/{EXTRACT_MODEL}"
KINDS = ("finding", "dead_end", "decision", "method", "data_note", "gotcha", "question", "other")
OFF_RECORD = "#offrecord"
MAX_TRANSCRIPT_CHARS = 120_000
MIN_TRANSCRIPT_CHARS = 800
MAX_MEMORIES_PER_CAPTURE = 5
INGEST_STALE_MINUTES = int(os.environ.get("COLLAUDE_INGEST_STALE_MINUTES", "30"))
INGEST_MAX_THREADS = 200
INGEST_QUIET_HOURS = 2.0


# --- configuration -----------------------------------------------------------


def load_user_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_user_config(cfg: dict) -> None:
    _write_private_json(CONFIG_FILE, cfg)


def _write_private_json(path: Path, data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def load_secrets() -> dict:
    """Connector credentials held on this machine, keyed by connector id. Never sent to the database."""
    try:
        return json.loads(SECRETS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_secret(connector_id: str, secret: str | None) -> None:
    secrets_ = load_secrets()
    if secret is None:
        secrets_.pop(connector_id, None)
    else:
        secrets_[connector_id] = secret
    _write_private_json(SECRETS_FILE, secrets_)


def user_dsn() -> str | None:
    return os.environ.get("COLLAUDE_DSN") or load_user_config().get("dsn")


def find_project(start: str | os.PathLike | None = None) -> tuple[Path, dict] | None:
    """Find the nearest .collaude.json at or above `start`."""
    d = Path(start or os.getcwd()).resolve()
    for p in (d, *d.parents):
        f = p / PROJECT_FILE
        if f.is_file():
            try:
                cfg = json.loads(f.read_text())
            except json.JSONDecodeError:
                return None
            if isinstance(cfg, dict) and cfg.get("group"):
                return p, cfg
            return None
    return None


def connect(dsn: str | None = None, timeout: int = 10):
    import psycopg

    dsn = dsn or user_dsn()
    if not dsn:
        sys.exit("collaude: no database configured; run `collaude setup` or set COLLAUDE_DSN")
    # prepare_threshold=None keeps us compatible with transaction-mode poolers
    # (PgBouncer, Supavisor), which cannot hold server-side prepared statements.
    return psycopg.connect(dsn, connect_timeout=timeout, application_name="collaude", prepare_threshold=None)


def log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "collaude.log", "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}\n")


def self_command() -> str:
    return f"uv run --quiet --script {shlex.quote(str(SCRIPT))}"


# --- transcripts -------------------------------------------------------------

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED SLACK TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "[REDACTED API KEY]"),
    (re.compile(r"(\w+://[^\s:/@]+:)[^\s@/]+@"), r"\1[REDACTED]@"),
    (re.compile(r"(?i)(\w*(?:password|passwd|pwd|secret|token|api[_-]?key)\w*)(\s*[=:]\s*)(['\"]?)[^\s'\"]{4,}\3"), r"\1\2[REDACTED]"),
]


def redact(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + f" …[{len(s) - n} more chars]"


def _tool_input_summary(inp) -> str:
    if not isinstance(inp, dict):
        return str(inp)
    for key in ("command", "file_path", "pattern", "url", "query", "prompt", "description"):
        if inp.get(key):
            extra = f"  # {inp['description']}" if key == "command" and inp.get("description") else ""
            return f"{inp[key]}{extra}"
    return json.dumps(inp)[:400]


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_injected(entry: dict) -> bool:
    """Entries Claude Code writes as user turns that the user did not type: loaded skills,
    command output, and compaction summaries."""
    return bool(entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"))


def condense_transcript(lines: list[str]) -> str:
    """Turn Claude Code JSONL transcript lines into compact, readable text."""
    out = []
    for raw in lines:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = entry.get("type")
        if kind not in ("user", "assistant") or entry.get("isSidechain") or _is_injected(entry):
            continue
        speaker = "USER" if kind == "user" else "ASSISTANT"
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                text = _REMINDER_RE.sub("", block.get("text", "")).strip()
                if text:
                    out.append(f"{speaker}: {_clip(text, 4000)}")
            elif bt == "tool_use":
                out.append(f"TOOL {block.get('name')}: {_clip(_tool_input_summary(block.get('input')), 400)}")
            elif bt == "tool_result":
                text = _REMINDER_RE.sub("", _result_text(block.get("content"))).strip()
                if text:
                    out.append(f"RESULT: {_clip(text, 600)}")
    return "\n".join(out)


def is_off_record(lines: list[str]) -> bool:
    for raw in lines:
        if OFF_RECORD not in raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "user" or _is_injected(entry):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if any(isinstance(b, dict) and b.get("type") == "text" and OFF_RECORD in b.get("text", "")
               for b in content or []):
            return True
    return False


# --- extraction --------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def build_prompt(transcript: str, existing: list[dict], group: str, template: Path = PROMPT_FILE) -> str:
    existing_txt = "\n".join(
        f"#{m['id']} [{m['kind']}] {m['title']} — {_clip(m['body'], 200)}" for m in existing
    ) or "(none yet)"
    return (
        template.read_text()
        .replace("{existing}", existing_txt)
        .replace("{group}", group)
        .replace("{transcript}", transcript)
    )


def run_extractor(prompt: str) -> str:
    cmd = os.environ.get("COLLAUDE_EXTRACT_CMD")
    argv = shlex.split(cmd) if cmd else [
        "claude", "-p",
        "--model", EXTRACT_MODEL,
        "--tools", "",
        "--no-session-persistence",
        "--system-prompt", "You extract structured research memories from transcripts. Reply with JSON only.",
    ]
    env = {**os.environ, "COLLAUDE_DISABLE": "1"}  # keep our own hooks out of the extractor's session
    r = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=600,
                       env=env, cwd=tempfile.gettempdir())
    if r.returncode != 0:
        raise RuntimeError(f"extractor exited {r.returncode}: {r.stderr.strip()[:500]}")
    return r.stdout


def parse_memories(raw: str, transcript: str, existing_ids: set[int],
                   allowed_source_ids: set[int] | None = None) -> list[dict]:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"extractor returned no JSON: {raw[:200]!r}")
    items = json.loads(raw[start:end + 1]).get("memories") or []
    norm_transcript = _norm(transcript)
    clean = []
    for m in items[:MAX_MEMORIES_PER_CAPTURE]:
        if not isinstance(m, dict):
            continue
        title = str(m.get("title") or "").strip()[:200]
        body = str(m.get("body") or "").strip()[:4000]
        if len(title) < 3 or not body:
            continue
        quote = str(m.get("evidence_quote") or "").strip()
        # Keep the quote only if it really is verbatim; it is our provenance anchor.
        if not quote or _norm(quote) not in norm_transcript:
            quote = None
        sup = m.get("supersedes")
        try:
            sup = int(sup) if sup is not None else None
        except (TypeError, ValueError):
            sup = None
        source_ids = []
        if allowed_source_ids is not None:
            for sid in m.get("source_ids") or []:
                try:
                    sid = int(str(sid).lstrip("Ss"))
                except ValueError:
                    continue
                if sid in allowed_source_ids and sid not in source_ids:
                    source_ids.append(sid)
            if not source_ids:
                continue  # a message-derived memory must point at its messages
        clean.append({
            "title": title,
            "body": body,
            "kind": m.get("kind") if m.get("kind") in KINDS else "other",
            "tags": [str(t).strip().lower()[:40] for t in (m.get("tags") or []) if str(t).strip()][:5],
            "confidence": m.get("confidence") if m.get("confidence") in ("low", "medium", "high") else "medium",
            "evidence_quote": quote[:1000] if quote else None,
            "supersedes": sup if sup in existing_ids else None,
            "source_ids": source_ids,
        })
    return clean


def git_info(cwd: str) -> dict:
    def git(*args):
        try:
            r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
            return r.stdout.strip() or None if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    return {
        "repo": git("remote", "get-url", "origin"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "commit_sha": git("rev-parse", "HEAD"),
    }


# --- database operations -----------------------------------------------------


def fetch_recent(conn, groups: list[str] | None, limit: int, include_inactive=False) -> list[dict]:
    from psycopg.rows import dict_row

    where, params = ["true"], {"limit": limit}
    if groups:
        where.append("m.group_id = any(%(groups)s)")
        params["groups"] = groups
    if not include_inactive:
        where.append("m.status = 'active'")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            select m.*, coalesce(u.display_name, u.email, m.author) as who
            from collaude.memories m left join collaude.members u on u.role_name = m.author
            where {' and '.join(where)}
            order by m.created_at desc limit %(limit)s""", params)
        return cur.fetchall()


def search_memories(conn, query: str, groups: list[str] | None, limit: int = 10, kind: str | None = None,
                    author: str | None = None, since: str | None = None, include_inactive=False) -> list[dict]:
    from psycopg.rows import dict_row

    words = re.findall(r"[A-Za-z0-9]+", query)
    if not words:
        return []
    params = {"q": query, "orq": " | ".join(words), "limit": limit}
    where = ["m.search @@ q.or_q"]
    if groups:
        where.append("m.group_id = any(%(groups)s)")
        params["groups"] = groups
    if not include_inactive:
        where.append("m.status = 'active'")
    if kind:
        where.append("m.kind = %(kind)s")
        params["kind"] = kind
    if author:
        where.append("(m.author = %(author)s or u.email ilike %(author_like)s or u.display_name ilike %(author_like)s)")
        params["author"], params["author_like"] = author, f"%{author}%"
    if since:
        where.append("m.created_at >= %(since)s::timestamptz")
        params["since"] = since
    with conn.cursor(row_factory=dict_row) as cur:
        # Rank OR-matches, boost rows that match every term, and decay gently with age.
        cur.execute(f"""
            with q as (select websearch_to_tsquery('english', %(q)s) as and_q,
                              to_tsquery('english', %(orq)s) as or_q)
            select m.*, coalesce(u.display_name, u.email, m.author) as who,
                   ts_rank_cd(m.search, q.or_q)
                     * (case when m.search @@ q.and_q then 2 else 1 end)
                     / (1 + extract(epoch from now() - m.created_at) / 86400 / 365) as score
            from collaude.memories m cross join q
            left join collaude.members u on u.role_name = m.author
            where {' and '.join(where)}
            order by score desc limit %(limit)s""", params)
        return cur.fetchall()


def insert_session(conn, session_id: str, group: str, cwd: str, transcript_path: str | None) -> None:
    info = git_info(cwd)
    conn.execute("""
        insert into collaude.sessions (id, group_id, host, cwd, repo, branch, commit_sha, transcript_path, client_version)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (id) do nothing""",
        (session_id, group, socket.gethostname(), cwd, info["repo"], info["branch"], info["commit_sha"],
         transcript_path, f"collaude/{VERSION}"))


def insert_memory(conn, group: str, m: dict, session_id: str | None, origin: str, extractor: str | None,
                  source_time: datetime | None = None) -> int:
    row = conn.execute("""
        insert into collaude.memories
          (group_id, kind, title, body, tags, confidence, evidence_quote, origin, session_id, extractor, source_time)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
        (group, m["kind"], m["title"], m["body"], m["tags"], m["confidence"], m.get("evidence_quote"),
         origin, session_id, extractor, source_time)).fetchone()
    new_id = row[0]
    for sid in m.get("source_ids") or []:
        conn.execute("insert into collaude.memory_sources (memory_id, source_id) values (%s, %s)", (new_id, sid))
    if m.get("supersedes"):
        set_status(conn, m["supersedes"], "superseded", superseded_by=new_id, note=f"superseded by #{new_id}")
    return new_id


def set_status(conn, memory_id: int, status: str, superseded_by: int | None = None, note: str | None = None) -> bool:
    cur = conn.execute(
        "update collaude.memories set status = %s, superseded_by = %s, status_note = %s where id = %s",
        (status, superseded_by, note, memory_id))
    return cur.rowcount == 1


# --- formatting --------------------------------------------------------------


def _age(ts: datetime) -> str:
    secs = (datetime.now(timezone.utc) - ts).total_seconds()
    for unit, n in (("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit} ago"
    return "just now"


def _attribution(m: dict) -> str:
    """'Alice' for session memories; 'from slack, ingested by Alice' for connector memories."""
    if m["origin"] in ("claude-code", "manual"):
        return m["who"]
    return f"from {m['origin']}, ingested by {m['who']}"


def _when(m: dict) -> datetime:
    return m.get("source_time") or m["created_at"]


def format_memory(m: dict, full=False) -> str:
    flag = "" if m["status"] == "active" else f" ({m['status'].upper()})"
    head = (f"#{m['id']} [{m['kind']}] {m['title']}{flag}\n"
            f"    {_attribution(m)} · {_when(m):%Y-%m-%d} ({_age(_when(m))}) · group {m['group_id']}"
            f" · confidence {m['confidence']}")
    body = m["body"] if full else _clip(m["body"], 400)
    lines = [head, textwrap.indent(textwrap.fill(body, 96), "    ")]
    if m["tags"]:
        lines.append(f"    tags: {', '.join(m['tags'])}")
    return "\n".join(lines)


def _json_default(o):
    return o.isoformat() if isinstance(o, datetime) else str(o)


# --- hooks -------------------------------------------------------------------


def _read_hook_payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def spawn_ingest_if_configured() -> bool:
    """If this machine holds connector credentials, catch up in the background.

    The database lease makes this a no-op when the last run is recent or another
    collaborator's session is already ingesting.
    """
    if not load_secrets() or not user_dsn():
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "collaude.log", "a") as logf:
        subprocess.Popen([sys.executable, str(SCRIPT), "ingest", "--stale-after", str(INGEST_STALE_MINUTES), "--quiet"],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
    return True


def hook_session_start(_args) -> None:
    if os.environ.get("COLLAUDE_DISABLE"):
        return
    payload = _read_hook_payload()
    spawn_ingest_if_configured()
    found = find_project(payload.get("cwd"))
    if not found or not user_dsn():
        return
    _, proj = found
    group = proj["group"]
    try:
        with connect(timeout=4) as conn:
            recent = fetch_recent(conn, [group], int(proj.get("recent_on_start", 8)))
    except Exception as e:  # never block a session on the memory server
        log(f"session-start: {e!r}")
        return
    cmd = self_command()
    lines = [f'Team memory (collaude) is active: this folder belongs to group "{group}". '
             f"Memories are captured automatically from sessions and shared with the group."]
    if recent:
        lines.append("Most recent shared memories:")
        lines += [f"  #{m['id']} [{m['kind']}] {m['title']} ({_attribution(m)}, {_age(_when(m))})" for m in recent]
    else:
        lines.append("No memories in this group yet.")
    lines.append(f"Search before investigating something a collaborator may already have looked at: "
                 f'`{cmd} search "<terms>"`; full text and provenance: `{cmd} show <id>`.')
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}}))


def hook_capture(_args) -> None:
    """PreCompact / SessionEnd: hand the transcript to a detached worker and return at once."""
    if os.environ.get("COLLAUDE_DISABLE"):
        return
    payload = _read_hook_payload()
    found = find_project(payload.get("cwd"))
    if not found or not found[1].get("capture", True) or not user_dsn():
        return
    if not payload.get("session_id") or not Path(payload.get("transcript_path") or "").is_file():
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "collaude.log", "a") as logf:
        subprocess.Popen([sys.executable, str(SCRIPT), "capture-worker", json.dumps(payload)],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)


def capture_worker(args) -> None:
    payload = json.loads(args.payload)
    session_id, transcript_path = payload["session_id"], payload["transcript_path"]
    cwd = payload.get("cwd") or os.getcwd()
    found = find_project(cwd)
    if not found:
        return
    _, proj = found
    group = proj["group"]

    (STATE_DIR / "offsets").mkdir(parents=True, exist_ok=True)
    offset_file = STATE_DIR / "offsets" / f"{session_id}.json"
    with open(STATE_DIR / "offsets" / f"{session_id}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # PreCompact and SessionEnd may overlap
        try:
            offset = json.loads(offset_file.read_text()).get("lines", 0)
        except (FileNotFoundError, json.JSONDecodeError):
            offset = 0
        with open(transcript_path) as f:
            lines = f.readlines()
        new = lines[offset:]

        def done(note: str) -> None:
            offset_file.write_text(json.dumps({"lines": len(lines), "updated": time.time()}))
            log(f"capture {session_id[:8]} [{group}] {note}")

        if is_off_record(lines):
            return done(f"skipped: {OFF_RECORD}")
        text = redact(condense_transcript(new))
        if len(text) < MIN_TRANSCRIPT_CHARS:
            log(f"capture {session_id[:8]} [{group}] waiting: only {len(text)} chars of new content")
            return
        if len(text) > MAX_TRANSCRIPT_CHARS:
            text = "…[earlier part of session omitted]\n" + text[-MAX_TRANSCRIPT_CHARS:]

        try:
            # Two short connections, so no transaction stays open while the extractor runs.
            with connect() as conn:
                existing = fetch_recent(conn, [group], 80)
            raw = run_extractor(build_prompt(text, existing, group))
            memories = parse_memories(raw, text, {m["id"] for m in existing})
            with connect() as conn:
                if memories:
                    insert_session(conn, session_id, group, cwd, transcript_path)
                    if proj.get("share_excerpts"):
                        conn.execute("insert into collaude.excerpts (session_id, content) values (%s, %s)",
                                     (session_id, text))
                    ids = [insert_memory(conn, group, m, session_id, "claude-code", EXTRACTOR_ID) for m in memories]
                conn.commit()
        except Exception as e:
            log(f"capture {session_id[:8]} [{group}] failed, will retry next time: {e!r}")
            return
        done(f"stored {len(memories)} memories" + (f": {ids}" if memories else ""))


# --- user commands -----------------------------------------------------------


def _groups_for(args, conn=None) -> list[str] | None:
    if getattr(args, "all_groups", False):
        return None
    if getattr(args, "group", None):
        return [args.group]
    found = find_project()
    if found:
        return [found[1]["group"]]
    return None  # outside a project: everything you can see


def cmd_setup(args) -> None:
    import psycopg

    dsn = args.dsn or input("Postgres connection string (postgresql://user:password@host:port/db): ").strip()
    try:
        with connect(dsn) as conn:
            me = conn.execute("select session_user").fetchone()[0]
            groups = [r[0] for r in conn.execute("select collaude.my_groups()")]
    except psycopg.Error as e:
        sys.exit(f"collaude: could not connect: {e}")
    cfg = load_user_config()
    cfg["dsn"] = dsn
    save_user_config(cfg)
    print(f"Connected as {me}. Groups: {', '.join(groups) or '(none yet; ask an admin to add you)'}")
    print(f"Saved to {CONFIG_FILE} (mode 600).")


def cmd_init(args) -> None:
    target = Path.cwd() / PROJECT_FILE
    if target.exists() and not args.force:
        sys.exit(f"collaude: {target} already exists (use --force to overwrite)")
    with connect() as conn:
        groups = [r[0] for r in conn.execute("select collaude.my_groups()")]
    if args.group not in groups:
        sys.exit(f"collaude: you are not a member of group {args.group!r}; your groups: {', '.join(groups) or 'none'}")
    cfg = {"group": args.group}
    if args.share_excerpts:
        cfg["share_excerpts"] = True
    target.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"Wrote {target}. Commit it so collaborators' sessions in this folder use group {args.group!r}.")


def cmd_status(args) -> None:
    from psycopg.conninfo import conninfo_to_dict

    dsn = user_dsn()
    if not dsn:
        sys.exit("collaude: not configured; run `collaude setup`")
    info = conninfo_to_dict(dsn)
    print(f"server:  {info.get('host')}:{info.get('port', 5432)}/{info.get('dbname')}")
    with connect() as conn:
        me = conn.execute("select session_user").fetchone()[0]
        member = conn.execute("select email, display_name, active from collaude.members where role_name = %s",
                              (me,)).fetchone()
        groups = conn.execute("""
            select g.id, g.name, (select count(*) from collaude.memories m where m.group_id = g.id)
            from collaude.groups g order by g.id""").fetchall()
    print(f"you:     {me}" + (f" <{member[0]}>" if member and member[0] else "")
          + ("" if not member or member[2] else " (DEACTIVATED)"))
    for gid, name, n in groups:
        print(f"group:   {gid} ({name}), {n} memories")
    found = find_project()
    print(f"project: {found[0] / PROJECT_FILE} -> group {found[1]['group']}" if found
          else "project: none (no .collaude.json here or above)")


def cmd_search(args) -> None:
    if args.messages:
        with connect() as conn:
            rows = search_sources(conn, " ".join(args.query), _groups_for(args), args.limit)
        if args.json:
            print(json.dumps(rows, default=_json_default, indent=2))
        else:
            print("\n\n".join(format_source(r) for r in rows) if rows else "No matching messages.")
        return
    with connect() as conn:
        rows = search_memories(conn, " ".join(args.query), _groups_for(args), args.limit, args.kind,
                               args.author, args.since, args.include_inactive)
    _print_rows(rows, args)


def cmd_recent(args) -> None:
    with connect() as conn:
        rows = fetch_recent(conn, _groups_for(args), args.limit, args.include_inactive)
    _print_rows(rows, args)


def _print_rows(rows, args) -> None:
    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if k != "search"} for r in rows], default=_json_default, indent=2))
    elif not rows:
        print("No matching memories.")
    else:
        print("\n\n".join(format_memory(r) for r in rows))


def cmd_show(args) -> None:
    from psycopg.rows import dict_row

    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select m.*, coalesce(u.display_name, u.email, m.author) as who, u.email
                       from collaude.memories m left join collaude.members u on u.role_name = m.author
                       where m.id = %s""", (args.id,))
        m = cur.fetchone()
        if not m:
            sys.exit(f"collaude: memory #{args.id} not found (or not in your groups)")
        cur.execute("select * from collaude.sessions where id = %s", (m["session_id"],))
        s = cur.fetchone()
        cur.execute("select id, title from collaude.memories where superseded_by = %s", (m["id"],))
        replaces = cur.fetchall()
        cur.execute("select content, captured_at from collaude.excerpts where session_id = %s order by id",
                    (m["session_id"],))
        excerpts = cur.fetchall()
        cur.execute("""select s.id, s.origin, s.container, s.author_name, s.source_time, s.permalink, s.subject,
                              s.content, s.group_id
                       from collaude.memory_sources ms join collaude.sources s on s.id = ms.source_id
                       where ms.memory_id = %s order by s.source_time""", (m["id"],))
        sources = cur.fetchall()
    if args.json:
        print(json.dumps({"memory": {k: v for k, v in m.items() if k != "search"}, "session": s, "sources": sources,
                          "replaces": replaces, "excerpts": excerpts if args.excerpt else len(excerpts)},
                         default=_json_default, indent=2))
        return
    print(format_memory(m, full=True))
    print("\nProvenance")
    print(f"    author:     {m['author']}" + (f" <{m['email']}>" if m["email"] else ""))
    print(f"    created:    {m['created_at']:%Y-%m-%d %H:%M %Z}")
    print(f"    origin:     {m['origin']}" + (f" via {m['extractor']}" if m["extractor"] else ""))
    if m.get("source_time"):
        print(f"    discussed:  {m['source_time']:%Y-%m-%d %H:%M %Z}")
    if m["evidence_quote"]:
        print(f'    evidence:   "{m["evidence_quote"]}"')
    if s:
        print(f"    session:    {s['id']}")
        print(f"    machine:    {s['host']}:{s['cwd']}")
        if s["repo"]:
            print(f"    git:        {s['repo']} @ {s['branch']} {(s['commit_sha'] or '')[:12]}")
        print(f"    transcript: {s['transcript_path']} (on {s['author']}'s machine)")
    if m["status"] != "active":
        print(f"    status:     {m['status']} by {m['status_changed_by']} at {m['status_changed_at']:%Y-%m-%d}"
              + (f" ({m['status_note']})" if m["status_note"] else ""))
        if m["superseded_by"]:
            print(f"    replaced by: #{m['superseded_by']}")
    for r in replaces:
        print(f"    replaces:   #{r['id']} {r['title']}")
    if sources:
        print("\nFrom these messages")
        print("\n".join(textwrap.indent(format_source(x), "    ") for x in sources))
    if excerpts:
        if args.excerpt:
            for e in excerpts:
                print(f"\nSession excerpt captured {e['captured_at']:%Y-%m-%d %H:%M}\n{e['content']}")
        else:
            print(f"    excerpts:   {len(excerpts)} shared transcript excerpt(s); add --excerpt to print")
    elif s:
        print(f"    (no transcript excerpt shared; ask {s['author']} or have their Claude open the transcript)")


def cmd_add(args) -> None:
    group = args.group or (find_project() or (None, {}))[1].get("group")
    if not group:
        sys.exit("collaude: no group; pass --group or run inside a folder with .collaude.json")
    m = {"title": args.title, "body": args.body, "kind": args.kind, "confidence": args.confidence,
         "tags": [t.strip().lower() for t in (args.tags or "").split(",") if t.strip()],
         "supersedes": args.supersedes}
    with connect() as conn:
        new_id = insert_memory(conn, group, m, None, "manual", None)
        conn.commit()
    print(f"Stored #{new_id} in group {group}.")


def cmd_supersede(args) -> None:
    with connect() as conn:
        ok = set_status(conn, args.old, "superseded", superseded_by=args.by, note=args.note)
        conn.commit()
    print(f"#{args.old} marked superseded by #{args.by}." if ok else f"#{args.old} not found.")


def cmd_retract(args) -> None:
    with connect() as conn:
        ok = set_status(conn, args.id, "retracted", note=args.reason)
        conn.commit()
    print(f"#{args.id} retracted." if ok else f"#{args.id} not found.")


def cmd_passwd(_args) -> None:
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    pw = getpass.getpass("New password: ")
    if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
        sys.exit("collaude: passwords differ or are shorter than 12 characters")
    with connect() as conn:
        conn.execute(sql.SQL("alter role {} password {}").format(sql.Identifier(conn.execute(
            "select session_user").fetchone()[0]), sql.Literal(pw)))
        conn.commit()
    cfg = load_user_config()
    if cfg.get("dsn"):
        cfg["dsn"] = make_conninfo(**{**conninfo_to_dict(cfg["dsn"]), "password": pw})
        save_user_config(cfg)
    print("Password changed" + (" and saved." if cfg.get("dsn") else "; update COLLAUDE_DSN."))


# --- connectors and ingest ---------------------------------------------------

SLACK_SETUP = """\
To create the Slack app (once per workspace):
  1. Open https://api.slack.com/apps -> Create New App -> From a manifest, pick your workspace,
     and paste the manifest printed by `collaude connector slack-manifest`.
  2. Install it to the workspace and copy the "Bot User OAuth Token" (xoxb-...).
  3. In each channel you want to share, type: /invite @collaude
"""


def _connectors_mod():
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    import collaude_connectors

    return collaude_connectors


def _my_groups(conn) -> list[str]:
    return sorted(r[0] for r in conn.execute("select collaude.my_groups()"))


def _whoami(conn) -> str:
    return conn.execute("select session_user").fetchone()[0]


def _read_secret(args, prompt: str) -> str:
    if getattr(args, "secret_env", None):
        value = os.environ.get(args.secret_env)
        if not value:
            sys.exit(f"collaude: environment variable {args.secret_env} is empty")
        return value
    if not sys.stdin.isatty():
        sys.exit("collaude: no terminal to prompt on; pass --secret-env VAR")
    value = getpass.getpass(f"{prompt}: ").strip()
    if not value:
        sys.exit("collaude: nothing entered")
    return value


def _connector_impl(conn, cid: str, secret: str | None = None):
    row = conn.execute("select kind, settings from collaude.connectors where id = %s", (cid,)).fetchone()
    if not row:
        sys.exit(f"collaude: no connector {cid!r} (see `collaude connector list`)")
    secret = secret or load_secrets().get(cid)
    if not secret:
        sys.exit(f"collaude: no credentials for {cid!r} on this machine; run `collaude connector login {cid}`")
    return _connectors_mod().make(row[0], row[1], secret)


def _add_routes(conn, impl, cid: str, pairs: list[tuple[str, str]], backfill_days: int) -> int:
    cc = _connectors_mod()
    groups = _my_groups(conn)
    added = 0
    for src, group in pairs:
        if group not in groups:
            print(f"  skipped {src}: you are not in group {group!r} (yours: {', '.join(groups)})")
            continue
        try:
            key, label = impl.resolve(src)
        except cc.ConnectorError as e:
            print(f"  skipped {src}: {e}")
            continue
        cur = conn.execute("""
            insert into collaude.connector_routes (connector_id, source_key, source_label, group_id, backfill_from)
            values (%s, %s, %s, %s, now() - make_interval(days => %s))
            on conflict (connector_id, source_key) do nothing""", (cid, key, label, group, backfill_days))
        if cur.rowcount:
            added += 1
            print(f"  {label} -> {group} (backfilling {backfill_days} days)")
        else:
            print(f"  {label} is already routed; `collaude connector unroute {cid} '{label}'` first to change it")
    return added


def _ask_routes(impl, groups: list[str]) -> list[tuple[str, str]]:
    sources = impl.list_sources()
    if not sources:
        print("  The credentials can't see any channels or folders yet.")
        return []
    print(f"Which group should each one feed? Your groups: {', '.join(groups)}. Press Enter to skip.")
    pairs = []
    for key, label in sources:
        answer = input(f"  {label}: ").strip()
        if answer:
            pairs.append((key, answer))
    return pairs


def _route_args(values: list[str] | None) -> list[tuple[str, str]]:
    pairs = []
    for v in values or []:
        src, sep, group = v.rpartition("=")
        if not sep or not src or not group:
            sys.exit(f"collaude: --route expects SOURCE=GROUP, got {v!r}")
        pairs.append((src, group))
    return pairs


def cmd_connector_add(args) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    cc = _connectors_mod()
    kind = "imap" if args.kind == "email" else args.kind
    cid = args.name or args.kind
    settings: dict = {}
    if kind == "imap":
        if not args.host or not args.user:
            sys.exit("collaude: email connectors need --host and --user (e.g. --host imap.gmail.com)")
        settings = {"host": args.host, "user": args.user, "port": args.port}
    if args.quiet_hours is not None:
        settings["quiet_hours"] = args.quiet_hours
    if kind == "slack" and not args.secret_env:
        print(SLACK_SETUP)
    secret = _read_secret(args, cc.KINDS[kind].secret_prompt)
    impl = cc.make(kind, settings, secret)
    try:
        settings.update(impl.check())
    except cc.ConnectorError as e:
        sys.exit(f"collaude: {e}")
    print(f"Credentials work: {impl.describe()}")
    with connect() as conn:
        try:
            conn.execute("insert into collaude.connectors (id, kind, settings) values (%s, %s, %s)",
                         (cid, kind, Jsonb(settings)))
        except psycopg.errors.UniqueViolation:
            sys.exit(f"collaude: a connector named {cid!r} already exists; pick another --name, "
                     f"or `collaude connector login {cid}` to run that one from this machine")
        pairs = _route_args(args.route)
        if not pairs and sys.stdin.isatty():
            pairs = _ask_routes(impl, _my_groups(conn))
        added = _add_routes(conn, impl, cid, pairs, args.backfill_days)
        conn.commit()
    save_secret(cid, secret)
    print(f"\nSaved connector {cid!r}. Its credentials stay on this machine ({SECRETS_FILE}).")
    if not added:
        print(f"It isn't feeding any group yet: `collaude connector route {cid} <channel-or-folder> <group>`.")
    print("It ingests in the background when a Claude session starts here (at most every "
          f"{INGEST_STALE_MINUTES} min). To run it now: `collaude ingest`.")


def cmd_connector_route(args) -> None:
    with connect() as conn:
        impl = _connector_impl(conn, args.name)
        _add_routes(conn, impl, args.name, [(args.source, args.group)], args.backfill_days)
        conn.commit()


def cmd_connector_unroute(args) -> None:
    with connect() as conn:
        cur = conn.execute("""delete from collaude.connector_routes
                              where connector_id = %s and (source_key = %s or source_label = %s)""",
                           (args.name, args.source, args.source))
        conn.commit()
    print(f"removed {cur.rowcount} route(s); messages already ingested stay searchable")


def cmd_connector_sources(args) -> None:
    with connect() as conn:
        impl = _connector_impl(conn, args.name)
        routed = dict(conn.execute("select source_key, group_id from collaude.connector_routes where connector_id = %s",
                                   (args.name,)).fetchall())
    for key, label in impl.list_sources():
        print(f"{label:40} {'-> ' + routed[key] if key in routed else ''}")


def cmd_connector_login(args) -> None:
    cc = _connectors_mod()
    with connect() as conn:
        row = conn.execute("select kind from collaude.connectors where id = %s", (args.name,)).fetchone()
        if not row:
            sys.exit(f"collaude: no connector {args.name!r} visible to you")
        secret = _read_secret(args, cc.KINDS[row[0]].secret_prompt)
        impl = _connector_impl(conn, args.name, secret)
    try:
        impl.check()
    except cc.ConnectorError as e:
        sys.exit(f"collaude: {e}")
    save_secret(args.name, secret)
    print(f"Credentials for {args.name!r} saved on this machine; sessions started here will help keep it up to date.")


def cmd_connector_list(args) -> None:
    from psycopg.rows import dict_row

    held = load_secrets()
    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from collaude.connectors order by id")
        connectors = cur.fetchall()
        cur.execute("""
            select r.*, (select count(*) from collaude.sources s
                         where s.connector_id = r.connector_id and s.source_key = r.source_key) as messages,
                        (select count(distinct ms.memory_id) from collaude.sources s
                         join collaude.memory_sources ms on ms.source_id = s.id
                         where s.connector_id = r.connector_id and s.source_key = r.source_key) as memories
            from collaude.connector_routes r order by r.connector_id, r.source_label""")
        routes = cur.fetchall()
    if not connectors:
        print("No connectors yet. Add one with `collaude connector add slack` or `collaude connector add email`.")
    for c in connectors:
        state = "" if c["enabled"] else " (PAUSED)"
        print(f"{c['id']}{state}  [{c['kind']}] created by {c['created_by']}"
              f" · credentials on this machine: {'yes' if c['id'] in held else 'no'}")
        if c["last_run_at"]:
            print(f"    last run {_age(c['last_run_at'])} by {c['last_run_by']}"
                  + (f" — ERROR: {c['last_error']}" if c["last_error"] else ""))
        if c["lease_until"] and c["lease_until"] > datetime.now(timezone.utc):
            print(f"    running now ({c['lease_holder']})")
        for r in (r for r in routes if r["connector_id"] == c["id"]):
            synced = f"synced {_age(r['last_synced_at'])}" if r["last_synced_at"] else "not synced yet"
            print(f"    {r['source_label'] or r['source_key']:32} -> {r['group_id']:16} {synced}, "
                  f"{r['messages']} messages, {r['memories']} memories")


def cmd_connector_toggle(args) -> None:
    with connect() as conn:
        cur = conn.execute("update collaude.connectors set enabled = %s where id = %s",
                           (args.connector_cmd == "resume", args.name))
        conn.commit()
    print(f"{args.name} {'resumed' if args.connector_cmd == 'resume' else 'paused'}" if cur.rowcount
          else f"collaude: only the creator of {args.name!r} can pause or resume it")


def cmd_connector_remove(args) -> None:
    with connect() as conn:
        row = conn.execute("select created_by from collaude.connectors where id = %s", (args.name,)).fetchone()
        if not row:
            sys.exit(f"collaude: no connector {args.name!r}")
        conn.execute("delete from collaude.connector_routes where connector_id = %s", (args.name,))
        if row[0] == _whoami(conn):
            has_sources = conn.execute("select exists (select 1 from collaude.sources where connector_id = %s)",
                                       (args.name,)).fetchone()[0]
            if has_sources:  # keep the provenance of what was already ingested
                conn.execute("update collaude.connectors set enabled = false where id = %s", (args.name,))
            else:
                conn.execute("delete from collaude.connectors where id = %s", (args.name,))
        conn.commit()
    save_secret(args.name, None)
    print(f"Removed {args.name!r} from your groups and deleted its credentials from this machine. "
          "Messages and memories already ingested are kept.")


def cmd_slack_manifest(_args) -> None:
    print(json.dumps(_connectors_mod().SLACK_MANIFEST, indent=2))


def ingest_connector(conn, cid: str, kind: str, settings: dict, secret: str, max_threads: int) -> str:
    impl = _connectors_mod().make(kind, settings, secret)
    routes = conn.execute("""select source_key, source_label, group_id, cursor, backfill_from
                             from collaude.connector_routes where connector_id = %s order by source_key""",
                          (cid,)).fetchall()
    conn.commit()  # end the read before talking to the remote service
    fetched = 0
    for key, label, group, cursor, backfill in routes:
        items, new_cursor = impl.fetch(key, cursor, backfill, label)
        for it in items:
            cur = conn.execute("""
                insert into collaude.sources
                  (connector_id, source_key, group_id, origin, external_id, thread_key, container,
                   author_name, author_handle, source_time, permalink, subject, content)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (connector_id, external_id) do nothing""",
                (cid, key, group, impl.origin, it["external_id"], it["thread_key"], it["container"],
                 it["author_name"], it["author_handle"], it["source_time"], it["permalink"], it["subject"],
                 redact(it["content"])))
            fetched += cur.rowcount
        conn.execute("""update collaude.connector_routes set cursor = %s, last_synced_at = now()
                        where connector_id = %s and source_key = %s""", (new_cursor, cid, key))
        conn.commit()
    made = distill(conn, cid, impl.origin, float(settings.get("quiet_hours", INGEST_QUIET_HOURS)), max_threads)
    return f"{fetched} new messages, {made} new memories"


def _render_thread(msgs: list[dict]) -> str:
    """One thread as '[S<id>] time author: text' lines; earlier, already-distilled messages marked as context."""
    context = [m for m in msgs if m["is_context"]][-10:]
    new = [m for m in msgs if not m["is_context"]]
    subject = next((m["subject"] for m in msgs if m["subject"]), None)
    lines = [f"=== thread in {msgs[0]['container']}" + (f": {subject}" if subject else "") + " ==="]
    for m in context + new:
        tag = " (context)" if m["is_context"] else ""
        lines.append(f"[S{m['id']}]{tag} {m['source_time']:%Y-%m-%d %H:%M} {m['author_name'] or 'unknown'}: "
                     f"{_clip(m['content'], 3000)}")
    return "\n".join(lines)


def distill(conn, cid: str, origin: str, quiet_hours: float, max_threads: int, max_chars: int = 60_000) -> int:
    """Turn threads that have gone quiet into memories, oldest first, at most max_threads per run."""
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            with pending as (
              select group_id, thread_key, min(source_time) as first_time
              from collaude.sources
              where connector_id = %(c)s and distilled_at is null
              group by group_id, thread_key
              having max(source_time) < now() - make_interval(secs => %(quiet)s * 3600)
              order by min(source_time)
              limit %(n)s)
            select s.id, s.group_id, s.thread_key, s.container, s.author_name, s.source_time, s.subject,
                   s.content, s.distilled_at is not null as is_context
            from collaude.sources s join pending p using (group_id, thread_key)
            where s.connector_id = %(c)s
            order by s.group_id, p.first_time, s.thread_key, s.source_time""",
            {"c": cid, "quiet": quiet_hours, "n": max_threads})
        rows = cur.fetchall()
    conn.commit()

    threads: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        threads.setdefault((r["group_id"], r["thread_key"]), []).append(r)

    made = 0
    by_group: dict[str, list[list[dict]]] = {}
    for (group, _), msgs in threads.items():
        by_group.setdefault(group, []).append(msgs)
    for group, group_threads in by_group.items():
        batch: list[list[dict]] = []
        size = 0
        for msgs in group_threads:
            text = _render_thread(msgs)
            if batch and size + len(text) > max_chars:
                made += _distill_batch(conn, group, origin, batch)
                batch, size = [], 0
            batch.append(msgs)
            size += len(text) + 1
        if batch:
            made += _distill_batch(conn, group, origin, batch)
    return made


def _distill_batch(conn, group: str, origin: str, batch: list[list[dict]]) -> int:
    new = {m["id"]: m for msgs in batch for m in msgs if not m["is_context"]}
    text = "\n\n".join(_render_thread(msgs) for msgs in batch)
    memories = []
    if sum(len(m["content"]) for m in new.values()) >= 40:  # skip batches that are only "thanks!"
        existing = fetch_recent(conn, [group], 80)
        conn.commit()  # don't hold a transaction open while the extractor runs
        raw = run_extractor(build_prompt(text, existing, group, MESSAGES_PROMPT_FILE))
        memories = parse_memories(raw, text, {m["id"] for m in existing}, set(new))
    for m in memories:
        when = max(new[sid]["source_time"] for sid in m["source_ids"])
        insert_memory(conn, group, m, None, origin, EXTRACTOR_ID, when)
    conn.execute("update collaude.sources set distilled_at = now() where id = any(%s)", (list(new),))
    conn.commit()
    return len(memories)


def cmd_ingest(args) -> None:
    import psycopg

    held = load_secrets()
    if not held:
        if not args.quiet:
            print("No connector credentials on this machine. Add one with `collaude connector add slack|email`.")
        return
    with connect() as conn:
        rows = conn.execute("""select id, kind, settings from collaude.connectors
                               where enabled and id = any(%s) order by id""", (list(held),)).fetchall()
        for cid, kind, settings in rows:
            if args.connector and cid != args.connector:
                continue
            try:
                claimed = conn.execute(
                    "select collaude.claim_ingest(%s, make_interval(mins => %s), interval '30 minutes')",
                    (cid, args.stale_after)).fetchone()[0]
                conn.commit()
            except psycopg.errors.InsufficientPrivilege as e:
                conn.rollback()
                log(f"ingest {cid}: {e}")
                continue
            if not claimed:
                if not args.quiet:
                    print(f"{cid}: skipped (ran less than {args.stale_after} min ago, or running elsewhere)")
                continue
            error = None
            try:
                summary = ingest_connector(conn, cid, kind, settings, held[cid], args.max_threads)
            except Exception as e:
                conn.rollback()
                error = f"{type(e).__name__}: {e}"[:500]
                summary = f"failed: {error}"
            conn.execute("select collaude.finish_ingest(%s, %s)", (cid, error))
            conn.commit()
            log(f"ingest {cid}: {summary}")
            if not args.quiet:
                print(f"{cid}: {summary}")


def search_sources(conn, query: str, groups: list[str] | None, limit: int = 10) -> list[dict]:
    from psycopg.rows import dict_row

    words = re.findall(r"[A-Za-z0-9]+", query)
    if not words:
        return []
    params = {"orq": " | ".join(words), "limit": limit}
    where = ["s.search @@ q.or_q"]
    if groups:
        where.append("s.group_id = any(%(groups)s)")
        params["groups"] = groups
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            with q as (select to_tsquery('english', %(orq)s) as or_q)
            select s.id, s.group_id, s.origin, s.container, s.author_name, s.source_time, s.permalink,
                   s.subject, s.content,
                   ts_rank_cd(s.search, q.or_q) / (1 + extract(epoch from now() - s.source_time) / 86400 / 365) as score
            from collaude.sources s cross join q
            where {' and '.join(where)}
            order by score desc limit %(limit)s""", params)
        return cur.fetchall()


def format_source(s: dict) -> str:
    head = (f"[S{s['id']}] {s['origin']} {s['container']} · {s['author_name'] or 'unknown'} · "
            f"{s['source_time']:%Y-%m-%d %H:%M} ({_age(s['source_time'])}) · group {s['group_id']}")
    lines = [head]
    if s.get("subject"):
        lines.append(f"    subject: {s['subject']}")
    lines.append(textwrap.indent(textwrap.fill(_clip(s["content"], 400), 96), "    "))
    if s.get("permalink"):
        lines.append(f"    {s['permalink']}")
    return "\n".join(lines)


# --- admin commands ----------------------------------------------------------


def admin_dsn(args) -> str:
    dsn = args.admin_dsn or os.environ.get("COLLAUDE_ADMIN_DSN")
    if not dsn:
        sys.exit("collaude: admin commands need --admin-dsn or COLLAUDE_ADMIN_DSN")
    return dsn


def cmd_migrate(args) -> None:
    with connect(admin_dsn(args)) as conn:
        conn.execute("create schema if not exists collaude")
        conn.execute("""create table if not exists collaude.schema_migrations
                        (version text primary key, applied_at timestamptz not null default now())""")
        applied = {r[0] for r in conn.execute("select version from collaude.schema_migrations")}
        conn.commit()
        for f in sorted(SQL_DIR.glob("*.sql")):
            if f.stem in applied:
                continue
            with conn.transaction():
                conn.execute(f.read_text())
                conn.execute("insert into collaude.schema_migrations (version) values (%s)", (f.stem,))
            print(f"applied {f.name}")
    print("schema up to date")


def _role_for_email(email: str) -> str:
    local = re.sub(r"[^a-z0-9_]", "_", email.split("@")[0].lower())
    return f"cm_{local}"[:63]


def cmd_add_group(args) -> None:
    with connect(admin_dsn(args)) as conn:
        conn.execute("insert into collaude.groups (id, name, description) values (%s, %s, %s)",
                     (args.id, args.name or args.id, args.description))
        conn.commit()
    print(f"group {args.id} created")


def cmd_add_user(args) -> None:
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    dsn = admin_dsn(args)
    role = args.role or _role_for_email(args.email)
    password = secrets.token_urlsafe(24)
    with connect(dsn) as conn:
        conn.execute(sql.SQL("create role {} login password {} in role collaude_member").format(
            sql.Identifier(role), sql.Literal(password)))
        conn.execute("insert into collaude.members (role_name, email, display_name) values (%s, %s, %s)",
                     (role, args.email, args.name))
        for g in args.group or []:
            conn.execute("insert into collaude.memberships (group_id, role_name) values (%s, %s)", (g, role))
        conn.commit()
    info = conninfo_to_dict(dsn)
    # Some poolers (e.g. Supabase's Supavisor) expect "<role>.<tenant>" as the user name;
    # reuse whatever suffix the admin's own user name carries.
    suffix = info.get("user", "").partition(".")[2]
    info.update(user=f"{role}.{suffix}" if suffix else role, password=password)
    print(f"Created {role} for {args.email}" + (f" in {', '.join(args.group)}" if args.group else "") + ".")
    print("Send them this connection string over a private channel; they run `collaude setup` with it:\n")
    print(f"  {make_conninfo(**info)}")


def cmd_add_member(args) -> None:
    with connect(admin_dsn(args)) as conn:
        role = conn.execute("select role_name from collaude.members where role_name = %s or email = %s",
                            (args.user, args.user)).fetchone()
        if not role:
            sys.exit(f"collaude: no member {args.user!r}")
        conn.execute("insert into collaude.memberships (group_id, role_name) values (%s, %s) on conflict do nothing",
                     (args.group, role[0]))
        conn.commit()
    print(f"{role[0]} added to {args.group}")


def cmd_remove_member(args) -> None:
    with connect(admin_dsn(args)) as conn:
        cur = conn.execute("""delete from collaude.memberships where group_id = %s and role_name in
                              (select role_name from collaude.members where role_name = %s or email = %s)""",
                           (args.group, args.user, args.user))
        conn.commit()
    print(f"removed {cur.rowcount} membership(s)")


def cmd_deactivate_user(args) -> None:
    from psycopg import sql

    with connect(admin_dsn(args)) as conn:
        role = conn.execute("select role_name from collaude.members where role_name = %s or email = %s",
                            (args.user, args.user)).fetchone()
        if not role:
            sys.exit(f"collaude: no member {args.user!r}")
        conn.execute(sql.SQL("alter role {} nologin").format(sql.Identifier(role[0])))
        conn.execute("update collaude.members set active = false where role_name = %s", role)
        conn.commit()
    print(f"{role[0]} can no longer log in; their memories stay, attributed to them")


def cmd_list_users(args) -> None:
    with connect(admin_dsn(args)) as conn:
        rows = conn.execute("""
            select u.role_name, u.email, u.active, coalesce(string_agg(m.group_id, ', ' order by m.group_id), '')
            from collaude.members u left join collaude.memberships m using (role_name)
            group by u.role_name order by u.role_name""").fetchall()
    for role, email, active, groups in rows:
        print(f"{role:24} {email or '':32} {'active' if active else 'INACTIVE':9} {groups}")


# --- CLI ---------------------------------------------------------------------


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="collaude", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, help=help_, **kw)
        if fn:
            sp.set_defaults(fn=fn)
        return sp

    def scope(sp):
        sp.add_argument("-g", "--group", help="group to search (default: this folder's group)")
        sp.add_argument("-a", "--all-groups", action="store_true", help="search every group you belong to")
        sp.add_argument("-n", "--limit", type=int, default=10)
        sp.add_argument("--include-inactive", action="store_true", help="include superseded/retracted memories")
        sp.add_argument("--json", action="store_true")

    sp = add("setup", cmd_setup, "save your database connection string")
    sp.add_argument("--dsn")
    sp = add("init", cmd_init, "link the current folder to a group (writes .collaude.json)")
    sp.add_argument("group")
    sp.add_argument("--share-excerpts", action="store_true",
                    help="also upload condensed transcript excerpts for provenance")
    sp.add_argument("--force", action="store_true")
    add("status", cmd_status, "show connection, identity, and groups")

    sp = add("search", cmd_search, "full-text search of shared memories")
    sp.add_argument("query", nargs="+")
    sp.add_argument("-m", "--messages", action="store_true", help="search ingested Slack/email messages instead")
    sp.add_argument("-k", "--kind", choices=KINDS)
    sp.add_argument("--author", help="role, email, or name fragment")
    sp.add_argument("--since", help="ISO date, e.g. 2026-09-01")
    scope(sp)
    sp = add("recent", cmd_recent, "most recent memories")
    scope(sp)
    sp = add("show", cmd_show, "one memory with full provenance")
    sp.add_argument("id", type=int)
    sp.add_argument("--excerpt", action="store_true", help="print shared transcript excerpts")
    sp.add_argument("--json", action="store_true")

    sp = add("add", cmd_add, "store a memory by hand")
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", required=True)
    sp.add_argument("--kind", choices=KINDS, default="finding")
    sp.add_argument("--tags", help="comma-separated")
    sp.add_argument("--confidence", choices=("low", "medium", "high"), default="medium")
    sp.add_argument("--supersedes", type=int)
    sp.add_argument("-g", "--group")
    sp = add("supersede", cmd_supersede, "mark a memory as replaced by a newer one")
    sp.add_argument("old", type=int)
    sp.add_argument("--by", type=int, required=True)
    sp.add_argument("--note")
    sp = add("retract", cmd_retract, "mark a memory as wrong")
    sp.add_argument("id", type=int)
    sp.add_argument("--reason")
    add("passwd", cmd_passwd, "change your database password")

    sp = add("ingest", cmd_ingest, "pull new messages from this machine's connectors and distil them")
    sp.add_argument("connector", nargs="?", help="only this connector")
    sp.add_argument("--stale-after", type=int, default=0, metavar="MIN",
                    help="skip connectors that ran less than MIN minutes ago (default 0: always run)")
    sp.add_argument("--max-threads", type=int, default=INGEST_MAX_THREADS,
                    help="threads to distil per run; the rest wait for the next run")
    sp.add_argument("--quiet", action="store_true")

    sp = add("connector", None, "add and manage Slack/email connectors")
    csub = sp.add_subparsers(dest="connector_cmd", required=True)

    def secret_opt(a):
        a.add_argument("--secret-env", metavar="VAR", help="read the token/password from this environment variable "
                                                          "instead of prompting")

    def backfill_opt(a):
        a.add_argument("--backfill-days", type=int, default=14, help="history to ingest on first run (default 14)")

    a = csub.add_parser("add", help="connect a Slack workspace or a mailbox (prompts for what it needs)")
    a.add_argument("kind", choices=("slack", "email", "imap"), help="email and imap are the same thing")
    a.add_argument("--name", help="connector name (default: the kind)")
    a.add_argument("--host", help="IMAP server, e.g. imap.gmail.com")
    a.add_argument("--user", help="IMAP login, e.g. lists-bot@example.org")
    a.add_argument("--port", type=int, default=993)
    a.add_argument("--route", action="append", metavar="SOURCE=GROUP",
                   help="'#channel=group' or 'folder=group'; repeatable. Without it you are asked per channel/folder")
    a.add_argument("--quiet-hours", type=float, help=f"wait this long after a thread's last message before "
                                                     f"distilling it (default {INGEST_QUIET_HOURS:g})")
    secret_opt(a)
    backfill_opt(a)
    a.set_defaults(fn=cmd_connector_add)
    a = csub.add_parser("route", help="feed a channel or folder into a group")
    a.add_argument("name")
    a.add_argument("source", help="'#channel', channel id, or mail folder")
    a.add_argument("group")
    backfill_opt(a)
    a.set_defaults(fn=cmd_connector_route)
    a = csub.add_parser("unroute", help="stop feeding a channel or folder")
    a.add_argument("name")
    a.add_argument("source")
    a.set_defaults(fn=cmd_connector_unroute)
    a = csub.add_parser("sources", help="channels/folders the credentials can see")
    a.add_argument("name")
    a.set_defaults(fn=cmd_connector_sources)
    a = csub.add_parser("login", help="store credentials for an existing connector on this machine")
    a.add_argument("name")
    secret_opt(a)
    a.set_defaults(fn=cmd_connector_login)
    csub.add_parser("list", help="connectors, routes, and sync status").set_defaults(fn=cmd_connector_list)
    for verb in ("pause", "resume"):
        a = csub.add_parser(verb)
        a.add_argument("name")
        a.set_defaults(fn=cmd_connector_toggle)
    a = csub.add_parser("remove", help="disconnect; keeps what was already ingested")
    a.add_argument("name")
    a.set_defaults(fn=cmd_connector_remove)
    csub.add_parser("slack-manifest", help="print the Slack app manifest").set_defaults(fn=cmd_slack_manifest)

    sp = add("hook", None, "entry points for Claude Code hooks")
    hsub = sp.add_subparsers(dest="hook", required=True)
    hsub.add_parser("session-start").set_defaults(fn=hook_session_start)
    hsub.add_parser("capture").set_defaults(fn=hook_capture)
    sp = add("capture-worker", capture_worker, argparse.SUPPRESS)
    sp.add_argument("payload")

    sp = add("admin", None, "administration (needs COLLAUDE_ADMIN_DSN)")
    sp.add_argument("--admin-dsn")
    asub = sp.add_subparsers(dest="admin", required=True)
    asub.add_parser("migrate", help="create or upgrade the schema").set_defaults(fn=cmd_migrate)
    a = asub.add_parser("add-group")
    a.add_argument("id")
    a.add_argument("--name")
    a.add_argument("--description")
    a.set_defaults(fn=cmd_add_group)
    a = asub.add_parser("add-user", help="create a login role and member")
    a.add_argument("email")
    a.add_argument("--name")
    a.add_argument("--role", help="Postgres role name (default cm_<email local part>)")
    a.add_argument("--group", action="append")
    a.set_defaults(fn=cmd_add_user)
    for name, fn in (("add-member", cmd_add_member), ("remove-member", cmd_remove_member)):
        a = asub.add_parser(name)
        a.add_argument("user", help="role name or email")
        a.add_argument("group")
        a.set_defaults(fn=fn)
    a = asub.add_parser("deactivate-user", help="revoke login; keeps their memories")
    a.add_argument("user")
    a.set_defaults(fn=cmd_deactivate_user)
    asub.add_parser("list-users").set_defaults(fn=cmd_list_users)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
