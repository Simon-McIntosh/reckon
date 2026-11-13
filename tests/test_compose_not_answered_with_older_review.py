"""A plan review composed for a newer plan version is not answered with an older one.

A plan review's record is stored only by its promotion, so a reviewer that has
delivered and is waiting to be promoted leaves a live pointer and no record.
Reading every such pointer as in flight made the plan's whole review gate wait
on a promotion nobody was running: the plan could move on to a newer version and
every compose request answered with the older review's run id, so a section that
only the new version covers could never be reviewed and its dispatch stayed
refused.

A review whose worker has ended stands for nothing, whether or not its record is
stored yet. A review still working stands only while it reads the plan's current
version, or already reads every unit changed since the version it composed for;
a newer version it does not cover is composed as a new review instead.

The negative control is the base revision's reader, which treats any live
pointer without a record as in flight and answers the first case with the older
run id.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import plan_review, recovery_review_dispatch, runs

PROJECT = "plan-review-version-fixture"
SESSION = "coordinator-session"
PLAN = "fixture-plan"
REVIEW_RUN_ID = "r-plan-review-older-version"
VERSION = 4
IN_FLIGHT_REASON = "a plan review is already in flight as a live run"

PLAN_HTML = (
    "<!doctype html><html><head>"
    '<meta charset="utf-8">'
    '<meta name="docs-project" content="{project}">'
    '<meta name="plan-slug" content="{slug}">'
    '<meta name="plan-version" content="{version}">'
    '<meta name="plan-modified" content="2026-11-13">'
    "</head><body>"
    '<h2 id="s1">§1 First section</h2>'
    "<p>{first}</p>"
    '<h2 id="s2">§2 Second section</h2>'
    "<p>{second}</p>"
    "</body></html>"
)


def _plan_text(
    version: int,
    *,
    first: str = "The first section.",
    second: str = "The second section.",
) -> str:
    """The plan at ``version``, with the authored prose the tests edit."""
    return PLAN_HTML.format(
        project=PROJECT, slug=PLAN, version=version, first=first, second=second
    )


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    """A checkout holding the plan the reviews read."""
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(_plan_text(VERSION), encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", "docs/plans/" + PLAN + ".html"),
        ("commit", "-q", "-m", "chore: seed the review fixture"),
    ):
        run = subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, check=False
        )
        assert run.returncode == 0, run.stderr.decode()
    return root


def _plan_path(repository: Path) -> Path:
    return repository / "docs" / "plans" / (PLAN + ".html")


def _move_plan(
    repository: Path,
    version: int,
    *,
    first: str = "The first section.",
    second: str = "The second section.",
) -> Path:
    """Write the plan at a newer version, keeping its path the same."""
    path = _plan_path(repository)
    path.write_text(_plan_text(version, first=first, second=second), encoding="utf-8")
    return path


def _write_sidecar(
    plan_text: str,
    *,
    plan_version: int = VERSION,
    traced_units: list[str] | None = None,
) -> Path:
    """The composed sidecar a live review of ``plan_text`` leaves behind."""
    directory = plan_review.review_report_directory(PROJECT, PLAN, REVIEW_RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    return plan_review.write_review_sidecar(
        directory,
        project=PROJECT,
        plan_slug=PLAN,
        plan_version=plan_version,
        reviewed_blob_sha="0" * 40,
        document=plan_text,
        rubric="content",
        report_path=directory / "report.md",
        traced_units=traced_units,
    )


def _write_live_review(
    repository: Path,
    *,
    phase: str = "working",
    manifest_status: str | None = None,
) -> str:
    """One plan review of the plan, live and holding no stored record."""
    directory = plan_review.review_report_directory(PROJECT, PLAN, REVIEW_RUN_ID)
    pointer: dict[str, Any] = {
        "run_id": REVIEW_RUN_ID,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "subject": "plan",
        "plan_slug": PLAN,
        "plan_path": str(_plan_path(repository)),
        "repo": str(repository),
        "node": {
            "id": "plan-review-of-" + PLAN,
            "plan": "",
            "section": "",
            "time_budget": "20m",
            "write_paths": [str(directory)],
        },
    }
    if manifest_status is not None:
        run_directory = runs.run_dir(REVIEW_RUN_ID)
        run_directory.mkdir(parents=True, exist_ok=True)
        manifest = run_directory / "manifest.md"
        manifest.write_text(
            "node: plan-review-of-" + PLAN + "\nstatus: " + manifest_status + "\n",
            encoding="utf-8",
        )
        pointer["manifest_path"] = str(manifest)
    runs._write_json(runs.pointer_path(REVIEW_RUN_ID), pointer)
    return REVIEW_RUN_ID


def _subject(repository: Path) -> dict[str, Any]:
    """A compose request for the plan, as ``crew review`` mints one."""
    return {
        "subject": "plan",
        "project": PROJECT,
        "plan_slug": PLAN,
        "plan_path": str(_plan_path(repository)),
        "repo": str(repository),
        "session": SESSION,
        "rubric": "content",
        "local": False,
        "run_id": runs.new_run_id("plan-review-of-" + PLAN),
    }


def _compose(repository: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the compose path's own dry run, returning its subject and report."""
    subject = _subject(repository)
    report = recovery_review_dispatch.dispatch_review_for_run(subject, dry_run=True)
    return subject, report


def _composed_plan_version(subject: dict[str, Any]) -> int:
    """The plan version the composed review's own sidecar reads."""
    fields = recovery_review_dispatch._review_dispatch_fields(subject)
    sidecar = json.loads(Path(fields["sidecar"]).read_text(encoding="utf-8"))
    return int(sidecar["plan_version"])


def _assert_composed_anew(subject: dict[str, Any], report: dict[str, Any]) -> None:
    assert report.get("review_run_id") != REVIEW_RUN_ID, report
    assert report.get("reason") != IN_FLIGHT_REASON, report
    assert report.get("dry_run") is True, report
    assert "argv" in report, report
    assert _composed_plan_version(subject) == VERSION + 1


def _assert_answered_with_the_standing_review(report: dict[str, Any]) -> None:
    assert report.get("review_run_id") == REVIEW_RUN_ID, report
    assert report.get("reason") == IN_FLIGHT_REASON, report
    assert "argv" not in report, report


def test_a_delivered_review_does_not_answer_a_newer_plan(
    crew_home: Path, repository: Path
) -> None:
    """A finished, unpromoted review of vN does not answer a compose for vN+1."""
    _write_sidecar(_plan_text(VERSION))
    _write_live_review(repository, manifest_status="complete")
    _move_plan(repository, VERSION + 1, first="The first section, rewritten.")

    subject, report = _compose(repository)

    _assert_composed_anew(subject, report)


def test_a_working_review_of_the_current_version_still_stands(
    crew_home: Path, repository: Path
) -> None:
    """A review still working on the plan's current version answers the compose."""
    _write_sidecar(_plan_text(VERSION))
    _write_live_review(repository)

    _, report = _compose(repository)

    _assert_answered_with_the_standing_review(report)


def test_a_working_review_covering_every_changed_unit_still_stands(
    crew_home: Path, repository: Path
) -> None:
    """A vN review whose scope holds the changed unit covers vN+1 and stands."""
    _write_sidecar(_plan_text(VERSION), traced_units=["s1", "s2"])
    _write_live_review(repository)
    _move_plan(repository, VERSION + 1, second="The second section, rewritten.")

    _, report = _compose(repository)

    _assert_answered_with_the_standing_review(report)


def test_a_working_review_missing_a_changed_unit_does_not_stand(
    crew_home: Path, repository: Path
) -> None:
    """The same vN review, scoped away from the changed unit, composes anew.

    This is the discriminator for the case above: the two differ only in the
    scope the working review traces, so the reader must be measuring the scope
    rather than answering on the plan version alone.
    """
    _write_sidecar(_plan_text(VERSION), traced_units=["s1"])
    _write_live_review(repository)
    _move_plan(repository, VERSION + 1, second="The second section, rewritten.")

    subject, report = _compose(repository)

    _assert_composed_anew(subject, report)


def test_an_unreadable_scope_keeps_the_working_review_standing(
    crew_home: Path, repository: Path
) -> None:
    """A review whose composed scope cannot be read keeps the gate.

    A compose decision on an unaccountable review must not start a second one
    beside it: the answer the reader cannot resolve is the one it already gave.
    """
    _write_live_review(repository)
    _move_plan(repository, VERSION + 1, first="The first section, rewritten.")

    _, report = _compose(repository)

    _assert_answered_with_the_standing_review(report)
