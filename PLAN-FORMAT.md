# reckon canonical plan format (v1)

> Single source of truth for the plan format. The server (`reckon/serve.py`,
> `reckon/_plan_html.py`), the SPA (`docs/ui/*.jsx`), the MCP layer
> (`reckon/_store.py`, `reckon/mcp.py`), and the skills (`skills/reckon-*`)
> all assume exactly this. There is **no legacy fallback** — one format.

## Core principle

**The plan HTML file is the sole store.** All plan data lives inside the
`.html` file. There is **no per-plan `state/<project>/<slug>.json` sidecar**.
Live edits (browser clicks, MCP tools) rewrite the HTML file in place.

A project keeps a single `docs/state/<project>/index.json` for **project-level
config only** — sprints, milestones, `active_sprint_id`, timeline. It is *not*
per-plan state.

## Any HTML file is a plan

Existence is sufficient: any `*.html` under a project's docs dir — except the
infrastructure files/dirs below — is surfaced as a plan. `<meta>` tags and the
state island only *enrich* an entry (status, decisions, sprint membership);
their absence never hides a plan. A bare page surfaces with `status=draft` and
its `<title>` as the title.

- Excluded dirs: `_shared`, `ui`, `state`, `assets`, `images`, `archive`.
- Excluded files: `index.html`, `sprints.html`, `milestones.html`,
  `decisions.html`, `inventory.html`, `blockers.html`, `questions.html`,
  `home.html`, `project.html`.
- Per-stage history (`<plan>-shipped.html`, `*-locked.html`, …) lives under
  `archive/` so it does not clutter the live inventory.

## File anatomy

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="docs-project" content="<project>">   <!-- required -->
  <meta name="plan-slug" content="<slug>">          <!-- optional; default = filename stem -->
  <title>Human title | <project></title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <!-- AUTHORED PROSE: ordinary HTML. Section headings carry ids:
         <h2 id="s1">§1 — …</h2> … so comments can anchor to them.
         Tables, <pre><code>, lists, etc. are all preserved verbatim. -->
  </main>

  <!-- THE STATE ISLAND: the entire mutable/structured data layer. -->
  <script type="application/json" id="reckon-state">
  { … see schema … }
  </script>
</body>
</html>
```

The SPA fetches the page, renders `.plan-doc` (falling back to `<main>`) as the
reading body, and renders decisions / followups / comment threads from the
island. Standalone viewing of the `.html` shows the prose; the SPA is the
interactive surface.

## State-island schema

```json
{
  "slug": "reckon-mcp-gaps",
  "title": "MCP gap-closure — power-user queries",
  "summary": "one-line synopsis",
  "status": "active",            // draft|pending|active|in-progress|blocked|shipped|done|superseded|abandoned
  "impl": 0.6,                    // [0,1] progress fraction
  "roi": "high",                  // high|mid|low
  "effort": "M",                  // S|M|L|XL
  "milestone": "PS",
  "sprint": "S4",                 // or null
  "tier": "sonnet",               // haiku|sonnet|opus
  "owner": "Simon McIntosh",
  "modified": "2026-05-26",       // server-written on each POST
  "version": 3,                   // optimistic-concurrency counter; server-owned
  "depends_on": ["other-slug"],
  "blocks": [],

  "decisions": {                  // MAP keyed by decision key (dotted-patchable)
    "scan-strategy": {
      "title": "How should scanning work?",   // the question
      "context": "extra context shown in the form",
      "choices": ["glob", "index"],            // options offered (may be [])
      "choice": "glob",                         // locked answer; "" = open
      "rationale": "…",
      "when": "2026-05-26 14:00",
      "by": "simon"
    }
  },

  "followups": [
    { "id": "f1", "status": "open|resolved", "title": "…", "body": "…",
      "recommends_skill": "/reckon-ship <slug>", "touches": ["path"],
      "tier": "sonnet", "est_turn": "2-3", "written_by": "…", "written_at": "…",
      "prompt": "§05 copy-paste prompt — MANDATORY",
      "resolved_at": null, "resolved_by": null, "outcome": null }
  ],

  "comments":  { "<sectionId>": [ { "id", "who", "when", "body", "quote?" } ] },
  "questions": [ { "id", "section", "body", "opened_by", "opened_at", "resolved_at?", "resolution?" } ],
  "research":  [ { "id", "type", "title", "source", "added_by", "when", "url?" } ],
  "notes":     [ { "id", "who", "when", "body", "quote?" } ]
}
```

Empty collections may be omitted. `decisions` is a **map** (not an array) so a
dotted patch like `decisions.scan-strategy.choice` updates one field without
dropping the authored `title`/`context`/`choices`.

## Server endpoints

| Method · path | Purpose |
|---|---|
| `GET /<project>` and `/<project>/` | the SPA shell (no redirect) |
| `GET /<project>/<slug>.html` | the plan page (SPA fetches this for prose) |
| `GET /_discover/<project>` | inventory + sprints + milestones, each plan carrying full island state |
| `GET /state/<project>/index.json` | project config + live-merged inventory |
| `GET /plan/<project>/<slug>` | the raw state island (incl. `version`) |
| `POST /plan/<project>/<slug>` | merge a flat **dotted** patch into the island, rewrite the HTML, bump `version`. Requires `If-Match: <version>`; 412 returns `{current_version, current_data}` |

All SPA asset/state URLs are **absolute** (`/_shared`, `/_ui`, `/_discover`,
`/plan`, `/state`) — no trailing-slash dependency, no project redirect. The
`reckon build` static bundle rewrites these to relative paths for GitHub Pages.

## Concurrency

Read `version` (GET `/plan/...` or the island), send it as `If-Match`. The
server bumps `version` on every successful write. A mismatch → 412 with the
current island; rebase the patch on `current_version` and retry once.

## What is gone (no legacy)

- ❌ per-plan `state/<project>/<slug>.json`
- ❌ `decisions_def[]` arrays / object-vs-array dual handling — decisions are a map
- ❌ per-doc vs central-index dual layouts in the loader
- ❌ `_shared/state.js` standalone interactivity, `.topbar`/`.page-head` chrome in plans
- ❌ the `/<project>` → `/<project>/` redirect and the duplicate `/<project>/state/...` route
- ❌ requiring a `plan-status` meta opt-in for discovery
