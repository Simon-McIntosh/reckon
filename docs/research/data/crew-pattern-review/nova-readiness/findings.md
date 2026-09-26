# Nova readiness at weekly snapshots

The result is **partial**: collection readiness improves at the captured head, lint debt rises, and there is no measured full-suite pass-rate trajectory. All six pinned trees were extracted with `git archive` and attempted on the same fleet CPU node. Five runs ended during collection. The captured head selected 7,273 tests but reached the census instrument's 1,500-second total-process bound before a terminal pytest summary. This is unfinished measurement due to an instrument time cap, not an environment-caused collection error or a passing lane.

The six written dates are Saturdays in 2026. They were used as instructed by the coordinator, despite “Friday” in the plan's prose. The pinned head is `16287918db2f90d0088c1786ec45965cd90e6652`; it also represents September 26. [Snapshot selection](snapshots.json) preserves the candidate commits and distances from noon. [Readiness data](readiness.json) has seven rows backed by six executions, with the head row explicitly aliased to September 26.

| Snapshot | Commit | Collected | Selected | Passed | Failed | Errored | Skipped | Xfailed | Ruff | TODO / FIXME | Xfail / skip markers | Python files |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-08-22 | ddef98a75c | 6365 | 5612 | 0 | 0 | 1 | 2 | 0 | 95 | 21 / 0 | 3 / 59 | 711 |
| 2026-08-29 | c650aaf6e9 | 6840 | 6044 | 0 | 0 | 10 | 2 | 0 | 95 | 21 / 0 | 3 / 62 | 789 |
| 2026-09-05 | e247cff66a | 7190 | 6385 | 0 | 0 | 10 | 2 | 0 | 95 | 21 / 0 | 3 / 66 | 849 |
| 2026-09-12 | 9062b6f121 | 7566 | 6732 | 0 | 0 | 10 | 2 | 0 | 100 | 21 / 0 | 4 / 84 | 910 |
| 2026-09-19 | c11eea7f11 | 7806 | 6952 | 0 | 0 | 10 | 2 | 0 | 121 | 21 / 0 | 4 / 88 | 938 |
| 2026-09-26 | 16287918db | 8152 | 7273 | unknown | unknown | unknown | unknown | unknown | 201 | 21 / 0 | 4 / 93 | 983 |

The historical zero passed/failed/xfailed counts are what their terminal collection-failure summaries reported. They do **not** mean a zero-percent pass rate. The head's full outcome counts are `null` in JSON and “unknown” above. Its captured progress contains **1,204 passed and 14 failed**, or **1,218 observed terminal outcomes out of 7,273 selected items (16.7%)**. The remaining 6,055 selected items have no recorded terminal outcome. Two collection skips are known at the head; total skips and expected failures remain unknown. Complete captured output is retained, including the absence of a terminal summary; the head log explicitly records its process timeout.

Collection errors are 1, 10, 10, 10 and 10 across the five historical dates, with **zero environment-classified collection errors**. August 22 fails importing `measure_exact_section_routes` from `tests/measure_external_wall_correction.py`, even though the helper is present in that same committed tree. The head qualifies this import as `tests.measure_exact_section_routes` and gets through collection. For August 29 through September 19, five modules each generate two collection-error reports under the configured doctest collection: `tests/imas/test_diiid_circuit_driven_forward_validation.py`, `tests/imas/test_diiid_diverted_solve_overlay.py`, `tests/imas/test_diiid_solenoid_inclusion_ladder.py`, `tests/test_efit_parity_warm_neighbour.py`, and `tests/test_moment_seed.py`. Their chains all reach an import of `_separatrix` from a snapshot-local `benchmarks.diiid_forward_gs_match` that no longer exports it. These are code/import-path failures in the measured trees; they are not missing third-party dependencies.

The historical two skipped reports per snapshot name unavailable requisite IMAS IDS data in `tests/test_matrix.py`. Missing data was handled as a collection skip, separately from errors. The interrupted head has no final runtime tracebacks, so the causes of its 14 observed failures cannot be classified; its environment-caused runtime error count is unknown, not zero.

Ruff findings rise **95 → 201 (+106, +111.6%) per snapshot**, under the same Ruff configuration hash and the same installed `ruff 0.15.22`. The last week rises **121 → 201 (+80, +66.1%)**. The head's 201 findings comprise E501 104, E402 61, E702 16, E741 7, E701 5, E401 3, F841 3, F401 1 and F821 1. These are findings across each whole extracted tree under its own configuration, not a count of failing tests.

Within Python files under `nova/` and `tests/`, TODO comment markers stay **21** and FIXME markers **0** across **711 → 983 files (+38.3%)**. Xfail spellings rise **3 → 4**; skip/skipif/call spellings rise **59 → 93 (+57.6%)**, or **8.30 → 9.46 occurrences per 100 Python files**. TODO/FIXME count only Python COMMENT tokens; pytest marker spellings count textual occurrences in those Python files. Root configuration, documentation and emitted artifacts are outside this marker denominator. These spellings are not counts of selected or skipped tests and do not establish that tests were hidden. The identical pytest settings select `-m 'not slow'` with the same testpaths and doctest collection; the collection hook differs on August 22, and the evolving test population means the selected cohort is not fixed.

## Named first-to-last observations

The first snapshot selected 5,612 node IDs but executed none. Among those IDs, **1,122 are observed passed and 6 failed** at the head; **4,484 are unobserved** before interruption. Another **82 passed and 8 failed** observations were not selected at the first snapshot. The three [state observation CSVs](state-changes-1.csv), [second shard](state-changes-2.csv), and [third shard](state-changes-3.csv) retain all 5,702 rows with explicit `not_run`, `not_selected` and `unobserved` states. These are observations, not 5,702 code regressions or confirmed test-state transitions.

The six first-selected tests now observed failed are:

- `tests/calibrate/test_gain.py::test_pulses_presenting_different_field_ratios_recover_both_parameters`
- `tests/imas/test_diiid_current.py::test_banked_circuit_receipt_matches_runtime_calibration`
- `tests/imas/test_diiid_vessel_hex_mesh.py::test_authoritative_limiter_is_clipped_to_the_preregistered_area`
- `tests/test_biotcircle.py::test_a_tiling_of_sub_sections_sums_to_the_whole_section_integral`
- `tests/test_biotcircle.py::test_an_all_to_all_matrix_has_no_divergent_entry[cells]`
- `tests/test_biotcircle.py::test_the_flux_and_the_vector_potential_stay_one_quantity`

The eight tests observed failed at the head but not selected at the first snapshot are:

- `tests/test_amplification_observation.py::test_nonfinite_achieved_residual_keeps_the_state_unpromoted`
- `tests/test_amplification_observation.py::test_qualified_contracting_trajectory_has_its_own_observation`
- `tests/test_amplification_observation.py::test_sustained_growth_is_reported_without_blocking_terminal_promotion`
- `tests/test_amplification_observation.py::test_unsuccessful_solver_status_keeps_the_state_unpromoted`
- `tests/test_amplification_observation.py::test_zero_step_at_material_residual_keeps_the_state_unpromoted`
- `tests/test_batched_labeller.py::test_masked_conditioning_keeps_the_augmented_multiplier`
- `tests/test_batched_labeller.py::test_result_contains_fixed_shape_topology_fields`
- `tests/test_batched_labeller.py::test_two_elements_match_compiled_route_and_padded_batch`

A direct pass-to-fail or fail-to-pass comparison is unavailable because the first run stopped at collection. The repaired qualified import above is a separately verified collection-state improvement. No inference about a particular worker lane causing these outcomes follows from this census.

## Evidence and reproduction

All six pytest logs and six full Ruff JSON outputs fit below 300,000 bytes and are saved beside this report. Each log begins with revision, extracted tree, actual command, imported `nova.__file__`, and resolved working directory. `readiness.json` records their paths, sizes and SHA-256 hashes. The run directory holds immutable per-snapshot receipts and full collection/outcome events. [The census log](census.log) preserves the single six-snapshot execution. The snapshots used the shared Nova Python 3.14.2 environment without syncing; numerical library thread counts were one and JAX used CPU. These are historical code replays in today's environment, not reconstructions of the environments on their original dates.

Run `python docs/research/data/crew-pattern-review/nova-readiness/reconcile.py` from this checkout to reproduce `readiness.json` and the CSVs from the saved receipts and hashed logs. Reconciliation reads source/configuration from pinned Git objects and does not require the extracted `/tmp` trees. Re-executing pytest is a new experiment, so runtime outcomes and log hashes are not promised byte-for-byte deterministic. Raw receipts are under `/home/ITER/mcintos/.config/reckon/crew/runs/r-20260926T105233192444-nova-readiness-snapshots/parts/`.

The inherited instrument was reproduced failing before pytest launch with `ValueError: stdout and stderr arguments may not be used with capture_output.` Output redirection was corrected before the census ran. A controlled run of the same timeout hook with a one-second bound emitted the expected timeout and exactly 2 failed, 1 passed, 1 skipped and 1 xfailed outcomes. A separate marker fixture proved the counter sees known TODO/FIXME and pytest markers, and an interrupted-log control proved unknown totals stay null. The census used the committed hook's 120-second per-test bound. No mutation of Nova was used as a negative control.

The next measurement is a **complete run of the captured head with the identical default selection and 120-second per-test timeout, under a larger explicit whole-run budget**. The 25-minute limit was inherited from the instrument; it is not the node's 55-minute ceiling. A second full invocation would consume nearly all remaining node time even at the already-insufficient cap, so it was not started. Alternatively, a coordinator can authorize a revised execution strategy that preserves the full selected population. Until then, retain this partial result and do not close the full readiness measure. Runtime failures and any Nova fast-lane repairs are outside this node's write scope.
