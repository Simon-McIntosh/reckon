"""A reported log citation must not resolve under the temporary root.

A promoted run record names a gate command, an exit status and a log path,
and the log path is what a later reader opens to check the claim. The run
directory persists with the run; the platform temporary directory is a small
allocation that is cleared without notice, so a citation resolving there can
point at a file that no longer exists, and nothing distinguishes that from a
citation to a file that was never written. The check under test reports such
citations; it never refuses, rewrites or relocates anything.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from reckon.crew import reports


@pytest.fixture()
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A root the check reads as the platform temporary directory.

    The check reads what ``tempfile.gettempdir`` reports at call time, so
    pointing it at a directory this test creates keeps the falsifiers
    deterministic on any host and lets the test own the root's location.
    """
    root = tmp_path / "temp-root"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    return root


def _manifest(
    *,
    test_logs: list[str] | None = None,
    baseline: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "node": "node-a",
        "status": "complete",
        "commits": ["abc123"],
        "tests": "pytest -q -> 28 passed",
        "test_logs": test_logs if test_logs is not None else [],
    }
    if baseline is not None:
        payload["baseline_suite"] = baseline
    if after is not None:
        payload["after_suite"] = after
    return reports.parse_manifest(json.dumps(payload, indent=2))


def test_a_log_under_the_temp_root_is_reported(temp_root: Path) -> None:
    log = temp_root / "suite.log"
    manifest = _manifest(test_logs=[str(log)])

    findings = reports.report_log_paths_under_temp_root(manifest, name="r-1")

    assert findings == [{"manifest": "r-1", "key": "test_logs", "path": str(log)}]


def test_the_report_names_the_manifest_the_key_and_the_path(temp_root: Path) -> None:
    log = temp_root / "baseline.log"
    manifest = _manifest(baseline={"log_path": str(log)})

    findings = reports.report_log_paths_under_temp_root(manifest, name="r-2")

    assert findings == [
        {"manifest": "r-2", "key": "baseline_suite.log_path", "path": str(log)}
    ]


def test_a_log_under_the_run_directory_is_not_reported(
    temp_root: Path, tmp_path: Path
) -> None:
    run_dir = tmp_path / "runs" / "r-2026-x"
    manifest = _manifest(test_logs=[str(run_dir / "suite.log")])

    assert reports.report_log_paths_under_temp_root(manifest, name="r") == []


def test_a_log_under_the_repository_is_not_reported(
    temp_root: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    manifest = _manifest(test_logs=[str(repo / "tests" / "suite.log")])

    assert reports.report_log_paths_under_temp_root(manifest, name="r") == []


def test_a_manifest_citing_no_logs_is_not_reported_and_does_not_raise(
    temp_root: Path,
) -> None:
    manifest = _manifest()

    assert reports.report_log_paths_under_temp_root(manifest, name="r") == []


def test_a_missing_path_is_judged_by_where_it_points_not_by_existence(
    temp_root: Path, tmp_path: Path
) -> None:
    # A missing durable path and a missing temporary path are different
    # findings, so the check must decide from the path's resolution alone.
    missing_under_root = temp_root / "never-written.log"
    missing_durable = tmp_path / "runs" / "r-x" / "never-written.log"
    manifest = _manifest(test_logs=[str(missing_under_root), str(missing_durable)])

    findings = reports.report_log_paths_under_temp_root(manifest, name="r")

    assert findings == [
        {"manifest": "r", "key": "test_logs", "path": str(missing_under_root)}
    ]


def test_a_symlink_and_a_relative_spelling_into_the_root_are_reported(
    temp_root: Path, tmp_path: Path
) -> None:
    target = temp_root / "suite.log"
    target.write_text("x")
    # The link's lexical location is durable; only its resolved form reaches
    # the root, so a prefix comparison would miss it.
    durable = tmp_path / "durable"
    durable.mkdir()
    link = durable / "suite.log"
    link.symlink_to(target)
    relative = os.path.relpath(target)

    manifest = _manifest(test_logs=[str(link), relative])

    findings = reports.report_log_paths_under_temp_root(manifest, name="r")

    assert len(findings) == 2
    assert {finding["path"] for finding in findings} == {str(link), relative}
    assert all(finding["key"] == "test_logs" for finding in findings)
    assert all(finding["manifest"] == "r" for finding in findings)


def test_only_the_citation_under_the_root_is_reported(
    temp_root: Path, tmp_path: Path
) -> None:
    under = temp_root / "suite.log"
    durable_1 = tmp_path / "runs" / "r-1" / "suite.log"
    durable_2 = tmp_path / "repo" / "suite.log"
    manifest = _manifest(
        test_logs=[str(under), str(durable_1), str(durable_2)],
        after={"log_path": str(durable_1)},
    )

    findings = reports.report_log_paths_under_temp_root(manifest, name="r")

    assert findings == [{"manifest": "r", "key": "test_logs", "path": str(under)}]


def test_the_check_reports_and_returns_without_refusing(temp_root: Path) -> None:
    log = temp_root / "suite.log"
    manifest = _manifest(test_logs=[str(log)])
    before = json.dumps(manifest, sort_keys=True)

    findings = reports.report_log_paths_under_temp_root(manifest, name="r")

    # The report is a value returned, never an exception raised, and the
    # manifest is left untouched: there is no refusal path to assert.
    assert isinstance(findings, list)
    assert len(findings) == 1
    assert json.dumps(manifest, sort_keys=True) == before
