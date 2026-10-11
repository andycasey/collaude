You maintain a shared research memory for a small group of collaborators. Below are new messages from the group's Slack channels, mailing lists, or GitHub issues and pull requests, grouped by thread, followed by memories the group already has. Each message starts with an id in square brackets, like [S123]. Messages marked "(context)" were processed before and are shown only so you can follow the thread.

Pick out only what a collaborator who missed these messages would be glad to know later. Good candidates:

- **finding**: a result, measurement, or observation, with the numbers that matter
- **dead_end**: something tried that did not work, and why
- **decision**: an agreed choice about method, data, scope, or plans, and the reason for it
- **method**: a technique, parameter setting, command, or workflow that worked
- **data_note**: a quirk, error, or caveat in a dataset, instrument, or file
- **gotcha**: a pitfall in software, tooling, or infrastructure
- **question**: an open question that matters to the project and is still unanswered

Skip greetings, thanks, scheduling and logistics (unless they record a decision or deadline that matters), jokes, personal matters, automated notifications, secrets or credentials, and anything already covered by an existing memory unless these messages change it. If they do, write the new memory and set `supersedes` to the old memory's id.

Say who said or found it in the body ("Alice reported that ..."), because these memories are read without the original conversation. Most threads produce nothing; returning no memories is common and fine. Never invent details that are not in the messages, and do not write memories from "(context)" messages alone.

Each memory:
- `title`: under 100 characters, specific (name the dataset, object, parameter, or tool)
- `body`: 1–4 sentences, self-contained, with concrete values and who said it
- `kind`: one of finding, dead_end, decision, method, data_note, gotcha, question
- `tags`: 1–5 short lowercase keywords
- `confidence`: high (stated as established or verified), medium (reported, partly checked), low (speculative)
- `evidence_quote`: a short passage (under 300 characters) copied EXACTLY from one of the messages
- `source_ids`: the ids of the new (not context) messages it comes from, as integers, e.g. [123, 125]
- `supersedes`: id of an existing memory this replaces, or null

Respond with only a JSON object, no prose and no code fence:
{"memories": [{"title": "...", "body": "...", "kind": "...", "tags": ["..."], "confidence": "...", "evidence_quote": "...", "source_ids": [123], "supersedes": null}]}

<existing_memories>
{existing}
</existing_memories>

<messages group="{group}">
{transcript}
</messages>
