---
name: reckon-edit
description: >-
  Edit an existing plan — prose edits, cumulative evidence updates, decision
  locking, followup creation (§05 template), and plan archiving. Decides between evergreen
  edit, cumulative execution evidence, and frozen snapshots. Trigger verbs: "update / amend /
  record / add to / revise / lock decision / resolve decisions / queue followup /
  archive / retire the plan / invoke reckon-edit with a slug". For new plans use
  reckon-create; for sprint / milestone / roadmap state use reckon-sprint; for
  executing work use reckon-ship; for read-only inspection use reckon-status.
allowed-tools: Read Write Edit Bash(*) Grep mcp__reckon___read_plan mcp__reckon___edit_plan mcp__reckon___roadmap mcp__reckon___audit
---

# reckon-edit — mutations to an existing plan

## Fast path
- Lock a decision → typed `read_plan(..., view="raw")` then `edit_plan` `lock` op.
- Add / resolve a followup → `edit_plan` `append` / `resolve` op (prompt mandatory).
- Fix prose / add a section → raw read then `edit_plan` in text mode with one exact old/new fragment.
- Repair dependencies → classify hard prerequisites vs `informs`/`blocks`, edit, then run `roadmap`.
- Relocate a misplaced plan → follow the cross-project relocation transaction below.
- Sprints / milestones / roadmap → use `reckon-sprint` (the index, not a plan).

Full detail below.

## When to invoke

| Signal | Intent path |
|---|---|
| "update / amend / revise / add section" | **prose** |
| "lock / record / resolve decision" + decision key | **decision** |
| "walk me through open decisions" | **decision** (interactive) |
| "queue followup / add note / add research / add comment" | **followup / note** |
| "resolve followup / mark followup done" + id | **followup** |
| "propose / start / close / rebalance sprint", "move item to sprint Y" | → use **reckon-sprint** |
| "archive / retire / ship the plan" | **archive** |
| `/reckon-edit <slug>` | detect from args or ask |

If the plan doesn't exist → hand off to `reckon-create`.
If the user wants to execute the work → hand off to `reckon-ship`.
If the intent is sprint / milestone / roadmap state → hand off to `reckon-sprint`.

## The model — the plan HTML is the document AND the store

**You are editing an HTML file.** The `<meta name="plan-*">` tags and
`<section data-reckon="…">` elements are baked into that same file. There is no
separate state file; edit the HTML and the plan's state changes.

**Three write paths:**

1. **`edit_plan` with `mode="text"`** (primary for prose, evergreen additions,
   new sections): replace one exact authored HTML fragment with optimistic
   concurrency. It rejects metadata or `data-reckon` mutations.

2. **`edit_plan` MCP call** (required for structured state: decisions, followups,
   sprint ops, status changes — especially when another agent or human may be
   editing concurrently): reads current `version`, applies ops atomically,
   rejects on version mismatch (412 → re-read and retry).

3. **Direct HTML edit** (fallback only when MCP cannot express the change or is
   unavailable): resolve the inventory row's typed path and announce the exact
   bypass reason. Never use it for structured state.

**Bypass-with-announcement rule:** any agent that edits plan HTML directly rather
than via `edit_plan` MUST announce "bypassing edit_plan because X" in their reply.
Silent bypasses hide drift.

## Hard rules

1. **HTML is the source of truth.** Frozen plan/research snapshots in a
   type-local `archive/` are immutable. A plan's cumulative execution record at
   `docs/evidence/archive/<slug>-landed.html` remains appendable until that plan
   closes; keep one coherent record rather than spawning section fragments.
2. **Decide evergreen-edit vs phase-transition before touching the page.**
3. **Content parity.** Add text in new blocks; do not reflow or paraphrase existing content.
4. **All plan state lives in semantic HTML elements.** Edits to decisions, followups, status,
   and impl go into `<meta name="plan-*">` scalars and `data-reckon` section elements.
5. **Every followup MUST carry a non-empty `<pre class="r-fu-prompt">` block.** A followup
   without a prompt is rejected at write time. See §05 template below.
6. **Locked decisions are a contract.** Use the dissent flow (write a new followup) — never silently re-lock.
7. **Relationships have distinct semantics.** `depends_on` is executable and
   blocks closure; research/evidence inputs use `informs`; downstream work uses
   `blocks`. Never put a reference document in `depends_on` merely because it
   was read first.
8. **Repository allocation is part of plan integrity.** Before relocating or
   materially changing scope, read both repositories' instructions and project
   scope policies. Preserve one canonical live owner.

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
- **Figures follow the figure-style rules** in `reckon-create` SKILL.md
  (hard-rule 8): Tufte / high data-ink, legible-first. When you add or revise a
  figure, light fills + dark text + thin borders + one semantic accent; **never
  gray/muted text on a dark or saturated fill** (banned — unreadable). If a
  figure already in the plan violates this, fix it as part of your edit. Apply
  the erase test: remove outer frames, background panels, card grids, repeated
  boxes/pills, duplicate titles, and any legend or decoration whose removal
  loses no information. If a compact table is clearer, use the table and omit
  the graphic.
- `<head><style>` is **dropped** by the SPA — never style via a head block; use
  `/_shared/*.css` or sparing inline `style=`.

## Update-as-you-go + audit before finishing (MANDATORY)

- **Record outcomes in the same session.** Studies, tests, benchmarks,
  decisions, and negative results go into the plan the moment they land —
  via `edit_plan` resolve/append ops or a prose edit. A stale plan whose state
  disagrees with the code/results is a **defect**, not a backlog item.
- **No build-up.** Resolve followups when their work lands; close/retire sprints
  and plans when finished. Don't accumulate open followups for completed work.
- **Keep relationship metadata current.** If a plan now clearly depends on,
  blocks, or is informed by another live doc, update `plan-depends-on`,
  `plan-blocks`, and/or `plan-informs` in the same edit. Use slug lists, not
  file paths.
- **Run `roadmap(project)` after every relationship, sprint, status, or
  relocation edit.** Clear cycles, missing/non-executable prerequisites,
  sprint-order inversions, and plan/sprint membership disagreement before
  finishing.
- **Run `reckon audit-doc docs/plans/<slug>.html` before ending your turn** (or
  `python -m reckon.doccheck docs/plans/<slug>.html`). It flags relative image `src`
  and literal `**markdown**` in a rendered body as **ERRORs** (non-zero exit),
  plus wrong-project image `src`, `<head><style>` reliance, and markdown
  list/heading markers as WARNs. **Clear all ERRORs before relying on the
  edited doc.**
- **Use an environment that can import `reckon`.** If the project venv cannot,
  run the module form from the reckon checkout (or with `PYTHONPATH` pointing
  at it) rather than skipping validation.

## State write pattern — `edit_plan`

`edit_plan` is the version-safe write path. Always call `read_plan` first to get
the current `version`; pass it as `expected_version`. On 412 conflict, re-read
and retry.

```python
# 1. Read current state + version
state = read_plan(
    resource={
        "project": "imas-ambix",
        "type": "plan",
        "id": "plasma-decoder-finetune",
    },
    view="raw",
)
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
      "capability": {"version": "1.0", "class": "general", "requirements": {}},
      "written_by": "smc",
      "written_at": "2026-05-29",
      "title": "Implement §2 — data prep pipeline",
      "body": "Base model locked. Next: implement the curation pipeline that feeds it.",
      "recommends_skill": "/reckon-ship plasma-decoder-finetune §2",
      "prompt": "Project: imas-ambix\nPlan: plasma-decoder-finetune\nSection: §2\nCapability: general\nRequirements: standard verification\n\nContext\n  Data prep is now unblocked. (Honour the locked / surface the open decisions shown live above — do not re-list them.)\n\nState to read  (code/files, not the plan URL — the builder injects it)\n  src/ and the data sources the curation pipeline reads\n\nDone-when\n  1. src/data_prep.py committed + pipeline smoke-test green\n  2. tests still green\n  3. followup written + this followup resolved"
    }}
  ],
  expected_version=5
)
```

**On 412:** repeat the typed `view="raw"` read → new `version` → retry `edit_plan`.

## Prose write pattern — `edit_plan` text mode

Use a lossless raw read for the current version, then provide an exact fragment
that occurs once:

```python
state = read_plan(
    resource={"project": "my-project", "type": "plan", "id": "my-plan"},
    view="raw",
)
edit_plan(
    project="my-project",
    slug="my-plan",
    ops=None,
    old_html="<p>Exact current prose.</p>",
    new_html="<p>Revised prose with <strong>evidence</strong>.</p>",
    expected_version=state["version"],
    doc_type="plan",
    mode="text",
)
```

The tool requires exactly one match, advances `plan-version`, returns the
written path, and refuses changes to metadata, decisions, followups, questions,
research, or comments. On a version conflict, repeat the raw read and rebase the
small fragment against current prose.

### Running inside a git worktree — pass `checkout_path`

The MCP server is a stdio process with **no access to your working
directory**; it resolves every project to the FIXED docs dir in `mounts.json`
— the **main** checkout. If you are a worker running in a worktree
(`.claude/worktrees/agent-XXX`, a separate checkout), a bare `edit_plan`
writes the **main** checkout, not your tree — you can't commit it and the
main checkout is left with an uncommitted duplicate.

Pass `checkout_path=<your repo root>` (the dir containing `docs/`) to both
`read_plan` and `edit_plan` so they read/write **your** worktree. The
read's `version` must come from the same `checkout_path` as the write.
`edit_plan` returns `path` — the absolute file written — commit that from
your tree. Omit `checkout_path` (default) when you are in the main checkout.

```python
root = "/repo/.claude/worktrees/agent-XYZ"
state = read_plan(
    resource={"project": "imas-efit", "type": "project", "id": "project"},
    view="raw",
    checkout_path=root,
)
edit_plan(project="imas-efit", slug="index", ops=[...],
          expected_version=state["version"], checkout_path=root)
# → returns path=<root>/docs/state/imas-efit/index.json — git add + commit it from <root>
```

Better still, let the **orchestrator** (in the main checkout) own
`index`/sprint/followup state writes; worktree workers author plan HTML in
their own tree and `read_plan(..., checkout_path=…)` for state.

## Op reference

| Op | Required keys | Notes |
|---|---|---|
| `set` | `path`, `value` | Plan: `status`, `impl`, `roi`, `effort`, `milestone`, `sprint`, `capability`, `owner`, `summary`, `title`, `type`, `archived`, `read`, `depends_on`, `blocks`, `informs`. Index: `active_sprint_id`, `sprints.<id>.<field>` |
| `append` | `target`, `item` | Plan targets: `followups`, `research`, `questions`, `comments`, `decisions` (+ `key`). Index targets: `sprints`, `sprints.<id>.items`, `milestones`, `timeline`, `blockers` |
| `resolve` | `target`, `id`, `by`, `outcome` or `resolution` | `followups` uses `outcome`; `questions` uses `resolution` |
| `lock` | `key`, `choice`, `rationale`, `by` | Locks a decision (`data-choice` + by/when). |
| `move` | `target="sprint_item"`, `slug`, `from`, `to` | Index only. Moves item between sprints. |

**Dependency blocking is derived.** Keep a plan's persisted `status` at its
underlying workflow state (`pending`, `active`, or `in-progress`) when
`depends_on` is the only reason it cannot run. Reckon exposes
`effective_status=blocked` until every dependency ships, then automatically
returns to the persisted status. Set `status=blocked` only for an explicit
human/external blocker and record that blocker through sprint state.

## Intent: prose edit

| Edit intent | Action | Output |
|---|---|---|
| Typo, clarification, new subsection | **Evergreen** (direct HTML edit) | Edit `docs/plans/<slug>.html` in place |
| Followup, note, question | **`edit_plan` append op** | Structured write via ops |
| Decision with rationale | **`edit_plan` lock op** | Lock the `.r-dec` element |
| Section fully landed | **Execution evidence update** | Append an anchored section to `docs/evidence/archive/<slug>-landed.html`; collapse section in evergreen |
| Decision locked irreversibly | **Phase transition** | Write `docs/plans/archive/<slug>-<key>-locked.html` |
| Plan fully implemented | **Phase transition + archive** | See §archive below |

**Evergreen steps:**
1. Read `docs/plans/<slug>.html` (or the mixed-layout path returned by discovery).
2. Make the smallest edit; add new `<h3>` blocks rather than modifying existing paragraphs.
3. The server stamps `modified` automatically on every successful `edit_plan` write — do not set it manually.
4. Follow the target repository's commit policy; same-session plan commits and
   pushes are mandatory where repository instructions require them.

**Execution-evidence steps:**
1. Reuse or create `docs/evidence/archive/<slug>-landed.html` with
   `reckon-type=evidence` and `plan-evidence-for=<slug>`.
2. Add one anchored section carrying the quantitative result, commits, tests,
   artifacts, and negative findings. Merge closely coupled sections into one
   narrative; do not create a new file for a single paragraph, table, commit,
   or test wave.
3. Collapse the evergreen section and link directly to the cumulative record's
   anchor.
4. Update implementation/status through `edit_plan`.

Use `docs/plans/archive/` only for a frozen plan snapshot or an irreversible
decision-state snapshot. Those resources remain `reckon-type=plan`; execution
outcomes never go there merely because a section landed.

## Intent: decisions

Decisions are `<div class="r-dec" data-key="…">` inside `<section data-reckon="decisions">`.
Open: `data-choice=""`. Locked: `data-choice="<answer>"` with `data-by`/`data-when` set.
The locked state is **derived** from `data-choice` being non-empty — no separate flag.
Options are `<button class="r-opt" data-value="…">` — chosen button gets class `chosen`.
Free-form decision: no `<button>` elements; `data-choice` holds the typed answer.

**Lock a decision:**
1. `read_plan(project, slug)` → get `version`.
2. `edit_plan(…, ops=[{"op":"lock","key":"…","choice":"…","rationale":"…","by":"…"}], expected_version=…)`.
3. If irreversible, write archival `docs/plans/archive/<slug>-<key>-locked.html`.

**Interactive walkthrough:**
1. Collect open decisions (where `choice == ""`); present each `r-dec-q` + options.
2. Accumulate choices; call `edit_plan` with one `lock` op per decision.
3. Report how many decisions were locked.

**Dissent / reopen (§07):**
1. Write a followup with `recommends_skill: "/reckon-edit <slug> --reopen <key>"`.
2. Body: locked choice, what evidence changed, what you propose.
3. Never silently re-lock. The locked decision is a contract.
4. On `--reopen`: snapshot into `docs/plans/archive/<slug>-<key>-locked.html`; send an
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
  "capability":       {"version": "1.0", "class": "routine | general | orchestrator", "requirements": {}},
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

**Surfacing followups to the user (binding).** Followup ids (`f-<...>`) are
internal plan-state keys. When telling the USER about queued or advised
follow-on work, always lead with the PLAN + SECTION/RUNG name (e.g.
"temporal-physics-spine §3, rung P1b"), and close the report with a fenced,
paste-ready invocation so a fresh session starts seamlessly:

````markdown
**Next up** — paste into a fresh session:

```
/reckon-ship <slug> §<N>   (<rung label> — <one-line what it does>)
```
````

Several follow-ons advised for one session → stack them in ONE fence in
execution order. The fence stays short (the skill reads the full §05 prompt
from the plan); the id appears at most once, in passing, after the name.

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

**Do NOT re-list decisions or plan state.** The generate-prompt builder injects
the live plan URL and the current Locked/Open decisions directly above this
brief; copying them here duplicates the builder and goes stale once a decision
is locked. Carry only what the builder can't: narrative, specific files,
non-decision constraints, done-when.

```
Project: <project-name>
Plan:    <slug> (http://localhost:8765/<project>/<slug>.html)
Section: <§ if applicable>
Capability: <routine | general | orchestrator>
Requirements: <reasoning/context/autonomy/verification/risk floors, or none>

Context
  2–3 sentences on why this is queued now.
  (Honour the Locked / surface the Open decisions shown live above — don't re-list.)

State to read  (CODE / FILES / DATA — the builder already injects the plan URL)
  <specific source files, dirs, datasets, prior artefacts to read>

Scope locks / constraints  (non-decision)
  <pre-registered scope not to re-litigate; licence, format, environment, SLURM>

Done-when
  1. <measurable artefact>
  2. tests still green
  3. followup written + this followup marked resolved
```

## Sprint / roadmap state → reckon-sprint

Sprint, milestone, timeline, and blocker state lives in
`docs/state/<project>/index.json` and is managed by the **`reckon-sprint`**
skill — propose / start / close / rebalance sprints, move items between
sprints, and edit milestones / timeline / blockers. It is the project *index*,
not a plan, and never dispatches workers. Use `reckon-sprint` for all of it.

## Intent: cross-project relocation

Use this transaction when the content is valid but the repository owner is
wrong:

1. Read the source plan raw state and complete HTML, both project resources,
   both root/nearest `AGENTS.md` files, and `roadmap` for source and destination.
2. State the ownership boundary and confirm the destination repository owns the
   executable mechanism. Do not infer ownership from naming similarity.
3. Preflight a unique destination slug and canonical typed path. Preserve
   history, decisions, followups, figures, and archive links; rewrite
   `docs-project`, project-absolute asset paths, and qualified cross-project
   relationships.
4. Remove source sprint membership and add destination sprint membership in the
   same session. Update `plan-sprint` to match. Backlog placement must be
   explicit.
5. Rewrite inbound relationships: local refs that cross the new boundary become
   `project:slug`; reference-only edges become `informs`, not hard prerequisites.
6. Remove the source live copy only after the destination validates. Never
   leave two canonical live plans.
7. Run `audit` and `roadmap` in both projects. Require no new error-level
   findings, no cycle, and no sprint-order inversion.
8. Commit and push each affected repository under its own authorization and
   repository policy. If one repository is not authorized, stop before the
   first mutation and return the complete relocation manifest.

## Intent: archive

1. Write `docs/plans/archive/<slug>-final.html` — frozen snapshot.
2. Edit `docs/plans/<slug>.html` — add landed-summary card, update prose to past-tense.
3. `edit_plan(project, slug, ops=[{"op":"set","path":"status","value":"archived"}], expected_version=…)`.

Before freezing, consolidate the plan's execution outcomes into the single
`docs/evidence/archive/<slug>-landed.html` record. Do not preserve a litter of
section-sized evidence pages.

**Migration routing rule:** if you are converting old markdown, move active or
pending work into its live typed root, but route completed/historical material into
`docs/research/archive/`. Keep research inputs live only while they still inform current
plans; otherwise archive them too.

## Cross-references

- `reckon-create` — scaffold a new plan.
- `reckon-sprint` — sprint / milestone / roadmap state (the project index).
- `reckon-ship` — execute the work a plan describes.
- `reckon-status` — read-only inspection.
- `reckon-roadmap` — dependency, sprint-order, blocker, and allocation validation.
- `reckon-sync` — register a project mount and seed shared assets.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (semantic HTML, schema contract, endpoints).
- `docs/_shared/plan.schema.json` — published JSON Schema.
