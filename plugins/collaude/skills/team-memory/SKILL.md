---
name: team-memory
description: Search, read, and curate the group's shared research memory (collaude), which also covers the group's Slack channels and mailing lists. Use when starting work on a question a collaborator may already have looked at, when the user asks what the group or a colleague knows, found, tried, or said on Slack or email, when you need the provenance of a shared memory, when the user asks to remember, correct, or retract something for the team, or when they want to connect Slack or email to it.
---

# Team memory (collaude)

Collaborators' Claude Code sessions, and the group's connected Slack channels and mailing lists, automatically produce short memories (findings, dead ends, decisions, methods, data caveats, gotchas, open questions) in a shared PostgreSQL database. A folder belongs to one or more groups through a `.collaude.json` file at or above it. A memory is shared with one or more groups, and everyone in any of them can read it.

Run the CLI through Bash. The session-start context gives the exact command; it has the form:

```
uv run --quiet --script <plugin>/bin/collaude.py <subcommand>
```

## Search before you investigate

Before digging into a dataset, method, bug, or result, search for what the group already knows:

```
collaude search "<a few distinctive terms>"        # this folder's groups
collaude search "<terms>" --all-groups             # every group the user belongs to
collaude search "<terms>" -g <group> -g <group>    # particular groups
collaude search "<terms>" --kind dead_end          # finding, dead_end, decision, method, data_note, gotcha, question
collaude search "<terms>" --author <name> --since 2026-09-01
collaude recent -n 20
collaude search "<terms>" --messages               # the raw Slack/email messages themselves
```

Search is full-text with stemming, so use the distinctive nouns (object names, dataset names, parameters, tools) rather than a sentence. If nothing comes back, try synonyms before concluding there is nothing.

When you use a memory, say whose it is and when (for example "per Alice's note from 3 Oct, #42"), so the user can follow it up.

Memories distilled from Slack or email say "from slack, ingested by <name>"; the people who actually said it are named in the body and listed by `show`.

## Provenance

```
collaude show <id>              # full text, author, time, evidence quote, session, host, git commit, transcript path
collaude show <id> --excerpt    # also the shared transcript excerpt, if the project opted in
```

For Slack or email memories, `show` lists the original messages with their authors, times, and links.

The transcript itself stays on the author's machine. If `show` lists a transcript path on the current user's own machine, you may read that JSONL file for more context. Otherwise suggest asking the author.

## Writing and curating

Capture is automatic at session end and before compaction, so do not add memories on your own initiative. Do so only when the user asks to remember, share, or record something:

```
collaude add --kind finding --title "<specific title>" --body "<1-4 self-contained sentences>" --tags a,b --confidence high
collaude add ... -g <group> -g <group>            # share with particular groups (default: this folder's)
collaude add ... --supersedes <old id>            # a newer result replaces an older one
collaude supersede <old id> --by <new id> --note "<why>"
collaude retract <id> --reason "<why it was wrong>"
```

Nothing is ever deleted; superseded and retracted memories stay visible with `--include-inactive` and keep their history.

## Privacy

If the user writes `#offrecord` anywhere in a message, nothing from that session is captured. Mention this if the user seems worried about what gets shared. To stop capture in a folder entirely, set `"capture": false` in its `.collaude.json`.

## Connecting Slack or email

Connectors are set up with `collaude connector ...`. Adding one asks for a token or password, so the user should run `collaude connector add slack` or `collaude connector add email --host <imap host> --user <login>` in a separate terminal window, since it prompts for the secret without echoing it; never ask them to paste a secret into the conversation. `collaude connector list` shows what is connected, which groups each channel or folder feeds, and when it last synced. One channel can feed several groups (`collaude connector route <name> '#channel' <group> <group>`). Ingest runs in the background when a session starts on a machine that holds the credentials; `collaude ingest` runs it immediately.
