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
