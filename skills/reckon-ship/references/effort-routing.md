# Effort routing — matching worker capacity to specification completeness

Advisory policy for the coordinator choosing per-node routing overrides. The
mechanics are unchanged: routing is always an explicit `--set` override on the
dispatch call, the run record attributes the outcome to the exact
configuration that ran, and flight config owns every model and effort
identifier. This reference owns only the decision procedure.

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

## Advisory routing (initial priors — the ledger calibrates them)

| Declared level | First-choice routing | Why |
|---|---|---|
| `exact` | small-model lane via its gate below, else the default backend at `--set backends.<name>.effort=medium` | The reasoning is already in the spec. |
| `guided` | `medium` for small nodes, `high` above ~1 worker-hour | Implementation reasoning remains; design reasoning does not. |
| `open` | `high`; `xhigh` for cross-cutting single-owner nodes | The worker carries design and implementation. |

Read the current slice results before trusting these rows: the committed
ledger records pass rate, worker minutes, tokens and redispatch lineage per
(configuration × declared level). The sweet spot for a slice is the cheapest
configuration whose pass rate stays at or above the capabilities success
threshold over at least ten usable runs, charged for its redispatches.

## The small-model lane — eligibility gate

Routing a node to a small-model backend (e.g. `--set
roles.implement.backend=codex-spark` where the project flight layer defines
it) is permitted only when **all** hold. The lane buys speed and budget on
work whose correctness is already pinned — never a way to write uncertain
code quickly.

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
