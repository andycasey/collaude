"""Connectors that pull external messages (Slack, email) into collaude.

Standard library only. Each connector implements:

    check()                        -> dict of non-secret settings learned from the credentials
    list_sources()                 -> [(source_key, label)] the credentials can read
    resolve(text)                  -> (source_key, label) for user input such as '#general'
    fetch(source_key, cursor, since) -> (items, new_cursor)

`cursor` is the connector's own opaque high-water mark (None on the first run,
in which case fetch starts at `since`). Items are dicts with the columns of
collaude.sources: external_id, thread_key, container, author_name,
author_handle, source_time, permalink, subject, content.
"""

from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import hashlib
import html
import imaplib
import json
import re
import secrets
import select
import sys
import threading
import time
import webbrowser
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


class ConnectorError(Exception):
    pass


# --- Slack ---------------------------------------------------------------------

SLACK_SCOPES = ["channels:history", "channels:read", "groups:history", "groups:read", "users:read"]

SLACK_MANIFEST = {
    "display_information": {
        "name": "collaude",
        "description": "Reads the channels it is invited to into the group's shared research memory.",
    },
    "features": {"bot_user": {"display_name": "collaude", "always_online": False}},
    "oauth_config": {"scopes": {"bot": SLACK_SCOPES}},
    "settings": {"org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False},
}

_SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
                  "channel_archive", "channel_unarchive", "bot_add", "bot_remove", "pinned_item", "group_join",
                  "group_leave"}


class SlackConnector:
    kind = "slack"
    origin = "slack"
    secret_prompt = "Slack bot token (starts with xoxb-)"

    def __init__(self, settings: dict, secret: str, api_base: str = "https://slack.com/api/"):
        self.settings = settings
        self.token = secret
        self.api_base = api_base
        self._users: dict[str, str] = {}

    def _api(self, method: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        url = f"{self.api_base}{method}?{urllib.parse.urlencode(params)}"
        for _ in range(5):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(min(int(e.headers.get("Retry-After", "5")), 60))
                    continue
                raise ConnectorError(f"Slack {method}: HTTP {e.code}") from e
            if not data.get("ok"):
                hint = ""
                if data.get("error") == "missing_scope":
                    hint = f" (the app needs scopes {', '.join(SLACK_SCOPES)})"
                elif data.get("error") == "not_in_channel":
                    hint = " (invite the bot to the channel with /invite @collaude)"
                raise ConnectorError(f"Slack {method}: {data.get('error')}{hint}")
            return data
        raise ConnectorError(f"Slack {method}: rate limited")

    def _paged(self, method: str, key: str, max_pages: int = 100, **params):
        cursor = None
        for _ in range(max_pages):
            data = self._api(method, cursor=cursor, limit=200, **params)
            yield from data.get(key, [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return

    def check(self) -> dict:
        d = self._api("auth.test")
        return {"team": d.get("team"), "team_id": d.get("team_id"), "url": d.get("url"), "bot_user": d.get("user")}

    def describe(self) -> str:
        return f"Slack workspace {self.settings.get('team')} as @{self.settings.get('bot_user')}"

    def list_sources(self) -> list[tuple[str, str]]:
        chans = self._paged("conversations.list", "channels", types="public_channel,private_channel",
                            exclude_archived="true")
        return sorted(((c["id"], f"#{c['name']}") for c in chans if c.get("is_member")), key=lambda x: x[1])

    def resolve(self, text: str) -> tuple[str, str]:
        if re.fullmatch(r"[CG][A-Z0-9]{6,}", text):
            info = self._api("conversations.info", channel=text)["channel"]
            return info["id"], f"#{info['name']}"
        name = text.lstrip("#")
        for key, label in self.list_sources():
            if label == f"#{name}":
                return key, label
        raise ConnectorError(f"the bot is not in a channel called #{name}; invite it with /invite @collaude")

    def _user(self, uid: str | None) -> str | None:
        if not uid:
            return None
        if uid not in self._users:
            try:
                u = self._api("users.info", user=uid)["user"]
                self._users[uid] = (u.get("profile") or {}).get("real_name") or u.get("real_name") or u.get("name") or uid
            except ConnectorError:
                self._users[uid] = uid
        return self._users[uid]

    def _text(self, m: dict) -> str:
        text = m.get("text") or ""
        text = re.sub(r"<@([UW][A-Z0-9]+)>", lambda x: "@" + (self._user(x.group(1)) or x.group(1)), text)
        text = re.sub(r"<#[CG][A-Z0-9]+\|([^>]+)>", r"#\1", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
        files = [f.get("name") or f.get("title") for f in m.get("files") or []]
        if files:
            text += "\n[attached: " + ", ".join(f for f in files if f) + "]"
        return html.unescape(text).strip()

    def _permalink(self, channel: str, ts: str, thread_ts: str | None) -> str | None:
        base = self.settings.get("url")
        if not base:
            return None
        link = f"{base.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"
        if thread_ts and thread_ts != ts:
            link += f"?thread_ts={thread_ts}&cid={channel}"
        return link

    def _item(self, channel: str, label: str, m: dict, thread_ts: str) -> dict | None:
        if m.get("subtype") in _SKIP_SUBTYPES:
            return None
        content = self._text(m)
        if not content:
            return None
        return {
            "external_id": f"{channel}:{m['ts']}",
            "thread_key": f"{channel}:{thread_ts}",
            "container": label,
            "author_name": self._user(m.get("user")) or (m.get("bot_profile") or {}).get("name") or m.get("username"),
            "author_handle": m.get("user"),
            "source_time": datetime.fromtimestamp(float(m["ts"]), timezone.utc),
            "permalink": self._permalink(channel, m["ts"], thread_ts),
            "subject": None,
            "content": content,
        }

    def fetch(self, source_key: str, cursor: str | None, since: datetime, label: str | None = None):
        label = label or source_key
        high = float(cursor) if cursor else since.timestamp()
        # Re-scan a lookback window so new replies to older threads are not missed;
        # duplicates are dropped by the (connector, external_id) unique key.
        lookback = float(self.settings.get("lookback_days", 7)) * 86400 if cursor else 0
        items, newest = [], high
        for m in self._paged("conversations.history", "messages", channel=source_key, oldest=f"{high - lookback:.6f}"):
            thread_ts = m.get("thread_ts") or m["ts"]
            if float(m["ts"]) > high and (it := self._item(source_key, label, m, thread_ts)):
                items.append(it)
            newest = max(newest, float(m["ts"]))
            if m.get("reply_count") and float(m.get("latest_reply") or 0) > high:
                for r in self._paged("conversations.replies", "messages", channel=source_key, ts=m["ts"],
                                     oldest=f"{high:.6f}"):
                    if r["ts"] != m["ts"] and float(r["ts"]) > high:
                        if it := self._item(source_key, label, r, m["ts"]):
                            items.append(it)
                        newest = max(newest, float(r["ts"]))
        return items, f"{newest:.6f}"


# --- email over IMAP -------------------------------------------------------------

_QUOTE_HEADER_RE = re.compile(r"^(On .{5,200}wrote:|-----\s*Original Message\s*-----|From: .+)$", re.M)
_LIST_FOOTER_RE = re.compile(
    r"^(--\s*$|You received this message because you are subscribed|To unsubscribe from this group"
    r"|_{10,}\s*$)", re.M)


def _imap_quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def clean_email_body(text: str) -> str:
    """Drop quoted replies, signatures and mailing-list footers."""
    m = _QUOTE_HEADER_RE.search(text)
    if m:
        text = text[:m.start()]
    m = _LIST_FOOTER_RE.search(text)
    if m:
        text = text[:m.start()]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_email(raw: bytes, folder: str, fallback_id: str, web_link: str | None = None) -> dict | None:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return None
    try:
        body = part.get_content()
    except (LookupError, UnicodeDecodeError):
        body = part.get_payload(decode=True).decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        body = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", "", body)))
    body = clean_email_body(body)
    if not body:
        return None
    msg_id = (msg.get("Message-ID") or "").strip() or fallback_id
    refs = (msg.get("References") or "").split()
    thread = refs[0] if refs else (msg.get("In-Reply-To") or "").strip() or msg_id
    name, addr = email.utils.parseaddr(msg.get("From") or "")
    try:
        when = email.utils.parsedate_to_datetime(msg.get("Date"))
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        when = datetime.now(timezone.utc)
    permalink = None
    if web_link == "gmail" and msg_id.startswith("<"):
        permalink = "https://mail.google.com/mail/u/0/#search/rfc822msgid%3A" + urllib.parse.quote(msg_id.strip("<>"))
    return {
        "external_id": msg_id,
        "thread_key": thread,
        "container": folder,
        "author_name": name or addr,
        "author_handle": addr or None,
        "source_time": when,
        "permalink": permalink,
        "subject": str(msg.get("Subject") or "").strip() or None,
        "content": body,
    }


# --- Google OAuth (XOAUTH2 for IMAP) -------------------------------------------------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SCOPE = "https://mail.google.com/"


def oauth_client_from_file(path) -> tuple[str, str]:
    """(client_id, client_secret) from the JSON that Google Cloud Console downloads for an OAuth client."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ConnectorError(f"cannot read OAuth client file {path}: {e}") from e
    client = data.get("installed") or data.get("web") or data
    if not client.get("client_id") or not client.get("client_secret"):
        raise ConnectorError(f"{path} has no client_id and client_secret; download the JSON of a "
                             f"Desktop app OAuth client")
    return client["client_id"], client["client_secret"]


def _token_request(params: dict) -> dict:
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=urllib.parse.urlencode(params).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            body = {}
        detail = f" ({body['error_description']})" if body.get("error_description") else ""
        raise ConnectorError(f"Google token request failed: {body.get('error') or f'HTTP {e.code}'}{detail}") from e
    except OSError as e:
        raise ConnectorError(f"Google token request failed: {e}") from e


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _code_from_redirect(url: str, state: str) -> str:
    """The authorization code in a redirect URL, after checking its state. A bare code is returned as is."""
    url = url.strip()
    if "code=" not in url and "error=" not in url:
        return url
    query = urllib.parse.parse_qs(url.split("?", 1)[-1])
    if "error" in query:
        raise ConnectorError(f"Google sign-in failed: {query['error'][0]}")
    if query.get("state", [None])[0] != state:
        raise ConnectorError("Google sign-in failed: the reply's state doesn't match this sign-in")
    return query["code"][0]


def _wait_for_code(state: str, got: dict, done: threading.Event, timeout: float) -> str:
    """Wait for the browser to hit the loopback server, or for the user to paste the address it reached."""
    deadline = time.time() + timeout
    while not done.is_set():
        if time.time() > deadline:
            raise ConnectorError("Google sign-in timed out")
        if sys.stdin.isatty() and select.select([sys.stdin], [], [], 0.5)[0]:
            line = sys.stdin.readline()
            if line.strip():
                return _code_from_redirect(line, state)
        else:
            done.wait(0.5)
    if "error" in got:
        raise got["error"]
    return got["code"]


def oauth_authorize(client_id: str, client_secret: str, login_hint: str | None = None,
                    timeout: float = 600, open_browser: bool = True) -> str:
    """Run Google's installed-app sign-in (loopback redirect with PKCE) and return a refresh token.

    The sign-in address is printed as well as opened, so this works over SSH: open it in
    any browser, and if that browser can't reach this machine, paste the address it ends
    up on (http://127.0.0.1:...) back at the prompt.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce()
    got: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(self.path.split("?", 1)[-1])
            if query.get("state", [None])[0] != state:  # not this sign-in's redirect: ignore it
                self.send_response(400)
                self.end_headers()
                return
            try:
                got["code"] = _code_from_redirect(self.path, state)
                msg = "collaude is connected. You can close this tab."
            except ConnectorError as e:
                got["error"] = e
                msg = f"collaude: {e}"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())
            done.set()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": GMAIL_SCOPE,
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
              "access_type": "offline", "prompt": "consent"}
    if login_hint:
        params["login_hint"] = login_hint
    url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        print(f"Sign in to Google to let collaude read this mailbox:\n\n  {url}\n")
        if open_browser:
            webbrowser.open(url)
        print("Waiting for the browser. If it's on another machine, paste the address it ends up on "
              "(starting http://127.0.0.1) here and press Enter.", flush=True)
        code = _wait_for_code(state, got, done, timeout)
    finally:
        server.shutdown()
        server.server_close()
    tokens = _token_request({"code": code, "client_id": client_id, "client_secret": client_secret,
                             "redirect_uri": redirect_uri, "grant_type": "authorization_code",
                             "code_verifier": verifier})
    if not tokens.get("refresh_token"):
        raise ConnectorError("Google didn't return a refresh token; remove collaude's access at "
                             "https://myaccount.google.com/permissions and try again")
    return tokens["refresh_token"]


def oauth_secret(client_id: str, client_secret: str, refresh_token: str) -> str:
    """What secrets.json holds for a mailbox that signs in with OAuth."""
    return json.dumps({"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token})


def xoauth2_string(user: str, access_token: str) -> str:
    return f"user={user}\1auth=Bearer {access_token}\1\1"


# --- IMAP ----------------------------------------------------------------------------


class ImapConnector:
    kind = "imap"
    origin = "email"
    secret_prompt = "Mailbox password (for Gmail, an app password)"

    def __init__(self, settings: dict, secret: str):
        self.settings = settings
        self.oauth = json.loads(secret) if settings.get("auth") == "oauth" else None
        self.password = None if self.oauth else secret
        self._access: tuple[str, float] | None = None  # (token, expiry)

    def _access_token(self) -> str:
        if self._access and time.time() < self._access[1] - 60:
            return self._access[0]
        try:
            tokens = _token_request({"client_id": self.oauth["client_id"], "client_secret": self.oauth["client_secret"],
                                     "refresh_token": self.oauth["refresh_token"], "grant_type": "refresh_token"})
        except ConnectorError as e:
            if "invalid_grant" in str(e):
                raise ConnectorError(f"Google no longer accepts the saved sign-in for {self.settings.get('user')} "
                                     f"(revoked or expired); run `collaude connector login <name>` again") from e
            raise
        self._access = (tokens["access_token"], time.time() + float(tokens.get("expires_in", 3600)))
        return self._access[0]

    def _open(self):
        try:
            box = imaplib.IMAP4_SSL(self.settings["host"], int(self.settings.get("port", 993)), timeout=60)
            if self.oauth:
                auth = xoauth2_string(self.settings["user"], self._access_token())
                box.authenticate("XOAUTH2", lambda _: auth.encode())
            else:
                box.login(self.settings["user"], self.password)
        except (OSError, imaplib.IMAP4.error) as e:
            raise ConnectorError(f"IMAP login to {self.settings.get('host')} failed: {e}") from e
        return box

    def check(self) -> dict:
        box = self._open()
        box.logout()
        host = self.settings["host"].lower()
        return {"web_link": "gmail"} if host.endswith("gmail.com") or host.endswith("googlemail.com") else {}

    def describe(self) -> str:
        return f"mailbox {self.settings.get('user')} on {self.settings.get('host')}"

    def list_sources(self) -> list[tuple[str, str]]:
        box = self._open()
        try:
            typ, data = box.list()
        finally:
            box.logout()
        out = []
        for line in data or []:
            line = line.decode() if isinstance(line, bytes) else str(line)
            if "\\Noselect" in line:
                continue
            m = re.search(r'"([^"]*)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if m:
                out.append((m.group(1), m.group(1)))
        return sorted(out)

    def resolve(self, text: str) -> tuple[str, str]:
        names = {k for k, _ in self.list_sources()}
        if text not in names:
            raise ConnectorError(f"no folder {text!r}; available: {', '.join(sorted(names))}")
        return text, text

    def fetch(self, source_key: str, cursor: str | None, since: datetime, label: str | None = None):
        box = self._open()
        try:
            typ, _ = box.select(_imap_quote(source_key), readonly=True)
            if typ != "OK":
                raise ConnectorError(f"cannot open folder {source_key!r}")
            validity = (box.untagged_responses.get("UIDVALIDITY") or [b"0"])[0]
            validity = validity.decode() if isinstance(validity, bytes) else str(validity)
            last = 0
            if cursor and cursor.split(":")[0] == validity:
                last = int(cursor.split(":")[1])
                typ, data = box.uid("search", None, f"UID {last + 1}:*")
            else:
                typ, data = box.uid("search", None, "SINCE", (since - timedelta(days=1)).strftime("%d-%b-%Y"))
            uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last)
            items = []
            for i in range(0, len(uids), 50):
                batch = ",".join(str(u) for u in uids[i:i + 50])
                typ, data = box.uid("fetch", batch, "(UID BODY.PEEK[])")
                for part in data or []:
                    if not isinstance(part, tuple):
                        continue
                    uid = re.search(rb"UID (\d+)", part[0])
                    it = parse_email(part[1], label or source_key,
                                     f"imap:{validity}:{uid.group(1).decode() if uid else '?'}",
                                     self.settings.get("web_link"))
                    if it and it["source_time"] >= since - timedelta(days=1):
                        items.append(it)
            return items, f"{validity}:{max(uids) if uids else last}"
        finally:
            try:
                box.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


KINDS = {"slack": SlackConnector, "imap": ImapConnector}


def make(kind: str, settings: dict, secret: str):
    try:
        return KINDS[kind](settings, secret)
    except KeyError:
        raise ConnectorError(f"unknown connector kind {kind!r}; known: {', '.join(KINDS)}") from None
