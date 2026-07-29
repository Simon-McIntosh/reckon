---
name: reckon-ship
description: >-
  Execute a complete Reckon plan OR an entire sprint. Resolves a plan slug with
  an optional section, `/reckon-ship S1`, and a project-qualified sprint id;
  reads dependencies plus prior research/evidence, builds execution waves,
  delegates through isolated worktrees with a model-neutral one-below policy,
  integrates and verifies worker commits, records outcomes continuously, and
  cleans up worktrees. Trigger verbs: "implement / execute / ship / land /
  deliver the sprint / run the sprint / /reckon-ship". For editing plan text
  use reckon-edit; for defining or rebalancing sprint state use reckon-sprint.
allowed-tools: Read Write Edit Bash(*) Grep Agent mcp__reckon___read_plan mcp__reckon___edit_plan
---

# reckon-ship — execute a complete plan or sprint and record outcomes

## Critical behaviour: resolve the target, then finish its executable scope

There are two execution modes:

- **Plan mode:** `/reckon-ship <slug>` implements the entire plan;
  `/reckon-ship <slug> §N` implements only the named section.
- **Sprint mode:** `/reckon-ship S1` executes the current project's sprint;
  `/reckon-ship <project>:S1` selects a project explicitly. It reads every
  sprint plan, transitive dependencies, linked research, and prior evidence,
  then executes ready dependency waves. Use `plan:<slug>` or `sprint:<id>`
  only to disambiguate unusual identifiers.

In plan mode without a section, you MUST:
1. Read the complete plan HTML and classify every section
2. Identify ALL implementable sections (not deferred/blocked/done)
3. Implement them all — sequentially for dependent sections, in parallel fleet for independent ones
4. Record outcomes after each section lands, then continue
5. Stop only when all implementable sections are done OR you hit a hard prerequisite blocker

In sprint mode, read `references/sprint-orchestration.md` completely before
dispatch. The sprint invocation authorises the listed plans and their actionable
same-project prerequisites; it does not broaden authority to unrelated projects,
external systems, destructive actions, or new outward-facing effects.

**Do NOT stop at routine checkpoints.** Keep going and update state as work
lands. Valid early stops are:
- A prerequisite plan is unshipped (hard stop — see §Prerequisite blocking)
- A NEW decision surfaced that is not already locked in the plan, is material to the work, and cannot be deduced from the plan/code/sensible defaults (an already-locked decision is NOT a reason to stop — honour it and proceed)
- The next section's scope would require writing files outside your allocated write scope
- Applicable safety policy or user authority requires confirmation
- A worker commit cannot be integrated safely without overwriting unrelated work

### These are not valid reasons to stop

Continue through ordinary complexity, validation, and recoverable integration:

| Rationalization | Reality |
|---|---|
| "This change is high-blast-radius / touches core code" | Allocate it to an appropriately capable worker, test it, and validate integration. |
| "Better to confirm the approach before executing" | The plan IS the approved approach. Locked decisions ARE the confirmation. Asking again is re-litigating settled decisions. |
| "This is a lot of work / the session is long / I've done enough" | Length and effort are not blockers. Continue until every implementable item is done or you hit a valid stop. |
| "It needs full-suite validation first" | Then run the full suite — that's part of doing the work, not a reason to hand back. |
| "I'll present options A/B and let the user choose" | If the plan already determines the path, there is no choice to present. Pick the plan's path and execute. Offering A/B on already-decided work is a checkpoint in disguise. |

Plans do not override global safety or expand user authority. A locked decision
settles implementation choices only inside the already-authorised scope.

## Fast path

```text
resolve target
├─ plan → read full plan → classify sections → execute dependency order
└─ sprint → read index + all plans/research/evidence → build DAG → execute waves
     ↓
select worker capability (one-below by default) + applicable skill
→ create detached worktree per delegated task
→ dispatch independent ready nodes in parallel
→ verify commits/tests → orchestrator merges and resolves conflicts
→ record plan/evidence/sprint outcomes
→ prove commits reachable → remove worktrees → close sprint when complete
```

Full detail below.

## When to invoke

- "implement / execute / ship X" / "land items from X" / "do the work in X plan"
- `/reckon-ship <slug>` — implements the WHOLE PLAN
- `/reckon-ship <slug> [§N]` — implements only the named section
- `/reckon-ship S1` — executes sprint `S1` in the current project
- `/reckon-ship <project>:S1` — executes a sprint in an explicit project
- Reading a §05 followup whose `recommends_skill` is `/reckon-ship`

**Dual-role:** invoked by human or orchestrator AND generates §05 dispatch prompts for workers.

If the user wants to *write* the plan → `reckon-edit`. Plan doesn't exist → `reckon-create` first.

## The model — the plan HTML is the document AND the store

**The plan HTML is the source of truth.** Read it first — ALL of it — implement what it
describes, then write back outcomes. The HTML documents the work; the
`data-reckon` sections carry structured state (decisions, followups). Do not
implement items marked "deferred", "post-v1", or behind an unmet trigger.

**Write path:** use `edit_plan` to record outcomes atomically:
1. `read_plan(project, slug)` → get `version`.
2. `edit_plan(…, ops=[set status/impl + resolve driving followup + append next followup], expected_version=…)`.
3. On 412 conflict: re-read + retry.

## Hard rules

1. **Read the FULL selected scope before ANY implementation.** In plan mode, read the complete plan. In sprint mode, read the sprint index, every member plan, transitive dependency, linked research document, and prior evidence record before dispatch.
2. **Full plan by default.** `/reckon-ship <slug>` without a section flag means ALL implementable sections. Never implement one section and stop unless there is a hard blocker.
3. **Whole sprint by default.** `/reckon-ship S1` means every executable item in the sprint plus actionable same-project prerequisites.
4. **Delegate independent ready nodes when workers are available.** Use isolated worktrees by default. If delegation is unavailable, execute the same DAG serially and record the capability fallback.
5. **Verify every worker.** Retrieve its complete result, audit `git show --stat <sha>` against declared scope, and run relevant tests before integration.
6. **Scope allocation precedes dispatch.** List each worker's exclusive write paths before sending a prompt. No two workers share a file.
7. **The portable dispatch contract is mandatory.** Read and embed the contract in `references/sprint-orchestration.md`.
8. **Update the plan continuously.** After EACH section lands: collapse it in the evergreen, write a per-stage archive HTML, and call `edit_plan` to advance `impl`. Do not accumulate all outcomes for a final write.
9. **Per-stage HTML and a followup are required after every landing.** Even single-item work gets a `docs/archive/<slug>-<section>-landed.html` and an updated §05 followup.
10. **Collapse the evergreen when a section ships.** Replace the section body with a 2-4 line landed-summary + link to per-stage HTML.
11. **No plan-state drift.** Plan and sprint state must reflect reality at the end of every turn.
12. **The orchestrator owns integration and shared state.** Workers commit in detached worktrees; they do not merge, push the primary branch, or mutate the shared index/plan state.
13. **Cleanup is mandatory and conservative.** Remove a worktree only after it is clean and its commit is reachable from the integrated primary branch. Never force-remove unmerged or dirty worktrees.

## §Prerequisite blocking — STOP and ask for authorization

In plan mode, an unshipped prerequisite remains a hard stop unless the user
authorises implementing or overriding it. In sprint mode, actionable
same-project prerequisites become nodes in the execution DAG automatically.
Stop for cross-project, unavailable, abandoned, or authority-expanding
prerequisites.

`depends_on` entries may be EXTERNAL — `project:slug[#stage]` refs into
another mounted project (bare slugs stay local). `read_plan(project, slug)`
returns a computed `deps` list resolving every ref (`scope`, `found`,
`status`, `impl`); gate on that instead of assuming a bare slug is local. An
unshipped external prerequisite is a hard stop like any other, but its work
belongs to the OTHER project's checkout — never implement it in this one;
surface it as `/reckon-ship <project>:<slug>`.

For a plan-mode stop, ask for explicit user authorization:

```
⛔ BLOCKED: cannot implement <slug> — prerequisite unmet

The plan '<slug>' depends on '<prereq-slug>' which is currently status='<status>'.

To proceed, one of the following is needed:
  A) Implement '<prereq-slug>' first: run /reckon-ship <prereq-slug>
  B) Manually mark '<prereq-slug>' as done if it is already complete
  C) Override the dependency (confirm you want to proceed without it)

Please authorize one of the above before I continue.
```

Wait for the user's response before doing anything else. If the user authorizes option A, switch to implementing the prerequisite first, then return to the blocked plan.

## Workflow

### 0. Resolve plan vs sprint

1. Derive the current project from the repository/mount context.
2. Read `index` and match the argument against exact sprint ids.
3. Treat an exact sprint match, `sprint:<id>`, or `<project>:<id>` as sprint
   mode. Treat `plan:<slug>` or every other slug as plan mode.
4. If sprint mode, read `references/sprint-orchestration.md` completely and
   follow it. Do not continue with the plan-only preflight below.

### 1. Plan pre-flight — read the FULL plan

**This step is NON-NEGOTIABLE. Do not skip it. Do not begin implementation until it is complete.**

```python
# Read ALL plan state including schema
state = read_plan(project="<project>", slug="<slug>", with_schema=True)

# Also fetch the raw HTML to read section prose
# (the MCP payload has parsed state; you also need the full prose sections)
# Use: curl http://127.0.0.1:8765/<project>/<slug>.html  OR  Read docs/<slug>.html
```

Then read the COMPLETE HTML file from disk:
```bash
# Read the full plan HTML — every section, every paragraph
cat docs/<slug>.html
```

Do not proceed until you have read and understood:
- Every `<h2>` section and its prose
- All `<section data-reckon="decisions">` items (locked and open)
- All `<section data-reckon="followups">` items (resolved and open)
- The `plan-depends-on` meta tag
- Any `Trigger:` subsections or deferral markers

### 2. Classify ALL items

Build a complete audit before implementing anything:

| Signal | Action |
|---|---|
| Past-tense prose / commit SHAs present | Skip — already done |
| Marked "deferred", "v1", "post-smoke" | Skip — note it |
| `Trigger:` subsection with unmet condition | Skip — surface to user |
| `depends_on` prerequisite not shipped | **STOP — ask user for authorization** (see §Prerequisite blocking) |
| Concrete deliverable, no deferral signal | Implement |

**Report a complete audit before dispatching a single worker:**
```
Audit for <slug>:
  Implementable: §2 (3 items), §3 (2 items), §4 (1 item)
  Deferred:      §5 — marked post-v1
  Already done:  §1 — commit abc1234 present in prose
  Prerequisites: CLEAR (no depends_on / all satisfied)

Dispatch plan:
  §2: fleet of 3 (parallel) — workers A/B/C
  §3: fleet of 2 (parallel) — workers D/E
  §4: inline — 1 item
  Sequential order: §2 → §3 → §4 (§3 depends on §2 output)
```

### 3. Scope allocation

List **exclusive write paths** per item. If two items share a file, serialise them or split it (`test_a.py` / `test_b.py`).

**Never dispatch two workers that write the same file.**

### 4. Dispatch workers

| Items | Strategy |
|---|---|
| 1 | Inline, or one worktree worker when isolation helps |
| 2–8 independent | Parallel worktree fleet when workers are available |
| > 8 | Reader fan-out followed by one synthesis/integration owner |
| Cross-cutting / strategic | One highest-capability owner; do not fragment context |

Choose workers with the one-below policy in
`references/sprint-orchestration.md`. Build each prompt from the §05 template
and the portable dispatch contract.

Use background mode when the runtime supports it. Launch each ready wave
together, then wait for all results before integration.

### 5. Verify every worker — MANDATORY

Do not proceed to the next dependency wave until every worker in the current
wave has been verified.

For each completed agent:
1. Use the runtime's result/wait tool to retrieve the complete report
2. Check the agent's report for success/failure
3. Run `git show --stat <sha>` — confirm ONLY assigned paths appear
4. Run the project test suite (or targeted tests for modified paths)
5. Confirm the worker returned commit, test, artifact, and evidence inputs.
   The orchestrator writes plan/index state after integration.

If an agent FAILS or produces incomplete work:
- Do the work yourself, or dispatch a corrective agent
- Do NOT proceed to the next section while a failed section's work is outstanding

If a worker fails, inspect its worktree and report. Repair inline or dispatch a
corrective worker; do not advance the dependency wave with incomplete work.

### 6. Record outcomes — after EACH section

**Do not wait until all sections are done.** Record outcomes immediately after each section lands.

**Per-stage file** — `docs/archive/<slug>-<section>-landed.html`:
- Links to `/_shared/foundation.css` and `/_shared/dashboard.css`
- Quick-status grid (shipped vs deferred)
- Outcomes table: item, badge, commit SHA, follow-up title
- "What's next" card pointing at the new followup
- **Figures where they communicate (mandate 2026-06-03)**: embed result graphics under `docs/figures/<topic>/` with project-absolute `src`. Worker prompts for doc-producing tasks MUST carry the graphics requirement.

### 6b. Collapse-on-landing — MANDATORY

**When a section ships, IMMEDIATELY collapse it in the evergreen.** Replace the section body with a 2–4 line landed-summary card. Do not accumulate shipped sections.

```html
<section id="s2" class="section-landed">
  <header>
    <span class="badge badge-shipped">✓ landed 2026-06-24</span>
    <h2>§2 — Data prep pipeline</h2>
  </header>
  <p class="landed-summary">
    Built <code>src/data_prep.py</code>; pipeline smoke-test green.
    Encoded 11,237 shots in 3h12m; eval MAE 0.04 — passing.
    Full record: <a href="archive/<slug>-s2-landed.html">§2 landed</a>
    (commit <code>abc1234</code>).
  </p>
</section>
```

**Rules for the landed summary:**
- 2-4 lines max: what was built (past tense), the **quantitative result** (numbers, verdict), artifact paths, link + SHAs
- A summary that omits the result is incomplete — "landed §2" is not a summary
- Section header gets `✓ landed YYYY-MM-DD` badge (`.badge-shipped`)
- Original prose moves to per-stage HTML — gone from evergreen
- **Author as HTML, never markdown**

### 7. Update plan state — after EACH section

```python
# After each section lands, update atomically
state = read_plan(project="<project>", slug="<slug>")

edit_plan(
  project="<project>",
  slug="<slug>",
  ops=[
    {"op": "set", "path": "impl", "value": <shipped_count> / <total_count>},
    {"op": "resolve", "target": "followups", "id": "<driving-followup-id>",
     "by": "reckon-ship", "outcome": "§<N> landed — commit <sha>; <one-line result>"},
    {"op": "append", "target": "followups", "item": {
      "id": "f-<timestamp>",
      "status": "open",
      "written_by": "reckon-ship",
      "written_at": "<iso-now>",
      "title": "<next section imperative>",
      "body": "<2–3 sentences: what landed, what comes next>",
      "recommends_skill": "/reckon-ship <slug> §<N+1>",
      "prompt": "<§05 template body — mandatory, non-empty>"
    }}
  ],
  expected_version=state["version"]
)
```

**`impl` calculation:**
- Set `impl = (count of shipped sections) / (count of total implementable sections)`
- Monotonic — only ever increases
- Set it on EVERY section landing, not just the final one

Note: `impl` is a settable scalar — the server does NOT compute it automatically. You MUST set it.

### 8. Final validation — eat the dog food

Before declaring the overall plan done:

```python
state = read_plan(project, slug)
# Verify:
assert state["data"]["status"] in ("shipped", "done")   # or "active" if more sections remain
assert state["data"]["impl"] == expected_fraction         # set correctly
# All shipped sections are collapsed in the HTML
# Driving followup is resolved
# A next followup or "done — no followup" outcome is present
# version has incremented
```

For sprint mode, also verify every sprint item is done or explicitly blocked,
all integrated worker commits are reachable from the primary branch, the
sprint summary links its plan/evidence outcomes, and no session worktree
remains. Close the sprint only when all executable nodes are complete.

```bash
# Validate HTML integrity
uv run --project ~/Code/reckon reckon audit-doc docs/<slug>.html
# Must report no ERRORs before committing
```

Commit:
```bash
git add docs/<slug>.html docs/archive/<slug>-<section>-landed.html
git commit -m "docs: record verified implementation outcome"
git pull --no-rebase origin <branch>
git push origin <branch>
```

### 9. Surface follow-on work to the user — MANDATORY final-report format

Followup ids (`f-<...>`) are internal plan-state keys — NEVER the primary way
follow-on work is presented to the user. Every session that ends with open
follow-on work MUST close its final report with a **"Next up"** block that
names each follow-on by PLAN + SECTION/RUNG (the human handles), and gives a
fenced, paste-ready prompt so switching to a fresh session is seamless:

````markdown
**Next up** — paste into a fresh session:

```
/reckon-ship <slug> §<N>   (<rung/step label> — <one-line what it does>)
/reckon-ship <project>:<sprint-id>   (resume the remaining sprint DAG)
```
````

Rules:
- One fenced prompt per advised follow-on; if several follow-ons are advised
  for one session, stack them in ONE fence in execution order.
- The fenced line is exactly the slash invocation the next session needs —
  the skill reads the full §05 prompt from the plan itself, so the fence
  stays SHORT (slash command + parenthetical label), never a pasted wall.
- Mention the followup id at most once, in passing (e.g. "tracked as
  f-tps-03"), and always AFTER the plan/section name — the id is for plan
  audits, the name is for humans.

## §05 dispatch prompt template

> **Canonical §05 template: `reckon-edit` SKILL.md.** Keep this copy in sync.

Embed in every worker prompt, substituting angle-bracket fields:

```
Project: <project-name>
Plan:    <slug> (<url>)
Section: <§N — section title>
Capability: <one-below default | orchestrator-level | routine>

Context
  <2–3 sentences: what this section does and why it is being shipped now>

State to read
  GET /plan/<project>/<slug>   (parsed state — decisions, followups, status, version)

Locked decisions to honour
  <key> → <choice>

Open decisions to surface (do not resolve)
  <key>, <key>

Constraints
  File scope (EXCLUSIVE — stage ONLY these paths):
    <path 1>
    <path 2>
  Base ref: <primary branch>
  Worktree: <absolute detached-worktree path>

Done-when
  1. <measurable artefact: commit, file, test result>
  2. tests still green
  3. commit SHA, test output, artifacts, and evidence inputs returned to orchestrator
```

## Delegation, model routing, integration, and cleanup

For any delegated plan work, and always for sprint mode, read
`references/sprint-orchestration.md` completely. It owns:

- model-family-neutral one-below worker selection;
- skill and reasoning-effort selection;
- detached worktree creation and worker prompt rules;
- orchestrator-owned merge/conflict handling;
- research-before and evidence-after gates;
- reachability checks and mandatory worktree cleanup.

Use `scripts/worktree_fleet.py` for deterministic worktree creation, inspection,
and cleanup. Workers never mutate shared Reckon state; the orchestrator records
followups, evidence, plan progress, sprint item outcomes, and sprint closure
after integration.

## Cross-references

- `reckon-edit/SKILL.md` — how the evergreen gets its landed subsection; edit_plan op reference.
- `reckon-create/SKILL.md` — first-time plan scaffolding and §05 template.
- `reckon-status/SKILL.md` — read-only inspection before deciding what to ship.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (semantic HTML elements, schema contract, endpoints).
- `docs/_shared/plan.schema.json` — published JSON Schema.
