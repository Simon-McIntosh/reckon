---
name: reckon-create
description: >-
  Scaffold a brand-new plan HTML page or non-plan doc in an already-synced repo.
  Creates a typed HTML resource under docs/plans/ or docs/research/ with plan-data in
  meta tags and data-reckon sections. Requires reckon-sync to have been run first.
  Trigger verbs:
  "create a plan / new plan / draft a plan / start a plan / write a dashboard /
  create an explainer / author a doc / invoke reckon-create with a slug". For editing an
  existing plan use reckon-edit; for executing plan work use reckon-ship.
allowed-tools: Read Write Edit Bash(*) Grep mcp__reckon___read_plan mcp__reckon___edit_plan mcp__reckon___roadmap
---

# reckon-create — scaffold a new HTML plan or doc

## Fast path
- Resolve repository ownership before selecting a checkout or project key.
- Read discovery's live `tag_inventory` before choosing `plan-tags`; reuse an
  existing canonical identity instead of inventing a near-duplicate.
- New plan → write `docs/plans/<slug>.html` from the skeleton below.
- New non-plan doc (RCA / explainer) → write `docs/research/<slug>.html`.
- Seed the first followup and link actionable work to a sprint in the same session.
- Run `roadmap(project)` after creation; clear allocation, relationship, and sprint wiring faults.
- Missing `docs/_shared/`? → run `/reckon-sync` first.

Full detail below.

## When to invoke

**Work discovered against a COMPLETED plan or a CLOSED sprint belongs here.** This is the
most-missed trigger. A finished plan is a record: appending a followup to it hides the work,
because `roadmap` excludes completed plans from `pending_work` and every open path. If you
are about to write a followup onto a plan at `impl` 1.0, or to reopen a `shipped` plan to
hang one more node on it, the correct move is usually a NEW plan linked to an advancing
sprint. Canonical rule: `reckon-ship` SKILL.md §7a-bis.

Trigger on any of:
- "create a plan for X" / "new plan: Y" / "draft a plan" / "start a plan called Z"
- "write a dashboard / create an explainer / author a doc"
- `/reckon-create <slug>` (slash command alias)
- the user names a plan or doc that does not yet exist in `docs/`

If the plan already exists → hand off to `reckon-edit`.
If the user wants to execute work in a plan → hand off to `reckon-ship`.

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
A third type, **evidence** (`reckon-type=evidence`, `docs/evidence/`), is the
outcome-record flavour: it carries `plan-evidence-for` naming the plan(s) whose
execution it documents. Landed records authored under `docs/research/` for
historical reasons still MUST carry `plan-evidence-for`.

**When ambiguous, default to plan.**

## Routing + relationship metadata

- **Live actionable work** → `docs/plans/<slug>.html` with `reckon-type=plan`.
- **Live reference / analysis** that feeds work → `docs/research/<slug>.html` with `reckon-type=research` (or `doc`) plus `plan-informs`.
- **Completed or historical material** → the owning type's `archive/` directory.

**Relationship fields use slugs only** — never file paths or `.html` / `.md` suffixes:

- `plan-depends-on` = prerequisites this doc cannot close without
- `plan-blocks` = downstream live plans this plan unblocks
- `plan-informs` = research/reference inputs that feed a plan
- `plan-evidence-for` = the plan(s) whose EXECUTION this record documents — the
  plan → generated-evidence back-link. Every landed/outcome/verification record
  MUST carry it; without it the graph shows research→plan (`informs`) but never
  plan→evidence, and the provenance of results silently vanishes.
- `plan-verifies` = optional `slug#section` anchors this evidence verifies

**`informs` vs `evidence-for` — direction of the arrow.** `informs` points
FORWARD (this doc feeds work not yet done); `evidence-for` points BACK (this
doc records work a plan already did). A landed record carrying only
`plan-informs` is mis-wired: its primary relationship is `plan-evidence-for`,
with `informs` added only when the same record also feeds new plans.

Set only the relationships that are clear from the source. If you are migrating
an old markdown doc, fix internal links to the final `.html` targets in the
same pass.

**Repository ownership precedes path selection.** A repository name or nearby
plan is not an ownership rule. Read the proposed repository's root and nearest
`AGENTS.md`, the project resource's `scope` policy when present, and neighboring
mounted projects with overlapping responsibilities. State why the target owns
the work before creating the file. If another repository owns the executable
mechanism, create it there and use a qualified relationship from the consumer.

## Hard rules

1. **HTML is the source of truth.** Never create a markdown plan file.
2. **Do NOT register mounts or create symlinks.** That is `reckon-sync`'s exclusive job.
3. **Do NOT copy CSS or JS into the project.** If `docs/_shared/` is missing, stop.
4. **Plan data lives as semantic HTML.** `<meta name="plan-*">` scalars in the head and `data-reckon` section elements inside `<main class="plan-doc">`. No sidecar JSON files.
5. **Every plan ships with a followup placeholder** — a `<section data-reckon="followups">` block.
6. **Follow the target repository's commit policy.** If it requires same-session
   commit and push, do so with explicit paths after validation; never leave live
   plan state uncommitted.
7. **Write full prose in HTML.** `<p>See state §2 for details</p>` is a hard failure.
8. **Illustrate only when graphics add understanding (user mandate 2026-06-03).**
   Plans and research docs embed figures/diagrams when a graphic improves
   understanding or communication with the lead — geometry, topology, per-machine
   comparisons, pipelines, before/after evidence. Save under
   `docs/figures/<topic>/`, embed with project-absolute
   `src="/<project>/figures/..."`. A geometry/topology claim without a figure
   is under-communicated; multi-pane grids for per-machine content.

   **A graphic is not a quota. There is no minimum image count.** Draw one only
   when it makes a relationship,
   comparison, geometry, or sequence materially clearer than a short table or
   prose. Do not turn a list of verdicts into a wall of cards merely to satisfy
   the illustration mandate. If aligned text or a compact table communicates
   the result with less ink, use that instead.

   **Representation gate — never imagify a table.** If the content is naturally
   rows and columns of labels, values, verdicts, or short explanations, author a
   semantic HTML `<table>` with real selectable text. Do not reproduce aligned
   tabular text as SVG, PNG, canvas, or a diagram: that reduces readability,
   accessibility, search, copy/paste, responsive reflow, and print quality while
   adding no understanding. A visual whose meaningful marks are mostly text in
   columns fails this gate even if it is clean and low-ink. Delete it and keep
   the HTML table. Images are reserved for spatial relationships, geometry,
   topology, continuous plots, trajectories, or sequences a table cannot express
   as clearly.

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
   - **Default to an unframed figure.** No outer container, background panel,
     card grid, per-row box, status pill, badge, or repeated separator unless it
     encodes a real grouping or threshold. Prefer aligned labels, a shared axis,
     whitespace, and at most a few hairline rules. Do not repeat a title already
     supplied by the section heading or caption.
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
   - **Run the erase test.** For every border, fill, icon, legend, annotation,
     heading, and repeated label, ask whether removing it loses information. If
     not, remove it. A clean figure is the smallest set of marks that carries the
     evidence; decorative organization is unnecessary ink.
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
reckon audit-doc docs/plans/<slug>.html
reckon audit-doc docs/research/<slug>.html --project imas-ambix
python -m reckon.doccheck docs/plans/<slug>.html
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
DOCS_DIR="$REPO_ROOT/docs"
```

- Resolve `PROJECT` by matching `$DOCS_DIR` against registered mounts or an
  existing `docs-project` identity. Use the repository basename only for an
  unsynced first-time proposal, and confirm it through `reckon-sync`.
- After resolving it, set `STATE_DIR="$DOCS_DIR/state/$PROJECT"`.
- If `$DOCS_DIR/_shared/` does not exist → **stop**: "Run `/reckon-sync` first."
- If `$STATE_DIR/` does not exist → **stop**: "Run `/reckon-sync` first."

### Step 1.5 — Allocation and graph preflight

1. Call `roadmap(project=PROJECT)` and read `allocation.scope`.
2. Read repository instructions and any explicit architecture boundary docs.
3. Search existing plan and research titles/summaries before creating a new
   resource; edit the existing owner when the work is already represented.
4. Classify each relation:
   - hard prerequisite → `depends_on`;
   - reference/research input → `informs`;
   - downstream plan unlocked here → `blocks`.
5. Choose the destination sprint now. If the work is intentionally backlog,
   record that explicitly instead of silently leaving it unscheduled.

### Step 2 — Resolve slug and title

- Slug: kebab-case, lowercase (`plasma-decoder-finetune`, not `Plasma Decoder`)
- Title: Title Case from slug
- If `/reckon-create <slug>` provided, use that slug verbatim.

### Step 2.5 — Choose tags, placement and relationship fields

Before writing the file:

1. Call `read_plan(project=PROJECT)` in discovery mode and read the response's
   live `tag_inventory`, including each tag's usage count.
2. Choose `plan-tags` from that inventory. Reuse an existing canonical tag
   identity when it names the topic; do not invent a near-duplicate spelling.
   Introduce a new tag only when no existing identity describes the topic.
3. Decide its type and whether it belongs in that type's live or `archive/` directory.
4. Decide whether it is a **plan** or **research/doc**.
5. Fill `plan-depends-on` / `plan-blocks` / `plan-informs` / `plan-evidence-for` with **slugs** for
   the relationships that are already explicit in the source material.

For execution outcomes, prefer one cumulative evidence resource at
`docs/evidence/archive/<plan-slug>-landed.html`, with stable section anchors.
Update it as the plan lands. Create another evidence file only when the result
is a materially independent artifact that is useful on its own; never create a
one-paragraph or one-table file merely because one plan section changed.

### Step 3 — Write the HTML

Use the Write tool to create the canonical typed path from the template below.
Then, optionally, use `edit_plan(create=True)` to register initial state
via the version-safe MCP path (required if other agents may be writing
concurrently to the same project's index).

**Authoring the file directly is the primary path.** The MCP tool is an
optional version-safety wrapper, not a gate.

### Step 4 — Register initial state and sprint membership

Use `edit_plan(create=True)` when concurrent writers exist or when creating the
resource through MCP. Add the plan to its named sprint through `reckon-sprint`
in the same session; plan `sprint` metadata and sprint membership must agree.

```python
# Minimal create call — expected_version=0 for a new plan, create=True
edit_plan(
  project="imas-ambix",
  slug="plasma-decoder-finetune",
  ops=[
    {"op": "set", "path": "status", "value": "draft"},
    {"op": "set", "path": "roi", "value": "high"},
    {"op": "set", "path": "effort_hours", "value": 4.0},
    {"op": "set", "path": "capability", "value": {
      "version": "1.0", "class": "orchestrator",
      "requirements": {"reasoning": "deep", "verification": "strict"}
    }},
    {"op": "set", "path": "summary", "value": "Fine-tune the plasma decoder on curated IMAS shots"},
    {"op": "set", "path": "milestone", "value": "M2"},
    {"op": "append", "target": "followups", "item": {
      "id": "f-pdf-001",
      "status": "open",
      "capability": {"version": "1.0", "class": "general", "requirements": {}},
      "written_by": "reckon-create",
      "written_at": "2026-05-29",
      "title": "Implement plasma decoder fine-tune §1 — data prep",
      "body": "Initial authoring complete. Next: run data curation pipeline and validate.",
      "recommends_skill": "/reckon-ship plasma-decoder-finetune §1",
      "prompt": "/reckon-ship plasma-decoder-finetune §1"
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
  <meta name="plan-effort-hours" content="4.0">
  <meta name="plan-capability-version" content="1.0">
  <meta name="plan-capability-class" content="orchestrator">
  <meta name="plan-capability-reasoning" content="deep">
  <meta name="plan-capability-verification" content="strict">
  <meta name="plan-milestone" content="M2">
  <meta name="plan-sprint"    content="S4">
  <meta name="plan-owner"     content="Simon McIntosh">
  <meta name="plan-tags"      content="plasma-control,imas-data">
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
  <meta name="plan-tags"     content="plasma-control,imas-data">
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

Missing: `reckon-type`, `plan-title`, `plan-roi`, `plan-capability-class`, `plan-summary`,
`plan-status`, CSS links, full prose body, decisions and followups sections.

## Machine-readable fields — what each tag is for

Only author fields that a view downstream consumes:

| Tag | Who reads it / why |
|---|---|
| `plan-slug` / `docs-project` | Server keys the plan + project; discovery + cross-plan links |
| `plan-title` / `plan-summary` | Dashboard cards, search, the fleet-prompt header |
| `plan-status` | Lifecycle filter; kanban columns; "what's open" queries |
| `plan-roi` / `plan-effort-hours` | Sprint ordering and capacity planning in worker-hours |
| `plan-milestone` / `plan-sprint` | Milestone rollup, sprint membership |
| `plan-tags` | Topical grouping across plans, research, evidence and sprints; discovery inventory with usage counts |
| `plan-capability-*` | Versioned capability class and structured dispatch requirements |
| `plan-depends-on` / `plan-blocks` | Dependency DAG → critical-path and fleet-prompt |
| `plan-archived` | `1` hides plan from default inventory (retirements) |
| `plan-read` | `1` marks a research/doc reviewed |
| `plan-impl` | Set by `reckon-ship` (shipped/total) on each landing — **not** server-computed; unset = 0%. |
| `plan-version` | **Server-owned** concurrency counter. Never author. |
| `plan-modified` | Staleness detection; server-stamped on write. Never author. |

## Meta scalars reference

**Author these** (plus `plan-sprint` and `plan-milestone` when applicable):

| Meta tag | Default | Values |
|---|---|---|
| `docs-project` | — | registered project key for the owning repository |
| `reckon-type` | `plan` | `plan` / `research` / `doc` (→ normalised to `research`) |
| `plan-slug` | filename stem | kebab-case override |
| `plan-title` | (empty) | Title Case |
| `plan-roi` | `mid` | `high` / `mid` / `low` |
| `plan-effort-hours` | — | Neutral worker-hours as a positive number in quarter-hour increments (for example `2.25`) |
| `plan-capability-version` | `1.0` | Capability contract version |
| `plan-capability-class` | `general` | `routine` / `general` / `orchestrator` |
| `plan-capability-reasoning` | (empty) | `standard` / `deep` |
| `plan-capability-context` | (empty) | `standard` / `extended` |
| `plan-capability-tool-autonomy` | (empty) | `guided` / `autonomous` |
| `plan-capability-verification` | (empty) | `standard` / `strict` |
| `plan-capability-risk` | (empty) | `low` / `moderate` / `elevated` / `critical` |
| `plan-summary` | (empty) | One-line synopsis |
| `plan-milestone` | (empty) | e.g. `M2` |
| `plan-sprint` | (empty) | e.g. `S4` |
| `plan-tags` | (empty) | Comma-separated canonical identities chosen from discovery's live `tag_inventory` |
| `plan-depends-on` | (empty) | Comma-separated slugs |
| `plan-informs` | (empty) | Comma-separated slugs (research type only) |
| `plan-evidence-for` | (empty) | Comma-separated slugs — the plan(s) this record is execution evidence FOR (mandatory on landed/outcome records) |
| `plan-verifies` | (empty) | Optional `slug#section` anchors this evidence verifies |
| `plan-archived` | (empty) | `1` to hide from inventory |
| `plan-read` | (empty) | `1` to mark reviewed |

**Do NOT author** (server-owned): `plan-version`, `plan-modified`. (`plan-impl`
starts at 0 and is advanced by `reckon-ship` as sections ship — not
server-computed.)

`plan-status` is authored on lifecycle transitions (draft → active → shipped).
Set it to `draft` on initial scaffolding; update it as the plan progresses.

## §05 followup invocation

> **Canonical §05 contract: `reckon-edit` SKILL.md.** Keep this copy in sync.

Every followup's `<pre class="r-fu-prompt">` contains exactly one line:

```
/reckon-ship <slug> [§N]
```

The plan owns all semantic guidance. Do not duplicate context, decisions,
inputs, constraints, done-when criteria, model, effort, or concurrency in the
stored handoff.

## Semantic element shapes

See `~/Code/reckon/PLAN-FORMAT.md` for the full reference. Quick shapes:

**Decision (select-from-options):** `<div class="r-dec" data-key="…" data-choice="">` with `<p class="r-dec-q">`, optional `<p class="r-dec-opts">` containing `<button class="r-opt" data-value="…">`, and `<p class="r-dec-rat">`.

**Decision (free-form, no options):** same but omit `<p class="r-dec-opts">`; `data-choice` holds the typed answer when locked. The locked state is derived from `data-choice` being non-empty — no separate flag.

**Followup:** `<article class="r-fu" data-id="f1" data-status="open" data-capability-version="1.0" data-capability-class="general" data-written-by="…" data-written-at="…">` with optional structured `data-capability-*` requirements, `<h4 class="r-fu-title">`, `<div class="r-fu-body">`, and `<pre class="r-fu-prompt">` (mandatory). Resolved by setting `data-resolved-at` + `data-resolved-by`; `status=resolved` is derived from `resolved_at`, not stored separately.

**Research item:** `<div class="r-research" data-id="r1" data-type="paper" data-url="https://…">` with `<span class="r-research-title">`.

**Comment:** `<div class="r-comment" data-section="s1" data-id="c1" data-who="…" data-when="…">` with `<div class="r-comment-body">`. Comments are created by text selection in the SPA — a "¶ Comment" button appears on hover; clicking it opens a popover. The comment anchors to the nearest `h2[id]`. Agents reading plans should check the `comments` section for human feedback left this way.

### Step 5 — Confirm

Report:
- Created file: `docs/plans/<slug>.html` or `docs/research/<slug>.html`
- Live URL: `http://localhost:8765/<project>/<slug>.html`
- Owning repository and the responsibility boundary that selected it
- Sprint membership, or the explicit backlog reason
- `roadmap` verification: no new error-level wiring findings
- Commit/push state required by the target repository

## Cross-references

- `reckon-sync/SKILL.md` — runs first; owns mounts, symlinks, `_shared/`.
- `reckon-edit/SKILL.md` — modify an existing plan.
- `reckon-ship/SKILL.md` — execute the work a plan describes.
- `reckon-status/SKILL.md` — read-only inspection.
- `reckon-roadmap/SKILL.md` — allocation preflight, graph validation, and ready work.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (all element shapes, schema contract, endpoints).
- `docs/_shared/plan.schema.json` — published JSON Schema (machine-checkable contract).
