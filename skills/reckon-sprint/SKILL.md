---
name: reckon-sprint
description: >-
  Manage independently versioned sprint and roadmap resources — propose / start
  / close / rebalance sprints, move items between sprints, and edit milestones,
  timeline, and blockers. Trigger verbs: "propose sprint / start
  sprint / close sprint / rebalance sprint / move item to sprint / plan the
  roadmap / add milestone / add blocker / /reckon-sprint". For editing a single
  plan's text or followups use reckon-edit; execute a plan slug or whole sprint
  with reckon-ship; use reckon-status for read-only
  inspection.
allowed-tools: Read Write Edit Bash(*) Grep mcp__reckon___read_plan mcp__reckon___edit_plan mcp__reckon___roadmap mcp__reckon___audit
---

# reckon-sprint — sprint, milestone, and roadmap orchestration

## Fast path
- Read execution state → `roadmap(project)`; use `read_plan` for named resource detail.
- Read/write one sprint → typed `read_plan(..., view="raw")` then
  `edit_plan(..., doc_type="sprint")`.
- Move an item between sprints → read both versions, then use the source sprint's
  `move` op with `to_version`.
- Propose a sprint → use `roadmap` ready/open paths, validate ownership and wiring, then write.

Full detail below. Sprint state is a typed resource, not a plan. This
skill never dispatches workers; `/reckon-ship S1` executes the sprint.

## The model — independently versioned resources

Sprints live under `docs/sprints/`, milestones under `docs/milestones/`,
blockers under `docs/blockers/`, and the append-only timeline at
`docs/state/<project>/timeline.html`. Each resource owns its own version.
`project.json` is identity/presentation-only. Sprint items reference plan
slugs; live plan status and implementation fraction are composed from plan
HTML and are never persisted in a sprint.

`read_plan(project, view="summary")` is the preferred compact composed view.
`read_plan(project, "index")` remains a read-only compatibility view.
It returns `source_format`, `resource_versions`, and the active sprint derived
from the unique sprint whose status is `active`. Never write the aggregate
index after distributed activation.

Check `summary["state"]["source_format"]` before writing. In
`distributed` mode, read and edit the named resource. In `legacy-index` mode,
typed raw reads are projections carrying the aggregate version, but named
resource writes are intentionally inactive: read `slug="index"` and use the
legacy aggregate op vocabulary until explicit project-state migration
activates distributed resources.

`edit_plan` is the version-safe write path: call `read_plan` first for the
current `version`, pass it as `expected_version`; on a 412 conflict re-read and
retry.

Call `roadmap(project)` before and after every membership, order, blocker, or
status mutation. Before the write it is the execution source of truth; after
the write it verifies that plan metadata and sprint resources still agree.

### Running inside a git worktree — pass `checkout_path`

The MCP server resolves every project to the FIXED docs dir in `mounts.json`
(the **main** checkout). If you run in a worktree, pass
`checkout_path=<your repo root>` (the dir containing `docs/`) to both
`read_plan` and `edit_plan` so the index write lands in **your** tree — the
read's `version` must come from the same `checkout_path`. Omit it (default) in
the main checkout. **Preferred:** let the orchestrator in the main checkout own
shared roadmap writes; worktree workers avoid resource contention entirely. Full
rationale in `reckon-edit` SKILL.md (§ worktree).

## Op reference (named resources)

| Op | Required keys | Notes |
|---|---|---|
| `set` | `path`, `value` | Top-level sprint/milestone/blocker/project field; timeline excluded |
| `append` | `target`, `item` | Sprint `items` or timeline `events` |
| `move` | `target="sprint_item"`, `slug`, `to`, `to_version` | Move from selected sprint with both versions checked |

## Read sprints

```python
sprint_cards = read_plan(project="imas-ambix", view="summary")
sprint = read_plan(
    resource={"project": "imas-ambix", "type": "sprint", "id": "S5"},
    view="raw",
)
# sprint_cards["state"]["active_sprint_id"], sprint["data"], sprint["version"]
```

## Create a sprint

```python
edit_plan(
  project="imas-ambix", slug="S5", doc_type="sprint", create=True,
  ops=[
    {"op": "set", "path": "theme", "value": "Foundation hardening"},
    {"op": "set", "path": "description", "value": "Schema and tooling."},
    {"op": "set", "path": "status", "value": "planned"}
  ],
  expected_version=0
)
```

## Start sprint (set active)

```python
edit_plan(
  project="imas-ambix", slug="S5", doc_type="sprint",
  ops=[{"op": "set", "path": "status", "value": "active"}],
  expected_version=1
)
```

## Add item to sprint

```python
edit_plan(
  project="imas-ambix", slug="S5", doc_type="sprint",
  ops=[{"op": "append", "target": "items", "item": {
    "slug": "plasma-decoder-finetune", "why_now": "Highest ROI; gates M2",
    "done_when": "Fine-tune run green; eval passing"
  }}],
  expected_version=2
)
```

## Move item between sprints

```python
edit_plan(
  project="imas-ambix", slug="S4", doc_type="sprint",
  ops=[{"op": "move", "target": "sprint_item", "slug": "plasma-decoder-finetune",
        "to": "S5", "to_version": 3}],
  expected_version=5
)
```

## Close sprint

```python
edit_plan(
  project="imas-ambix", slug="S5", doc_type="sprint",
  ops=[{"op": "set", "path": "status", "value": "done"}],
  expected_version=4
)
```

## Propose a sprint (manual workflow)

1. Call `roadmap(project)`; do not reconstruct dependency order from discovery.
2. Read `allocation.scope` and repository instructions before assigning work
   whose owner is ambiguous. Cross-project consumer work stays qualified; do
   not copy the provider plan into this repository.
3. Keep only **actionable live plans**: usually `status in {active, pending}`.
   Exclude research docs, archived/done plans, README/reference pages, and
   cross-repo coordination plans unless the user explicitly wants them tracked.
4. Fix error-level `wiring_findings` before scheduling. A research/evidence
   input in `depends_on` becomes `informs`; a cycle or sprint-order inversion
   is never accepted as a roadmap.
5. Order the `critical_path` prerequisite-first, then alternative `open_paths`.
   Within each ready wave use the analyzer's immediate order.
6. Partition into sprints; each item carries `why_now` and `done_when`. Every
   actionable plan must either belong to exactly one sprint with matching
   `plan-sprint`, or carry an explicit backlog decision.
7. Keep **one active sprint** at a time. Future sprints start as `planned`.
8. Treat any legacy `tier` value as compatibility input only, never as runtime
   model guidance. The current user prompt and coordinator own worker routing.
9. If the user requested a roadmap change, write it without a redundant
   checkpoint unless a material ownership or priority choice remains open.
10. Re-run `roadmap` and `audit`; clear new graph or membership errors and
    report both completion percentages.

## Execution handoff

After a sprint is defined or started, surface the executable handle:

```text
/reckon-ship S1
```

Use `/reckon-ship <project>:S1` outside the project's canonical checkout.
The ship skill reads the sprint plans, transitive dependencies, research and
prior evidence; assigns ready nodes to capability-appropriate workers; uses
detached worktrees by default; integrates results; writes evidence and state;
and cleans up worktrees before sprint closure.

## Milestones, timeline, blockers

Each is selected by `doc_type`. Create or edit milestones and blockers as named
resources. Read `slug="timeline", doc_type="timeline"` before appending one
`events` item (`{when, who, what}`); existing events cannot be changed. Blocker
reference counts are derived from sprint item `blocked_by` references.

## Project allocation scope

Repository responsibility and routing guidance lives on the independently
versioned `project` resource under `scope`:

```python
project_state = read_plan(
    resource={"project": "my-project", "type": "project", "id": "project"},
    view="raw",
)
edit_plan(
    project="my-project",
    slug="project",
    doc_type="project",
    expected_version=project_state["version"],
    ops=[{"op": "set", "path": "scope", "value": {
        "owns": ["runtime responsibilities"],
        "excludes": ["work owned by another repository"],
        "routes": [{"work": "vocabulary", "project": "language"}],
    }}],
)
```

Keep entries concise and mechanism-based. `routes[].project` must be a safe
project key. Scope guides allocation preflight but never overrides repository
instructions or user write authorization.

## Close and rebalance gates

**Never wire new work into a closed sprint, and keep the horizon advancing.** A `done`
sprint is a record; an item added to it is invisible to the advancing horizon. When the
active sprint's items are all resolved, CLOSE it and open the next rather than parking new
work in it, in a stale `planned` sprint, or in a completed one. Work discovered against a
closed sprint belongs to the advancing sprint. Same rule one level down: work discovered
against a COMPLETED PLAN needs a new plan, not a followup on the finished one — a followup
there is excluded from `roadmap`'s `pending_work` and is hidden rather than tracked.
Canonical rule: `reckon-ship` SKILL.md §7a-bis.

- Do not close a sprint while `roadmap(project, sprint=<id>)` returns ready or
  in-progress work. Execute it or record a genuine external/human blocker.
- A derived prerequisite is not duplicated as an explicit blocker. Keep the
  plan's workflow state and let `effective_status` project dependency blocking.
- When moving a plan, update both sprint resources and the plan's `sprint`
  scalar in the same session, then verify no membership finding remains.
- Rebalancing must not move a prerequisite behind its successor. Treat a
  `sprint-order-inversion` as a failed rebalance.

## Cross-references

- `reckon-edit` — edit a single plan's prose, decisions, followups (+ the full `edit_plan` op reference and worktree `checkout_path` detail).
- `reckon-ship` — execute the work a plan describes.
- `reckon-status` — read-only plan and audit inspection.
- `reckon-roadmap` — executable order, progress, blockers, and wiring health.
- `reckon-create` — scaffold a new plan.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (index schema, endpoints).
