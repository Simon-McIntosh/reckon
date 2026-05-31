---
name: reckon-edit
description: >-
  Edit an existing plan or manage the central index — prose edits, per-stage
  files, decision locking, followup creation (§05 template), sprint management,
  and plan archiving. Decides between evergreen edit vs new stage file for phase
  transitions. Trigger verbs: "update / amend / record / add to / revise /
  lock decision / resolve decisions / queue followup / propose sprint / start
  sprint / close sprint / rebalance / archive / retire the plan /
  /reckon-edit <slug>". For new plans use reckon-create; for executing work use
  reckon-ship; for read-only inspection use reckon-status.
allowed-tools: Read Write Edit Bash(*) Grep
---

# reckon-edit — mutations to existing plans and the central index

## When to invoke

| Signal | Intent path |
|---|---|
| "update / amend / revise / add section" | **prose** |
| "lock / record / resolve decision" + decision key | **decision** |
| "walk me through open decisions" | **decision** (interactive) |
| "queue followup / add note / add research / add comment" | **followup / note** |
| "resolve followup / mark followup done" + id | **followup** |
| "propose / start / close / rebalance sprint" | **sprint** |
| "move item X to sprint Y" | **sprint** |
| "archive / retire / ship the plan" | **archive** |
| `/reckon-edit <slug>` | detect from args or ask |

If the plan doesn't exist → hand off to `reckon-create`.
If the user wants to execute the work → hand off to `reckon-ship`.

## The model — the plan HTML is the document AND the store

**You are editing an HTML file.** The `<meta name="plan-*">` tags and
`<section data-reckon="…">` elements are baked into that same file. There is no
separate state file; edit the HTML and the plan's state changes.

**Two write paths:**

1. **Direct HTML edit** (primary for prose, evergreen additions, new sections):
   Use the Write/Edit tool on `docs/<slug>.html`. Announce "editing HTML directly"
   in your reply. This is fine when you are the sole writer.

2. **`edit_plan` MCP call** (preferred for structured state: decisions, followups,
   sprint ops, status changes — especially when another agent or human may be
   editing concurrently): reads current `version`, applies ops atomically,
   rejects on version mismatch (412 → re-read and retry).

**Bypass-with-announcement rule:** any agent that edits plan HTML directly rather
than via `edit_plan` MUST announce "bypassing edit_plan because X" in their reply.
Silent bypasses hide drift.

## Hard rules

1. **HTML is the source of truth.** Never edit `docs/archive/` files — they are frozen.
2. **Decide evergreen-edit vs phase-transition before touching the page.**
3. **Content parity.** Add text in new blocks; do not reflow or paraphrase existing content.
4. **All plan state lives in semantic HTML elements.** Edits to decisions, followups, status,
   and impl go into `<meta name="plan-*">` scalars and `data-reckon` section elements.
5. **Every followup MUST carry a non-empty `<pre class="r-fu-prompt">` block.** A followup
   without a prompt is rejected at write time. See §05 template below.
6. **Locked decisions are a contract.** Use the dissent flow (write a new followup) — never silently re-lock.

```html
<!-- ❌ WRONG — stub body -->
<h2 id="s3">§3 · Implementation</h2>
<p>See state §3.</p>

<!-- ✅ CORRECT — full prose in HTML -->
<h2 id="s3">§3 · Implementation</h2>
<p>New module <code>src/preprocess.py</code> exposes …</p>
```

## Authoring for faithful display (the SPA render contract)

The SPA renders the authored body by **raw-HTML passthrough** — there is **no
markdown processor**. When you edit prose, a comment body, or a followup
body/outcome, author **HTML, never markdown**:

- `<strong>`, `<code>`, `<a>`, `<p>`, `<ul>/<li>` render. Literal `**bold**`,
  leading `- ` or `# ` render **verbatim** as those characters. This covers all
  body fields: `.r-comment-body`, followup `.r-fu-body` + outcomes, question
  bodies, and section prose. The parser preserves your authored inner-HTML
  across `edit_plan` writes — so when you pass `body`/`outcome`/`resolution`
  strings to `edit_plan`, write **HTML markup** in them, not markdown.
- **Exception:** a followup's `<pre class="r-fu-prompt">` stays **plain text**
  (preserved verbatim, wrapped by CSS).
- Images use a project-absolute `src="/<project>/figures/<name>.svg"` —
  relative `src="figures/..."` 404s under the no-trailing-slash plan URL.
- `<head><style>` is **dropped** by the SPA — never style via a head block; use
  `/_shared/*.css` or sparing inline `style=`.

## Update-as-you-go + audit before finishing (MANDATORY)

- **Record outcomes in the same session.** Studies, tests, benchmarks,
  decisions, and negative results go into the plan the moment they land —
  via `edit_plan` resolve/append ops or a prose edit. A stale plan whose state
  disagrees with the code/results is a **defect**, not a backlog item.
- **No build-up.** Resolve followups when their work lands; close/retire sprints
  and plans when finished. Don't accumulate open followups for completed work.
- **Run `reckon audit-doc docs/<slug>.html` before ending your turn** (or
  `python -m reckon.doccheck docs/<slug>.html`). It flags relative/wrong-project
  image `src` and literal `**markdown**` in a rendered body as **ERRORs**
  (non-zero exit), plus `<head><style>` reliance and markdown list/heading
  markers as WARNs. **Clear all ERRORs before relying on the edited doc.**

## State write pattern — `edit_plan`

`edit_plan` is the version-safe write path. Always call `read_plan` first to get
the current `version`; pass it as `expected_version`. On 412 conflict, re-read
and retry.

```python
# 1. Read current state + version
state = read_plan(project="imas-ambix", slug="plasma-decoder-finetune")
# state["version"] → e.g. 5

# 2. Apply ops atomically
edit_plan(
  project="imas-ambix",
  slug="plasma-decoder-finetune",
  ops=[
    {"op": "lock", "key": "base-model", "choice": "t5-large",
     "rationale": "Larger context window handles full shot sequences; t5-base truncates.", "by": "smc"},
    {"op": "append", "target": "followups", "item": {
      "id": "f-pdf-002",
      "status": "open",
      "tier": "sonnet",
      "written_by": "smc",
      "written_at": "2026-05-29",
      "title": "Implement §2 — data prep pipeline",
      "body": "Base model locked. Next: implement the curation pipeline that feeds it.",
      "recommends_skill": "/reckon-ship plasma-decoder-finetune §2",
      "prompt": "Project: imas-ambix\nPlan: plasma-decoder-finetune\nSection: §2\nTier: sonnet\n\nContext\n  base-model locked to t5-large. Data prep is now unblocked.\n\nState to read\n  GET /plan/imas-ambix/plasma-decoder-finetune\n\nLocked decisions to honour\n  base-model → t5-large\n\nOpen decisions to surface (do not resolve)\n  training-batch-size\n\nDone-when\n  1. src/data_prep.py committed + pipeline smoke-test green\n  2. tests still green\n  3. followup written + this followup resolved"
    }}
  ],
  expected_version=5
)
```

**On 412:** `read_plan(project, slug)` → new `version` → retry `edit_plan`.

## Op reference

| Op | Required keys | Notes |
|---|---|---|
| `set` | `path`, `value` | Plan: `status`, `impl`, `roi`, `effort`, `milestone`, `sprint`, `tier`, `owner`, `summary`, `title`, `type`, `archived`, `read`, `depends_on`, `blocks`, `informs`. Index: `active_sprint_id`, `sprints.<id>.<field>` |
| `append` | `target`, `item` | Plan targets: `followups`, `research`, `questions`, `comments`, `decisions` (+ `key`). Index targets: `sprints`, `sprints.<id>.items`, `milestones`, `timeline`, `blockers` |
| `resolve` | `target`, `id`, `by`, `outcome` or `resolution` | `followups` uses `outcome`; `questions` uses `resolution` |
| `lock` | `key`, `choice`, `rationale`, `by` | Locks a decision (`data-choice` + by/when). |
| `move` | `target="sprint_item"`, `slug`, `from`, `to` | Index only. Moves item between sprints. |

## Intent: prose edit

| Edit intent | Action | Output |
|---|---|---|
| Typo, clarification, new subsection | **Evergreen** (direct HTML edit) | Edit `docs/<slug>.html` in place |
| Followup, note, question | **`edit_plan` append op** | Structured write via ops |
| Decision with rationale | **`edit_plan` lock op** | Lock the `.r-dec` element |
| Section fully landed | **Phase transition** | Write `docs/archive/<slug>-<n>-landed.html`; collapse section in evergreen |
| Decision locked irreversibly | **Phase transition** | Write `docs/archive/<slug>-<key>-locked.html` |
| Plan fully implemented | **Phase transition + archive** | See §archive below |

**Evergreen steps:**
1. Read `docs/<slug>.html`.
2. Make the smallest edit; add new `<h3>` blocks rather than modifying existing paragraphs.
3. Update `modified` via `edit_plan` set op (`{"op":"set","path":"modified","value":"YYYY-MM-DD"}`)
   — or the server stamps it automatically on any write.
4. Suggest commit: `docs(<slug>): <short summary>`. Do not commit unless asked.

**Phase transition steps:**
1. Determine suffix: `landed`, `locked`, `final`, or section-specific (`02-landed`).
2. Create `docs/archive/<slug>-<suffix>.html` — frozen snapshot with link back to evergreen.
3. Append a short `<h3>` block in the evergreen pointing at the archive file.
4. Update status via `edit_plan` set op.

## Intent: decisions

Decisions are `<div class="r-dec" data-key="…">` inside `<section data-reckon="decisions">`.
Open: `data-choice=""`. Locked: `data-choice="<answer>"` with `data-by`/`data-when` set.
The locked state is **derived** from `data-choice` being non-empty — no separate flag.
Options are `<button class="r-opt" data-value="…">` — chosen button gets class `chosen`.
Free-form decision: no `<button>` elements; `data-choice` holds the typed answer.

**Lock a decision:**
1. `read_plan(project, slug)` → get `version`.
2. `edit_plan(…, ops=[{"op":"lock","key":"…","choice":"…","rationale":"…","by":"…"}], expected_version=…)`.
3. If irreversible, write archival `docs/archive/<slug>-<key>-locked.html`.

**Interactive walkthrough:**
1. Collect open decisions (where `choice == ""`); present each `r-dec-q` + options.
2. Accumulate choices; call `edit_plan` with one `lock` op per decision.
3. Report how many decisions were locked.

**Dissent / reopen (§07):**
1. Write a followup with `recommends_skill: "/reckon-edit <slug> --reopen <key>"`.
2. Body: locked choice, what evidence changed, what you propose.
3. Never silently re-lock. The locked decision is a contract.
4. On `--reopen`: snapshot into `docs/archive/<slug>-<key>-locked.html`; send an
   `edit_plan` `lock` op clearing `decisions.<key>.choice` to re-open it.

## Intent: followups / notes / research / comments

**Append a followup** via `edit_plan` append op. A followup without `prompt` is rejected.

Minimum followup item shape:
```json
{
  "id":               "f-<base36-or-timestamp>",
  "status":           "open",
  "written_by":       "<agent | smc>",
  "written_at":       "YYYY-MM-DD HH:MM",
  "title":            "<imperative short title>",
  "body":             "<2–3 sentences of context>",
  "recommends_skill": "/reckon-ship <slug> [section] | /reckon-edit <slug> | null",
  "tier":             "haiku | sonnet | opus",
  "prompt":           "<§05 template body — mandatory, non-empty>"
}
```

**Resolve a followup** via `edit_plan` resolve op:
```python
edit_plan(project, slug, ops=[{
  "op": "resolve", "target": "followups",
  "id": "f-pdf-001", "by": "smc", "outcome": "Data prep landed — see commit abc1234"
}], expected_version=…)
```

**Research items:** `edit_plan` append op to `target="research"`. No prompt required.

**Questions:** `edit_plan` append op to `target="questions"`.

**Resolve a question:**
```python
edit_plan(project, slug, ops=[{
  "op": "resolve", "target": "questions",
  "id": "q1", "by": "smc", "resolution": "Chose torch; cv2 absent from venv."
}], expected_version=…)
```

**Comments:** The SPA creates comments when a user selects text — a "¶ Comment" button
appears, clicking it opens a popover. The comment is stored under the nearest `h2[id]`
anchor (`data-section`). When reading a plan, check `<section data-reckon="comments">` for
human feedback. Comments are agent-readable; respond to them in the next followup or
prose edit. Agents may also append comments via `edit_plan` append op to `target="comments"`.

## §05 followup template

Every followup `prompt` MUST be built from this template. It is the dispatch
prompt for the next agent. A non-empty prompt is enforced at write time.

```
Project: <project-name>
Plan:    <slug> (http://localhost:8765/<project>/<slug>.html)
Section: <§ if applicable>
Tier:    <haiku | sonnet | opus>

Context
  2–3 sentences on why this is queued now.

State to read
  GET /plan/<project>/<slug>   (decisions, followups, status, version)

Locked decisions to honour
  <key> → <choice>

Open decisions to surface (do not resolve)
  <key>, <key>

Constraints
  <licence, format, environment>

Done-when
  1. <measurable artefact>
  2. tests still green
  3. followup written + this followup marked resolved
```

## Intent: sprint management (index slug)

Sprint state lives in `docs/state/<project>/index.json`, accessed via
`read_plan(project, "index")` and mutated via `edit_plan(project, "index", ops=…)`.
Does NOT dispatch workers (use `reckon-ship`).

**Read sprints:**
```python
state = read_plan(project="imas-ambix", slug="index")
# state["data"]["sprints"], state["data"]["active_sprint_id"], state["version"]
```

**Create a sprint:**
```python
edit_plan(
  project="imas-ambix", slug="index",
  ops=[{"op": "append", "target": "sprints", "item": {
    "id": "S5", "theme": "Foundation hardening",
    "description": "Schema, tooling, test coverage.",
    "status": "planned", "starts": "2026-05-26", "ends": "2026-06-06", "items": []
  }}],
  expected_version=7
)
```

**Start sprint (set active):**
```python
edit_plan(
  project="imas-ambix", slug="index",
  ops=[
    {"op": "set", "path": "active_sprint_id", "value": "S5"},
    {"op": "set", "path": "sprints.S5.status", "value": "active"}
  ],
  expected_version=8
)
```

**Add item to sprint:**
```python
edit_plan(
  project="imas-ambix", slug="index",
  ops=[{"op": "append", "target": "sprints.S5.items", "item": {
    "slug": "plasma-decoder-finetune", "why_now": "Highest ROI; gates M2",
    "tier": "opus", "done_when": "Fine-tune run green; eval passing"
  }}],
  expected_version=9
)
```

**Move item between sprints:**
```python
edit_plan(
  project="imas-ambix", slug="index",
  ops=[{"op": "move", "target": "sprint_item", "slug": "plasma-decoder-finetune",
        "from": "S4", "to": "S5"}],
  expected_version=9
)
```

**Close sprint:**
```python
edit_plan(
  project="imas-ambix", slug="index",
  ops=[{"op": "set", "path": "sprints.S5.status", "value": "done"}],
  expected_version=10
)
```

**Propose a sprint (manual workflow):**
1. Discover plans via `read_plan(project, slug=None)` (discovery mode).
2. Filter `status in {active, pending}`.
3. Score: `roi × effort_inverse × milestone_priority`. Apply dependency-aware ordering.
4. Partition into N sprints; each item has `why_now`, `tier`, `done_when`.
5. Print proposal; ask to confirm before writing.

## Intent: archive

1. Write `docs/archive/<slug>-final.html` — frozen snapshot.
2. Edit `docs/<slug>.html` — add landed-summary card, update prose to past-tense.
3. `edit_plan(project, slug, ops=[{"op":"set","path":"status","value":"archived"}], expected_version=…)`.

## Cross-references

- `reckon-create` — scaffold a new plan.
- `reckon-ship` — execute the work a plan describes.
- `reckon-status` — read-only inspection.
- `reckon-sync` — register a project mount and seed shared assets.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (semantic HTML, schema contract, endpoints).
- `docs/_shared/plan.schema.json` — published JSON Schema.
