# Lane routing — operator reference

This is the dispatch procedure for a named lane. A lane is the backend name in
flight configuration. The orchestrator names it; no worker component chooses,
ranks, or recommends one.

## Name the lane, then trust no fallback

Add `--backend <lane>` to a live `reckon crew dispatch` invocation. The same
option is available on `reckon crew shadow` and `reckon crew resume`, so a
shadow comparison and a continuation use the same lane vocabulary. A named
lane is a requirement, not a hint:

- if the merged flight configuration does not declare it, dispatch refuses;
- if it is declared but cannot serve the requested work, dispatch refuses;
- the default lane is never substituted silently.

Availability, budget, competence, and execution-fit refusals remain refusals on
a named lane. These are hard refusals because substituting the default would
erase the operator's request and make the run's routing evidence lie.

An explicitly declared fallback is different from silent substitution. It is
still subject to the destination lane's checks and the run records the choice.
Read `requested_backend` beside the resolved `backend` in the live pointer or
committed run record. A difference is the one-read signal that a handover
occurred; equality says the named lane ran. Keep `agent.backend` as the
resolved execution identity, not as a replacement for that comparison.

## Re-read the roster before every wave

Declarations in the roster now have routing authority. List them before the
next wave and compare every member's `harness` with the lane intended for that
member:

```text
reckon crew member list --project <project> --pretty
```

With `--member <member>`, a dispatch follows that member's declared harness
when no backend is named. If `--backend <lane>` and the declaration disagree,
the dispatch refuses and names both lanes. This matters in both directions: a
sizeable roster can declare an unmetered local lane, and reusing one of those
members for metered work now routes the work to the local lane. Omitting a
member declaration and allowing the metered default is the less likely mistake.

## Where the selection evidence lives

When the choice is not obvious, consult evidence rather than folklore:

- `crew(project, view="lanes")` reports which configured endpoints will serve,
  their observation times, effective context windows, and short and weekly
  quota horizons. Read it before choosing a lane, not after a refusal.
- `crew(project, view="flight")` reports the resolved flight configuration,
  layer provenance, and endpoint availability.
- `crew(project, view="budget")` reports recorded headroom, holds, and reset
  horizons for dispatch and resume.
- `crew(project, view="routing")` reads the mounted committed ledgers and
  durable worker evidence used for selection. The lane-evidence rows are
  conditioned on the role and specification shape; a shallow population emits
  `state: insufficient_evidence`, not a fabricated figure.

The committed ledgers and durable worker receipts are the evidence sources;
roster order and a component's preference are not. No surface selects a lane,
and none will: the orchestrator owns that decision, because a component that
picks would hide the decision and its reason.

When a row says `insufficient_evidence`, generate an observation instead of
guessing. Dispatch the same node to two lanes and compare the outcomes; the
shadow command exists for this controlled comparison and does not merge its
work. A shadow keeps the primary's base and named check while the caller names
the candidate with `--backend <lane>`.

## Escalate without losing the worktree

Use this procedure when the selected lane stops honestly:

<ol type="a">
<li>Route to the cheaper lane first, but only after the endpoint and selection
evidence are readable and the lane passes its dispatch gates.</li>
<li>When it stops, move the run to a stronger lane with the command that keeps
the existing worktree and run identity:

```text
reckon crew redispatch --run <run-id> --backend <stronger-lane> --reason "<why>" --advice "<checkpoint instruction>"
```
</li>
<li>Tell the successor to commit the inherited diff as a checkpoint before
continuing, then compare the requested and resolved lane fields again.</li>
</ol>

This handover is safe only because a lane that can report false completion is
never a first rung. The live plan owns the independent measure that establishes
that prerequisite; keep its verdict there rather than copying a moving value
into this reference.

For specification-level ownership and its dispatch gate, see
[effort-routing.md](effort-routing.md). This file intentionally carries no
model identity, lane ranking, price, token figure, or quota percentage.
