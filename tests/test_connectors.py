"""Connector, ingest, and distillation tests against a fake Slack API and a fake IMAP mailbox."""

import io
import json
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from helpers import cl, needs_db

cc = cl._connectors_mod()
NOW = time.time()
DAY = 86400


# --- fake Slack ------------------------------------------------------------------


class FakeSlack:
    """Enough of the Slack Web API for the connector: one channel the bot is in, one it is not."""

    def __init__(self):
        t0 = NOW - 3 * DAY
        self.users = {"U1": "Alice Astro", "U2": "Bob Bolometer"}
        self.channels = [{"id": "C0STARS1", "name": "clump-rotation", "is_member": True},
                         {"id": "C0OTHER1", "name": "random", "is_member": False}]
        self.broken = set()  # channels whose history calls fail
        self.history = {"C0STARS1": [
            {"ts": f"{t0:.6f}", "user": "U1", "text": "Gaia DR3 parallaxes for the clump sample need the "
             "Lindegren zero-point correction, otherwise distances come out 3% too large.",
             "reply_count": 2, "latest_reply": f"{t0 + 600:.6f}", "thread_ts": f"{t0:.6f}"},
            {"ts": f"{t0 + 300:.6f}", "user": "U2", "thread_ts": f"{t0:.6f}",
             "text": "Confirmed on <@U1>'s sample: the offset is -0.017 mas."},
            {"ts": f"{t0 + 600:.6f}", "user": "U1", "thread_ts": f"{t0:.6f}", "text": "thanks!"},
            {"ts": f"{t0 + 900:.6f}", "subtype": "channel_join", "user": "U2", "text": "<@U2> has joined"},
            {"ts": f"{NOW - 40 * DAY:.6f}", "user": "U1", "text": "Ancient message outside the backfill window."},
        ]}

    def add_reply(self, channel, parent_ts, user, text, ts):
        self.history[channel].append({"ts": f"{ts:.6f}", "user": user, "thread_ts": parent_ts, "text": text})
        parent = next(m for m in self.history[channel] if m["ts"] == parent_ts)
        parent["reply_count"] += 1
        parent["latest_reply"] = f"{ts:.6f}"

    def handle(self, method, q):
        if method == "auth.test":
            return {"ok": True, "team": "Flatiron Test", "team_id": "T1", "url": "https://fi-test.slack.com/",
                    "user": "collaude"}
        if method == "conversations.list":
            return {"ok": True, "channels": self.channels}
        if method == "conversations.info":
            return {"ok": True, "channel": {"id": q["channel"], "name": "clump-rotation"}}
        if method == "users.info":
            return {"ok": True, "user": {"name": q["user"], "profile": {"real_name": self.users[q["user"]]}}}
        if q.get("channel") in self.broken:
            return {"ok": False, "error": "not_in_channel"}
        msgs = self.history.get(q.get("channel"), [])
        oldest = float(q.get("oldest", 0))
        if method == "conversations.history":  # top-level messages and thread parents only
            out = [m for m in msgs if float(m["ts"]) > oldest and m.get("thread_ts", m["ts"]) == m["ts"]]
            return {"ok": True, "messages": sorted(out, key=lambda m: -float(m["ts"]))}
        if method == "conversations.replies":
            out = [m for m in msgs if m.get("thread_ts") == q["ts"] and (m["ts"] == q["ts"] or float(m["ts"]) > oldest)]
            return {"ok": True, "messages": sorted(out, key=lambda m: float(m["ts"]))}
        return {"ok": False, "error": "unknown_method"}


@pytest.fixture
def slack(monkeypatch):
    fake = FakeSlack()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            assert self.headers["Authorization"] == "Bearer xoxb-test"
            body = json.dumps(fake.handle(url.path.rsplit("/", 1)[-1], dict(urllib.parse.parse_qsl(url.query))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/api/"
    orig = cc.SlackConnector.__init__
    monkeypatch.setattr(cc.SlackConnector, "__init__",
                        lambda self, settings, secret, api_base=base: orig(self, settings, secret, api_base))
    yield fake
    server.shutdown()


# --- fake IMAP -------------------------------------------------------------------


def make_mail(i, subject, body, sender="Carol <carol@example.org>", refs=None, days_ago=2):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = sender, subject, f"<msg{i}@example.org>"
    m["Date"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    if refs:
        m["References"] = m["In-Reply-To"] = refs
    m.set_content(body)
    return m.as_bytes()


class FakeImap:
    mailbox = {}

    def __init__(self, host, port, timeout=None):
        self.untagged_responses = {}

    def login(self, user, password):
        if password != "app-pass":
            raise cc.imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def authenticate(self, mechanism, authobject):
        if mechanism != "XOAUTH2" or authobject(b"") != b"user=bot\1auth=Bearer access-1\1\1":
            raise cc.imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "lists/stars"',
                      b'(\\Noselect \\HasChildren) "/" "[Gmail]"']

    def select(self, folder, readonly=False):
        self.folder = folder.strip('"')
        self.untagged_responses["UIDVALIDITY"] = [b"77"]
        return ("OK", [b"1"]) if self.folder in self.mailbox else ("NO", [])

    def uid(self, cmd, *args):
        msgs = self.mailbox[self.folder]
        if cmd == "search":
            if args[1].startswith("UID"):
                lo = int(args[1].split()[1].split(":")[0])
                hits = [u for u in msgs if u >= lo] or [max(msgs)]  # IMAP returns the last UID for N:*
            else:
                hits = list(msgs)
            return "OK", [" ".join(map(str, hits)).encode()]
        if cmd == "fetch":
            return "OK", [(f"{u} (UID {u} BODY[] {{1}}".encode(), msgs[u]) for u in map(int, args[0].split(","))]

    def logout(self):
        pass


# --- unit tests --------------------------------------------------------------------


def test_clean_email_body_drops_quotes_signatures_and_footers():
    body = ("Fits converge with the new priors.\n\n> old quoted line\n\n"
            "On Mon, 6 Oct 2026 at 10:00, Bob <bob@x.org> wrote:\n> earlier\n")
    assert cl._connectors_mod().clean_email_body(body) == "Fits converge with the new priors."
    body = "Result: 42.\n-- \nCarol\nYou received this message because you are subscribed to the group"
    assert cc.clean_email_body(body) == "Result: 42."


def test_parse_email_threads_by_references():
    it = cc.parse_email(make_mail(2, "Re: priors", "Agreed.", refs="<msg1@example.org>"), "lists/stars", "x", "gmail")
    assert it["thread_key"] == "<msg1@example.org>" and it["external_id"] == "<msg2@example.org>"
    assert it["author_name"] == "Carol" and it["author_handle"] == "carol@example.org"
    assert it["permalink"].startswith("https://mail.google.com/") and it["subject"] == "Re: priors"


def test_slack_fetch_threads_lookback_and_mentions(slack):
    conn = cc.SlackConnector({"url": "https://fi-test.slack.com/", "lookback_days": 7}, "xoxb-test")
    since = datetime.now(timezone.utc) - timedelta(days=14)
    items, cursor = conn.fetch("C0STARS1", None, since, "#clump-rotation")
    texts = [i["content"] for i in items]
    assert len(items) == 3  # parent + 2 replies; join message and 40-day-old message skipped
    assert "Confirmed on @Alice Astro's sample: the offset is -0.017 mas." in texts
    assert {i["thread_key"] for i in items} == {f"C0STARS1:{NOW - 3 * DAY:.6f}"}
    assert items[1]["permalink"].startswith("https://fi-test.slack.com/archives/C0STARS1/p")
    assert "thread_ts=" in items[1]["permalink"]
    # A new reply to a 3-day-old thread is found through the lookback window.
    slack.add_reply("C0STARS1", f"{NOW - 3 * DAY:.6f}", "U2", "Update: offset depends on magnitude.", NOW - 60)
    items, cursor2 = conn.fetch("C0STARS1", cursor, since, "#clump-rotation")
    assert [i["content"] for i in items] == ["Update: offset depends on magnitude."]
    assert float(cursor2) > float(cursor)


def test_imap_fetch_incremental(monkeypatch):
    monkeypatch.setattr(cc.imaplib, "IMAP4_SSL", FakeImap)
    FakeImap.mailbox = {"lists/stars": {1: make_mail(1, "priors", "Use the broad prior.", days_ago=3),
                                        2: make_mail(2, "old", "Too old.", days_ago=60)}}
    conn = cc.ImapConnector({"host": "imap.example.org", "user": "bot"}, "app-pass")
    assert conn.list_sources() == [("INBOX", "INBOX"), ("lists/stars", "lists/stars")]
    items, cursor = conn.fetch("lists/stars", None, datetime.now(timezone.utc) - timedelta(days=14))
    assert [i["subject"] for i in items] == ["priors"] and cursor == "77:2"
    FakeImap.mailbox["lists/stars"][3] = make_mail(3, "Re: priors", "Narrow prior fails.", refs="<msg1@example.org>")
    items, cursor = conn.fetch("lists/stars", cursor, datetime.now(timezone.utc) - timedelta(days=14))
    assert [i["thread_key"] for i in items] == ["<msg1@example.org>"] and cursor == "77:3"
    items, _ = conn.fetch("lists/stars", cursor, datetime.now(timezone.utc))
    assert items == []
    with pytest.raises(cc.ConnectorError):
        cc.ImapConnector({"host": "imap.example.org", "user": "bot"}, "wrong").check()


def test_parse_memories_requires_known_source_ids():
    raw = json.dumps({"memories": [
        {"title": "Offset is -0.017 mas", "body": "b", "kind": "finding", "source_ids": ["S5", 99]},
        {"title": "No sources given", "body": "b", "kind": "finding", "source_ids": []}]})
    out = cl.parse_memories(raw, "", set(), allowed_source_ids={5, 6})
    assert [m["source_ids"] for m in out] == [[5]]


def test_session_start_spawns_ingest_only_with_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "SECRETS_FILE", tmp_path / "secrets.json")
    monkeypatch.setattr(cl, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("COLLAUDE_DSN", "postgresql://example/none")
    spawned = []
    monkeypatch.setattr(cl.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    assert not cl.spawn_ingest_if_configured()
    (tmp_path / "secrets.json").write_text('{"slack": "xoxb-test"}')
    assert cl.spawn_ingest_if_configured()
    assert spawned[0][-4:] == ["ingest", "--stale-after", str(cl.INGEST_STALE_MINUTES), "--quiet"]


# --- database tests ------------------------------------------------------------------


class FakeTokenEndpoint:
    """Stands in for urlopen against Google's token endpoint."""

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.requests = reply or {"access_token": "access-1", "expires_in": 3600}, error, []

    def __call__(self, req, timeout=None):
        self.requests.append((req.full_url, dict(urllib.parse.parse_qsl(req.data.decode()))))
        if self.error:
            raise cc.urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                            io.BytesIO(json.dumps({"error": self.error}).encode()))
        return io.BytesIO(json.dumps(self.reply).encode())


def test_imap_oauth_refreshes_once_and_logs_in_with_xoauth2(monkeypatch):
    monkeypatch.setattr(cc.imaplib, "IMAP4_SSL", FakeImap)
    token = FakeTokenEndpoint()
    monkeypatch.setattr(cc.urllib.request, "urlopen", token)
    secret = cc.oauth_secret("client-1", "shh", "refresh-1")
    conn = cc.ImapConnector({"host": "imap.gmail.com", "user": "bot", "auth": "oauth"}, secret)
    assert conn.check() == {"web_link": "gmail"}
    assert conn.list_sources()[0] == ("INBOX", "INBOX")
    assert token.requests == [(cc.GOOGLE_TOKEN_URL, {"client_id": "client-1", "client_secret": "shh",
                                                     "refresh_token": "refresh-1", "grant_type": "refresh_token"})]
    assert cc.xoauth2_string("me@x.org", "T") == "user=me@x.org\x01auth=Bearer T\x01\x01"

    monkeypatch.setattr(cc.urllib.request, "urlopen", FakeTokenEndpoint(error="invalid_grant"))
    with pytest.raises(cc.ConnectorError, match="connector login"):
        cc.ImapConnector({"host": "imap.gmail.com", "user": "bot", "auth": "oauth"}, secret).check()


def test_oauth_redirect_checks_state():
    assert cc._code_from_redirect("http://127.0.0.1:5000/?state=s1&code=abc&scope=x", "s1") == "abc"
    assert cc._code_from_redirect("/?code=abc&state=s1", "s1") == "abc"
    assert cc._code_from_redirect("  abc \n", "s1") == "abc"  # a bare pasted code
    with pytest.raises(cc.ConnectorError, match="state"):
        cc._code_from_redirect("/?code=abc&state=forged", "s1")
    with pytest.raises(cc.ConnectorError, match="access_denied"):
        cc._code_from_redirect("/?error=access_denied&state=s1", "s1")


def test_oauth_authorize_uses_a_loopback_redirect_with_pkce(monkeypatch, capsys):
    import base64
    import hashlib
    import http.client

    seen = {"statuses": []}

    def browser(url):  # Google, after the user signs in, sends the browser back to the loopback server
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.update(q)
        port = urllib.parse.urlsplit(q["redirect_uri"]).port

        def visit():
            for state in ("forged", q["state"]):  # a redirect for some other sign-in is ignored
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", f"/?code=code-1&state={state}")
                seen["statuses"].append(c.getresponse().status)
        seen["visit"] = threading.Thread(target=visit)
        seen["visit"].start()

    def token(req, timeout=None):
        seen["form"] = dict(urllib.parse.parse_qsl(req.data.decode()))
        return io.BytesIO(json.dumps({"access_token": "a", "refresh_token": "refresh-1"}).encode())

    monkeypatch.setattr(cc.webbrowser, "open", browser)
    monkeypatch.setattr(cc.urllib.request, "urlopen", token)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cc.oauth_authorize("client-1", "shh", login_hint="me@x.org", timeout=10) == "refresh-1"
    seen["visit"].join(5)
    assert seen["statuses"] == [400, 200]
    assert seen["scope"] == cc.GMAIL_SCOPE and seen["access_type"] == "offline" and seen["prompt"] == "consent"
    assert seen["code_challenge_method"] == "S256" and seen["login_hint"] == "me@x.org"
    form = seen["form"]
    assert form["code"] == "code-1" and form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == seen["redirect_uri"] and form["redirect_uri"].startswith("http://127.0.0.1:")
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == seen["code_challenge"]
    assert "https://accounts.google.com/" in capsys.readouterr().out  # printed for SSH sessions


def test_oauth_client_file(tmp_path):
    f = tmp_path / "client_secret.json"
    f.write_text(json.dumps({"installed": {"client_id": "client-1", "client_secret": "shh"}}))
    assert cc.oauth_client_from_file(f) == ("client-1", "shh")
    f.write_text("{}")
    with pytest.raises(cc.ConnectorError, match="Desktop app"):
        cc.oauth_client_from_file(f)


@pytest.fixture
def as_user(db, tmp_path, monkeypatch):
    """Switch the CLI to a user, with credentials stored per user in their own config dir."""
    fake = tmp_path / "fake_extractor.py"
    fake.write_text(FAKE_EXTRACTOR)
    monkeypatch.setenv("COLLAUDE_EXTRACT_CMD", f"{sys.executable} {fake}")
    monkeypatch.setattr(cl, "STATE_DIR", tmp_path / "state")

    def switch(name):
        monkeypatch.setenv("COLLAUDE_DSN", db[name])
        monkeypatch.setattr(cl, "SECRETS_FILE", tmp_path / f"{name}-secrets.json")
        monkeypatch.setattr(cl, "CONFIG_DIR", tmp_path)
        return db[name]
    return switch


# Writes one memory per thread from its first non-context message, citing that message.
FAKE_EXTRACTOR = r'''
import json, re, sys
prompt = sys.stdin.read()
out = []
for block in prompt.split("=== thread in ")[1:]:
    for line in block.splitlines():
        m = re.match(r"\[S(\d+)\] \S+ \S+ ([^:]+): (.*)", line)
        if m and len(m.group(3)) > 40:
            out.append({"title": "From " + m.group(2) + ": " + m.group(3)[:60], "body": m.group(3),
                        "kind": "finding", "tags": ["test"], "confidence": "medium",
                        "evidence_quote": m.group(3)[:50], "source_ids": [int(m.group(1))], "supersedes": None})
            break
print(json.dumps({"memories": out}))
'''


@needs_db
def test_slack_end_to_end(db, slack, as_user, monkeypatch, capsys):
    import psycopg

    as_user("alice")
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-test")
    cl.main(["connector", "add", "slack", "--secret-env", "SLACK_TOKEN", "--route", "#clump-rotation=g1",
             "--route", "#random=g1", "--route", "#clump-rotation=g2"])
    out = capsys.readouterr().out
    assert "Credentials work: Slack workspace Flatiron Test as @collaude" in out
    assert "#clump-rotation -> g1" in out
    assert "skipped #random" in out and "not in group 'g2'" in out
    assert json.loads(cl.SECRETS_FILE.read_text()) == {"slack": "xoxb-test"}

    with cl.connect(db["admin"]) as conn:  # credentials never reach the database
        assert "xoxb" not in json.dumps(conn.execute("select settings from collaude.connectors").fetchall())

    cl.main(["ingest"])
    out = capsys.readouterr().out
    assert "slack: 3 new messages, 1 new memories" in out
    assert "fetching #clump-rotation -> g1" in out and "3 new of 3 fetched" in out  # progress as it goes
    assert "distilling 1 quiet threads in 1 extractor calls" in out and "1/1: 1 threads -> 1 memories" in out
    cl.main(["ingest", "--stale-after", "30"])
    assert "skipped (ran less than 30 min ago" in capsys.readouterr().out

    with cl.connect(db["carol"]) as conn:  # carol is in g1
        hits = cl.search_sources(conn, "zero-point", None)
        assert hits and hits[0]["author_name"] == "Alice Astro" and hits[0]["container"] == "#clump-rotation"
        mem = cl.search_memories(conn, "Lindegren", ["g1"])[0]
        assert mem["origin"] == "slack" and mem["author"].endswith("_alice")
        assert abs((mem["source_time"].timestamp()) - (NOW - 3 * DAY)) < 2
    as_user("carol")
    cl.main(["show", str(mem["id"])])
    out = capsys.readouterr().out
    assert "from slack, ingested by Alice" in out and "From these messages" in out
    assert "https://fi-test.slack.com/archives/C0STARS1/p" in out

    as_user("bob")  # not in g1
    with cl.connect(db["bob"]) as conn:
        assert not cl.search_sources(conn, "zero-point", None)
        assert not conn.execute("select * from collaude.connectors").fetchall()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select collaude.claim_ingest('slack', interval '0', interval '1 minute')")

    # A late reply to the old thread: distilled with the earlier messages as context.
    slack.add_reply("C0STARS1", f"{NOW - 3 * DAY:.6f}", "U2",
                    "Update from Bob: the zero-point offset depends on G magnitude, use the full recipe.",
                    NOW - 3 * 3600)
    as_user("alice")
    cl.main(["ingest"])
    assert "slack: 1 new messages, 1 new memories" in capsys.readouterr().out
    cl.main(["connector", "list"])
    out = capsys.readouterr().out
    assert "credentials on this machine: yes" in out and "4 messages, 2 memories" in out


@needs_db
def test_lease_lets_one_collaborator_ingest_at_a_time(db, slack, as_user, monkeypatch, capsys):
    as_user("carol")
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-test")
    cl.main(["connector", "add", "slack", "--name", "slack2", "--secret-env", "SLACK_TOKEN",
             "--route", "#clump-rotation=g1", "--quiet-hours", "0"])
    as_user("alice")  # a second credential holder
    cl.main(["connector", "login", "slack2", "--secret-env", "SLACK_TOKEN"])
    claim = "select collaude.claim_ingest('slack2', make_interval(mins => %s), interval '30 minutes')"
    with cl.connect(db["carol"]) as c1, cl.connect(db["alice"]) as c2:
        assert c1.execute(claim, (0,)).fetchone()[0] is True
        c1.commit()
        assert c2.execute(claim, (0,)).fetchone()[0] is False  # carol holds the lease
        c2.commit()
        c1.execute("select collaude.finish_ingest('slack2', null)")
        c1.commit()
        assert c2.execute(claim, (30,)).fetchone()[0] is False  # ran just now
        assert c2.execute(claim, (0,)).fetchone()[0] is True
        c2.execute("select collaude.finish_ingest('slack2', 'boom')")
        c2.commit()
    as_user("alice")
    cl.main(["connector", "list"])
    assert "ERROR: boom" in capsys.readouterr().out


@needs_db
def test_email_connector_and_distill_cap(db, as_user, monkeypatch, capsys):
    monkeypatch.setattr(cc.imaplib, "IMAP4_SSL", FakeImap)
    FakeImap.mailbox = {"lists/stars": {
        i: make_mail(100 + i, f"thread {i}", f"Result number {i}: the spectroscopic pipeline gives "
                     f"a systematic offset of {i}0 m/s in radial velocity.", days_ago=5 - i) for i in (1, 2, 3)}}
    as_user("carol")
    monkeypatch.setenv("MAILPW", "app-pass")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    cl.main(["connector", "add", "email", "--name", "lists", "--host", "imap.example.org", "--user", "bot",
             "--secret-env", "MAILPW", "--route", "lists/stars=g2"])
    cl.main(["ingest", "lists", "--max-threads", "2"])
    assert "lists: 3 new messages, 2 new memories" in capsys.readouterr().out
    cl.main(["ingest", "lists", "--max-threads", "2"])  # catches up on the rest
    assert "lists: 0 new messages, 1 new memories" in capsys.readouterr().out
    with cl.connect(db["bob"]) as conn:  # bob is in g2
        titles = [m["title"] for m in cl.fetch_recent(conn, ["g2"], 10) if m["origin"] == "email"]
        assert len(titles) == 3 and all(t.startswith("From Carol: Result number") for t in titles)
    cl.main(["connector", "remove", "lists"])
    assert "lists" not in json.loads(cl.SECRETS_FILE.read_text())
    with cl.connect(db["bob"]) as conn:  # history kept
        assert len(cl.search_sources(conn, "spectroscopic", ["g2"])) == 3


@needs_db
def test_email_connector_with_oauth(db, as_user, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cc.imaplib, "IMAP4_SSL", FakeImap)
    FakeImap.mailbox = {"lists/stars": {1: make_mail(201, "priors", "Use the broad prior.", days_ago=1)}}
    monkeypatch.setattr(cc.urllib.request, "urlopen", FakeTokenEndpoint())
    signins = []
    monkeypatch.setattr(cc, "oauth_authorize",
                        lambda cid, csec, login_hint=None: signins.append((cid, csec, login_hint)) or "refresh-1")
    client = tmp_path / "client_secret.json"
    client.write_text(json.dumps({"installed": {"client_id": "client-1", "client_secret": "shh"}}))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    as_user("carol")
    cl.main(["connector", "add", "email", "--oauth", "--oauth-client-file", str(client), "--name", "gmail",
             "--user", "bot", "--route", "lists/stars=g2"])
    assert signins == [("client-1", "shh", "bot")]
    saved = json.loads(json.loads(cl.SECRETS_FILE.read_text())["gmail"])
    assert saved == {"client_id": "client-1", "client_secret": "shh", "refresh_token": "refresh-1"}
    with cl.connect(db["carol"]) as conn:  # the database knows how to sign in, but holds no secrets
        settings = conn.execute("select settings from collaude.connectors where id = 'gmail'").fetchone()[0]
    assert settings["auth"] == "oauth" and settings["host"] == "imap.gmail.com"
    assert "shh" not in json.dumps(settings) and "refresh" not in json.dumps(settings)
    cl.main(["ingest", "gmail"])
    assert "gmail: 1 new messages" in capsys.readouterr().out

    as_user("bob")  # a second machine, signing in with the same client
    cl.main(["connector", "login", "gmail", "--oauth-client-file", str(client)])
    assert signins[-1] == ("client-1", "shh", "bot") and "gmail" in json.loads(cl.SECRETS_FILE.read_text())
    cl.main(["connector", "login", "gmail"])  # signing in again reuses the client saved here
    assert len(signins) == 3
    monkeypatch.setattr(cl, "SECRETS_FILE", tmp_path / "fresh-secrets.json")
    with pytest.raises(SystemExit, match="OAuth client"):  # no client given and none saved here
        cl.main(["connector", "login", "gmail"])


@needs_db
def test_one_failing_route_does_not_stop_the_others(db, slack, as_user, monkeypatch, capsys):
    slack.channels.append({"id": "C0GONE01", "name": "aaa-kicked", "is_member": True})
    as_user("alice")
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-test")
    cl.main(["connector", "add", "slack", "--name", "slack3", "--secret-env", "SLACK_TOKEN",
             "--route", "#aaa-kicked=g1", "--route", "#clump-rotation=g1"])
    slack.broken.add("C0GONE01")  # e.g. the bot was removed from the channel
    capsys.readouterr()
    cl.main(["ingest", "slack3"])
    out = capsys.readouterr().out
    assert "slack3: 3 new messages, 1 new memories; 1 of 2 sources failed" in out
    assert "#aaa-kicked: ConnectorError: Slack conversations.history: not_in_channel" in out
    cl.main(["connector", "list"])
    assert "ERROR: #aaa-kicked: ConnectorError" in capsys.readouterr().out


@needs_db
def test_a_thread_the_extractor_fails_on_is_retried_then_given_up(db, as_user, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cc.imaplib, "IMAP4_SSL", FakeImap)
    FakeImap.mailbox = {"lists/stars": {
        1: make_mail(201, "good", "Result: the asteroseismic masses agree with the eclipsing binaries to 2%.", days_ago=4),
        2: make_mail(202, "bad", "POISON this thread makes the extractor reply with something that is not JSON.",
                     days_ago=3),
        3: make_mail(203, "also good", "Result: the red clump luminosity calibration shifts by 0.03 mag.", days_ago=2)}}
    as_user("carol")
    fake = tmp_path / "flaky_extractor.py"
    fake.write_text("import sys\nprompt = sys.stdin.read()\n"
                    "if 'POISON' in prompt:\n    print('Sorry, I cannot help with that.')\n"
                    f"else:\n    sys.stdin = __import__('io').StringIO(prompt)\n    exec({FAKE_EXTRACTOR!r})\n")
    monkeypatch.setenv("COLLAUDE_EXTRACT_CMD", f"{sys.executable} {fake}")
    monkeypatch.setenv("MAILPW", "app-pass")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    cl.main(["connector", "add", "email", "--name", "flaky", "--host", "imap.example.org", "--user", "bot",
             "--secret-env", "MAILPW", "--route", "lists/stars=g2"])
    capsys.readouterr()
    cl.main(["ingest", "flaky"])  # the two good threads get through despite sharing a batch with the bad one
    assert "flaky: 3 new messages, 2 new memories" in capsys.readouterr().out
    state = "select distill_failures, distilled_at is not null from collaude.sources where subject = 'bad' " \
            "and connector_id = 'flaky'"
    with cl.connect(db["carol"]) as conn:
        assert conn.execute(state).fetchone() == (1, False)
    cl.main(["ingest", "flaky"])
    cl.main(["ingest", "flaky"])
    with cl.connect(db["carol"]) as conn:
        assert conn.execute(state).fetchone() == (3, True)  # given up, so it no longer blocks the queue
    assert "extractor failed (3/3, giving up)" in (cl.STATE_DIR / "collaude.log").read_text()


@needs_db
def test_lease_is_renewed_and_a_lapsed_lease_stops_the_run(db, slack, as_user, monkeypatch):
    as_user("alice")
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-test")
    cl.main(["connector", "add", "slack", "--name", "slack4", "--secret-env", "SLACK_TOKEN",
             "--route", "#clump-rotation=g1"])
    until = "select lease_until from collaude.connectors where id = 'slack4'"
    with cl.connect(db["alice"]) as conn, cl.connect(db["admin"]) as admin:
        assert conn.execute("select collaude.claim_ingest('slack4', interval '0', interval '1 minute')").fetchone()[0]
        conn.commit()
        before = conn.execute(until).fetchone()[0]
        conn.commit()
        cl._renew_lease(conn, "slack4")
        assert conn.execute(until).fetchone()[0] > before + timedelta(minutes=20)
        conn.commit()
        admin.execute("update collaude.connectors set lease_until = now() - interval '1 second' where id = 'slack4'")
        admin.commit()
        with pytest.raises(cl.LeaseLost):
            cl._renew_lease(conn, "slack4")
        with cl.connect(db["carol"]) as other:  # someone else takes over; alice can't renew their lease
            assert other.execute("select collaude.claim_ingest('slack4', interval '0', interval '1 minute')").fetchone()[0]
            other.commit()
        with pytest.raises(cl.LeaseLost):
            cl._renew_lease(conn, "slack4")


# --- fake GitHub -----------------------------------------------------------------


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """Enough of the GitHub REST API for the connector: one repo with an issue, a PR, and their comments."""

    repo = "stars-lab/clump-rotation"

    def __init__(self):
        t0 = NOW - 3 * DAY
        base = f"https://github.com/{self.repo}"
        user = {"login": "alice", "type": "User"}
        self.issues = [
            {"id": 11, "number": 7, "title": "Distances 3% too large", "user": user, "created_at": _iso(t0),
             "updated_at": _iso(t0 + 900), "html_url": f"{base}/issues/7",
             "body": "<!-- describe the bug -->\nGaia DR3 parallaxes need the Lindegren zero-point correction, "
                     "otherwise the clump distances come out 3% too large."},
            {"id": 12, "number": 8, "title": "Apply the zero-point recipe", "pull_request": {}, "user": user,
             "created_at": _iso(t0 + 600), "updated_at": _iso(t0 + 1200), "html_url": f"{base}/pull/8",
             "body": "Fixes #7."},
            {"id": 13, "number": 9, "title": "Bump numpy", "user": {"login": "dependabot[bot]", "type": "Bot"},
             "created_at": _iso(t0), "updated_at": _iso(t0), "html_url": f"{base}/pull/9", "body": "Bumps numpy."},
        ]
        self.comments = [
            {"id": 21, "issue_url": f"https://api.github.com/repos/{self.repo}/issues/7",
             "user": {"login": "bob", "type": "User"}, "created_at": _iso(t0 + 300), "updated_at": _iso(t0 + 300),
             "html_url": f"{base}/issues/7#issuecomment-21",
             "body": "Confirmed on Alice's sample: the zero-point offset is -0.017 mas for the whole clump."},
            {"id": 22, "issue_url": f"https://api.github.com/repos/{self.repo}/issues/7",
             "user": {"login": "ci-bot", "type": "Bot"}, "created_at": _iso(t0 + 400),
             "updated_at": _iso(t0 + 400), "html_url": f"{base}/issues/7#issuecomment-22", "body": "Build passed."},
        ]
        self.review_comments = [
            {"id": 31, "pull_request_url": f"https://api.github.com/repos/{self.repo}/pulls/8", "path": "fit.py",
             "user": {"login": "bob", "type": "User"}, "created_at": _iso(t0 + 1200),
             "updated_at": _iso(t0 + 1200), "html_url": f"{base}/pull/8#discussion_r31",
             "body": "The offset depends on G magnitude, so use the full recipe, not a constant."},
        ]

    def handle(self, path, q):
        since = q.get("since", "")
        if path == "/user":
            return {"login": "alice"}
        if path == "/user/repos":
            return [{"full_name": self.repo}, {"full_name": "stars-lab/old", "archived": True}]
        if path.lower() == f"/repos/{self.repo}".lower():
            return {"full_name": self.repo}
        if path == f"/repos/{self.repo}/issues":
            return [i for i in self.issues if i["updated_at"] >= since]
        if path == f"/repos/{self.repo}/issues/comments":
            return [c for c in self.comments if c["updated_at"] >= since]
        if path == f"/repos/{self.repo}/pulls/comments":
            return [c for c in self.review_comments if c["updated_at"] >= since]
        return None


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            assert self.headers["Authorization"] == "Bearer ghp-test"
            q = dict(urllib.parse.parse_qsl(url.query))
            data = fake.handle(url.path, q)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            page = int(q.get("page", 1))  # serve lists one item per page, to exercise Link paging
            if isinstance(data, list):
                body = data[page - 1:page]
                if page < len(data):
                    nxt = urllib.parse.urlencode({**q, "page": page + 1})
                    link = f'<http://127.0.0.1:{server.server_port}{url.path}?{nxt}>; rel="next"'
                else:
                    link = None
            else:
                body, link = data, None
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if link:
                self.send_header("Link", link)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    orig = cc.GitHubConnector.__init__
    monkeypatch.setattr(cc.GitHubConnector, "__init__",
                        lambda self, settings, secret, api_base=base: orig(self, settings, secret, api_base))
    yield fake
    server.shutdown()


def test_github_fetch_threads_skips_bots_and_pages(github):
    g = cc.make("github", {}, "ghp-test")
    assert g.check() == {"login": "alice"}
    assert g.list_sources() == [(FakeGitHub.repo, FakeGitHub.repo)]
    assert g.resolve(f"https://github.com/{FakeGitHub.repo.upper()}.git") == (FakeGitHub.repo, FakeGitHub.repo)
    with pytest.raises(cc.ConnectorError, match="404"):
        g.resolve("stars-lab/missing")

    items, cursor = g.fetch(FakeGitHub.repo, None, datetime.now(timezone.utc) - timedelta(days=14))
    by_id = {it["external_id"].split(":")[1]: it for it in items}
    assert set(by_id) == {"issue11", "issue12", "comment21", "review31"}  # no bot issue or comment
    assert by_id["issue11"]["thread_key"] == by_id["comment21"]["thread_key"] == f"{FakeGitHub.repo}#7"
    assert by_id["comment21"]["subject"] == "Issue #7: Distances 3% too large"
    assert by_id["review31"]["subject"] == "PR #8: Apply the zero-point recipe"
    assert by_id["review31"]["content"].startswith("[on fit.py] The offset depends")
    assert "describe the bug" not in by_id["issue11"]["content"]
    assert by_id["issue11"]["permalink"].endswith("/issues/7") and by_id["comment21"]["author_name"] == "bob"
    assert cursor == _iso(NOW - 3 * DAY + 1200)

    github.comments.append({**github.comments[0], "id": 23, "created_at": _iso(NOW - 3600),
                            "updated_at": _iso(NOW - 3600), "body": "A late follow-up on the zero point."})
    github.issues[0]["updated_at"] = _iso(NOW - 3600)
    items, cursor2 = g.fetch(FakeGitHub.repo, cursor, datetime.now(timezone.utc))
    assert "comment23" in {it["external_id"].split(":")[1] for it in items} and cursor2 == _iso(NOW - 3600)


def test_github_token_from_gh(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return cc.subprocess.CompletedProcess(cmd, 0, stdout="gho_fromgh\n", stderr="")

    monkeypatch.setattr(cc.subprocess, "run", fake_run)
    g = cc.make("github", {"login": "alice"}, "gh")
    assert g.token() == g.token() == "gho_fromgh" and calls == [["gh", "auth", "token"]]
    assert g.describe() == "GitHub as @alice (via gh)"
    monkeypatch.setattr(cc.subprocess, "run",
                        lambda cmd, **kw: cc.subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in"))
    with pytest.raises(cc.ConnectorError, match="gh auth login"):
        cc.make("github", {}, "gh").token()


def test_ask_routes_by_name_when_there_are_many_sources(monkeypatch, capsys):
    class Many:
        def list_sources(self):
            return [(f"o/r{i}", f"o/r{i}") for i in range(30)]

    monkeypatch.setattr(sys, "stdin", io.StringIO("o/r3=g1\nnonsense\n o/r7 = g2 \n\n"))
    assert cl._ask_routes(Many(), ["g1", "g2"]) == [("o/r3", "g1"), ("o/r7", "g2")]
    assert "can see 30 sources" in capsys.readouterr().out


@needs_db
def test_github_end_to_end(db, github, as_user, monkeypatch, capsys):
    as_user("alice")
    monkeypatch.setenv("GH_TOKEN_TEST", "ghp-test")
    cl.main(["connector", "add", "github", "--secret-env", "GH_TOKEN_TEST", "--route", f"{FakeGitHub.repo}=g1"])
    out = capsys.readouterr().out
    assert "Credentials work: GitHub as @alice" in out and f"{FakeGitHub.repo} -> g1" in out
    cl.main(["ingest", "github"])
    assert "github: 4 new messages, 2 new memories" in capsys.readouterr().out
    cl.main(["ingest", "github"])  # the boundary items come back and are dropped as duplicates
    assert "github: 0 new messages" in capsys.readouterr().out
    with cl.connect(db["carol"]) as conn:
        mems = [m for m in cl.fetch_recent(conn, ["g1"], 10) if m["origin"] == "github"]
        assert len(mems) == 2  # one per thread with something to say; the bot PR has nothing
        hits = cl.search_sources(conn, "zero-point offset", None)
        assert hits and hits[0]["container"] == FakeGitHub.repo


@needs_db
def test_rename_connector(db, slack, as_user, monkeypatch, capsys):
    as_user("alice")
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-test")
    cl.main(["connector", "add", "slack", "--name", "lab", "--secret-env", "SLACK_TOKEN",
             "--route", "#clump-rotation=g1"])
    cl.main(["ingest", "lab"])
    capsys.readouterr()

    as_user("carol")  # in g1, but not the creator
    with pytest.raises(SystemExit, match="only the creator"):
        cl.main(["connector", "rename", "lab", "stars-slack"])
    as_user("alice")
    with pytest.raises(SystemExit, match="lowercase"):
        cl.main(["connector", "rename", "lab", "Stars Slack"])
    with cl.connect(db["alice"]) as conn:  # a run in progress blocks the rename
        assert conn.execute("select collaude.claim_ingest('lab', interval '0', interval '5 minutes')").fetchone()[0]
        conn.commit()
        with pytest.raises(SystemExit, match="is running"):
            cl.main(["connector", "rename", "lab", "stars-slack"])
        conn.execute("select collaude.finish_ingest('lab', null)")
        conn.commit()

    cl.main(["connector", "rename", "lab", "stars-slack"])
    assert "Renamed 'lab' to 'stars-slack'" in capsys.readouterr().out
    assert json.loads(cl.SECRETS_FILE.read_text()) == {"stars-slack": "xoxb-test"}
    with cl.connect(db["alice"]) as conn:
        assert conn.execute("select count(*) from collaude.sources where connector_id = 'stars-slack'").fetchone()[0] == 3
        assert conn.execute("select count(*) from collaude.connector_routes "
                            "where connector_id = 'stars-slack'").fetchone()[0] == 1
        assert not conn.execute("select 1 from collaude.connectors where id = 'lab'").fetchall()

    # Another machine still holds the credentials under the old name: its next ingest adopts them.
    cl.SECRETS_FILE.write_text(json.dumps({"lab": "xoxb-test"}))
    cl.main(["ingest", "--max-threads", "0"])
    assert "stars-slack:" in capsys.readouterr().out
    assert json.loads(cl.SECRETS_FILE.read_text()) == {"stars-slack": "xoxb-test"}


@needs_db
def test_one_channel_feeds_several_groups(db, slack, as_user, monkeypatch, capsys):
    as_user("carol")  # in g1 and g2
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-test")
    cl.main(["connector", "add", "slack", "--name", "shared", "--secret-env", "SLACK_TOKEN",
             "--route", "#clump-rotation=g1,g2"])
    out = capsys.readouterr().out
    assert "#clump-rotation -> g1 (backfilling 14 days)" in out and "#clump-rotation -> g2 as well" in out

    as_user("alice")  # only in g1, but she holds the credentials too and runs the ingest
    cl.main(["connector", "login", "shared", "--secret-env", "SLACK_TOKEN"])
    cl.main(["connector", "route", "shared", "#clump-rotation", "g1"])
    assert "only the creator of connector shared can route it" in capsys.readouterr().out
    cl.main(["ingest", "shared"])
    out = capsys.readouterr().out
    assert "fetching #clump-rotation -> g1, g2" in out and "shared: 3 new messages, 1 new memories" in out

    with cl.connect(db["bob"]) as conn:  # g2 sees the messages and the memory, each stored once
        hits = cl.search_sources(conn, "zero-point", None)
        assert len(hits) == 1 and hits[0]["group_ids"] == ["g1", "g2"]
        mems = cl.search_memories(conn, "Lindegren", None)
        assert len(mems) == 1 and mems[0]["group_ids"] == ["g1", "g2"] and mems[0]["author"].endswith("_alice")
        assert "groups g1, g2" in cl.format_memory(mems[0])
        shared_memory = mems[0]["id"]

    as_user("bob")  # stops the channel feeding g2; it keeps feeding g1
    cl.main(["connector", "unroute", "shared", "#clump-rotation"])
    assert "no longer feeds your groups" in capsys.readouterr().out
    with cl.connect(db["carol"]) as conn:
        assert conn.execute("select group_ids from collaude.connector_routes "
                            "where connector_id = 'shared'").fetchall() == [(["g1"],)]
    with cl.connect(db["bob"]) as conn:  # what was already shared stays shared
        assert cl.search_sources(conn, "zero-point", None)

    slack.add_reply("C0STARS1", f"{NOW - 3 * DAY:.6f}", "U2",
                    "Update from Bob: the zero-point offset depends on G magnitude, use the full recipe.",
                    NOW - 3 * 3600)
    as_user("alice")
    cl.main(["ingest", "shared"])
    assert "shared: 1 new messages, 1 new memories" in capsys.readouterr().out
    with cl.connect(db["bob"]) as conn:
        assert not cl.search_sources(conn, "magnitude", None)
        assert [m["id"] for m in cl.search_memories(conn, "zero point", None)] == [shared_memory]

    # Routing g2 back in shares what was ingested meanwhile, and the memories made only from it.
    as_user("carol")
    cl.main(["connector", "route", "shared", "#clump-rotation", "g2"])
    out = capsys.readouterr().out
    assert "#clump-rotation -> g2 as well (sharing what was ingested in the last 14 days)" in out
    cl.main(["connector", "route", "shared", "#clump-rotation", "g2"])
    assert "#clump-rotation already feeds g2" in capsys.readouterr().out
    with cl.connect(db["bob"]) as conn:
        assert cl.search_sources(conn, "magnitude", None)[0]["group_ids"] == ["g1", "g2"]
        assert {m["id"] for m in cl.search_memories(conn, "magnitude", None)} - {shared_memory}
