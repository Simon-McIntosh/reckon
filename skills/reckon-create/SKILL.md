---
name: reckon-create
description: >-
  Scaffold a brand-new plan HTML page or non-plan doc in an already-synced repo.
  Creates docs/<slug>.html as self-contained semantic HTML with plan-data in
  meta tags and data-reckon sections. Requires reckon-sync to have been run first.
  Trigger verbs:
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
4. **Plan data lives as semantic HTML.** `<meta name="plan-*">` scalars in the head and `data-reckon` section elements inside `<main class="plan-doc">`. No sidecar JSON files.
5. **Every page ships with a followup placeholder** — even empty (a `<section data-reckon="followups">` block or absent if truly empty).
6. **Do not commit automatically.** Report what was created and suggest a commit message.
7. **Plan body prose lives in HTML directly.** Write full section content in `docs/<slug>.html`. A stub like `<p>See state §2 for details</p>` is a hard failure.

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
- `<meta name="plan-*">` scalars — authored metadata (status, roi, effort, tier, etc.)
- `<main class="plan-doc">` — prose body (section headings carry `id="s1"` etc. for comment anchoring) plus `data-reckon` sections for decisions, followups, questions, research, comments

Plan data lives as HTML elements — no sidecar JSON.

**Canonical file anatomy:**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="docs-project" content="<project>">
  <meta name="plan-slug"    content="<slug>">
  <meta name="plan-status"  content="draft">
  <meta name="plan-roi"     content="mid">
  <meta name="plan-effort"  content="M">
  <meta name="plan-tier"    content="sonnet">
  <meta name="plan-summary" content="">
  <title><title> | <project></title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <h1><title></h1>

    <h2 id="s1">§1 — Overview</h2>
    <p>…</p>

    <!-- additional prose sections as needed -->

    <!-- reckon-owned sections: server regenerates these on write -->
    <section data-reckon="decisions" id="decisions" class="r-decisions">
      <h2><span class="sec">§</span> Decisions</h2>
      <!-- example: select-from-options decision -->
      <div class="r-dec" data-key="example-decision" data-choice="" data-by="" data-when="">
        <p class="r-dec-q">Question to answer</p>
        <p class="r-dec-opts">
          <button class="r-opt" data-value="option-a">Option A</button>
          <button class="r-opt" data-value="option-b">Option B</button>
        </p>
        <p class="r-dec-rat"></p>
      </div>
      <!-- example: free-form decision (no option buttons) -->
      <div class="r-dec" data-key="freeform-decision" data-choice="" data-by="" data-when="">
        <p class="r-dec-q">Free-form question</p>
        <p class="r-dec-rat"></p>
      </div>
    </section>

    <section data-reckon="followups" id="followups" class="r-followups">
      <h2><span class="sec">§</span> Followups</h2>
      <!-- followups appended here by MCP or POST -->
    </section>
  </main>
</body>
</html>
```

Key substitutions:

| Token | Value |
|---|---|
| `<slug>` | kebab-case slug |
| `<project>` | basename of repo root |
| `<title>` | Title Case title |
| `<tier>` | `sonnet` (default; `haiku` for research/audit, `opus` for multi-file/solver) |

**For a non-plan doc**, use the same anatomy with plain `<body>` prose instead of
`<main class="plan-doc">`. Omit `data-reckon` sections unless structured state is needed.

### Step 4 — Meta scalars

All structured scalars live in `<meta name="plan-*">` tags in `<head>`. The server
reads and writes these. `version` and `impl` are server-owned — never author them:

| Meta tag | Default | Notes |
|---|---|---|
| `plan-slug` | filename stem | Optional override |
| `plan-status` | `draft` | Server-written |
| `plan-impl` | `0.0` | Server-written |
| `plan-version` | (omit) | Server-owned concurrency counter |
| `plan-roi` | `mid` | `high`/`mid`/`low` |
| `plan-effort` | `M` | `S`/`M`/`L`/`XL` |
| `plan-tier` | `sonnet` | `haiku`/`sonnet`/`opus` |
| `plan-milestone` | (empty) | e.g. `M2` |
| `plan-sprint` | (empty) | e.g. `S4` |
| `plan-summary` | (empty) | One-line synopsis |
| `plan-depends-on` | (empty) | Comma-separated slugs |

### Step 5 — Register in index.json (if present)

Check if `$DOCS_DIR/state/$PROJECT/index.json` exists. If yes, it holds
**project-level config** (sprints, milestones, `active_sprint_id`). Plans are
auto-discovered from HTML files — no plan inventory entry in `index.json` is needed.
Skip this step unless the project explicitly maintains a plans list there.

### Step 6 — Confirm

Report to the user:

- Created file: `docs/<slug>.html` (relative to repo root)
- Live URL: `http://localhost:8765/<project>/<slug>.html`
- State: `<meta name="plan-*">` scalars + `data-reckon` section elements in the HTML
- Suggested commit: `docs(plans): scaffold <slug>.html (<title>)`

## Semantic data reference

Plan data lives as HTML elements, not JSON. The complete element shapes:

**Decisions** (`<section data-reckon="decisions">`):
```html
<div class="r-dec" data-key="my-decision" data-choice="" data-by="" data-when="">
  <p class="r-dec-q">The question to answer</p>
  <p class="r-dec-ctx">optional context</p>          <!-- omit if empty -->
  <p class="r-dec-opts">                              <!-- omit if free-form only -->
    <button class="r-opt" data-value="option-a">Option A</button>
    <button class="r-opt chosen" data-value="option-b">Option B</button>  <!-- chosen = locked -->
  </p>
  <p class="r-dec-rat">rationale text</p>            <!-- empty when open -->
</div>
```
`data-choice=""` = open; `data-choice="option-b"` = locked. A decision with no `<button>` elements is pure free-form — `data-choice` holds the typed answer.

**Followups** (`<section data-reckon="followups">`):
```html
<article class="r-fu" data-id="f1" data-status="open" data-tier="sonnet"
         data-written-by="smc" data-written-at="2026-05-27"
         data-recommends-skill="/reckon-ship slug"
         data-resolved-at="" data-resolved-by="">
  <h4 class="r-fu-title">…</h4>
  <div class="r-fu-body">…</div>
  <pre class="r-fu-prompt">§05 copy-paste prompt — MANDATORY</pre>
  <!-- on resolve: data-resolved-at/-by set + <p class="r-fu-outcome">…</p> -->
</article>
```

**Questions** (`<section data-reckon="questions">`):
```html
<div class="r-q" data-id="q1" data-section="§2" data-status="open"
     data-opened-by="smc" data-opened-at="2026-01-01"
     data-resolved-at="" data-resolved-by="">
  <p class="r-q-body">…</p>
</div>
```

**Research** (`<section data-reckon="research">`):
```html
<div class="r-research" data-id="r1" data-type="paper" data-source="arxiv"
     data-added-by="smc" data-when="2026-01-01" data-url="https://…">
  <span class="r-research-title"><a href="https://…">Title</a></span>
</div>
```

The authoritative reference is `~/Code/reckon/PLAN-FORMAT.md`.

## Cross-references

- `~/.claude/skills/reckon-sync/SKILL.md` — runs first; owns mounts, symlinks, `_shared/` population.
- `~/.claude/skills/reckon-edit/SKILL.md` — for modifying an existing plan.
- `~/.claude/skills/reckon-ship/SKILL.md` — for executing the work a plan describes.
- `~/.claude/skills/reckon-status/SKILL.md` — read-only inspection of plan state.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format reference (semantic HTML elements, endpoints).
