# Conditional operating guidance

This reference holds the explanatory material, observed failure shapes, and
worked procedures that support the fixed rules in `../SKILL.md`. Read a named
section only after the rule in the skill has made that situation relevant. It is
not a substitute for the skill's dispatch, gate, closure, or landing rules.

## Continuity, stopping, and prerequisite diagnosis

A session continues the task it began and nothing else. Reusing a transcript
for a different node pays for unrelated context on every turn: a measurement
found 312 of 2,089 local runs and 1,315 of 3,105 metered runs resumed another
task, with substantial inherited context. The deliberately valid continuations
are `reckon crew resume` of the same run, `reckon crew redispatch` of the same
run, and new work on that same node for a repair or re-review. Every new node,
new write scope, and widened file set starts a fresh session.

The invalid stopping rationalisations and the check separating a transient
hiccup from a blocker are in `sprint-orchestration.md` §Role of stopping. Read
that section only while evaluating whether a reported inability to continue is
actually a hard blocker.

`pending_work` is the authoritative set on a relaunch. A plan at `impl` 1.0 or
status `shipped`/`done` is already excluded from `pending_work`, `ready_now`,
the critical path, and every open path. Re-read such a plan only when a pending
plan depends on it, and then read its landed contract and locked decisions, not
its historical execution prose. A sprint's membership count can differ from
the plan metadata count, so never reconstruct the dispatch set from that
figure.

Before treating a prerequisite as a user-owned stop, inspect
`roadmap.wiring_findings`. Research or evidence belongs in `informs`; a plan
that merely consumes another plan's landed evidence belongs there too. A
section that waits for evidence is a gate with a measure and required evidence,
so its result is visible in `gate_blockers` rather than buried in prose. A
superseded umbrella, dangling slug, cycle, or sprint-order inversion is a
wiring defect: repair the relationship, re-run `roadmap`, and only then
classify remaining prerequisites. Do not ask a user to override a relationship
the roadmap calls malformed.

When a plan-mode prerequisite is genuinely unmet, use this user-facing
response:

```
⛔ BLOCKED: cannot implement <slug> — prerequisite unmet

The plan '<slug>' depends on '<prereq-slug>' which is currently status='<status>'.

To proceed, one of the following is needed:
  A) Implement '<prereq-slug>' first: run /reckon-build <prereq-slug>
  B) Manually mark '<prereq-slug>' as done if it is already complete
  C) Override the dependency (confirm you want to proceed without it)

Please authorize one of the above before I continue.
```

## Local-lane specification and capacity rationale

A worker is graded on the done-when, not on surrounding prose. A measured
failure showed a goal naming three call sites while its done-when named only
two: the worker correctly delivered two and reported the third as a follow-on.
The fault was the unmeasured claim. This is particularly visible on a local
lane, which tends to implement the stated measure precisely rather than infer
an omitted one.

For a complete measure, read these details while reviewing its wording:

- Every claim in the goal appears in the done-when as something that emits
  evidence. Read the pair back and find any sentence left unmeasured.
- State removals as a negative measurement. A test proving a new module works
  does not prove an old duplicate stopped existing; use an absence check for
  that part of the claim.
- Put execution of the negative control in the done-when. A declaration records
  the mutation; it does not prove the named check was made to fail. The measure
  must require the mutation, failure observation, and recorded log.
- Spend cheap capacity on narrower nodes rather than broader ones. Node width
  consumes both specification attention and lane context capacity.

Published lane capacity is advisory, not an automatic stop. Its occupancy
target can be a pre-emption guard instead of measured saturation. Before
treating a headroom value as a reason to hold work, inspect what it was derived
from and whether the pressure it guards against has actually been observed.

## Dispatch lifecycle, read views, and integrated gates

Once the `launch` table in the skill has selected the path, its detail is as
follows. A CLI run can be resumed or stopped through `reckon crew resume` and
`reckon crew stop`. An in-harness run has no spawned process for those commands:
continue, answer, or cancel through its attached harness task or session. A
fresh coordinator inheriting live work starts with `reckon crew recover`, not a
redispatch. The recovery report classifies each pointer and names the next
action. A harness never adds a third launch kind, and backend flags never go
into a worker prompt.

After a worker's commit is integrated, run its own gate at the integrated head:

```bash
reckon crew verify-gate --project <project> --run <run-id> \
  --checkout-path <the merged tree> [--revision HEAD]
```

The command reads the committed gate and base verdict, reruns it at the tree
that will ship, and records `integrated_gate_check` in the ledger. It refuses a
non-integrated tree; a missing command or an overrun reports `not-run`, not
`passed`. A passing worker gate only proves its worktree's base, not a merge
that gained another change after that base. Fold worker streams with
`reckon crew observe --run <id>` before promotion; it records phase, captured
session, and possibly an `unknown` budget signal. Unknown is not exhaustion.

`crew` is the agent-facing read surface. Use `live` for a fleet's current
processes and recover classifications, `scopes` for ownership conflicts,
`drain` for the closing count, `ledger` or `records` for committed outcomes,
`summary` for roster and gate aggregates, `flight` for resolved routing,
`budget` for headroom, `lanes` for backend identities, and `directory` before
contacting another coordinator. The corresponding CLI is for state-changing
verbs. A cross-repository finding is sent as a finding rather than an
instruction or an authority transfer. `phase` can lag its stream, whereas
process liveness and log age remain fresher; `observe` is also what captures
token usage.

## Follower mechanics and stalled-fleet recovery

Run state is pull-only, so producer and follower are distinct. One project
producer turns live-pointer changes into transitions; one session follower
delivers those transitions to its coordinator. The follower is the monitor,
not redundant decoration. To observe peers, add their sessions with
`--observe-session` to that one follower. Observing does not register the
named peer and does not substitute for its own delivery channel.

When the observed set changes, arm a replacement with the fuller flags and then
stop the old follower. The replacement reads until the first registration is
released, then takes it over. The counts at the end of a follower line describe
the project fleet rather than the selected session; use `crew(project,
view="live")` and count matching sessions for a session-local inventory.

The producer's `watcher_live` or `seat_held` only proves that a producer exists.
`session_attached` proves that the current coordinator receives lines. Arm the
payload's `attach_line` before the first CLI dispatch. If the refusal names a
missing follower, use that line; if it names a missing project producer, run
`reckon crew watch --ensure --project P`. The synchronous `--no-watch` waiver
records why no session delivery exists.

The host harness owns how a line-producing follower is attached. Read
`orchestrator-harness/<harness>.md` before arming it. The command produces
lines rather than an exit, so an exit-only background primitive, output-file
polling, or a shell filter loses delivery. Use the bare line with colour; do
not pass `--no-color` because colour carries the row state.

Silence after a drain is ambiguous: transitions do not announce their own
absence. When transitions stop, query `crew(project, view="live")`; look for
zero `working` alongside non-zero `blocked` or `unpromoted`, and compare
`process_alive` with `manifest_status`. A non-terminal pointer whose process is
gone cannot announce itself. The follower mechanics, row buckets, and flag
variants remain in `sprint-orchestration.md` §15.

One arming lasts for the session, not only one wave. A second producer exits as
already live; `--once` releases a seat and therefore needs a later re-arm.
Use `reckon crew unwatch --project <project>` only to replace a producer, never
to quiet active work. The live classifier turns a complete manifest into
`completed_unpromoted`, keeps `blocked` and `failed`, and calls a missing
terminal manifest `abandoned`; none of those classifications proves its gate.
Reconcile an old complete or blocked pointer before new work, or explicitly
record the deliberate waiver on the new run.

## Verification depth and incomplete worker reports

Verification begins with a manifest and ends with a scoped review; it is not a
trust decision based on a green worker-owned test. Read the manifest returned
by dispatch, compare its mtime with dispatch time, confirm its commit is the
worktree head, require a clean worktree, check `git show --stat` against the
exclusive scope, and open the named gate log before reading the diff by anomaly.
The `worker-verification.md` reference gives the four measured gate shapes and
the review depth rules.

A provider-refusal manifest is not automatically a worker failure. Inspect its
worktree and resume its same session while the live pointer exists; promotion
removes the pointer needed for resume. An idle worker without a report is also
not automatically failed: inspect its manifest path, logs, and artifacts, then
ask for the named delivery before redispatching. If work is genuinely incomplete
inside its original scope, dispatch a corrective worker and do not release a
dependent node. Provider recovery details are in `outage-recovery.md`.

## Handoff prose and the attention budget

The fixed-read budget protects the text every coordinator holds before it knows
which exceptional circumstance applies. Conditional material belongs in this
file and is read at the named decision point; the core skill retains the rules,
fences, launch table, and closure procedure. The budget is a word-count proxy
for attention rather than a tokeniser limit. When it binds, extract explanatory
or worked material first; do not shave a rule merely to accommodate prose.

For a cross-plan handoff, the final report gives the human plan and section,
then one fenced slash invocation per justified exempt row. The invocation is
the complete persisted prompt because the live plan owns context. Do not make a
handoff for work that the current session could fold, and omit the whole block
when the drain closed with no exempt continuation.
