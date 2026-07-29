---
name: reckon-sprint
description: >-
  Manage sprint and roadmap state in a project's central index — propose / start
  / close / rebalance sprints, move items between sprints, and edit milestones,
  timeline, and blockers. All state lives in the project's docs/state index,
  read and written via the index slug. Trigger verbs: "propose sprint / start
  sprint / close sprint / rebalance sprint / move item to sprint / plan the
  roadmap / add milestone / add blocker / /reckon-sprint". For editing a single
  plan's text or followups use reckon-edit; execute a plan slug or whole sprint
  with reckon-ship; use reckon-status for read-only
  inspection.
allowed-tools: Read Write Edit Bash(*) Grep mcp__reckon___read_plan mcp__reckon___edit_plan
---

# reckon-sprint — sprint, milestone, and roadmap orchestration

## Fast path
- Read sprints → `read_plan(project, "index")` → `data.sprints`, `active_sprint_id`.
- Create / start / close a sprint → `edit_plan(project, "index", ops=[…])` (append / set).
- Move an item between sprints → `edit_plan` `move` op (`target="sprint_item"`).
- Propose a sprint → discover plans, score by **dependency order first**, confirm, then write.

Full detail below. Sprint state is the project **index**, not a plan. This
skill never dispatches workers; `/reckon-ship S1` executes the sprint.

## The model — sprints live in the project index

Sprint, milestone, timeline, and blocker state lives in
`docs/state/<project>/index.json` (schema `reckon/_schema.py:IndexState`),
**not** in any plan's HTML. You reach it through the special `index` slug:
`read_plan(project, "index")` to read, `edit_plan(project, "index", ops=…)` to
mutate. The index version counter is `data._version` (distinct from a plan's
`plan-version`). Sprint items reference plan slugs; the live plan state (impl,
decisions, followups) is always read from each plan's HTML, never duplicated
into the index.

`edit_plan` is the version-safe write path: call `read_plan` first for the
current `version`, pass it as `expected_version`; on a 412 conflict re-read and
retry.

### Running inside a git worktree — pass `checkout_path`

The MCP server resolves every project to the FIXED docs dir in `mounts.json`
(the **main** checkout). If you run in a worktree, pass
`checkout_path=<your repo root>` (the dir containing `docs/`) to both
`read_plan` and `edit_plan` so the index write lands in **your** tree — the
read's `version` must come from the same `checkout_path`. Omit it (default) in
the main checkout. **Preferred:** let the orchestrator in the main checkout own
index/sprint writes; worktree workers avoid index contention entirely. Full
rationale in `reckon-edit` SKILL.md (§ worktree).

## Op reference (index slug)

| Op | Required keys | Notes |
|---|---|---|
| `set` | `path`, `value` | `active_sprint_id`, `sprints.<id>.<field>`, `milestones.<id>.<field>` |
| `append` | `target`, `item` | `sprints`, `sprints.<id>.items`, `milestones`, `timeline`, `blockers` |
| `move` | `target="sprint_item"`, `slug`, `from`, `to` | Move an item between sprints |

## Read sprints

```python
state = read_plan(project="imas-ambix", slug="index")
# state["data"]["sprints"], state["data"]["active_sprint_id"], state["version"]
```

## Create a sprint

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

## Start sprint (set active)

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

## Add item to sprint

```python
edit_plan(
  project="imas-ambix", slug="index",
  ops=[{"op": "append", "target": "sprints.S5.items", "item": {
    "slug": "plasma-decoder-finetune", "why_now": "Highest ROI; gates M2",
    "done_when": "Fine-tune run green; eval passing"
  }}],
  expected_version=9
)
```

## Move item between sprints

```python
edit_plan(
  project="imas-ambix", slug="index",
  ops=[{"op": "move", "target": "sprint_item", "slug": "plasma-decoder-finetune",
        "from": "S4", "to": "S5"}],
  expected_version=9
)
```

## Close sprint

```python
edit_plan(
  project="imas-ambix", slug="index",
  ops=[{"op": "set", "path": "sprints.S5.status", "value": "done"}],
  expected_version=10
)
```

## Propose a sprint (manual workflow)

1. Discover plans via `read_plan(project, slug=None)` (discovery mode).
2. Keep only **actionable live plans**: usually `status in {active, pending}`.
   Exclude research docs, archived/done plans, README/reference pages, and
   cross-repo coordination plans unless the user explicitly wants them tracked.
3. **Score by dependency order first** — a plan whose `depends_on` are not all
   `shipped`/`done` is NOT ready; never schedule it ahead of its prerequisites.
   Refs may be external (`project:slug`): resolve them via `read_plan`'s
   `deps` list, and record an unshipped external prerequisite as a BLOCKER in
   the index (it cannot be scheduled inside this project's sprints).
   Then order the ready set by `roi × effort_inverse × milestone_priority`.
4. Partition into N sprints; each item carries `why_now` and `done_when`.
5. Keep **one active sprint** at a time. Future sprints start as `planned`.
6. Treat any legacy `tier` value as a relative hint, not a model id. Runtime
   worker selection belongs to reckon-ship's one-below policy.
7. Print the proposal; ask to confirm before writing.

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

All live in the same index. Append a milestone (`target="milestones"`, with
`depends_on` other milestone ids), a timeline entry (`target="timeline"`:
`{when, who, what}`), or a blocker (`target="blockers"`:
`{id, summary, origin, owner, next}`) via `edit_plan` append ops. Set a
milestone field with `{"op":"set","path":"milestones.<id>.<field>","value":…}`.

## Cross-references

- `reckon-edit` — edit a single plan's prose, decisions, followups (+ the full `edit_plan` op reference and worktree `checkout_path` detail).
- `reckon-ship` — execute the work a plan describes.
- `reckon-status` — read-only inspection; ready-set / dependency-order view.
- `reckon-create` — scaffold a new plan.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (index schema, endpoints).
