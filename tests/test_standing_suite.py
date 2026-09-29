"""The standing suite: run it, record it, and hold on a bad one.

Every project here is built under ``tmp_path`` and run with ``sys.executable``,
so nothing is written outside the test's own temporary tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import standing_suite
from reckon.flight import FlightConfigError, review_suite

PROJECT = "proj"

# A command the run helper can execute directly, independent of the outer
# test runner's own arguments, so the throwaway project's suite is the only
# thing measured.
BASE_COMMAND = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def _git_init(project_root: Path) -> None:
    """Make ``project_root`` a one-commit git repository with a real HEAD."""

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(project_root), *args],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (project_root / "README").write_text("seed\n")
    git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "README")
    git(
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        "seed",
    )


def _make_project(tmp_path: Path, *, tests_source: str) -> Path:
    """A throwaway project whose suite is the given test module source."""
    project_root = tmp_path / "project"
    tests_dir = project_root / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_sample.py").write_text(tests_source)
    _git_init(project_root)
    return project_root


def _declaration(budget: str = "30s"):
    return review_suite(
        {"review": {"suite": {"command": BASE_COMMAND, "budget": budget}}}
    )


# ── flight reader ───────────────────────────────────────────────────────────


def test_flight_reader_returns_none_when_undeclared():
    assert review_suite({}) is None
    assert review_suite({"review": {}}) is None
    assert review_suite({"review": {"tiers": {"light_changed_lines": 5}}}) is None


def test_flight_reader_refuses_a_malformed_block_by_name():
    with pytest.raises(FlightConfigError) as excinfo:
        review_suite({"review": {"suite": {"command": "pytest", "budget": "20m"}}})
    assert "review.suite.command" in str(excinfo.value)

    with pytest.raises(FlightConfigError) as excinfo:
        review_suite({"review": {"suite": {"command": ["pytest"], "budget": "20"}}})
    assert "review.suite.budget" in str(excinfo.value)


def test_declared_budget_converts_to_seconds():
    declaration = review_suite(
        {"review": {"suite": {"command": ["pytest"], "budget": "20m"}}}
    )
    assert declaration is not None
    assert declaration.budget_seconds() == 1200


# ── a collection failure holds the light tiers ──────────────────────────────


def test_a_collection_failure_is_recorded_and_holds_none_and_light(tmp_path):
    # BASE_COMMAND carries -q, so this is the quiet form the reckon suite
    # declares. A syntax error exits 2 with pytest's own collection-error line
    # and no summary token at all.
    project_root = _make_project(tmp_path, tests_source="def test_broken(:\n")
    run_record = standing_suite.run(
        project_root, _declaration(), tmp_path / "collection.log"
    )
    assert run_record["collection_failed"] is True
    assert run_record["collected"] == 0
    assert run_record["exit_status"] == 2
    standing_suite.record(project_root, PROJECT, run_record)

    assert standing_suite.hold_reason(project_root, "none", PROJECT) is not None
    assert standing_suite.hold_reason(project_root, "light", PROJECT) is not None
    assert standing_suite.hold_reason(project_root, "full", PROJECT) is None

    reason = standing_suite.hold_reason(project_root, "none", PROJECT)
    assert run_record["revision"] in reason
    assert run_record["observed_at"] in reason


def test_a_suite_that_collects_and_fails_is_not_a_collection_failure(tmp_path):
    """A red suite that ran is an ordinary failing suite, not a broken one.

    Under ``-q`` pytest prints no collection line, so a run that collected,
    ran and failed leaves only its summary behind. Reading that as a collection
    failure would hold the lighter tiers against a suite that is merely red.
    """
    project_root = _make_project(
        tmp_path, tests_source="def test_fails():\n    assert 1 == 2\n"
    )
    run_record = standing_suite.run(
        project_root, _declaration(), tmp_path / "failing.log"
    )

    assert run_record["collected"] == 1
    assert run_record["failed"] == 1
    assert run_record["collection_failed"] is False
    assert run_record["exit_status"] == 1

    standing_suite.record(project_root, PROJECT, run_record)
    assert standing_suite.hold_reason(project_root, "none", PROJECT) is None
    assert standing_suite.hold_reason(project_root, "light", PROJECT) is None


def test_an_unresolvable_root_raises_rather_than_reading_as_no_hold(tmp_path):
    """A root that cannot be named is a question that cannot be answered.

    The record is a collection failure — the exact case the hold exists for —
    so returning ``None`` because the mount table does not know the project
    would let a broken suite read exactly like a passing one. Naming the
    project still answers the question, which is the escape hatch.
    """
    project_root = _make_project(tmp_path, tests_source="def test_broken(:\n")
    standing_suite.record(
        project_root,
        PROJECT,
        standing_suite.run(project_root, _declaration(), tmp_path / "unmounted.log"),
    )

    with pytest.raises(standing_suite.StandingSuiteError) as excinfo:
        standing_suite.hold_reason(project_root, "none")
    assert str(project_root) in str(excinfo.value)

    assert standing_suite.hold_reason(project_root, "none", PROJECT) is not None


def test_a_later_passing_run_lifts_the_hold(tmp_path):
    project_root = _make_project(tmp_path, tests_source="def test_broken(:\n")
    standing_suite.record(
        project_root,
        PROJECT,
        standing_suite.run(project_root, _declaration(), tmp_path / "bad.log"),
    )
    assert standing_suite.hold_reason(project_root, "none", PROJECT) is not None

    (project_root / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert 1\n"
    )
    good = standing_suite.run(project_root, _declaration(), tmp_path / "good.log")
    assert good["collection_failed"] is False
    assert good["passed"] == 1
    standing_suite.record(project_root, PROJECT, good)

    assert standing_suite.hold_reason(project_root, "none", PROJECT) is None
    assert standing_suite.hold_reason(project_root, "light", PROJECT) is None


def test_a_waiver_lifts_the_hold(tmp_path):
    project_root = _make_project(tmp_path, tests_source="def test_broken(:\n")
    standing_suite.record(
        project_root,
        PROJECT,
        standing_suite.run(project_root, _declaration(), tmp_path / "bad.log"),
    )
    assert standing_suite.hold_reason(project_root, "light", PROJECT) is not None

    standing_suite.record_waiver(
        project_root, PROJECT, who="lead", why="known-broken fixture, tracked elsewhere"
    )
    assert standing_suite.hold_reason(project_root, "none", PROJECT) is None
    assert standing_suite.hold_reason(project_root, "light", PROJECT) is None


# ── a run past its budget is stopped and its group reaped ───────────────────


def test_a_run_past_its_budget_is_stopped_and_recorded(tmp_path, monkeypatch):
    pid_file = tmp_path / "suite.pid"
    source = (
        "import os, time\n"
        "def test_slow():\n"
        f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "    time.sleep(30)\n"
    )
    project_root = _make_project(tmp_path, tests_source=source)
    run_record = standing_suite.run(
        project_root, _declaration("1s"), tmp_path / "slow.log"
    )

    assert run_record["over_budget"] is True
    assert run_record["duration_seconds"] < 20
    assert run_record["budget_seconds"] == 1

    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# ── the record lands under the project's state tree ─────────────────────────


def test_record_lands_under_the_project_state_tree(tmp_path):
    project_root = _make_project(
        tmp_path, tests_source="def test_ok():\n    assert 1\n"
    )
    run_record = standing_suite.run(project_root, _declaration(), tmp_path / "ok.log")
    path = standing_suite.record(project_root, PROJECT, run_record)

    assert path.parent == project_root / "docs" / "state" / PROJECT / "suite-runs"
    assert path.is_file()
    assert path.is_relative_to(tmp_path)
