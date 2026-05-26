---
name: reckon-create
description: >-
  Scaffold a brand-new plan HTML page or non-plan doc in an already-synced repo.
  Creates docs/<slug>.html with an embedded reckon-state island — no sidecar
  JSON created. Requires reckon-sync to have been run first. Trigger verbs:
  "create a plan / new plan / draft a plan / start a plan / write a dashboard /
  create an explainer / author a doc / /reckon-create <slug>". For editing an
  existing plan use reckon-edit; for executing plan work use reckon-ship.
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
4. **Every plan carries a `reckon-state` island** — even if minimal. Future agents expect it.
5. **Every page ships with a NEXT card placeholder** — even empty (via empty `followups: []` in the island).
6. **Do not commit automatically.** Report what was created and suggest a commit message.
7. **Plan body prose lives in HTML — never in the island.** Write the full section content
   directly in `docs/<slug>.html`. The island contains ONLY structured data
   (`status`, `tier`, `decisions`, `followups`, `research`, `questions`, etc.) — never prose
   sections or a `sections[]` array. A stub like `<p>See state §2 for details</p>` is a hard failure.

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

Scaffold `docs/<slug>.html` as a **self-contained plan page**. The page includes:
- `<meta name="docs-project">` — required for server discovery
- `<main class="plan-doc">` — prose body (section headings carry `id="s1"` etc. for comment anchoring)
- `<script type="application/json" id="reckon-state">` — the state island

**Canonical file anatomy:**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="docs-project" content="<project>">
  <meta name="plan-slug"    content="<slug>">
  <title><title> | <project></title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <h1><title></h1>

    <h2 id="s1">§1 — Overview</h2>
    <p>…</p>

    <!-- additional sections as needed -->
  </main>

  <script type="application/json" id="reckon-state">
  <!-- island — see §Island seed schema below -->
  </script>
</body>
</html>
```

Key substitutions:

| Token | Value |
|---|---|
| `<slug>` | kebab-case slug |
| `<project>` | basename of repo root |
| `<title>` | Title Case title |
| `<date>` | `date +%Y-%m-%d` |
| `<tier>` | `sonnet` (default) |

**For a non-plan doc**, use the same anatomy with a plain `<body>` prose structure
instead of `<main class="plan-doc">`. Omit the island unless structured state is needed.

### Step 4 — Seed the state island

Embed the island directly in the HTML file (no sidecar JSON created).
`tier` defaults: `haiku` for research/audit, `sonnet` for routine work, `opus` for multi-file/solver.

```json
{
  "slug": "<slug>",
  "title": "<title>",
  "summary": "",
  "status": "draft",
  "impl": 0.0,
  "roi": "mid",
  "effort": "M",
  "milestone": null,
  "sprint": null,
  "tier": "sonnet",
  "owner": "",
  "modified": "<today>",
  "decisions": {},
  "followups": [],
  "comments": {},
  "questions": [],
  "research": [],
  "notes": []
}
```

`version` is server-managed — never write it in the seed.

`decisions` is a **map** keyed by decision key (e.g. `"scan-strategy": {...}`).
See §Island schema reference for the full field set.

### Step 5 — Register in index.json (if present)

Check if `$DOCS_DIR/state/$PROJECT/index.json` exists. If yes, it holds
**project-level config** (sprints, milestones, `active_sprint_id`). Plans are
auto-discovered from HTML — no plan inventory entry is needed. Skip this step
unless the project explicitly maintains a plans list in index.json and expects
you to add an entry.

### Step 6 — Confirm

Report to the user:

- Created file: `docs/<slug>.html` (relative to repo root)
- Live URL: `http://localhost:8765/<project>/<slug>.html`
- State: embedded island in the HTML (no sidecar JSON)
- Suggested commit: `docs(plans): scaffold <slug>.html (<title>)`

## Island schema reference

```json
{
  "slug":      "<slug>",
  "title":     "Human title",
  "summary":   "one-line synopsis",
  "status":    "draft",           // draft|pending|active|in-progress|blocked|shipped|done|superseded|abandoned
  "impl":      0.0,               // [0,1] progress fraction
  "roi":       "mid",             // high|mid|low
  "effort":    "M",               // S|M|L|XL
  "milestone": null,
  "sprint":    null,
  "tier":      "sonnet",          // haiku|sonnet|opus
  "owner":     "",
  "modified":  "YYYY-MM-DD",     // server-written on each POST
  "depends_on": [],
  "blocks":    [],

  "decisions": {                  // MAP keyed by decision key
    "my-decision": {
      "title":    "The question to answer",
      "context":  "extra context",
      "choices":  ["option-a", "option-b"],
      "choice":   "",             // locked answer; "" = open
      "rationale": "",
      "when":     "",
      "by":       ""
    }
  },

  "followups": [
    {
      "id":               "f-<base36>",
      "status":           "open",
      "title":            "…",
      "body":             "…",
      "recommends_skill": "/reckon-ship <slug>",
      "touches":          ["path"],
      "tier":             "sonnet",
      "est_turn":         "~1h",
      "written_by":       "…",
      "written_at":       "…",
      "prompt":           "§05 copy-paste prompt — MANDATORY",
      "resolved_at":      null,
      "resolved_by":      null,
      "outcome":          null
    }
  ],

  "comments":  {},
  "questions": [],
  "research":  [],
  "notes":     []
}
```

Empty collections may be omitted. The `version` field is server-owned — never write it.

## Cross-references

- `~/.claude/skills/reckon-sync/SKILL.md` — runs first; owns mounts, symlinks, `_shared/` population.
- `~/.claude/skills/reckon-edit/SKILL.md` — for modifying an existing plan.
- `~/.claude/skills/reckon-ship/SKILL.md` — for executing the work a plan describes.
- `~/.claude/skills/reckon-status/SKILL.md` — read-only inspection of plan state.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format reference (island schema, endpoints, what is gone).
