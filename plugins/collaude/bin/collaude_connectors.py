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

import email
import email.policy
import email.utils
import html
import imaplib
import json
import re
import time
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


class ImapConnector:
    kind = "imap"
    origin = "email"
    secret_prompt = "Mailbox password (for Gmail, an app password)"

    def __init__(self, settings: dict, secret: str):
        self.settings = settings
        self.password = secret

    def _open(self):
        try:
            box = imaplib.IMAP4_SSL(self.settings["host"], int(self.settings.get("port", 993)), timeout=60)
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
