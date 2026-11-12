"""A line a test prints is not a failing test the landing check counts.

A gate log carries a test's captured logging output beside pytest's own
short-summary lines, and a record such as ``ERROR package.module:file.py:3553
message`` opens with the same word a collection error's line does. The landing
check reads a failure from a log by its test identity, so it counts only the
node id a test is reported under: a printed record names no test, and two arms
carrying the same failing ids compare equal however much either prints.

The logs below are written into a synthesised run directory, header and
terminal record included, because the check reads the shape a gate log
actually has rather than a text its own test hand-assembled.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew import review

BASE_REVISION = "1111111111111111111111111111111111111111"
HEAD_REVISION = "2222222222222222222222222222222222222222"
COMMAND = "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q tests"

#: Two tests the base and the following run both report failing.
SHARED_FAILURES = (
    "tests/test_pool_compose_retry.py::TestPoolComposeRetry::test_retry_exhausted_falls_through",
    "tests/test_gap_rule_verdicts.py::TestOrdinalSamples::test_ordinal_gap_is_a_rule_violation_not_a_missing_token[position-arc_of_circle_start_point-None]",
)

#: Two records a test prints while the head arm runs, in the shape pytest's
#: logging plugin captures them.
PRINTED_RECORDS = (
    "imas_codex.standard_names.export:export.py:3553 Withholding a from catalog export",
    "imas_codex.standard_names.export:export.py:3553 Withholding b from catalog export",
)

#: One test the head arm reports failing that the base does not.
ADDED_FAILURE = "tests/test_pool_admission.py::test_a_stranded_pool_is_admitted"


def _write_log(
    run_directory: Path,
    name: str,
    revision: str,
    failures: tuple[str, ...],
    *,
    printed: tuple[str, ...] = (),
) -> Path:
    """Write one gate log into the run directory and return its path."""
    run_directory.mkdir(parents=True, exist_ok=True)
    path = run_directory / name
    lines = [
        f"# revision: {revision}",
        f"# command: {COMMAND}",
        "",
        "======================== short test summary info ========================",
        *(f"FAILED {test_id} - AssertionError: truncated" for test_id in failures),
    ]
    if printed:
        lines += [
            "",
            "---------------------------- live log call ----------------------------",
            *(f"ERROR    {record}" for record in printed),
        ]
    lines += ["", f"{len(failures)} failed, 2 passed in 12.34s", "EXIT=1"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _read_logs(tmp_path: Path, *, added: tuple[str, ...] = ()) -> tuple[str, str]:
    """Return the base and head arm texts of one run, header and EXIT= intact."""
    run_directory = tmp_path / "run"
    base = _write_log(run_directory, "base-suite.log", BASE_REVISION, SHARED_FAILURES)
    head = _write_log(
        run_directory,
        "head-suite.log",
        HEAD_REVISION,
        SHARED_FAILURES + added,
        printed=PRINTED_RECORDS,
    )
    for path, revision in ((base, BASE_REVISION), (head, HEAD_REVISION)):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert path.parent == run_directory
        assert lines[0] == f"# revision: {revision}"
        assert lines[1] == f"# command: {COMMAND}"
        assert lines[-1] == "EXIT=1"
    return (
        base.read_text(encoding="utf-8"),
        head.read_text(encoding="utf-8"),
    )


def test_printed_records_are_not_added_failures(tmp_path: Path) -> None:
    """The head arm prints records and adds no failing test: the delta is zero."""
    base_text, head_text = _read_logs(tmp_path)

    count, added_ids = review.added_failures_from_gate_logs(base_text, head_text)

    assert (count, added_ids) == (0, [])
    assert all(
        record not in review.gate_log_failure_ids(head_text)
        for record in PRINTED_RECORDS
    )
    assert review.gate_log_failure_ids(head_text) == set(SHARED_FAILURES)


def test_one_real_added_failure_is_reported_alone(tmp_path: Path) -> None:
    """A head arm carrying one genuinely added test reports exactly that id."""
    base_text, head_text = _read_logs(tmp_path, added=(ADDED_FAILURE,))

    count, added_ids = review.added_failures_from_gate_logs(base_text, head_text)

    assert count == 1
    assert added_ids == [ADDED_FAILURE]


def test_the_annotated_record_reports_a_clean_delta(tmp_path: Path) -> None:
    """The landing record's added count is zero when only records are printed."""
    base_text, head_text = _read_logs(tmp_path)

    annotated = review.annotate_added_failures(
        {"total": 40}, base_text=base_text, head_text=head_text
    )

    assert annotated["added_failure_count"] == 0
    assert annotated["added_failure_ids"] == []
    assert annotated["total"] == 40
