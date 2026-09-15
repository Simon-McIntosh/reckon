# Orientation- and gate-evidence test failures are pre-existing, not added

**Question under test.** Are the 12 failures in `tests/test_crew_orientation.py` and
`tests/test_gate_evidence.py` added by the commits `8417c0f` (the manifest-block template
repair) and `2e550a8b` (its landing record), or do they already fail at their base?

The landing record for `8417c0f` claimed the latter — "the wider prompt-composing
modules carry the identical pre-existing failures at base (`b5480ee`) and after — 12 in
`test_crew_orientation` and `test_gate_evidence` — so the change adds no failures."
That sentence is a claim about two revisions; this note measures it.

## Method

One environment, two revisions, two runs. Both revisions were checked out from the shared
object store into the same worktree and run with the same interpreter and flags:

- environment: the repo's single root virtualenv (`/home/ITER/mcintos/Code/reckon/.venv`),
  `PYTHONPATH=$PWD`, `pytest -p no:cacheprovider -q`
- files: `tests/test_crew_orientation.py` and `tests/test_gate_evidence.py`
- base revision: `b5480eef8d88cc9182391e31dd8e18e53e0ac1e9`
- head revision: `2e550a8bc5435e21a05d0050f2197a764d2f695a` (base + `8417c0f` + `2e550a8b`)

Both test files are byte-identical across the pair (equal `sha1sum` at each revision), and
neither candidate commit touches either file, so the two runs measured the same tests
against the same code — the only differences between the runs are the two candidate commits
themselves and the docs record.

## Result

| Revision | Result | Failures |
|---|---|---|
| `b5480eef` (base) | **12 failed, 18 passed** | the 12 ids below |
| `2e550a8b` (head) | **12 failed, 18 passed** | the identical 12 ids |

The two failure-id sets are exactly equal: the added set is empty and the removed set is
empty.

```
added   (head − base):  none
removed (base − head):  none
```

### The identical failure-id set (both revisions)

```
FAILED tests/test_crew_orientation.py::test_first_observation_blocks_and_names_a_mismatched_field
FAILED tests/test_crew_orientation.py::test_first_observation_leaves_a_matching_phase_untouched
FAILED tests/test_gate_evidence.py::test_added_failure_ids_matches_promotions_suite_delta_arithmetic
FAILED tests/test_gate_evidence.py::test_a_failing_or_not_run_gate_needs_no_check_evidence
FAILED tests/test_gate_evidence.py::test_a_log_digest_satisfies_the_check_in_place_of_a_log_path
FAILED tests/test_gate_evidence.py::test_attribution_naming_a_pre_existing_failure_is_dropped
FAILED tests/test_gate_evidence.py::test_fifteen_repair_promotions_with_no_added_failures_are_accepted
FAILED tests/test_gate_evidence.py::test_promoted_run_stores_failure_attribution_beside_added_ids
FAILED tests/test_gate_evidence.py::test_promoting_a_passing_gate_with_a_full_check_is_accepted_and_stored
FAILED tests/test_gate_evidence.py::test_promoting_a_passing_gate_with_no_check_is_refused
FAILED tests/test_gate_evidence.py::test_reasoned_waiver_promotes_and_stores_observations
FAILED tests/test_gate_evidence.py::test_repair_run_with_only_baseline_failures_is_clean
```

### Failure shapes

The orientation tests fail in `crew.observe` → `crew/runs.py:_mutate_pointer` →
`crew/reports.py:_refuse_undetermined_status`: the fixture manifest the test writes carries
no `status` key and the parser refuses it as undetermined. The gate-evidence tests fail on
the promotion path (`crew complete --gate`), surfacing `AssertionError` and
`ManifestParseError` from `crew/reports.py`. Both shapes are identical at the two revisions;
the archive's assertion that the modules grew no new failures at the commit is confirmed by
direct measurement rather than inferred.

## Conclusion

The answer to the question in the header is **zero added failures**. The 12 failures are
pre-existing at the base of `8417c0f` and unchanged by `8417c0f` and `2e550a8b`. A red base
with an unchanged set is a pass of the attribution measure. Nothing in the two candidate
commits reaches any of the 12 failures.

## Artefacts

- base run: `base_b5480eef_run.log` in the run directory
- head run: `head_2e550a8b_run.log` in the run directory
- run: `r-20260915T115738959461-attribute-the-orientation-and-gate-evidence-failures`
