---
name: reckon-build
description: >-
  Execute a complete Reckon plan, or coordinate an entire sprint, without doing
  implementation inline. Resolves a plan slug with an optional section,
  `/reckon-build S1`, a project-qualified sprint id, a bare graph handle, and
  `graph:<handle>`. All targets are strictly
  coordinator-only, a one-node plan included: build the execution DAG, delegate
  every implementation, investigation, test, pipeline, and repair node through
  isolated worktrees by default, audit and integrate worker commits, record
  outcomes continuously, and clean up worktrees. Trigger verbs: "implement /
  execute / ship / land / deliver the sprint / run the sprint / /reckon-build".
  Requests to use local workers, local agents, or local dispatch name the
  lane with `--local` on every dispatch, selecting the backend named by
  `local_backend`.
  For editing plan text use reckon-edit; for defining or rebalancing sprint
  state use reckon-sprint.
allowed-tools: Read Write Edit Bash(*) Grep Agent mcp__reckon__read_plan mcp__reckon__edit_plan mcp__reckon__roadmap mcp__reckon__audit mcp__reckon__crew
---

# reckon-build — the coordinator's loop

You coordinate; you do not implement. This core is the six-step loop, the
refusals the tool raises, and the three fences. The former skill body is kept
whole at [references/coordinator-reference.md](references/coordinator-reference.md)
for the detailed mechanics.

## Three targets, one contract

`/reckon-build <slug>` ships the whole plan; `/reckon-build <slug> [§N]` ships
one section. `/reckon-build S1` ships the current project's sprint and
`/reckon-build <project>:S1` names the project; use `plan:<slug>` and
`sprint:<id>` only to disambiguate unusual identifiers. `/reckon-build <handle>`
ships the derived cross-project closure an endpoint carries, and
`/reckon-build graph:<handle>` is the unambiguous long form; handles match
`[A-Za-z0-9][A-Za-z0-9._-]*`. Only the handle is authored on the endpoint —
membership, shipped-of-total, critical path and average width are derived live.
A token no live endpoint claims as its handle remains a single-plan target.

Call `roadmap(project=<id>)` for a sprint, and
`roadmap(project="graph:<handle>", view="raw")` for a graph, as the canonical
plan-level graph; do not rebuild dependency order from repeated discovery
calls. Report `schedule_override.deferred` and its members before dispatch
rather than silently treating them as schedule-ready; report `judgment_required`
and its members beside it, and decide each — build or un-defer the section, or
narrow or remove the dependency — rather than dispatching past it.

**All targets are coordinator-only, a one-node plan included.** The coordinator
resolves and reads state, reads code for design, checkpoints the DAG and scopes,
creates worktrees, dispatches and messages workers, audits evidence, integrates
and pushes, writes shared plan state, cleans up, and reports. It MUST NOT write
product source or tests, run the executable pipeline, or repair worker code:
every implementation, investigation, test execution, operational run and
corrective repair is a worker node, even when only one item is ready. A small
plan is not an exception — isolation, independent review, a manifest and a
ledger record hold for one node too. Inline fallback is the reported exception
only when no capable worker backend exists; member scarcity never qualifies.
Detail: `crew(project, view="live")`, `crew(project, view="scopes")`,
`crew(project, view="drain")`, `crew(project, view="ledger")`,
`crew(project, view="summary")`, `crew(project, view="flight")`,
`crew(project, view="records")`, `crew(project, view="budget")`,
`crew(project, view="lanes")`; also `crew(view="directory")` and
`crew(view="obligations", session=<session>)`.

The loop has six steps, in order.

## Read

Read the plan, the code it names and the prior evidence. Call `roadmap(project)`
first and require the target in `ready_now`; resolve every error-level wiring
finding before dispatch. Read the COMPLETE plan HTML from disk — every section,
decision, follow-up, `plan-depends-on` and deferral marker.

```python
state = read_plan(resource={"project": "<project>", "type": "plan",
                            "id": "<slug>"}, view="raw")
contract = read_plan(resource={"project": "<project>", "type": "plan",
                               "id": "<slug>"}, view="schema")
```

Classify every section against the code as implementable, deferred, or already
done, then **Persist that DAG-build classification before dispatch** with one
version-safe `edit_plan` write to `section_declarations`, mapping every authored
section id to `implementable`, `deferred`, or `done`; re-read and require the
field to match the audit, because omitting it means the remainder is unknown,
never zero. Audit plan currency against the code before cutting nodes. Read
`view="summary"` before `view="raw"`; `raw` is for editing.

## Review the design

A plan owes one design review — the prior-art and depth search of the codebase
for machinery it could reuse — before its first implementation node, and the
dispatch gate refuses an implementation node until one exists, at any version,
with every finding answered. Compose it once per plan:

```bash
reckon crew review-plan --project <project> --plan <slug> --rubric design --session <session>
```

Read it back with `crew(project, view="plan-review", plan=<slug>)`. The reuse
map is its output, and every node's brief cites it. The gate also needs a stored
review covering the content about to be built. The author's content review
usually supplies it, and a section edited since stays covered when the
review-need judge finds its change needs no new review. A refusal names the
exact command it is waiting for.

Answer every finding before dispatching, acted on or declined with a reason:
`reckon crew review-plan --project <project> --plan <slug> --answer <finding> --acted`,
or `--declined "<reason>"` in place of `--acted`. **Answer rather than edit.**
Edit the plan only for findings about the sections you are about to build;
answer a finding an existing section already meets `--acted`, naming it, and
one on a landed section `--declined` with the honest reason; then dispatch at
once. A landed section keeps the coverage it had, so land a covered wave before
composing the next review rather than interleaving landings and reviews.

The plan is the passing surface: work that changes a plan's product carries its
plan, and findings go record, commit, dispatch, in that order — a worker reads
its section from its own worktree at the base revision. A probe, a measurement,
a review or an investigation may instead carry a **brief**:
`reckon crew dispatch --brief <file>` names a stored file as the node's
authority in place of `--plan` and `--section`, meeting the same eight node
properties. Large inputs travel by reference. The flags carry only what cannot
live in a plan or brief.

## Shape the wave

The unit of dispatch is the section's deliverable. A section is one node unless
two of its write scopes must run in parallel; list exclusive write paths per
item, use isolated worktrees by default, and never dispatch two workers that
write the same file. Each node carries a brief citing the reuse map: the module
to extend, the interface not to widen, the question the worker may not settle.

A node is well-formed BEFORE it is sent. Single goal, Fully specified,
Demonstrable, Closed, Scoped, Bounded and Independently verifiable all hold, and
the specification level is declared. `reckon crew dispatch --dry-run` enforces
the same checks and exits 2 naming every failing property. `reckon crew
dispatch` needs nothing installed in the dispatched repository; commit the plan
before dispatching. A goal containing `;` is not one deliverable, and every node
needs at least one `--write-path`. Every refusal answers with a JSON document on
stdout carrying `error` and `detail`.

Dispatch ensures one live producer before creating a worktree. Peer scopes come
from live pointers in the same project and repository: dispatch refuses
containing or contained path claims before creating a worktree and names the
owning run. `--peer <other-node>=<their-paths>` is optional, to supplement peers
with no live pointer yet. `--no-watch` is the explicit exception for a
synchronous one-off; it records the arming command, the watcher liveness and the
unattached session on the live run and its promoted ledger record. Use
`scripts/worktree_fleet.py` for deterministic worktree creation and cleanup.

Cleanup is conservative: remove a worktree only after it is clean and its commit
is reachable from the integrated primary branch.

#### Locally served worker routing

Do not dispatch background work to an orchestrator lane: it runs the
orchestrators; background work there costs orchestrator capacity, and
saturating it stops every session rather than one node. Use the
[lane-routing reference](references/lane-routing.md) before selecting a lane.

The lane is the picker's to choose: a dispatch that names no lane is routed by the picker, which picks the family and model or holds the node, and a lane a coordinator names is never overridden. Naming a lane is the exception, not the default — a request to use local workers, an exact declared level, a constrained metered lane, or a node that needs no decision may name `--local`, which selects the backend named by `local_backend` and refuses when it is unset; it costs no metered quota. Reviews stay off metered lanes through `review_excluded_backends`, so an unnamed review runs locally or holds. The context-fit refusal rejects a node exceeding the lane's window before
a worktree exists, rather than the node dying mid-run. The node still gets a
worktree, a manifest, a gate and a ledger record.

#### Refusals the tool raises

| Failure | Exit | Remedy |
|---|---:|---|
| `success` | 0 | Continue with the returned launch contract. |
| `request-error` | 1 | Correct malformed options and retry. |
| `not-dispatchable` | 2 | Repair the node contract named in `validation`. |
| `budget-hold` | 3 | Keep the node ready; retry after the reported reset. |
| `plan-unavailable` | 4 | Commit the plan before dispatching. |
| `competence-refusal` | 5 | Route the node to a backend meeting the capability. |
| `unreconciled-runs` | 6 | Reconcile each reported run, or set the waiver. |
| `scope-conflict` | 7 | Wait for the owning run to release its path claim. |
| `watcher-required` | 8 | Arm the `attach_line`; use `--no-watch` only deliberately. |
| `member-in-flight` | 9 | Wait for the named run to reach a terminal phase. |

Read the JSON document rather than inferring a refusal from an empty stream.
Remedies, the carrier rules and the `--brief` contract are in
[references/worker-protocol.md](references/worker-protocol.md).

#### The crew surface — MCP owns reads, the CLI owns actions

```text
reckon crew attach --run <id> --task <task-id>
reckon crew check-manifest --run <id>
reckon crew complete --run <id> --gate <verdict> --commit <sha> --outcome <text> --tests-added <n> --scope-changed
reckon crew directory --project <project> --run <id> --node <node>
reckon crew dispatch --project <project> --plan <slug> --section <section> --role <role> --node <node> --goal <goal> --done-when <measure> --write-path <path> --peer <other-node>=<their-paths> --time-budget <duration> --session <session> --set <override> --dry-run --member <member> --manifest <path> --allow-unreconciled-runs --no-watch
reckon crew discard --run <id>    reckon crew drain --project <project> --leave <id>=<disposition>
reckon crew follow --project <project>    reckon crew gc    reckon crew ledger --project <project> --view <view>
reckon crew gate (--pause <reason> | --open) [--pretty]    # prints the shared gate document before and after the change
reckon crew list    reckon crew member add    reckon crew member list --project <project>
reckon crew observe --run <id>    reckon crew preflight --project <project> --role <role>
reckon crew pick --project <project> --role <role> --spec-level <level> --goal <goal> --done-when <measure> --replay <n> --outcomes --since <ISO> --all-projects --checkout-path <path>    # reads routing state or outcome history, dispatches nothing
reckon crew placement --ensure --session <session> --project <project>
reckon crew recover    reckon crew redispatch --run <id> --backend <backend> --reason <text>
reckon crew repair-status --run <id> --status <verdict> --reason <text>    # replaces a manifest's status word, keeping the file as delivered
reckon crew resume --run <id> --advice <text>    reckon crew resume-ready --project <project>
reckon crew review-plan --project <project> --plan <slug> --rubric <rubric> --session <session> --local --answer <finding> --acted --declined <reason>
reckon crew shadow    reckon crew stop    reckon crew unwatch --project <project>
reckon crew verify-gate --project <project> --run <id> --checkout-path <path>
reckon crew watch --project <project> --stall-window <duration>
reckon crew widen --run <id> --write-path <path>
reckon flight --project <project>    reckon audit-doc    reckon crew suite
```

#### Dispatch, then branch once

`reckon crew dispatch` is one call for every backend: state what the node is;
reckon resolves how it runs. Branch once, on the returned `launch` kind:

| `launch` | What you do |
|---|---|
| `cli` | run it in the foreground, then `reckon crew observe` |
| `in-harness` | dispatch your own delegation primitive, then `reckon crew attach --run <id> --task <task-id>` |

An in-harness run has no spawned process; continue, answer or cancel it through
the attached harness task/session. Never dispatch a member that already owns a
non-terminal live pointer, and never treat an absent budget signal as
exhaustion.

## Decide between nodes

At each landing read the diff for design, not only for scope: a new helper
beside an owner, a wrapper over a mechanism that should have been extended, a
widened surface. Then choose exactly one of three: accept and record; re-brief
the next node; or re-plan the section and say so in the plan. Nothing is
dispatched on an unverified or undecided landing. Treat a provider refusal or an
idle worker without a report as a resume, not a failure: inspect its recorded
deliverables first.

## Integrate

Immediately after EACH `reckon crew complete`, review the worker's authored
record and merge it: the landing is a review of an authored record, not a
transcription of a manifest — read it, and may edit or append to it. Workers
author their own landing record in the same beat: Write your landing record to
your own fragment path and never to the plan: your evidence anchor to your
scope's fragment under docs/evidence/fragments/<plan>/<node-id>.html, and
exactly one `landing:` line in your manifest in place of a plan edit. The
fragment goes into your final commit; promotion lands the `landing:` line on
your plan section. Never mutate the shared project index, sprint state, or a
plan other than the one you are landing against. Do not edit the plan-version or
plan-modified meta lines: every worker touching them makes every merge conflict
there. Dispatching an unrelated ready node is outside this freeze.

The landing beat ticks a section rather than collapsing it, and leaves the
section body as authored: the tick records completion without moving the design
out of the page. It is three `edit_plan` ops, not a hand edit:
`{op:'append_evidence', plan, anchor, title, body}` writes the node's anchored
record into the cumulative landing record; `{op:'set', path, value}` sets the
section's declaration to `done`; and `{op:'append', target, item}` writes a
`c-close-<section>` comment on that section carrying the result and a link to the
evidence, all in one call. Nothing resolves a link inside a comment body, so
write the project-absolute href
`/<project>/evidence/archive/<plan>-landed.html#<anchor>` into the comment, with
the `anchor` that `append_evidence` writes.

```python
edit_plan(project="<project>", slug="<slug>", ops=[
  {"op": "set", "path": "impl", "value": <completed> / <total>},
  {"op": "set", "path": "commits", "value": <...sha appended once>},
  {"op": "append", "target": "comments", "section": "<section-id>",
   "item": {"id": "c-<ts>", "who": "reckon-build", "when": "<iso-now>",
            "body": "gate <gate-name> <verdict>; section <closure-state>"}}],
  expected_version=state["version"])
```

`impl` = count of completed executable nodes / count of total executable nodes
over the whole plan. Set it on EVERY node landing; the server does not compute
it. After ticking a section, its `section_declarations` entry reads `done`; a
section comment records a landed node, not completion, so a node that
does not close its section leaves that followup open — never wait for section
closure to record earlier nodes. A project's declared suite holds a lighter
promotion: `reckon crew suite run --project <project>` runs it under its budget
and records the result, and `reckon crew suite waive --project <project>
--reason "<why>"` records the waiver that lifts the hold.

<!-- landing-beat-examples -->
```json
[
  {"op": "append_evidence", "plan": "my-plan", "anchor": "s2",
   "title": "§2 — data prep pipeline landed",
   "body": "<p>Built <code>src/data_prep.py</code>; 11,237 shots in 3h12m; eval MAE 0.04.</p>"},
  {"op": "set", "path": "section_declarations.s2", "value": "done"},
  {"op": "append", "target": "comments", "section": "s2",
   "item": {"id": "c-close-s2", "who": "reckon-build", "when": "2026-06-24T00:00:00Z",
     "body": "<p>§2 landed: built <code>src/data_prep.py</code> (commit <code>abc1234</code>); 11,237 shots encoded in 3h12m; eval MAE 0.04. <a href=\"/my-project/evidence/archive/my-plan-landed.html#s2\">evidence</a></p>"}}
]
```

If the node also closes its section, include the driving-followup resolution in
the same `edit_plan` call and tick it too: append the evidence, set the
declaration to `done`, and append the `c-close-<section>` comment.

### 5. Verify every worker

Verify each finished worker before integration or releasing a dependent node:
read its manifest, confirm its commit and a clean worktree, compare `git show
--stat` with declared scope, open the named gate log, then read the diff by
anomaly. Fold final stream state with `reckon crew observe --run <id>` and
promote only with `reckon crew complete --run <id> --gate <verdict> --commit
<sha>` after the evidence is coherent. The live classifier reads the manifest's
recorded status. Dispatch a test worker and audit its compact result manifest
before releasing a dependent node — the implementing worker's own gate is not
independent evidence. Read the obligations view,
`crew(view="obligations", session=<session>)`, as part of this and work it to
empty, or acknowledge each item with `reckon crew ack`, before the turn ends; a
non-terminal in-flight run with no process is not a completed fleet.

### 5b. Read what the worker produced

A passing gate the implementing worker wrote and ran against its own diff
measures internal consistency, not correctness, so every landing is read before
integration and release. Run the cheap checks in yield-per-second order, then
read the diff by anomaly at a depth set by blast radius × mechanicalness. The
practice lives in [references/worker-verification.md](references/worker-verification.md).

## Close

Close the section when its deliverable is in the tree, not when its last sliver
promoted. A worker does not close its own node: it must not resolve its own
driving followup and must not set a terminal status, because only the
coordinator observes the other nodes — a worker knows its node landed, not
whether the section closed. Re-triage open followups after every landing beat;
Manifest `follow_ons` enter the same triage loop as open plan followups. Folding
is the default: Same-plan follow-on work becomes a section, never a followup.
Re-triage until a complete pass finds nothing foldable — a fixed pass count is
not the termination condition. Continuation closes at THREE altitudes: classify
every `follow_ons` entry, keep same-plan sections active on the plan, and report
the sprint altitude from `feeds_sprints` rather than from memory.

An exempt open followup records which exemption it claims: `authority-required`
(spend, an outward-facing effect, or an irreversible action), `dissent-reopen`
(asks to reopen a locked decision), or `foreign-owner` (work belonging to a
different plan or repository). Do not set `shipped` or `done` while a foldable
followup is open. Every stored followup prompt and user-facing handoff is one line:
`/reckon-build <slug> [§n]`. The live plan owns all semantic guidance. Report
what changed, what remains and why.

## One producer for the project, one follower for your session

A producer turns project pointer changes into transitions; a follower delivers
this session's transitions. That producer is not your wake-up.

**Session-host delivery comes first; arming the follower by hand is the
fallback.** Where a session host runs — on Claude Code, with the
`reckon-crew-host` plugin linked — dispatch arms the follower for you: the
payload's `watch.delivery` reads `host`, and **nothing re-arms it**, because the host
holds the follower for the life of the session. A second arming there would
double-deliver, so do not open one: confirm `session_attached`, not merely
producer liveness, and let the host's delivery stand.

**Where no session host delivers the follower, arm it yourself** — a Codex
session, a Claude session without the plugin, and a `-p` session, in which
Claude Code starts no plugin monitors. Arm the payload's `attach_line` —
`reckon crew follow --project P --session S` through the host's per-line
primitive named in `references/orchestrator-harness/<harness>.md` — and re-arm
when that primitive ends. The follower produces lines, not an exit; a shell must
never be used as a wake-up. The Monitor fallback's lifetime is `--lifetime 29m`, except in
a `-p` session, where the primitive is ended at ten minutes and it is
`--lifetime 9m`.

`delivery` reading `monitor` is what tells you the host did not deliver the
follower and the fallback is yours. Exactly one monitor per session. A session
arms and attaches exactly ONE monitor, one per session. A second follower on the
same session is a defect, not redundancy. To watch more than your own runs, name
them on the one follower with `--observe-session`. Never arm a second follower.
See `references/sprint-orchestration.md` §17.

## Concurrency — the roster is the whole authority

There is no slot pool and no numeric worker cap anywhere in Reckon. free members
are the only ceiling: per-member serialisation refuses a member that already
owns a non-terminal live pointer. Every dispatch gets a disposable worker, and
concurrency comes from the members a project already has: reuse them, register
none. Where ready nodes outnumber free members, the wave waits; redispatch each
member as soon as its finished node is verified, because no dependent node
builds on unverified work. Do not wait for the slowest active node.

## Advisory fleet-size guide

This table is advisory: it shapes the active fleet, never whether to delegate;
none of them is a slot pool.

| Items | Strategy |
|---|---|
| 1 | One worktree worker |
| 2–8 independent | Parallel worktree fleet when workers are available |
| > 8 | Reader fan-out followed by one synthesis/integration owner |
| Cross-cutting / strategic | One highest-capability worker |

## The fences and the summary reflex

### 4e. The summary reflex — what, why, how, when

Report at dispatch, at completion, when a wave is held, and when micro-planning
the next step: four lines, one per axis, at most two lines each.

```text
Dispatching wave 2 — 3 workers
WHAT   §3 dispatch primitive (impl-a) · §4 observation (impl-b) · §5 docs (impl-c)
WHY    §3 unblocks §4 and §5; all three read the §2 contract; no shared files
HOW    detached worktrees, scopes below, manifests on disk
WHEN   ~20 min each; gate g-end-to-end closes the wave — §6 stays shut until it passes
```

`WHAT` names nodes and artifacts; the `WHY` axis carries that gate's evidence —
at completion or a hold it carries the figure; `HOW` carries runtime and
isolation facts only; `WHEN` gives a duration and names the gate that closes the
wave, or the reset that lifts the hold.

### 4b. The gate fence — work does not cross a closed gate

Authored here and nowhere else. Read computed gate state for the plan through
`read_plan` and `roadmap`: use the returned `blocking` and `gate_blockers`
rather than reconstructing verdicts from prose. Read the resolved `gates.enforce`
through `crew(project, view="flight")`: strict enforcement refuses a closed gate,
while advisory enforcement records a warning. Under strict enforcement, refuse
to dispatch work behind the gate until its measure has produced evidence. Name
the gate in the `WHEN` axis before the work starts.

### 4c. The budget fence — a ready slot does not open into a spent quota

Authored here and nowhere else. Run `reckon crew preflight --project P --role
implement --role review` before opening a wave and refuse to open one on a
backend whose headroom is spent. It reads the budget signal earlier runs already
recorded, so it spends nothing. Four properties hold: a hold is not a failure
(nodes stay ready); a hold is per-backend; Unknown never holds, because absence
of a signal is not exhaustion; a hold is never silent. Resuming a held wave
without a human is a host capability documented in
`references/orchestrator-harness/<harness>.md`.

### 4d. The closure fence — a session does not end into available work

Authored here and nowhere else. The obligations view is the inbox:
`crew(view="obligations", session=<session>)` lists every duty this session still
owes; work it to empty, or acknowledge each item with `reckon crew ack --run
<run-id> --reason "<why>" --until <iso-time>`, before the turn ends. The
installed hook refuses a Stop while an item is unacknowledged. Before the closing
summary, run the followup drain, call `crew(project, view="drain")`, write all
three figures into the ledger, and read them back:

```text
rows: N   foldable-remaining: 0 unreconciled-runs: 0 executable-remaining: 0
```

A row with no disposition is an unfinished drain; a row marked `folded` with no
node id is a stop with paperwork; a session does not end while
`foldable-remaining`, `unreconciled-runs` or `executable-remaining` is nonzero.
Record a deliberate pointer remainder with `reckon crew drain --project
<project> --leave <run-id>=<disposition>` using `handed-off` or `still-working`.

## Cross-references

- [references/coordinator-reference.md](references/coordinator-reference.md) —
  the former skill body, whole, for detailed mechanics.
- [references/worker-protocol.md](references/worker-protocol.md) — the task
  contract, fences, manifest and escape hatch; read only when hand-composing.
- [references/worker-verification.md](references/worker-verification.md) — the
  untrust checks and the anomaly read.
- [references/sprint-orchestration.md](references/sprint-orchestration.md) — the
  full orchestration reference for a session composing a delegation by hand.
- [references/conditional-guidance.md](references/conditional-guidance.md) —
  dispatch lifecycle, follower mechanics, and prerequisite diagnosis.
- `references/orchestrator-harness/<harness>.md` — what the host lets the
  coordinator do: watch the fleet, wake on completion, self-schedule, and budget
  visibility. Read the one you run inside.
- `reckon-edit/SKILL.md`, `reckon-create/SKILL.md`, `reckon-status/SKILL.md`,
  `reckon-roadmap/SKILL.md` — sibling skills.
