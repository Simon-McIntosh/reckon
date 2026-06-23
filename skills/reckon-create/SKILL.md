---
name: reckon-create
description: >-
  Scaffold a brand-new plan HTML page or non-plan doc in an already-synced repo.
  Creates docs/<slug>.html as self-contained semantic HTML with plan-data in
  meta tags and data-reckon sections. Requires reckon-sync to have been run first.
  Trigger verbs:
  "create a plan / new plan / draft a plan / start a plan / write a dashboard /
  create an explainer / author a doc / /reckon-create <slug>". For editing an
  existing plan use reckon-edit; for executing plan work use reckon-implement.
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
If the user wants to execute work in a plan → hand off to `reckon-implement`.

**Guard:** if `docs/_shared/` does not exist in the target repo, stop immediately
and say: _"Run `/reckon-sync` first — `docs/_shared/` is missing."_

## The model — plans are HTML documents

**The plan HTML is the document AND the store.** You are authoring an HTML file;
the machine-readable tags (`<meta name="plan-*">`) and section elements
(`<section data-reckon="…">`) are baked into that same file. There is no
separate state file, no sidecar JSON, no POST needed to create a plan. Just write
the HTML with the right elements and the server discovers it.

`edit_plan(create=True)` is the version-safe path for initial state ops when
other agents or a human might be concurrently writing to the same project — it
creates the plan atomically with a version counter from zero. For a new plan
where you are the sole author, writing the HTML file directly is fine; announce
"authoring HTML directly" in your reply.

## Document types

| Type | `reckon-type` | Use when |
|---|---|---|
| **plan** | `plan` (default) | Actionable work with lifecycle: status, decisions, followups, sprints |
| **doc / explainer / RCA** | `doc` (stored as `research`) | Standalone prose — RCA, explainer, ticket, review |
| **research** | `research` | Non-actionable input/reference; no decision/followup workflow |

Authoring `reckon-type=doc` is accepted. The schema normalises `doc`→`research`
on read, so `doc` never appears in parsed state — only `plan` or `research`.
Research docs carry `plan-informs` listing the plans they feed, and appear with
a "research" banner in the SPA. Use them for literature surveys, data
characterisations, and reference analyses that inform — but do not describe — work.

**When ambiguous, default to plan.**

## Routing + relationship metadata

- **Live actionable work** → `docs/<slug>.html` with `reckon-type=plan`.
- **Live reference / analysis** that feeds work → `docs/<slug>.html` with `reckon-type=research` (or `doc`) plus `plan-informs`.
- **Completed or historical material** → `docs/archive/`; do not crowd the live inventory with migrated history.

**Relationship fields use slugs only** — never file paths or `.html` / `.md` suffixes:

- `plan-depends-on` = prerequisites this doc cannot close without
- `plan-blocks` = downstream live plans this plan unblocks
- `plan-informs` = research/reference inputs that feed a plan

Set only the relationships that are clear from the source. If you are migrating
an old markdown doc, fix internal links to the final `.html` targets in the
same pass.

## Hard rules

1. **HTML is the source of truth.** Never create a markdown plan file.
2. **Do NOT register mounts or create symlinks.** That is `reckon-sync`'s exclusive job.
3. **Do NOT copy CSS or JS into the project.** If `docs/_shared/` is missing, stop.
4. **Plan data lives as semantic HTML.** `<meta name="plan-*">` scalars in the head and `data-reckon` section elements inside `<main class="plan-doc">`. No sidecar JSON files.
5. **Every plan ships with a followup placeholder** — a `<section data-reckon="followups">` block.
6. **Do not commit automatically.** Report what was created and suggest a commit message.
7. **Write full prose in HTML.** `<p>See state §2 for details</p>` is a hard failure.
8. **Illustrate with graphics (user mandate 2026-06-03).** Plans and research
   docs MUST embed figures/diagrams wherever a graphic improves understanding
   or communication with the lead — geometry, topology, per-machine
   comparisons, pipelines, before/after evidence. Save under
   `docs/figures/<topic>/`, embed with project-absolute
   `src="/<project>/figures/..."`. A geometry/topology claim without a figure
   is under-communicated; multi-pane grids for per-machine content.

   **Figure style — Tufte, high data-ink, legible (user mandate 2026-06-04).**
   A figure that cannot be read is worse than no figure. Every figure (inline
   SVG or saved asset) MUST satisfy:
   - **Legibility is non-negotiable — contrast first.** Body text ≥ 13px,
     system-ui; titles ≥ 14px. Text-on-fill must clear WCAG-AA (~4.5:1). The
     classic failure (banned): **gray/muted text on a dark or saturated fill**
     — it is unreadable. If a box has a dark fill, its text is near-white
     (`#f5f5f7`), never gray. Prefer the opposite: light/white fills with dark
     text and a thin 1px border.
   - **Maximize the data-ink ratio (Tufte).** Default to light/transparent
     backgrounds, thin 1px rules/borders, and dark text. No gradients, drop
     shadows, 3-D, glows, or heavy filled blocks used decoratively. Ink should
     encode information, not decoration.
   - **Colour is semantic and sparing.** One accent hue per logical role
     (e.g. one colour for "arm A", one for "verdict"), carried as a thin
     border / header rule / small swatch — not a full saturated background
     behind text. Monochrome + a single accent beats a rainbow of fills. The
     figure must still read in grayscale / when printed.
   - **Label directly; minimize indirection.** Put labels on the elements they
     describe rather than in a separate legend where a direct label fits. One
     clear caption beneath the figure stating what it shows.
   - **Honest and minimal.** Right-size the canvas (`viewBox` for scaling), no
     duplicated chrome, no unexplained jargon. If a flow has N stages, show N
     boxes and N-1 arrows — nothing else competing for attention.
   - **Self-check before finishing:** "Could the lead read every label at a
     glance, and would it survive a black-and-white printout?" If not, fix it.

## Authoring for faithful display (the SPA render contract)

The reckon SPA renders the authored `<main class="plan-doc">` body by **raw-HTML
passthrough** — the browser parses your HTML directly. There is **no markdown
processor**. Author accordingly:

1. **Body fields are HTML, never markdown.** `<strong>`, `<code>`, `<a>`, `<p>`,
   `<ul>/<li>` render. Literal `**bold**`, leading `- ` or `# ` render
   **verbatim** as those characters. This applies to all section prose and to
   every body field: `data-reckon="comments"` `.r-comment-body`, followup
   `.r-fu-body` and outcomes, and question bodies. (The one exception: a fleet
   followup's `<pre class="r-fu-prompt">` stays **plain text** — preserved
   verbatim and wrapped by CSS.)
2. **Images use a project-absolute `src`:** `src="/<project>/figures/<name>.svg"`
   (e.g. `/imas-ambix/figures/foo.svg`). `docs/figures/` is served at
   `/<project>/figures/`. A relative `src="figures/..."` **404s** under the
   no-trailing-slash plan URL.
3. **`<head><style>` is DROPPED by the SPA** — doc-local CSS never applies. Use
   the shared `/_shared/*.css` links, or sparing inline `style=` on elements.
   Never put plan/doc styling in a `<head><style>` block.
4. **Each comment / followup renders exactly once.** The parser preserves your
   authored inner-HTML in body fields across MCP edits — write real HTML once.

**Run `reckon audit-doc` before you rely on the doc.** After authoring, validate:

```bash
reckon audit-doc docs/<slug>.html              # uses <meta name=docs-project>
reckon audit-doc docs/<slug>.html --project imas-ambix
python -m reckon.doccheck docs/<slug>.html     # equivalent module form
```

If the current repo environment cannot import `reckon`, run the module form
from the reckon checkout (or with `PYTHONPATH` pointing at it) rather than
skipping validation.

It flags relative image `src` (**ERROR**), literal `**markdown**` in a rendered
body (**ERROR**), missing required meta `plan-slug`/`plan-status` (**ERROR**);
wrong-project image `src`, reliance on `<head><style>`, and leading `- `/`# `
markers (WARN); over-long `<pre>` lines, stub prose, empty sections (INFO). It
exits non-zero only on ERRORs. **Clear all ERRORs before ending your turn.**

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

### Step 2.5 — Decide live vs archive and relationship fields

Before writing the file:

1. Decide whether it belongs in **live `docs/`** or **`docs/archive/`**.
2. Decide whether it is a **plan** or **research/doc**.
3. Fill `plan-depends-on` / `plan-blocks` / `plan-informs` with **slugs** for
   the relationships that are already explicit in the source material.

### Step 3 — Write the HTML

Use the Write tool to create `docs/<slug>.html` from the template below.
Then, optionally, use `edit_plan(create=True)` to register initial state
via the version-safe MCP path (required if other agents may be writing
concurrently to the same project's index).

**Authoring the file directly is the primary path.** The MCP tool is an
optional version-safety wrapper, not a gate.

### Step 4 — (Optional) Register initial state via edit_plan

Only needed if other agents are concurrently active on the same project, or if
you need the plan to appear immediately in the server's version-tracked state:

```python
# Minimal create call — expected_version=0 for a new plan, create=True
edit_plan(
  project="imas-ambix",
  slug="plasma-decoder-finetune",
  ops=[
    {"op": "set", "path": "status", "value": "draft"},
    {"op": "set", "path": "roi", "value": "high"},
    {"op": "set", "path": "effort", "value": "L"},
    {"op": "set", "path": "tier", "value": "opus"},
    {"op": "set", "path": "summary", "value": "Fine-tune the plasma decoder on curated IMAS shots"},
    {"op": "set", "path": "milestone", "value": "M2"},
    {"op": "append", "target": "followups", "item": {
      "id": "f-pdf-001",
      "status": "open",
      "tier": "sonnet",
      "written_by": "reckon-create",
      "written_at": "2026-05-29",
      "title": "Implement plasma decoder fine-tune §1 — data prep",
      "body": "Initial authoring complete. Next: run data curation pipeline and validate.",
      "recommends_skill": "/reckon-implement plasma-decoder-finetune §1",
      "prompt": "Project: imas-ambix\nPlan: plasma-decoder-finetune\nSection: §1\nTier: sonnet\n\nContext\n  Plan authored; data prep is the first shippable section.\n\nState to read\n  GET /plan/imas-ambix/plasma-decoder-finetune\n\nLocked decisions to honour\n  (none yet)\n\nDone-when\n  1. Data pipeline script committed\n  2. Tests green\n  3. Followup written + this one resolved"
    }}
  ],
  expected_version=0,
  create=True
)
```

## Templates

### ✅ New plan — minimal valid skeleton

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="docs-project"   content="imas-ambix">
  <meta name="reckon-type"    content="plan">
  <meta name="plan-slug"      content="plasma-decoder-finetune">
  <meta name="plan-title"     content="Plasma Decoder Fine-tune">
  <meta name="plan-summary"   content="Fine-tune the plasma decoder on curated IMAS shots">
  <meta name="plan-status"    content="draft">
  <meta name="plan-roi"       content="high">
  <meta name="plan-effort"    content="L">
  <meta name="plan-tier"      content="opus">
  <meta name="plan-milestone" content="M2">
  <meta name="plan-sprint"    content="S4">
  <meta name="plan-owner"     content="Simon McIntosh">
  <meta name="plan-depends-on" content="tokenizer-eval,data-curation">
  <!-- plan-impl / plan-version / plan-modified: server-owned — do NOT author -->
  <title>Plasma Decoder Fine-tune | imas-ambix</title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <h2 id="s1">§1 — Overview</h2>
    <p>Full authored prose here. Not a stub.</p>

    <h2 id="s2">§2 — Implementation plan</h2>
    <p>Concrete deliverables with done-when criteria…</p>

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
      <!-- first followup will be appended here -->
    </section>
  </main>
</body>
</html>
```

### ✅ Research / doc skeleton

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="docs-project"  content="imas-ambix">
  <meta name="reckon-type"   content="research">
  <meta name="plan-slug"     content="plasma-decoder-survey">
  <meta name="plan-title"    content="Plasma Decoder Architecture Survey">
  <meta name="plan-summary"  content="Survey of plasma decoder architectures for IMAS shots">
  <meta name="plan-informs"  content="plasma-decoder-finetune,tokenizer-eval">
  <title>Plasma Decoder Architecture Survey | imas-ambix</title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <h2 id="s1">§1 — Findings</h2>
    <p>Full prose — no decision or followup sections needed for research docs.</p>
  </main>
</body>
</html>
```

Use `reckon-type=doc` for RCAs, incident reports, and explainers; the schema
normalises it to `research`. Research docs show a "research" banner in the SPA
and link to the plans they inform.

## ❌ Anti-pattern — missing metadata, stub body

```html
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="imas-ambix">
  <title>My Plan</title>
</head>
<body><p>See state §2 for details</p></body>
```

Missing: `reckon-type`, `plan-title`, `plan-roi`, `plan-tier`, `plan-summary`,
`plan-status`, CSS links, full prose body, decisions and followups sections.

## Machine-readable fields — what each tag is for

Only author fields that a view downstream consumes:

| Tag | Who reads it / why |
|---|---|
| `plan-slug` / `docs-project` | Server keys the plan + project; discovery + cross-plan links |
| `plan-title` / `plan-summary` | Dashboard cards, search, the fleet-prompt header |
| `plan-status` | Lifecycle filter; kanban columns; "what's open" queries |
| `plan-roi` / `plan-effort` | Sprint ordering, capacity planning (ROI × effort) |
| `plan-milestone` / `plan-sprint` | Milestone rollup, sprint membership |
| `plan-tier` | Model-tier hint for dispatch (haiku/sonnet/opus) |
| `plan-depends-on` / `plan-blocks` | Dependency DAG → critical-path and fleet-prompt |
| `plan-archived` | `1` hides plan from default inventory (retirements) |
| `plan-read` | `1` marks a research/doc reviewed |
| `plan-impl` / `plan-version` | **Server-owned.** impl computed; version is concurrency counter. Never author. |
| `plan-modified` | Staleness detection; server-stamped on write. Never author. |

## Meta scalars reference

**Author these** (plus `plan-sprint` and `plan-milestone` when applicable):

| Meta tag | Default | Values |
|---|---|---|
| `docs-project` | — | basename of repo root |
| `reckon-type` | `plan` | `plan` / `research` / `doc` (→ normalised to `research`) |
| `plan-slug` | filename stem | kebab-case override |
| `plan-title` | (empty) | Title Case |
| `plan-roi` | `mid` | `high` / `mid` / `low` |
| `plan-effort` | `M` | `S` / `M` / `L` / `XL` |
| `plan-tier` | `sonnet` | `haiku` / `sonnet` / `opus` |
| `plan-summary` | (empty) | One-line synopsis |
| `plan-milestone` | (empty) | e.g. `M2` |
| `plan-sprint` | (empty) | e.g. `S4` |
| `plan-depends-on` | (empty) | Comma-separated slugs |
| `plan-informs` | (empty) | Comma-separated slugs (research type only) |
| `plan-archived` | (empty) | `1` to hide from inventory |
| `plan-read` | (empty) | `1` to mark reviewed |

**Do NOT author** (server-owned): `plan-impl`, `plan-version`, `plan-modified`.

`plan-status` is authored on lifecycle transitions (draft → active → shipped).
Set it to `draft` on initial scaffolding; update it as the plan progresses.

## §05 followup template

Every followup's `<pre class="r-fu-prompt">` MUST be built from this template.
A followup without a non-empty prompt is rejected at write time.

**Do NOT re-list decisions or plan state in the prompt.** The generate-prompt
builder injects the live plan URL and the CURRENT Locked/Open decisions directly
above this brief — copying them into the prompt duplicates the builder and, worse,
goes **stale** the moment a decision is locked (the frozen copy then contradicts
the live list above). The brief carries only what the builder can't: the task
narrative, the specific files to read, non-decision constraints, and done-when.

```
Project: <project-name>
Plan:    <slug> (http://localhost:8765/<project>/<slug>.html)
Section: <§ if applicable>
Tier:    <haiku | sonnet | opus>

Context
  2–3 sentences on why this is queued now and what landed before it.
  (Honour the Locked decisions and surface the Open decisions shown live above
   this brief — do not re-list them.)

State to read  (CODE / FILES / DATA — not the plan itself; the builder already
                injects the live plan-state URL above)
  <specific source files, dirs, datasets, prior artefacts the worker must read>

Scope locks / constraints  (non-decision)
  <pre-registered scope that must not be re-litigated; licence, format,
   environment, compute/SLURM rules, blockers cleared>

Done-when
  1. <measurable artefact: commit, file, test result>
  2. tests still green
  3. followup written into plan + this followup marked resolved
```

## Semantic element shapes

See `~/Code/reckon/PLAN-FORMAT.md` for the full reference. Quick shapes:

**Decision (select-from-options):** `<div class="r-dec" data-key="…" data-choice="">` with `<p class="r-dec-q">`, optional `<p class="r-dec-opts">` containing `<button class="r-opt" data-value="…">`, and `<p class="r-dec-rat">`.

**Decision (free-form, no options):** same but omit `<p class="r-dec-opts">`; `data-choice` holds the typed answer when locked. The locked state is derived from `data-choice` being non-empty — no separate flag.

**Followup:** `<article class="r-fu" data-id="f1" data-status="open" data-tier="sonnet" data-written-by="…" data-written-at="…">` with `<h4 class="r-fu-title">`, `<div class="r-fu-body">`, and `<pre class="r-fu-prompt">` (mandatory). Resolved by setting `data-resolved-at` + `data-resolved-by`; `status=resolved` is derived from `resolved_at`, not stored separately.

**Research item:** `<div class="r-research" data-id="r1" data-type="paper" data-url="https://…">` with `<span class="r-research-title">`.

**Comment:** `<div class="r-comment" data-section="s1" data-id="c1" data-who="…" data-when="…">` with `<div class="r-comment-body">`. Comments are created by text selection in the SPA — a "¶ Comment" button appears on hover; clicking it opens a popover. The comment anchors to the nearest `h2[id]`. Agents reading plans should check the `comments` section for human feedback left this way.

### Step 5 — Confirm

Report:
- Created file: `docs/<slug>.html`
- Live URL: `http://localhost:8765/<project>/<slug>.html`
- Suggested commit: `docs(plans): scaffold <slug>.html (<title>)`

## Cross-references

- `reckon-sync/SKILL.md` — runs first; owns mounts, symlinks, `_shared/`.
- `reckon-edit/SKILL.md` — modify an existing plan.
- `reckon-implement/SKILL.md` — execute the work a plan describes.
- `reckon-status/SKILL.md` — read-only inspection.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (all element shapes, schema contract, endpoints).
- `docs/_shared/plan.schema.json` — published JSON Schema (machine-checkable contract).
