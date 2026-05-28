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

## Document types

| Type | `reckon-type` | Use when |
|---|---|---|
| **plan** | `plan` (default) | Actionable work with lifecycle: status, decisions, followups, sprints |
| **doc** | `plan` with plain `<body>` | Standalone prose — RCA, explainer, review |
| **research** | `research` | Non-actionable input/reference; no decision/followup workflow |

Research docs carry `plan-informs` listing the plans they feed, and appear with a "research" banner in the SPA. Use them for literature surveys, data characterisations, and reference analyses that inform — but do not describe — work.

**When ambiguous, default to plan.**

## Hard rules

1. **HTML is the source of truth.** Never create a markdown plan file.
2. **Do NOT register mounts or create symlinks.** That is `reckon-sync`'s exclusive job.
3. **Do NOT copy CSS or JS into the project.** If `docs/_shared/` is missing, stop.
4. **Plan data lives as semantic HTML.** `<meta name="plan-*">` scalars in the head and `data-reckon` section elements inside `<main class="plan-doc">`. No sidecar JSON files.
5. **Every plan ships with a followup placeholder** — a `<section data-reckon="followups">` block.
6. **Do not commit automatically.** Report what was created and suggest a commit message.
7. **Write full prose in HTML.** `<p>See state §2 for details</p>` is a hard failure.

## Workflow

### Step 1 — Guard check

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
PROJECT="$(basename "$REPO_ROOT")"
DOCS_DIR="$REPO_ROOT/docs"
STATE_DIR="$DOCS_DIR/state/$PROJECT"
```

- If `$DOCS_DIR/_shared/` does not exist → **stop**: "Run `/reckon-sync` first."
- If `$STATE_DIR/` does not exist → **stop**: "Run `/reckon-sync` first."

### Step 2 — Resolve slug and title

- Slug: kebab-case, lowercase (`plasma-decoder-finetune`, not `Plasma Decoder`)
- Title: Title Case from slug
- If `/reckon-create <slug>` provided, use that slug verbatim.

### Step 3 — Create HTML

## ✅ Good plan head — fully wired

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="docs-project"  content="imas-ambix">
  <meta name="reckon-type"   content="plan">
  <meta name="plan-slug"     content="plasma-decoder-finetune">
  <meta name="plan-roi"      content="high">
  <meta name="plan-effort"   content="L">
  <meta name="plan-tier"     content="opus">
  <meta name="plan-summary"  content="Fine-tune the plasma decoder on curated IMAS shots">
  <meta name="plan-milestone" content="M2">
  <meta name="plan-sprint"   content="S4">
  <meta name="plan-depends-on" content="tokenizer-eval,data-curation">
  <!-- server-written scalars — omit from authored head; server adds on first write -->
  <title>Plasma Decoder Fine-tune | imas-ambix</title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <h2 id="s1">§1 — Overview</h2>
    <p>Full authored prose here. Not a stub.</p>

    <section data-reckon="decisions" id="decisions" class="r-decisions">
      <h2><span class="sec">§</span> Decisions</h2>
      <div class="r-dec" data-key="base-model" data-choice="" data-by="" data-when="">
        <p class="r-dec-q">Which base model to fine-tune from?</p>
        <p class="r-dec-opts">
          <button class="r-opt" data-value="t5-base">t5-base</button>
          <button class="r-opt" data-value="t5-large">t5-large</button>
        </p>
        <p class="r-dec-rat"></p>
      </div>
    </section>

    <section data-reckon="followups" id="followups" class="r-followups">
      <h2><span class="sec">§</span> Followups</h2>
    </section>
  </main>
</body>
</html>
```

## ❌ Anti-pattern — missing metadata, stub body

```html
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="imas-ambix">
  <title>My Plan</title>
</head>
<body><p>See state §2 for details</p></body>
```

Missing: `reckon-type`, `plan-roi`, `plan-tier`, `plan-summary`, `plan-sprint`, `plan-milestone`,
CSS links, prose body, decisions and followups sections.

## Research doc head

```html
<head>
  <meta charset="utf-8">
  <meta name="docs-project"  content="imas-ambix">
  <meta name="reckon-type"   content="research">
  <meta name="plan-slug"     content="plasma-decoder-survey">
  <meta name="plan-summary"  content="Survey of plasma decoder architectures for IMAS shots">
  <meta name="plan-informs"  content="plasma-decoder-finetune,tokenizer-eval">
  <title>Plasma Decoder Architecture Survey | imas-ambix</title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <h2 id="s1">§1 — Findings</h2>
    <p>Full prose — no decision or followup sections needed.</p>
  </main>
</body>
```

Research docs show a "research" banner in the SPA and link to the plans they inform.
Use `plan-depends-on` on the consuming plan to create the reverse link.

### Step 4 — Meta scalars reference

**Author these** (plus `plan-sprint` and `plan-milestone` when applicable):

| Meta tag | Default | Values |
|---|---|---|
| `docs-project` | — | basename of repo root |
| `reckon-type` | `plan` | `plan` / `research` |
| `plan-slug` | filename stem | kebab-case override |
| `plan-roi` | `mid` | `high` / `mid` / `low` |
| `plan-effort` | `M` | `S` / `M` / `L` / `XL` |
| `plan-tier` | `sonnet` | `haiku` / `sonnet` / `opus` |
| `plan-summary` | (empty) | One-line synopsis |
| `plan-milestone` | (empty) | e.g. `M2` |
| `plan-sprint` | (empty) | e.g. `S4` |
| `plan-depends-on` | (empty) | Comma-separated slugs |
| `plan-informs` | (empty) | Comma-separated slugs (research type only) |

**Do NOT author** (server-owned): `plan-status`, `plan-impl`, `plan-version`, `plan-modified`.

### Step 5 — Confirm

Report:
- Created file: `docs/<slug>.html`
- Live URL: `http://localhost:8765/<project>/<slug>.html`
- Suggested commit: `docs(plans): scaffold <slug>.html (<title>)`

## §05 followup template

Every followup's `<pre class="r-fu-prompt">` MUST be built from this template:

```
Project: <project-name>
Plan:    <slug> (http://localhost:8765/<project>/<slug>.html)
Section: <§ if applicable>
Tier:    <haiku | sonnet | opus>

Context
  2–3 sentences on why this is queued now and what landed before it.

State to read
  GET /plan/<project>/<slug>   (parsed state — decisions, followups, status, version)

Locked decisions to honour
  <key> → <choice>

Open decisions to surface (do not resolve)
  <key>, <key>

Constraints
  <licence, format, environment, blockers cleared>

Done-when
  1. <measurable artefact: commit, file, test result>
  2. tests still green
  3. followup written into plan + this followup marked resolved
```

## Semantic element shapes

See `~/Code/reckon/PLAN-FORMAT.md` for the full reference. Quick shapes:

**Decision (select-from-options):** `<div class="r-dec" data-key="…" data-choice="">` with `<p class="r-dec-q">`, optional `<p class="r-dec-opts">` containing `<button class="r-opt" data-value="…">`, and `<p class="r-dec-rat">`.

**Decision (free-form, no options):** same but omit `<p class="r-dec-opts">`; `data-choice` holds the typed answer when locked.

**Followup:** `<article class="r-fu" data-id="f1" data-status="open" data-tier="sonnet" data-written-by="…" data-written-at="…">` with `<h4 class="r-fu-title">`, `<div class="r-fu-body">`, and `<pre class="r-fu-prompt">` (mandatory).

**Research item:** `<div class="r-research" data-id="r1" data-type="paper" data-url="https://…">` with `<span class="r-research-title">`.

**Comment:** `<div class="r-comment" data-section="s1" data-id="c1" data-who="…" data-when="…">` with `<div class="r-comment-body">`. Comments are created by text selection in the SPA — a "¶ Comment" button appears on hover; clicking it opens a popover. The comment anchors to the nearest `h2[id]`. Agents reading plans should check the `comments` section for human feedback left this way.

## Cross-references

- `reckon-sync/SKILL.md` — runs first; owns mounts, symlinks, `_shared/`.
- `reckon-edit/SKILL.md` — modify an existing plan.
- `reckon-ship/SKILL.md` — execute the work a plan describes.
- `reckon-status/SKILL.md` — read-only inspection.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (all element shapes, endpoints).
