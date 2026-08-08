# Operational documents

Visual, people-facing documents about how MalAvi runs. **Not published** — only `docs/`
is copied to the public site repo (`publish/push_site.sh`), so anything here stays private
until it is deliberately shared.

They live in the repo rather than in a session scratchpad for one reason: the two earlier
planning artifacts were assembled in a scratchpad that did not survive, so updating them
means rebuilding them from nothing. A source file in git does not have that problem.

| File | What it is | Published at |
|---|---|---|
| `malavi-user-guide.src.html` | Orientation to how MalAvi works after the rebuild. Also published as the site's `how-it-works.html`. | https://claude.ai/code/artifact/4915b73f-7414-409a-802e-551ab24599b8 |
| `curator-instructions.src.html` | What a curator is asked to do and how to do it. Private for now. | https://claude.ai/code/artifact/701dcd9c-2a79-4f12-b106-584a4c83b7f9 |

Two earlier documents, sources lost, listed so nobody redraws them by accident:

- **MalAvi operating map** — how MalAvi ran before this work: https://claude.ai/code/artifact/1099b83e-a37a-478b-bbdf-9d6b68c8df43
- **The submission loop** — the design draft these decisions came out of: https://claude.ai/code/artifact/a66140fe-5f0f-4016-8fd4-0e4a2227fdcc

## Editing a document

**Edit the `.src.html` file.** Ordinary HTML. Two markers matter and should be left alone:
`<!-- INCLUDE:STYLE -->`, where the shared stylesheet is spliced in, and the
`GUIDE:CONTENT-START` / `GUIDE:CONTENT-END` pair around the body. Then:

```bash
.venv/bin/python ops/build_docs.py             # both, --dry-run to check first
.venv/bin/python ops/build_docs.py curator     # just one
```

Styling lives in `_shared_style.html` and is spliced into both, so the documents read as
one series and a change to the type scale cannot land in one and not the other.

That writes `malavi-user-guide.html`, the self-contained page with League Gothic inlined
from `curation/src/malavi_curation/_league_gothic_b64.py`. Publish **that** file, and pass
the existing artifact URL so the link stays stable for anyone who already has it.

Do not edit `malavi-user-guide.html` directly — the next build overwrites it. The split
exists because the font is 17,000 characters of base64 on one line, which makes the file
hostile to edit by hand; and it cannot simply be left out, because the artifact host blocks
external fonts and the page would silently fall back to Impact.

`build_docs.py` refuses to run on an already-built file, and fails if any external
reference has crept in.

## Design

Tokens are lifted from the site's own `docs/assets/css/malavi.css`: violet-biased
neutrals, the violet accent, the three Okabe-Ito genus colors, and League Gothic — the
typeface MalAvi wore for fifteen years. These documents should look like the site, not
like a separate product.
