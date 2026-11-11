"""A delivered plan review does not hold the gate for the next round.

The next plan review could not be composed until the previous review run was
promoted, because the in-flight check counted any live review pointer for the
plan, including one whose report was already stored. Promotion took ten to
fifteen minutes, and a promotion that failed partway left the gate shut until it
was re-run by hand, so every round cost reviewer time plus promotion time on the
coordinator's critical path.

A review whose record is stored and readable has delivered. What remains is its
promotion, which is owed and listed as it is today; it no longer stands in the
way of the next compose. A review still working, or one whose record is missing
or unreadable, keeps the gate exactly as before.

The last two cases drive the compose path itself, not only the reader: a
delivered review's preview composes, and a working one's preview is refused
naming its run. The control for this file restores the any-live-pointer check,
which makes the first case and the composed-path case both refuse.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import plan_review, recovery_review_dispatch, runs

PROJECT = "plan-review-gate-fixture"
SESSION = "coordinator-session"
PLAN = "fixture-plan"
REVIEW_RUN_ID = "r-plan-review-delivered"
VERSION = 3
IN_FLIGHT_REASON = "a plan review is already in flight as a live run"

PLAN_HTML = (
    "<!doctype html><html><head>"
    f'<meta name="plan-slug" content="{PLAN}">'
    '</head><body><h2 id="fixture-section">Fixture</h2>'
    "<p>One section to review.</p></body></html>\n"
)


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    """A checkout holding the plan the review reads."""
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(PLAN_HTML, encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", f"docs/plans/{PLAN}.html"),
        ("commit", "-q", "-m", "chore: seed the review fixture"),
    ):
        run = subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, check=False
        )
        assert run.returncode == 0, run.stderr.decode()
    return root


def _write_live_review(repository: Path | None = None) -> str:
    """One plan review of the plan, live and not yet promoted."""
    node: dict[str, Any] = {
        "id": f"plan-review-of-{PLAN}",
        "plan": "",
        "section": "",
        "time_budget": "20m",
        "write_paths": [],
    }
    pointer: dict[str, Any] = {
        "run_id": REVIEW_RUN_ID,
        "project": PROJECT,
        "session": SESSION,
        "phase": "running",
        "node": node,
    }
    if repository is not None:
        pointer["subject"] = "plan"
        pointer["plan_slug"] = PLAN
        pointer["plan_path"] = str(repository / "docs" / "plans" / f"{PLAN}.html")
        pointer["repo"] = str(repository)
    runs._write_json(runs.pointer_path(REVIEW_RUN_ID), pointer)
    return REVIEW_RUN_ID


def _store_review_record(review_run_id: str = REVIEW_RUN_ID) -> Path:
    """The record the reviewer wrote, in the store the gate reads."""
    return plan_review.store_plan_review(
        {
            "project": PROJECT,
            "plan_slug": PLAN,
            "plan_version": VERSION,
            "review_run_id": review_run_id,
            "rubric": "plan_review",
            "status": "ready",
        }
    )


def _unreadable_record() -> Path:
    """A record file at the store path that no reader can parse."""
    path = plan_review.plan_review_path(PROJECT, PLAN, VERSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    return path


def _preview() -> dict[str, Any]:
    """The compose path's own dry run for the live review of the plan."""
    return recovery_review_dispatch._dispatch_text_review_for_run(
        runs.read_pointer(REVIEW_RUN_ID), dry_run=True
    )


def test_a_delivered_unpromoted_review_lets_the_next_review_compose(
    crew_home: Path,
) -> None:
    """The record is stored, so the review has delivered and stands for nothing."""
    _write_live_review()
    _store_review_record()

    standing = recovery_review_dispatch._standing_plan_review(PROJECT, PLAN)

    assert standing == "", standing


def test_a_review_still_working_refuses_as_before(crew_home: Path) -> None:
    """With no stored record the review is still working, and names itself."""
    _write_live_review()

    standing = recovery_review_dispatch._standing_plan_review(PROJECT, PLAN)

    assert standing == REVIEW_RUN_ID, standing


def test_a_delivered_review_with_an_unreadable_record_still_refuses(
    crew_home: Path,
) -> None:
    """A record that cannot be read is no delivery, so the gate stays shut."""
    _write_live_review()
    _unreadable_record()

    standing = recovery_review_dispatch._standing_plan_review(PROJECT, PLAN)

    assert standing == REVIEW_RUN_ID, standing


def test_a_record_stored_for_another_review_run_does_not_clear_this_one(
    crew_home: Path,
) -> None:
    """A later round's record is not this run's delivery."""
    _write_live_review()
    _store_review_record("r-plan-review-from-an-earlier-round")

    standing = recovery_review_dispatch._standing_plan_review(PROJECT, PLAN)

    assert standing == REVIEW_RUN_ID, standing


def test_the_compose_preview_goes_on_for_a_delivered_review(
    crew_home: Path, repository: Path
) -> None:
    """The compose path itself composes, rather than refusing as in flight.

    The preview's own ``refused`` flag is the staging dispatch's node
    validation, which a synthetic review node need not satisfy; what this case
    measures is the gate, so it asserts the refusal the gate would return and
    the compose the path builds once past it.
    """
    _write_live_review(repository)
    _store_review_record()

    report = _preview()

    assert report.get("reason") is None, report
    assert report.get("review_run_id") != REVIEW_RUN_ID, report
    assert report["argv"][:3] == ["reckon", "crew", "dispatch"], report


def test_the_compose_preview_refuses_a_working_review(
    crew_home: Path, repository: Path
) -> None:
    """A review still working still stops the compose, naming its run."""
    _write_live_review(repository)

    report = _preview()

    assert report.get("reason") == IN_FLIGHT_REASON, report
    assert report.get("review_run_id") == REVIEW_RUN_ID, report
    assert "argv" not in report, report


def test_an_unreadable_record_does_not_let_the_compose_preview_go_on(
    crew_home: Path, repository: Path
) -> None:
    """The durable record missing is no delivery at the compose path either."""
    _write_live_review(repository)
    _unreadable_record()

    report = _preview()

    assert report.get("reason") == IN_FLIGHT_REASON, report
    assert report.get("review_run_id") == REVIEW_RUN_ID, report
