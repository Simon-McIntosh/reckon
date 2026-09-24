# Effort routing — matching worker capacity to specification completeness

Policy for routing by specification level. The mechanics: the coordinator
declares one dial per dispatch — `--spec-level exact|guided|open` — and flight
config resolves the backend, model, provider-effort and time budget underneath
it through `roles.<role>.by_spec_level` (locked decision `routing-mode:
config-mapped` on the crew-effort-routing plan). A `--set` override remains the
per-dispatch escape hatch: the override layer rewrites the mapping itself, so
it still wins. The run record attributes the outcome to the exact configuration
that ran, and flight config owns every model and effort identifier. This
reference owns the decision procedure — what level to declare, and what mapping
to put in config.

## The principle

A node's specification level names **who owns the design** — and effort pays
for reasoning, so route effort to wherever the reasoning still lives:

| Declared level | The plan section provides | The worker owns |
|---|---|---|
| `exact` | The prescribed change: files, snippets or exact steps, and the named check | Transcription and verification. A prescription failing its own check is a blocker to report, not a licence to redesign. |
| `guided` | The fixed design: interfaces, invariants, where things live, and the measure | Deriving the implementation within that design. |
| `open` | The goal, constraints, and the measure | The design and the implementation. |

**Choose the level first, by ownership — never inflate it to enable a cheaper
worker.** If the design is genuinely settled, write it exactly and route down.
If it is not, do not spend coordinator tokens writing code into the plan:
declare `guided` or `open` and route the reasoning to the worker. An exact
spec written for a high-effort worker is the same reasoning paid for twice;
an exact spec only amortises when several dispatches share it.

Declare the level on the dispatch (`--spec-level exact|guided|open`) so the
ledger can test this table against outcomes.

## The mapping (live measurements)

The selected rows go into `roles.<role>.by_spec_level` in a flight layer; the
coordinator no longer applies them by hand on each call. Read them from
`crew(project, view="routing")`. That call groups every mounted committed
ledger by model, effort, specification level and role, and returns the sample
depth, pass, rework and redispatch rates, median tool steps, median input, and
both worker-only and worker-plus-coordinator cost.

The call deliberately reports immediate per-run spend beside rework-charged
cost per durable node. A short observation window can reflect the former while
the latter is still back-loaded, so do not change a mapping merely because its
first few hours show the immediate column rising. The durable column is the
routing measure; the pass column remains a safety signal rather than the cost
ranking.

At a calibration checkpoint, compare rows with the same role and specification
level. Prefer the configuration with the lowest worker-plus-coordinator cost
per durable node once its sample depth is adequate for the pilot being judged.
Keep the incumbent when coordinator spend is unknown, the durable figure has
not matured, or the candidate has not yet produced live rework evidence. A node
the mapping routes somewhere its gate forbids is a config bug, regardless of
the cost ordering.

## The small-model lane — qualified, live-mapped, still gated

The small-model backend now has a live mapping of its own; the local lane
routes on a measured record instead of waiting unused. It earned the mapping
the ordinary way — shadow pairs, a gated pilot, a slice lock — and the mapping
outlives the pilot. The mapping behind `roles.<role>.by_spec_level` for a `--local` dispatch,
read as a dated snapshot:

| Declared level | Routing decision | Snapshot (dated 2026-09-05) |
|---|---|---|
| `exact` | `deepseek-v4-flash` — qualified | 42 passed of 44 runs |
| `guided` | `deepseek-v4-flash` — permitted only with the measurement handed over | 30 passed of 38 runs |
| `open` | never routed — the lane takes no open design | zero runs |

`glm-5.3` is explicitly NOT routed — its snapshot reads 0 passed of 9 runs,
every one a window-overflow death on nodes that did not fit its 73,728-token
budget, so its ledger record indicts the node-to-lane pairing, not the model.
It stays defined on the host layer for shadow qualification only.

**Every figure above is a snapshot, not a standing fact.** The pass count and
the sample size both move as dispatch volume accumulates — later local-lane
runs land in the same ledgers and change each. Re-derive the rows at a
calibration checkpoint; do not read them as current. The recipe, so a reader
can reproduce or update the figures without a guess:

- **Ledger.** The mounted committed run ledgers — `docs/state/<project>/crew.json`
  under every mounted project's `docs/` directory, the same source
  `crew(project, view="routing")` reads (the mounted set comes from
  mounts.json). The lane's runs appear across every mounted project, not only
  in the repo that owns the lane, so reading one repo's ledger alone
  understates the sample.
- **Filter.** Keep runs whose `agent.model` is the lane's backend identifier
  (`deepseek-v4-flash` for the routed rows, `glm-5.3` for the exclusion row)
  AND whose `spec_level` equals the declared level of the row. A run passes
  iff its `gate` is exactly `"passed"`; the run count is every run the filter
  keeps, whatever the gate outcome.
- **Deduplication.** Each committed ledger run counts once. The ledger keys
  runs by `run_id` and a re-dispatch appends a new run record under a new
  `run_id` rather than overwriting the earlier attempt, so repeated attempts
  on one node each count — in the 2026-09-05 snapshot the 44 exact-tier runs
  trace to 43 distinct nodes because exactly one node was re-dispatched as a
  second run. Without this rule a twice-attempted node reads as one node or
  as two runs at the reader's choice.

The lane buys speed and budget on work whose correctness is already pinned —
never a way to write uncertain code quickly. Routing a node to it is permitted
only when **all** conditions below hold; a candidate that has not yet earned
the mapping still climbs the ladder by the same gate.

1. Declared level is `exact`, and the plan section actually prescribes the
   change with a named check — or `guided` with the measurement handed over
   as a numeric bound or named check the worker can verify against its own
   work. A declaration the section does not support is a specification bug to
   fix before dispatch.
2. The done-when is a runnable named check — a test command, a compared
   command output, or a numeric bound. Prose measures disqualify.
3. Node estimate ≤ 0.5 worker-hours until the lane's configuration has its
   own measured competence horizon; thereafter the competence gate governs.
4. No `--requires-decision` keys.
5. Mechanical roles only (`implement`, `test`, `documentation`) — never
   `review` or `investigate`.
6. **Pilot audit:** while the lane's configuration has fewer than twenty
   ledger runs, audit the full diff, not only the manifest and gate.
7. **Circuit breaker:** two consecutive lane gate failures close the lane
   until the failing specifications are re-examined. A gate failure on an
   exact-tier node indicts the specification before the model.

## Failure classification — gate failures versus infrastructure failures

A run that dies mid-stream with **no diff and no manifest** — a malformed
tool call, a truncated stream, a launcher error — is an **infrastructure
failure**, not a gate failure. Redispatch it without indicting the
specification, and do not count it toward the lane's two-consecutive-failure
circuit breaker: the breaker exists to catch specifications (or a model)
producing *wrong work*, and a run that produced *no work* is evidence about
the transport, not the tier. A gate failure — the named check ran against a
delivered diff and failed — always counts.

## Small-model budget discipline

A small-model lane may carry its **own dedicated rate meter**, far smaller
than the default backend's. Measured 2026-08-25: eight spark runs (~41M input
tokens) consumed ~60% of that model's weekly meter while 845 default-backend
runs consumed ~40% of theirs — a lane capacity of roughly a dozen runs/week
at that profile. Consequences:

- `budget_check: true` on any backend with a dedicated meter, so pre-flight
  reads real headroom instead of `unknown`.
- Keep lane dispatches token-lean: the targeted named check, not the full
  suite, unless the plan demands it; small file scopes.
- The lane buys latency and default-meter relief — never fleet share. Do not
  plan a wave that assumes the lane can absorb it.

## Effort must be pinned, and sessions must not cross models

With no effort flag, the codex CLI inherits `model_reasoning_effort` from the
user's interactive `~/.codex/config.toml` — so an unpinned backend's effective
effort **drifts with the user's interactive preference** and the ledger cannot
see it. Every backend in flight config pins `effort` explicitly. When
adopting a tier that ran unpinned, pin the value its audited runs actually
executed at (read the rollout `turn_context`), so the calibration slice does
not fork.

Session reuse has the same trap in the other direction: resuming a member's
session recorded by a *different model* drags that model's whole history into
the new model's context window (measured: one spark run resuming a ten-turn
sol session cost 22.9M input tokens in a two-minute run). Reuse a session
only when the recorded model matches the dispatch's resolved model.

## Onboarding a new model — the qualification ladder

New identifiers appear on the account without notice; nothing in reckon
source ever names one. The ladder, cheapest step first, each gating the next:

1. **Probe** (worker node, minutes): one trivial read-only exec per candidate
   identifier, bare and with an explicit effort; record the exact
   accept/reject responses as a run artifact.
2. **Define, don't default**: an accepted identifier enters the *host* flight
   layer as a backend with pinned effort and `budget_check: true`. No role
   and no `by_spec_level` overlay selects it.
3. **Shadow qualification**: sampled shadow runs (`reckon crew shadow`) pair
   the candidate against live nodes' primaries — same node, same base sha,
   same named check, never merged. A tier qualifies for a pilot after at least
   ten usable controlled pairs establish its tool steps, input and gate
   agreement. Shadow work cannot establish rework because it is never merged.
4. **Gated pilot**: live routing under the small-model lane gate above (or a
   role overlay for a full-size tier), first twenty runs full-diff audited.
5. **Slice-lock**: the calibration checkpoint reads
   `crew(project, view="routing")` and locks the winner into
   `roles.<role>.by_spec_level` per the decision rule — the lowest measured
   worker-plus-coordinator cost per durable node on the matching live pilot
   slice, with redispatch and rework charged rather than treated as free.

Shadow evidence qualifies a tier for its pilot; **only live gated evidence
opens routing**. Layer placement: measured cross-project defaults live in the
host layer; a project layer carries only its deviations (e.g. a pilot pin);
the shipped layer stays provider-neutral, always.
