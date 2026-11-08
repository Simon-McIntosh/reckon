"""Failure comparisons share coverage while preserving qualified identities."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import landing_measure, promotion_checks, reports, review
from reckon.crew.node import CrewError

HEAD_ID = "tests/a.py::test_x"
OTHER_ID = "tests/b.py::test_x"


def _arm(ids: list[str]) -> dict:
    return {
        "revision": "a" * 40,
        "command": "pytest tests/a.py",
        "exit_status": int(bool(ids)),
        "log_path": "arm.log",
        "completed": True,
        "failure_count": len(ids),
        "failure_ids": ids,
    }


def _log(ids: list[str]) -> str:
    return "\n".join(
        [
            *(f"FAILED {test_id} - AssertionError" for test_id in ids),
            f"{len(ids)} failed in 0.01s",
            "EXIT=1",
            "",
        ]
    )


@pytest.mark.parametrize("reference", ["test_x", OTHER_ID])
@pytest.mark.parametrize("comparison_arm", ["after_suite", "baseline_suite"])
def test_control_admission_counts_only_uncovered_failures(
    tmp_path: Path, reference: str, comparison_arm: str
) -> None:
    control_log = tmp_path / "control.log"
    control_log.write_text(_log([HEAD_ID]), encoding="utf-8")
    manifest = {
        "negative_control_log": str(control_log),
        comparison_arm: _arm([reference]),
    }
    record = {
        "node": {"write_paths": ["tests/a.py"], "negative_control": "remove the guard"}
    }

    def admit():
        return promotion_checks._require_declared_negative_control(
            "matching-fixture", record, gate="passed", manifest=manifest
        )

    if reference == "test_x":
        with pytest.raises(CrewError, match="adds no failure"):
            admit()
    else:
        result = admit()
        assert result["verdict"] == "matched"
        assert result["added_failure_ids"] == [HEAD_ID]


@pytest.mark.parametrize("reference, expected", [("test_x", []), (OTHER_ID, [HEAD_ID])])
def test_ledger_counts_only_uncovered_failures(
    reference: str, expected: list[str]
) -> None:
    assert ledger.added_failure_ids(_arm([reference]), _arm([HEAD_ID])) == expected


@pytest.mark.parametrize("reference", ["test_x", OTHER_ID])
def test_attribution_accepts_only_names_covering_observed_failures(
    reference: str,
) -> None:
    missing = ledger.failure_attribution_missing_fields(
        {reference: "b" * 40}, _arm([HEAD_ID])
    )
    assert bool(missing) == (reference == OTHER_ID)
    if missing:
        assert "not in after_suite.failure_ids" in missing[0]


def test_bare_attribution_is_stored_under_the_added_qualified_id() -> None:
    assert ledger.new_failure_attribution(
        _arm([]), _arm([HEAD_ID]), {"test_x": "b" * 40}
    ) == {HEAD_ID: "b" * 40}
    assert (
        ledger.new_failure_attribution(
            _arm(["test_x"]), _arm([HEAD_ID]), {"test_x": "b" * 40}
        )
        == {}
    )
    assert (
        ledger.new_failure_attribution(_arm([]), _arm([HEAD_ID]), {OTHER_ID: "b" * 40})
        == {}
    )


@pytest.mark.parametrize("reference, expected", [("test_x", []), (OTHER_ID, [HEAD_ID])])
def test_landing_measure_isolates_only_uncovered_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str, expected: list[str]
) -> None:
    base_tree = tmp_path / "base"
    head_tree = tmp_path / "head"
    base_tree.mkdir()
    head_tree.mkdir()

    def run(argv, *, cwd, **kwargs):
        ids = [reference] if Path(cwd) == base_tree else [HEAD_ID]
        return subprocess.CompletedProcess(argv, 1, _log(ids), "")

    result = landing_measure.measure_run(
        command="pytest",
        tree=head_tree,
        base_sha="a" * 40,
        head_sha="b" * 40,
        base_tree=base_tree,
        logs_dir=tmp_path / "logs",
        changed=[],
        run=run,
        repeats=1,
    )
    assert result.unreadable == ""
    assert sorted(result.isolated) == expected


@pytest.mark.parametrize("reference, expected", [("test_x", []), (OTHER_ID, [HEAD_ID])])
def test_review_logs_count_only_uncovered_failures(
    reference: str, expected: list[str]
) -> None:
    assert review.added_failures_from_gate_logs(_log([reference]), _log([HEAD_ID])) == (
        len(expected),
        expected,
    )


@pytest.mark.parametrize("reference, expected", [("test_x", True), (OTHER_ID, False)])
def test_retirement_names_cover_only_the_same_test(
    reference: str, expected: bool
) -> None:
    assert review._retires_id(f"Retire `{reference}`.", HEAD_ID) is expected


def test_retirement_keeps_the_recorded_scratch_directory_reduction(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch-arm"
    token = f"{scratch.name}/tests/a.py::test_x"
    assert review._retires_id(f"Retire `{token}`.", HEAD_ID, (scratch,)) is True
    assert review._retires_id(f"Retire `{token}`.", HEAD_ID) is False


@pytest.mark.parametrize(
    "after_id, overlap", [(HEAD_ID, True), ("tests/b.py::test_y", False)]
)
def test_retry_audit_recognises_overlap_in_both_name_shapes(
    after_id: str, overlap: bool
) -> None:
    text = "\n".join(
        [
            "status: complete",
            'commits: ["' + "a" * 40 + '"]',
            "tests: pytest tests/a.py",
            'retry_failures: ["test_x"]',
            'failure_attribution: {"test_x": "scaffolding: probe failed"}',
            "baseline_suite: " + json.dumps(_arm([])),
            "after_suite: " + json.dumps(_arm([after_id])),
        ]
    )
    result = reports.audit_manifest(text, suite_armed=True)
    finding = "retry_failures ids must differ from after_suite.failure_ids"
    assert (finding in result["findings"]) is overlap


def test_two_way_match_reports_matched_and_unmatched_ids_on_both_sides() -> None:
    result = review.failure_ids_match(
        ["test_x", "tests/c.py::test_y", "test_z[1]", OTHER_ID],
        [HEAD_ID, "test_y", "tests/d.py::test_z[2]"],
    )
    assert result.matched_base == {"test_x", "tests/c.py::test_y"}
    assert result.matched_head == {HEAD_ID, "test_y"}
    assert result.unmatched_base == {"test_z[1]", OTHER_ID}
    assert result.unmatched_head == {"tests/d.py::test_z[2]"}


@pytest.mark.parametrize(
    "base, heads, expected",
    [
        (["test_x"], [HEAD_ID + "[1]", HEAD_ID + "[2]"], set()),
        (["test_x[1]"], [HEAD_ID + "[1]", HEAD_ID + "[2]"], {HEAD_ID + "[2]"}),
        ([HEAD_ID], ["test_x"], {"test_x"}),
    ],
)
def test_coverage_keeps_its_direction_and_parameter_identity(
    base, heads, expected
) -> None:
    assert review.failure_ids_covered(base, heads) == expected
