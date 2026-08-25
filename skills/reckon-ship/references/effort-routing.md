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

## The mapping (initial priors — the ledger calibrates them)

These rows are what goes into `roles.<role>.by_spec_level` in a flight layer;
the coordinator no longer applies them by hand on each call:

| Declared level | First-choice mapping | Why |
|---|---|---|
| `exact` | the small-model backend via its gate below, else the default backend at reduced effort | The reasoning is already in the spec. |
| `guided` | reduced effort for small nodes, full effort above ~1 worker-hour | Implementation reasoning remains; design reasoning does not. |
| `open` | full effort; the top tier for cross-cutting single-owner nodes | The worker carries design and implementation. |

A node the mapping routes somewhere its gate forbids (an `open` node toward a
small model, say) is a config bug — the mapping encodes this table, and the
ledger's slices amend it at the plan's calibration checkpoints.

Read the current slice results before trusting these rows: the committed
ledger records pass rate, worker minutes, tokens and redispatch lineage per
(configuration × declared level). The sweet spot for a slice is the cheapest
configuration whose pass rate stays at or above the capabilities success
threshold over at least ten usable runs, charged for its redispatches.

## The small-model lane — eligibility gate

Routing a node to a small-model backend — whether the `by_spec_level` mapping
resolves it there or a `--set` override sends it — is permitted only when
**all** hold. The lane buys speed and budget on work whose correctness is
already pinned — never a way to write uncertain code quickly.

1. Declared level is `exact`, and the plan section actually prescribes the
   change with a named check. A declaration the section does not support is a
   specification bug to fix before dispatch.
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
   same named check, never merged. A tier qualifies for a pilot at ≥ 10
   usable shadow pairs meeting the capabilities success threshold.
4. **Gated pilot**: live routing under the small-model lane gate above (or a
   role overlay for a full-size tier), first twenty runs full-diff audited.
5. **Slice-lock**: the §6 checkpoint locks the winner into
   `roles.<role>.by_spec_level` per the decision rule — cheapest
   configuration with pass ≥ 0.8 over ≥ 10 usable runs, charged for
   redispatches.

Shadow evidence qualifies a tier for its pilot; **only live gated evidence
opens routing**. Layer placement: measured cross-project defaults live in the
host layer; a project layer carries only its deviations (e.g. a pilot pin);
the shipped layer stays provider-neutral, always.
