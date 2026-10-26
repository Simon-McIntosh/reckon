---
name: reckon-roadmap
description: >-
  Scan Reckon plan dependency graphs and return all pending work, ready work,
  true blockers, sprint ordering, lifecycle and implementation percentages,
  critical paths, open paths, and wiring faults. Use for "scan the DAG / what
  can run now / immediate roadmap / critical path / open paths / true blockers /
  sprint resolution order / portfolio roadmap / is this dependency wired
  correctly / /reckon-roadmap". Read-only; use reckon-edit or reckon-sprint to
  repair findings and reckon-build to execute them.
allowed-tools: Read Bash(*) Grep mcp__reckon___roadmap mcp__reckon___read_plan mcp__reckon___audit
---

# reckon-roadmap — executable work and graph health

## Fast path

- One project → `roadmap(project="<project>")`.
- One sprint plus transitive prerequisites → `roadmap(project="<project>", sprint="<id>")`.
- Mounted portfolio → `roadmap(project="*")`.
- Worktree → pass the same absolute `checkout_path` used for plan reads.

Never rebuild the dependency graph by hand when the `roadmap` tool is
available. The analyzer is shared by MCP, CLI, and audit.

## Fan-out boundary

In a crew-managed repository, investigation and review fan-out is `reckon crew dispatch` work under the investigate and review roles; harness-native background agents bypass the run ledger, manifests and calibration and are refused by the guard.

## Hard rules

1. **Read-only.** Never call a write tool or change a file.
2. **Report both percentages.** Lifecycle completion counts completed plans;
   implementation completion averages stored `plan-impl`. They answer different
   questions and one must not hide the other.
3. **Use hard dependencies narrowly.** `depends_on` means the plan cannot close
   without the target completing. Research, specifications, and reference data
   belong in `informs`; downstream work belongs in `blocks`.
4. **Do not call derived blocking a human blocker.** An open prerequisite
   creates `effective_status=blocked` while preserving the plan's workflow
   status. Explicit blockers and persisted `status=blocked` are separate.
5. **Do not break cycles heuristically.** Report the exact cycle and route the
   repair through `reckon-edit`.
6. **Validate repository ownership before suggesting a move or new plan.** Read
   `allocation.scope` when configured, then the target and neighboring
   repositories' `AGENTS.md` ownership guidance. A matching project name is not
   evidence that the repository owns the work.

## Read the report

The tool returns:

| Field | Meaning |
|---|---|
| `completion` | Plan count, completed count, pending count, lifecycle %, stored implementation % |
| `sprints` | Sprint sequence with item resolution, ready/blocked/deferred counts, and both percentages |
| `pending_work` | Every open plan in scope, including prerequisites and exact blockers |
| `ready_now` | Dependency-ready workflow plans whose hard prerequisites and explicit blockers are clear; schedule readiness is a separate axis |
| `schedule` | Configured sprint window, ordered sprints holding open work, horizon depth, and schedule-ready/deferred counts |
| `schedule_deferred` | Plans outside the configured sprint window, retained in roadmap order with the sprint each sits behind |
| `immediate_roadmap` | Ready work ordered by critical-path membership, sprint order, ROI, unlocks, and remaining effort |
| `critical_path` | Longest remaining local dependency chain weighted by effort and progress; `critical_path.sections` names its links section by section |
| `pending_work[].implementable_sections` / `ready_sections` | Per plan, the sections declared buildable and the sections whose section waits and gates are clear; their intersection, less sections with a live run, is the ready-idle set |
| `open_paths` | Alternative longest execution paths, capped by `max_paths` |
| `cycles` | Exact local dependency cycles |
| `wiring_findings` | Invalid, dangling, non-executable, inactive, contradictory, cyclic, sprint-order, and membership faults |
| `allocation` | Optional project responsibility/routing policy and the ownership preflight reminder |

`blocked` contains only explicit, dependency-derived, persisted, or cycle
blockers. `deferred` contains valid non-runnable work such as plans missing a
dispatch handoff. Schedule deferral is different again: each pending row exposes
`dependency_readiness` beside `schedule_readiness`, and `schedule_deferred`
retains queued rows with `schedule_deferred_reason` and
`schedule_behind_sprint`. Read both axes; neither answers the other question.

The project manifest may declare `schedule_horizon_sprints` as a positive
integer. Reckon derives the ordered window from the earliest sprint holding open
work and reports the total count as `schedule.horizon_depth`. With no declaration,
the schedule axis remains visible but does not silently impose a queue.

## Ready-idle sections, owners and the critical path

A plan reads `ready` while most of its sections wait, and a ready section with
no run leaves the lane empty unnoticed. So the plan-level answer is not the
coordination answer. The coordination answer is section-level, joined with live
runs and owners:

1. **The ready-idle set** is each plan's `implementable_sections ∩
   ready_sections`, less every section `crew(project, view="live")` shows a run
   on. A plan whose `dependency_readiness` reads `closable` belongs in the set
   too: it is owed a close, or a descope decision.
2. **The owner** of each section is the coordinator that dispatches its plan:
   `crew(view="directory", project=<project>)`. A plan no live coordinator holds
   is unowned; report it as such.
3. **The critical path, link by link.** For each entry in
   `critical_path.sections`, state one of: in flight (run id), ready-idle
   (owner), gated (gate id), or waiting (the ref it waits on). An idle link at
   the head of the path is the most expensive idle work in the project.

`roadmap(view="summary")` carries none of the section fields, and on a project
with many plans `view="detail"` and `view="raw"` exceed the MCP response ceiling.
Until the tool serves the set as one bounded view, read the lossless CLI report:

```bash
reckon roadmap --project <project> --json-output | python3 -c '
import json,sys; d=json.load(sys.stdin)
print("critical path:", d["critical_path"].get("sections"))
for p in d["pending_work"]:
    ready=set(p.get("ready_sections") or [])&set(p.get("implementable_sections") or [])
    if ready or p.get("dependency_readiness")=="closable":
        print(p.get("slug") or p.get("plan"), sorted(ready), p.get("dependency_readiness"))'
```

This readiness view describes which sections can launch now. It does not limit
the build skill's one-beat load: that dispatch set also includes implementable
sections whose declared upstream has not landed, so the queue can hold them.

## Wiring diagnosis

Treat these as graph defects, not scientific or delivery blockers:

- `non-executable-hard-dependency`: `depends_on` resolves only to research or
  evidence. Move the relation to `informs` unless an executable plan is missing.
- `dangling-hard-dependency`: target plan is missing or renamed.
- `inactive-hard-dependency`: target is abandoned, superseded, historical, or
  otherwise terminal without satisfying the prerequisite.
- `contradictory-hard-relation`: the same target is both a prerequisite and
  downstream work.
- `dependency-cycle`: no member can enter the ready set.
- `sprint-order-inversion`: a successor is scheduled before its prerequisite.
- `plan-sprint-missing-item`, `plan-sprint-mismatch`, or
  `duplicate-sprint-membership`: plan metadata and sprint resources disagree.
- `orphaned-blocked-status`: blocked workflow state has no recorded cause.

An `unscheduled-open-plan` is informational: either link actionable work to a
sprint or explicitly retain it as backlog. Do not silently discard it.

## Repository allocation preflight

Before advising where work belongs:

1. Read the current project resource and `allocation.scope` in the report.
2. Read root and nearest-path `AGENTS.md` in the proposed repository.
3. Inspect neighboring mounted projects whose responsibilities overlap.
4. State the ownership boundary in mechanism terms: what the repository owns,
   consumes, or routes elsewhere.
5. If the plan is misplaced, hand off to `reckon-edit` for relocation and to
   `reckon-sprint` for source/destination membership repair. Never leave a copy
   in both repositories.

Recommended project resource shape:

```json
{
  "scope": {
    "owns": ["executable responsibilities of this repository"],
    "excludes": ["similar work owned elsewhere"],
    "routes": [{"work": "description", "project": "destination"}]
  }
}
```

The policy is advisory evidence for agents; it does not replace repository
instructions or user authority.

## Output

Lead with the result:

1. project and sprint completion percentages;
2. the ready-idle sections, each with its owner, critical-path links first, and
   the closable plans nobody has closed;
3. ready-now work in execution order;
4. the critical path link by link (in flight, ready-idle, gated or waiting), and
   the alternative open paths;
5. true blockers, separated from wiring defects;
6. exact wiring repairs and the skill that owns each mutation.

For a portfolio, preserve project boundaries. Cross-project prerequisites stay
qualified and remain blockers until their owning project completes them.

## Repair and execution handoff

- Relationship, prose, status, or relocation repair → `reckon-edit`.
- Sprint membership, ordering, milestone, or blocker repair → `reckon-sprint`.
- Execute ready plans or a sprint → `reckon-build`.
- Schema/lifecycle audit after graph repair → `reckon-status --review`.
