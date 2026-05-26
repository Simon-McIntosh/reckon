---
name: reckon-create
description: >-
  Scaffold a brand-new plan HTML page or non-plan doc in an already-synced repo.
  Creates docs/<slug>.html from template, seeds docs/state/<project>/<slug>.json
  with initial state, and registers the plan in index.json (central-index layout).
  Requires reckon-sync to have been run first. Trigger verbs: "create a plan /
  new plan / draft a plan / start a plan / write a dashboard / create an explainer /
  author a doc / /reckon-create <slug>". For editing an existing plan use reckon-edit;
  for executing plan work use reckon-ship.
allowed-tools: Read Write Edit Bash(*) Grep
---

# reckon-create — scaffold a new HTML plan or doc

## When to invoke

Trigger on any of:
- "create a plan for X" / "new plan: Y" / "draft a plan" / "start a plan called Z"
- "write a dashboard / create an explainer / author a doc"
- `/reckon-create <slug>` (slash command alias)
- the user names a plan or doc that does not yet exist in `docs/`

If the plan already exists → hand off to `reckon-edit`.
If the user wants to execute work in a plan → hand off to `reckon-ship`.

**Guard:** if `docs/_shared/` does not exist in the target repo, stop immediately
and say: _"Run `/reckon-sync` first — `docs/_shared/` is missing."_

## Hard rules

1. **HTML is the source of truth.** Never create a markdown plan file.
2. **Do NOT register mounts or create symlinks.** That is `reckon-sync`'s exclusive job.
3. **Do NOT copy CSS or JS into the project.** If `docs/_shared/` is missing, stop (rule above).
4. **`state.js` must be wired** in every scaffolded page — future agents expect it.
5. **Every page ships with a NEXT card placeholder** — even empty.
6. **Do not commit automatically.** Report what was created and suggest a commit message.
7. **Plan body prose lives in HTML — never in state JSON.** Write the full section content
   directly in `docs/<slug>.html`. State JSON (`data`) contains ONLY:
   `{status, tier, decisions, notes, followups, research, questions}` — never `sections[]`
   with prose. A stub like `<p>See state §2 for details</p>` is a hard failure.

## Workflow

### Intent detection

| Signal | Intent |
|---|---|
| "plan", sprint/milestone language, decision capture | **plan** — lifecycle page |
| "dashboard", "explainer", "review page", "doc" | **doc** — standalone HTML page |

When ambiguous, default to **plan**.

### Step 1 — Guard check

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
PROJECT="$(basename "$REPO_ROOT")"
DOCS_DIR="$REPO_ROOT/docs"
STATE_DIR="$DOCS_DIR/state/$PROJECT"
```

- If `$DOCS_DIR/_shared/` does not exist → **stop**: "Run `/reckon-sync` first — `docs/_shared/` is missing."
- If `$STATE_DIR/` does not exist → **stop**: "Run `/reckon-sync` first — `docs/state/$PROJECT/` is missing."

### Step 2 — Resolve slug and title

- Slug: kebab-case, lowercase (`tokenizer-eval`, not `Tokenizer Eval`)
- Title: Title Case from slug (`Tokenizer Evaluation`)
- If the user provided `/reckon-create <slug>`, use that slug verbatim.

### Step 3 — Create HTML

**For a plan page**, copy the structure of an existing plan in `docs/` (or use
`~/Code/reckon/docs/index.html` as the canonical reference). The page must include:

```html
<meta name="docs-project" content="<project>">
<meta id="plan-meta" data-slug="<slug>" data-tier="sonnet">
<link rel="stylesheet" href="_shared/foundation.css">
<link rel="stylesheet" href="_shared/dashboard.css">
<!-- React CDN (match versions in ~/Code/reckon/docs/index.html) -->
<script src="ui/state-loader.js"></script>
<script type="text/babel" src="ui/v6-plan.jsx"></script>
```

**For a non-plan doc**, use `~/Code/reckon/docs/index.html` as the structural
template with different JSX entry point or a plain HTML body as appropriate.

Key substitutions:

| Token | Value |
|---|---|
| `<slug>` | kebab-case slug |
| `<project>` | basename of repo root |
| `<title>` | Title Case title |
| `<date>` | `date +%Y-%m-%d` |
| `<tier>` | `sonnet` (default) |

Include a NEXT card placeholder in the rendered content area:

```html
<!-- If not using React JSX, embed directly -->
<div class="next-card" id="next-card">
  <div class="head">
    <span class="lbl">Next</span>
    <span class="ts"><date></span>
  </div>
  <div class="title-line">(no next step queued yet)</div>
  <div class="body">Edit with <code>/reckon-edit <slug></code>.</div>
</div>
```

For React-rendered plans the NEXT card is driven by state JSON — `followups: []` in the seed is sufficient.

### Step 4 — Seed state JSON

Create `$STATE_DIR/<slug>.json` using the schema in [State seed schema](#state-seed-schema) below.
`tier` defaults: `haiku` for research/audit, `sonnet` for routine work, `opus` for multi-file/solver.
Do **not** include `_version` — the server manages that field.

### Step 5 — Register in index.json (central-index only)

Check if `$DOCS_DIR/state/$PROJECT/index.json` exists. If yes, append to its
`plans[]` array:

```json
{
  "slug": "<slug>",
  "path": "docs/<slug>.html",
  "title": "<title>",
  "status": "draft",
  "milestone": "M0",
  "roi": "mid",
  "effort": "M",
  "implementation_fraction": 0.0,
  "tier": "sonnet",
  "summary": "(fill in)",
  "last_modified": "<today>",
  "evidence": []
}
```

If `index.json` does not exist, skip this step — the project uses per-doc layout.

### Step 6 — Confirm

Report to the user:

- Created file: `docs/<slug>.html` (relative to repo root)
- Live URL: `http://localhost:8765/<project>/<slug>.html`
- State file: `docs/state/<project>/<slug>.json`
- index.json updated: yes / no (per-doc layout)
- Suggested commit: `docs(plans): scaffold <slug>.html (<title>)`

## State seed schema

```json
{
  "updated": "<ISO datetime>",
  "project": "<project>",
  "doc": "<slug>",
  "data": {
    "status": "draft",
    "tier": "sonnet",
    "decisions": {},
    "notes": [],
    "followups": [],
    "research": [],
    "questions": []
  }
}
```

`_version` is server-managed — never write it in the seed.

## Cross-references

- `~/.claude/skills/reckon-sync/SKILL.md` — runs first; owns mounts, symlinks, `_shared/` population.
- `~/.claude/skills/reckon-edit/SKILL.md` — for modifying an existing plan.
- `~/.claude/skills/reckon-ship/SKILL.md` — for executing the work a plan describes.
- `~/.claude/skills/reckon-status/SKILL.md` — read-only inspection of plan state.
- `~/Code/reckon/docs/index.html` — canonical HTML structure reference.
