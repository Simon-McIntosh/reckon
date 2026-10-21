# Judging plan links

Read by `reckon-review` in plan and portfolio mode. Every link in scope gets a
verdict; a review that only adds links has judged nothing.

## What to inventory

From `read_plan(project, slug, view="summary")` and the plan's row in
`roadmap(project)`:

- plan relationships: `depends_on`, `blocks`, `after`, `informs`,
  `evidence_for`, `verifies`, `supersedes`;
- each section's waits, `section_depends_on.<sid>`;
- every gate's `required_evidence`, with the gate's status and verdict;
- qualified cross-project refs (`project:slug#sN`), which resolve against the
  named mount and stay blocking until that project closes the target.

The roadmap row's `depends_on` and `section_depends_on` refs carry `satisfied`
and the target's `status`. For a section ref, judge the section's own
declaration, not the target plan's status: a done section of an in-progress plan
is satisfied.

## Verdicts

| What the link shows | Verdict |
|---|---|
| Target closed and the edge satisfied, and no open section still waits on it | **remove**: it orders nothing, and the history is in the landed record |
| Target superseded or abandoned | **re-point** to the successor its `supersedes` names, or **remove** |
| A plan-level `depends_on` where only one section waits | **narrow** to `section_depends_on.<sid>` or a gate on that section |
| `depends_on` on research or evidence | **convert** to `informs` |
| Implied by another declared edge (A→B→C makes A→C redundant) | **remove** the redundant edge |
| Neither the plan's prose nor any section needs it | **remove**, or ask the owner when unsure |
| The same target both waited on and unblocked | **resolve** to the one true direction |
| An open gate whose gated sections are done, or whose evidence has landed | **close** the gate with its verdict |
| An `after` whose target has closed | **remove**: it no longer sequences anything |
| A `blocks` the target does not reflect as a wait | **add** the wait on the target, or **remove** |
| Still ordering open work, explained by the prose | **keep** |

## When a link may be added

Only on evidence that something waits on or consumes the target:

- a blocker, run manifest, gate measure or followup that names a ref the plan
  does not declare;
- another plan that consumes this plan's outputs: a test pinning its values, an
  import, a figure, a record it quotes;
- a peer coordinator's account of a wait they are holding by hand.

A wait between sections is a section edge, never a plan-level one: a plan-level
`depends_on` reports the whole plan blocked from its first section onward.

## Ops

A relationship list is rewritten whole, without the removed ref:

```json
{"op": "set", "path": "depends_on", "value": ["other-plan"]}
{"op": "set", "path": "after", "value": ["other-plan#s3"]}
{"op": "set", "path": "section_depends_on.s5", "value": ["other-plan#s3"]}
{"op": "gate", "id": "g-<slug>-<n>", "section": "s5", "gated_sections": ["s5"], "measure": "...", "required_evidence": "other-plan#s3"}
{"op": "pass", "id": "g-<slug>-<n>", "evidence": "<landed record anchor or commit>"}
{"op": "fail", "id": "g-<slug>-<n>", "evidence": "<why the measure cannot be met>"}
```

`blocks`, `after`, `informs`, `evidence_for`, `verifies` and `supersedes` take
the same whole-list `set`, validated against the same ref grammar — a bare slug
is local, `project:slug` is external; a malformed ref is refused and named.

## Authority

Links on another project's plan, or on a plan a live session is building, are
proposed to the owner and not applied. Find the owner with
`crew(view="directory")` and send the `LINK` line. A merge or retirement moves
or removes the links that pointed at the plan in the same pass.
