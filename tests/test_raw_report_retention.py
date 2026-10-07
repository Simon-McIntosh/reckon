"""A plan review's raw report is pruned once its committed record exists.

The raw report text is the one part of a delivered review the committed record
already distils: coverage reads the sidecar and the snapshot beside it, and the
record filed under the project's ``docs/state`` tree survives a clone. So the
report text is removed after ``review.raw_report_retention_days`` on the host —
but only once the committed record exists, because until then the raw text is
the only copy, and a report whose record is absent is reported rather than
removed. The fixture synthesises the reports root and the repository under the
isolated ``RECKON_HOME``, so a case that inspects the real config home would
have nothing of its own to look at.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon import flight
from reckon.crew import plan_review, routing
from reckon.crew.runs import reports_dir

PROJECT = "sample"
SLUG = "fixture"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def repository(root: Path) -> Path:
    """A minimal repository with a committed tree, so gc's HEAD resolves."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "test: seed")
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    return repo


def report_directory(run_id: str) -> Path:
    """The plan-review report directory for one review run under the home."""
    return plan_review.review_report_directory(PROJECT, SLUG, run_id)


def write_report(run_id: str, *, age_days: float) -> Path:
    """Write a report, or the whole trio, aged by the given number of days."""
    directory = report_directory(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / plan_review._REVIEW_REPORT_NAME
    report.write_text("RUBRIC wiring: pass\n", encoding="utf-8")
    (directory / plan_review._REVIEW_SIDECAR_NAME).write_text(
        json.dumps({"plan_slug": SLUG, "review_run_id": run_id}), encoding="utf-8"
    )
    (directory / plan_review._REVIEW_SNAPSHOT_NAME).write_text(
        "<html></html>", encoding="utf-8"
    )
    stamp = (datetime.now(tz=UTC) - timedelta(days=age_days)).timestamp()
    for member in directory.iterdir():
        os.utime(member, (stamp, stamp))
    os.utime(directory, (stamp, stamp))
    return report


def commit_record(repo: Path, run_id: str) -> Path:
    """Write the committed plan-review record a review run is filed under."""
    path = (
        repo / "docs" / "state" / PROJECT / "reviews" / "plan" / SLUG / f"{run_id}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"review_run_id": run_id}), encoding="utf-8")
    return path


def write_project_layer(repo: Path, days: int) -> None:
    layer = repo / "docs" / "state" / PROJECT / "flight.yaml"
    layer.write_text(
        f"review:\n  raw_report_retention_days: {days}\n", encoding="utf-8"
    )


def gc(repo: Path, *, apply: bool) -> dict:
    return routing.garbage_collect(
        repo=repo,
        project=PROJECT,
        integrated_into="HEAD",
        apply=apply,
        now=datetime.now(tz=UTC),
    )


def raw_reports(report: dict) -> dict[str, dict]:
    return {item["run_id"]: item for item in report["plan_review_raw_reports"]}


def test_a_past_report_with_a_record_is_pruned_only_under_apply(tmp_path):
    repo = repository(tmp_path)
    report = write_report("kept-run", age_days=40)
    commit_record(repo, "kept-run")

    dry = gc(repo, apply=False)
    row = raw_reports(dry)["kept-run"]
    assert row["action"] == "prune", "the past report is listed as prunable"
    assert row["age_days"] >= 30
    assert report.is_file(), "a dry run removes nothing"

    applied = gc(repo, apply=True)
    assert raw_reports(applied)["kept-run"]["action"] == "prune"
    assert not report.is_file(), "apply removes the raw report text"


def test_a_past_report_without_a_record_is_withheld_and_never_removed(tmp_path):
    repo = repository(tmp_path)
    report = write_report("orphan-run", age_days=40)
    # No committed record: the raw text is the only copy of the review.

    dry = gc(repo, apply=False)
    row = raw_reports(dry)["orphan-run"]
    assert row["action"] == "withheld"
    assert row["withheld"] == "missing-committed-record"

    applied = gc(repo, apply=True)
    assert raw_reports(applied)["orphan-run"]["action"] == "withheld"
    assert report.is_file(), "a withheld report is never removed"


def test_a_report_inside_the_commit_within_retention_is_not_listed(tmp_path):
    repo = repository(tmp_path)
    report = write_report("fresh-run", age_days=3)
    commit_record(repo, "fresh-run")

    dry = gc(repo, apply=False)
    assert "fresh-run" not in raw_reports(dry)
    assert report.is_file()


def test_the_sidecar_and_snapshot_survive_every_case(tmp_path):
    repo = repository(tmp_path)
    for run_id, committed in (("kept-run", True), ("orphan-run", False)):
        write_report(run_id, age_days=40)
        if committed:
            commit_record(repo, run_id)

    gc(repo, apply=True)
    for run_id in ("kept-run", "orphan-run"):
        directory = report_directory(run_id)
        assert (directory / plan_review._REVIEW_SIDECAR_NAME).is_file()
        assert (directory / plan_review._REVIEW_SNAPSHOT_NAME).is_file()


def test_the_retention_defaults_to_thirty_days(tmp_path):
    repo = repository(tmp_path)
    write_report("kept-run", age_days=45)
    commit_record(repo, "kept-run")
    # Nothing declares the key, so the shipped default 30 gates the report.
    assert flight.raw_report_retention_days({}) == 30
    assert "kept-run" in raw_reports(gc(repo, apply=False))


def test_a_project_layer_changes_the_cutoff(tmp_path):
    repo = repository(tmp_path)
    report = write_report("kept-run", age_days=45)
    commit_record(repo, "kept-run")
    write_project_layer(repo, 60)  # a longer retention keeps the report

    assert (
        flight.raw_report_retention_days({"review": {"raw_report_retention_days": 60}})
        == 60
    )
    assert "kept-run" not in raw_reports(gc(repo, apply=False))
    assert report.is_file()

    write_project_layer(repo, 30)  # the shorter value prunes it again
    assert raw_reports(gc(repo, apply=False))["kept-run"]["action"] == "prune"


def test_the_reader_defaults_on_a_wrong_shape():
    assert (
        flight.raw_report_retention_days({"review": {"raw_report_retention_days": -1}})
        == 30
    )
    assert (
        flight.raw_report_retention_days({"review": {"raw_report_retention_days": "x"}})
        == 30
    )
    assert (
        flight.raw_report_retention_days(
            {"review": {"raw_report_retention_days": True}}
        )
        == 30
    )
    assert (
        flight.raw_report_retention_days({"review": {"raw_report_retention_days": 0}})
        == 0
    )


def test_the_real_config_home_is_untouched(tmp_path):
    repo = repository(tmp_path)
    write_report("kept-run", age_days=40)
    commit_record(repo, "kept-run")

    real_home = Path.home() / ".config/reckon"
    before = real_home.exists()
    gc(repo, apply=True)
    # The isolated home the fixture installed is where the fixture's own
    # reports root lives; the operator's config home is neither created nor
    # read.
    assert real_home.exists() == before
    assert str(reports_dir()).startswith(os.environ["RECKON_HOME"])
