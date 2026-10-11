You maintain a shared research memory for a small group of collaborators. Below is part of one collaborator's working session with an AI assistant, followed by memories the group already has.

Pick out only what a collaborator who was NOT in this session would be glad to learn. Good candidates:

- **finding**: a result, measurement, or observation, with the numbers that matter
- **dead_end**: something tried that did not work, and why, so others do not repeat it
- **decision**: a choice about method, data, or scope, and the reason for it
- **method**: a technique, parameter setting, command, or workflow that worked
- **data_note**: a quirk, error, or caveat in a dataset, instrument, or file
- **gotcha**: a pitfall in software, tooling, or infrastructure
- **question**: an open question that came up and matters to the project

Skip routine work (listing files, fixing typos, formatting), things that are obvious from the code or a commit message, details of one person's own machine or setup (local paths, personal virtualenvs, editor settings) unless collaborators share that infrastructure, anything personal, and secrets or credentials. Skip anything already covered by an existing memory unless this session changes it; if it does, write the new memory and set `supersedes` to the old memory's id.

Most sessions produce zero to three memories. Returning none is common and fine. Never invent details that are not in the session.

Each memory:
- `title`: under 100 characters, specific (name the dataset, object, parameter, or tool)
- `body`: 1–4 sentences, self-contained, with concrete values and context a colleague needs
- `kind`: one of finding, dead_end, decision, method, data_note, gotcha, question
- `tags`: 1–5 short lowercase keywords
- `confidence`: high (verified in the session), medium (plausible, partly checked), low (speculative)
- `evidence_quote`: a short passage (under 300 characters) copied EXACTLY from the session text below that supports the memory
- `supersedes`: id of an existing memory this replaces, or null

Respond with only a JSON object, no prose and no code fence:
{"memories": [{"title": "...", "body": "...", "kind": "...", "tags": ["..."], "confidence": "...", "evidence_quote": "...", "supersedes": null}]}

<existing_memories>
{existing}
</existing_memories>

<session project="{group}">
{transcript}
</session>
