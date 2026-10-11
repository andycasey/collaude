# collaude

Shared, searchable team memory for Claude Code.

When a collaborator's Claude Code session ends (or is about to compact), a background job reads the new part of the transcript, asks Haiku what a colleague would be glad to know, and stores a few short memories in a PostgreSQL database that the rest of the group can search. Each memory records who, when, and where it came from. The next time anyone in the group opens Claude in a project folder, it's told about the latest memories and can search the rest.

```
 your laptop                                   any PostgreSQL server
 ┌──────────────────────────────┐              (Supabase, your own, RDS, ...)
 │ Claude Code session          │              ┌─────────────────────────┐
 │   SessionStart hook ─────────┼── recent ───▶│ collaude.memories       │
 │   PreCompact/SessionEnd hook │              │ collaude.sessions       │
 │     └─ detached worker       │              │ collaude.excerpts       │
 │          condense + redact   │              │ groups / memberships    │
 │          claude -p (haiku)   │── insert ───▶│ row-level security by   │
 │   team-memory skill ─────────┼── search ───▶│   group membership      │
 └──────────────────────────────┘              └─────────────────────────┘
```

## How it meets the requirements

| Requirement | How |
|---|---|
| Closed groups, tied to folders | A committed `.collaude.json` (`{"group": "stars"}`) puts a folder in a group. Each person is a real Postgres login role. Row-level security in the database limits every read and write to that person's groups. |
| Searchable | Postgres full-text search with stemming and weighting (title and tags above body), ranked with a gentle recency decay. |
| Provenance | Each memory stores its author (taken from the database login, so it can't be forged), the time, a verbatim evidence quote from the transcript (checked to be verbatim), the session ID, host, folder, git remote, branch and commit, and the transcript path. Projects can opt in to uploading a condensed transcript excerpt as well. Changes to a memory's status record who made them and when. Nothing is deleted. |
| Automatic | Claude Code hooks. You do nothing. Writing `#offrecord` in any message skips that session. |
| Slack / mailing lists | Connectors pull messages from chosen channels and mail folders, then distil quiet threads into memories that link back to the original messages. Catch-up runs in the background when a session starts on a machine that holds the credentials. See [Connectors](#connectors-slack-and-email). |

## Not tied to Supabase

The memory layer is plain PostgreSQL (version 14 or later). It needs no extensions, no Supabase Auth, and no PostgREST. Clients connect with an ordinary connection string. To move to another server, `pg_dump` the `collaude` schema plus the roles, restore it on the new server, and send people new connection strings.

## Admin: one-time setup

You need [uv](https://docs.astral.sh/uv/) and a Postgres connection string for a role that can create roles and schemas.

```sh
CL=plugins/collaude/bin/collaude.py
export COLLAUDE_ADMIN_DSN='postgresql://...'      # see below for Supabase

$CL admin migrate                                  # creates the collaude schema (re-run after upgrades)
$CL admin add-group stars --name "Stellar astrophysics"
$CL admin add-user alice@flatironinstitute.org --name "Alice" --group stars
#   -> prints a personal connection string; send it to Alice privately
$CL admin add-member alice@flatironinstitute.org another-group
$CL admin remove-member alice@flatironinstitute.org stars
$CL admin deactivate-user alice@flatironinstitute.org   # revokes login, keeps her memories
$CL admin list-users
```

**Locally (for trying it out or building connectors):**

```sh
brew install postgresql@16 && brew services start postgresql@16
createdb collaude
export COLLAUDE_ADMIN_DSN=postgresql://localhost/collaude
```

Only your machine can reach it. When you're ready to share, migrate to a server collaborators can reach and send out new connection strings.

**On Supabase:**
1. Create a project.
2. Under *Connect*, copy the **Session pooler** connection string. It works over IPv4 and looks like `postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`. Use it as `COLLAUDE_ADMIN_DSN`.
3. Run `add-user`. When the admin user name carries a `.<ref>` suffix, `add-user` adds the same suffix to each new user's name, which is how Supabase's pooler routes custom roles.

Don't add the `collaude` schema to Supabase's exposed API schemas. Nothing here uses the API.

**On your own server:** any reachable Postgres 14+ works, for example inside the institute network, on a VPN, or behind Tailscale. Use TLS (`sslmode=require` or stricter) if it is reachable from outside.

## Collaborators: setup

1. Install [uv](https://docs.astral.sh/uv/).
2. Install the plugin in Claude Code:
   ```
   /plugin marketplace add andycasey/collaude
   /plugin install collaude@collaude
   ```
3. Save your connection string (stored in `~/.config/collaude/config.json`, mode 600):
   ```sh
   uv run --script <path-to-plugin>/bin/collaude.py setup --dsn '<the string the admin sent you>'
   uv run --script <path-to-plugin>/bin/collaude.py passwd     # optional: pick your own password
   ```
4. Link a project folder to a group, once per project, and commit the file:
   ```sh
   cd ~/projects/clump-rotation
   uv run --script <path-to-plugin>/bin/collaude.py init stars
   git add .collaude.json && git commit -m "Share Claude memories with the stars group"
   ```

Tip: `ln -s <path-to-plugin>/bin/collaude.py ~/.local/bin/collaude` lets you type `collaude search ...`, because the script runs itself through uv.

## Day to day

Nothing changes. Claude sees recent memories when a session starts, and the `team-memory` skill tells it when to search. You can also use the CLI yourself:

```sh
collaude search "clump rotation detection limit"
collaude search "gaia zero point" --all-groups --kind dead_end
collaude recent -n 20
collaude show 42                 # full provenance
collaude retract 42 --reason "bug in the binning"
collaude add --kind decision --title "..." --body "..."
collaude status
```

`.collaude.json` options:

```json
{
  "group": "stars",
  "share_excerpts": false,
  "capture": true,
  "recent_on_start": 8
}
```

- `share_excerpts`: also upload the condensed, redacted transcript text for each captured session, so collaborators can see the context behind a memory. It's off by default; when off, transcripts stay on the author's machine.
- `capture`: set to `false` to read memories in this folder without contributing any.
- `recent_on_start`: how many recent memories Claude is shown when a session starts.

## Connectors: Slack and email

Connectors are added from the CLI, which asks for what it needs and checks the credentials before saving anything. There are no config files to edit. The non-secret settings (which channel or folder feeds which group) live in the database, so collaborators see them. The token or password stays in `~/.config/collaude/secrets.json` (mode 600) on the machine where you entered it.

```sh
# Slack: the command prints how to create the app; the manifest sets the scopes for you
collaude connector slack-manifest            # paste at api.slack.com/apps -> Create New App -> From a manifest
collaude connector add slack                 # asks for the xoxb- token, then which group each channel feeds
#   then /invite @collaude in each channel you want to share

# Email: a mailbox subscribed to your lists (for Gmail, use an app password)
collaude connector add email --host imap.gmail.com --user lists-bot@flatironinstitute.org
#   asks for the password, then which group each mail folder feeds

# Non-interactive versions, e.g. for scripts
collaude connector add slack --secret-env SLACK_BOT_TOKEN --route '#clump-rotation=stars'
collaude connector add email --host ... --user ... --secret-env MAILPW --route 'lists/stars=stars'

collaude connector list                      # what's connected, routes, last sync, errors
collaude connector sources slack             # channels/folders the credentials can see
collaude connector route slack '#new-channel' stars [--backfill-days 30]
collaude connector unroute slack '#new-channel'
collaude connector login slack               # a second person stores credentials, so either can catch up
collaude connector pause slack / resume slack
collaude connector remove slack              # disconnects; what was ingested stays searchable
collaude ingest                              # run now instead of waiting for the next session start
collaude search --messages "zero point"      # search the raw messages
```

**How ingest runs.** When a Claude session starts on a machine that holds a connector's credentials, a background job checks whether that connector ran in the last 30 minutes (`COLLAUDE_INGEST_STALE_MINUTES`). If it didn't, the job runs. A lease row in the database makes sure only one machine runs a given connector at a time. Each run:

1. Fetches new messages since the stored cursor. On the first run it goes back `--backfill-days`, 14 by default. Slack also re-checks the last 7 days for new replies to older threads.
2. Redacts secrets and stores each message in `collaude.sources`. Re-fetching the same message never creates a duplicate.
3. Waits until a thread has had no new messages for 2 hours (`--quiet-hours`), then sends it to Haiku together with up to 10 earlier, already-processed messages from the same thread as context. Haiku ignores chatter. Every memory must cite the IDs of the messages it came from; memories that don't are dropped.
4. Processes at most 200 threads per run (`--max-threads`) and leaves the rest for the next session, so a long gap never turns into one huge run.

If nobody with credentials opens Claude for a while, nothing runs, and the next run catches up from the cursor. One exception: Slack's free plan hides messages older than 90 days. If you might go that long without opening Claude, add a cron or launchd job that runs `collaude ingest --stale-after 30 --quiet`.

**Who can do what.** Only a connector's creator can point it at a group, and only at a group they belong to. Any member of a group can disconnect a channel from that group. Messages and memories are visible only to the group they were routed to. Memories from a connector are attributed to the person whose machine ingested them ("from slack, ingested by Alice"), and `collaude show` lists the original messages with their real authors and links.

## What leaves your machine

When a session in a linked folder ends or compacts:

1. **To Anthropic, through your own `claude` login:** the new part of the transcript, with secrets redacted, is sent to Haiku for extraction. Text from user and assistant turns is kept, tool inputs are clipped to 400 characters, tool outputs to 600 characters, and thinking, sub-agents and system reminders are dropped.
2. **To the database:** only the resulting memories (usually zero to three), each with its provenance metadata. The condensed transcript goes too only if `share_excerpts` is on.

Redaction covers private keys, AWS, GitHub, Slack and `sk-` API keys, passwords in URLs, and `password=` / `token:`-style assignments. It's a safety net and doesn't catch everything; `#offrecord` is the reliable switch.

Logs are written to `~/.local/state/collaude/collaude.log`. If the database is unreachable, the capture is retried at the next compaction or session end.

## Limitations and roadmap

- **More connectors** (GitHub issues and PRs, arXiv or ADS alerts, Google Doc comments): add a class to `bin/collaude_connectors.py` that implements `check`, `list_sources`, `resolve`, and `fetch`, and register it in `KINDS`.
- **Email login** uses an IMAP password or app password. OAuth (XOAUTH2) isn't implemented, so Google Workspace domains that disable app passwords will need it.
- **Meaning-based search:** add `pgvector` and an embedding column, and blend it with full-text ranking. The schema change is additive.
- **One group per folder.** Memories from other groups are still reachable with `--all-groups`.
- **Extraction quality** depends on the prompt in `prompts/extract.md`. Tune it for your field. Each memory records its `extractor` version, so you can audit old output.
- **Removing someone from a group** stops their future reads, but can't take back what they have already seen.

## Development

```sh
# unit tests only
uv run --with pytest --with 'psycopg[binary]' pytest tests
# full suite against a disposable server (creates and drops its own database and roles)
COLLAUDE_TEST_ADMIN_DSN=postgresql://postgres:pw@localhost:5432/postgres \
  uv run --with pytest --with 'psycopg[binary]' pytest tests
```

To try the plugin without installing it: `claude --plugin-dir plugins/collaude`.
