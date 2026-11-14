"""A review refused for a record it never stored is retried only when it moves.

A review run that ended without writing its record is refused by the sweep on
every pass: the record search that establishes the absence runs again for a run
nothing about which has changed, and the station reports the same refusal again.
One such run accounted for 141 of 19,920 sweep-failure lines in a single
afternoon, competing with the tick for the station's interpreter.

The property is that a refused review is read once per pointer-and-run-directory
identity, and its refusal is reported once per identity:

- three sweeps over an unmoved run search for its record once;
- touching its run directory moves the identity, so the next sweep searches
  again;
- over three station sweeps, with the run's durable promotion request drained
  between them, the station writes one failure line naming the run, not one per
  sweep;
- the refusal itself is unchanged: the reason carried in the sweep's
  ``review_self_refused`` is the reason the base revision states.

The fixture runs a temporary crew home and repository and asserts afterwards
that the workstation's real crew pointer directory is untouched, because an
isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _plan_html, file_memo
from reckon.crew import recovery, resumption, station
from reckon.crew import recovery_review_acceptance as acceptance
from reckon.crew import review as review_module
from reckon.crew.promotion_requests import PromotionService
from reckon.crew.runs import _write_json, crew_home, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "refused-review-fixture"
SESSION = "refused-review-session"
PLAN = "refused-review-target"
REVIEWED_RUN_ID = "r-20261114T140000000001-reviewed-run"
REVIEW_RUN_ID = "r-20261114T140500000000-review-of-reviewed-run"
REVIEW_NODE_ID = f"review-of-{REVIEWED_RUN_ID}"
REVIEWED_BASE = "b" * 40
REVIEWED_HEAD = "a" * 40
FAILURE_PREFIX = "station-sweep-run-failure"

#: The refusal the base revision states for this fixture. The change that
#: remembers the refusal must not alter what it says, so the reason is compared
#: with the literal the base produces rather than with a value this code builds.
EXPECTED_REFUSAL = (
    "the review run stored no readable record for head "
    f"{REVIEWED_HEAD} for the run it read, so it has nothing a coordinator "
    "would not add"
)


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{REVIEW_RUN_ID}.json"
    )
    before = real_pointer.exists()
    yield
    assert real_pointer.exists() is before


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A committed repository whose docs directory is the project's mount."""
    assert crew_home().is_relative_to(isolated_reckon_home)
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Refused Review Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


def _record_path() -> Path:
    """The head-keyed review-store path the reviewer was dispatched to write."""
    return review_module.review_path(
        PROJECT, REVIEWED_RUN_ID, reviewed_head_sha=REVIEWED_HEAD
    )


def _write_ended_review_pointer() -> Path:
    """A review run that ended with no stored record, and its run directory."""
    directory = run_dir(REVIEW_RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "node: review-of-reviewed-run\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": REVIEW_RUN_ID,
        "project": PROJECT,
        "session": SESSION,
        "role": "review",
        "launch": "in-harness",
        "backend": "native",
        "created_at": "2026-11-14T14:05:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": REVIEW_NODE_ID,
            "plan": PLAN,
            "section": "s54",
            "time_budget": "25m",
            "role": "review",
            "write_paths": [str(_record_path())],
        },
    }
    _write_json(pointer_path(REVIEW_RUN_ID), pointer)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": REVIEW_RUN_ID, "exit_code": 0}),
        encoding="utf-8",
    )
    return directory


def _count_record_searches(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Record every call to the search a refused review's verdict rests on."""
    calls = {"count": 0}
    original = acceptance._delivered_review_record

    def counting(pointer, project, **kwargs):
        calls["count"] = calls["count"] + 1
        return original(pointer, project, **kwargs)

    monkeypatch.setattr(acceptance, "_delivered_review_record", counting)
    return calls


def _sweep() -> dict:
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT,
        session=SESSION,
        launcher=lambda *args, **kwargs: 0,
    )


def _station_beat() -> station.SweepBeat:
    """The station's runner, driving the same sweep the live station drives."""
    return station.SweepBeat(sweep=resumption.sweep, start=lambda work: work())


def _drain() -> None:
    """Run the durable promotion queue as the fleet service does."""
    service = PromotionService(width=1)
    service.tick()
    service.settle(timeout=120)


def test_an_unmoved_refusal_is_read_once_and_reported_once(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Three sweeps over an unmoved run read and report its refusal once."""
    file_memo.clear()
    directory = _write_ended_review_pointer()
    searches = _count_record_searches(monkeypatch)

    first = _sweep()
    _sweep()
    _sweep()
    three_sweep_searches = searches["count"]
    refusals = [
        entry
        for entry in first["review_self_refused"]
        if entry.get("run_id") == REVIEW_RUN_ID
    ]
    reason = refusals[0]["reason"] if refusals else ""

    # Moving the run directory moves the identity the refusal is remembered
    # under, so the next sweep reads the run again.
    os.utime(directory, None)
    _sweep()
    moved_searches = searches["count"]

    # The station's runner writes one line per entry of the report's
    # ``reviews.failed``. The refusal reaches that report through the durable
    # promotion request the sweep queues, which the fleet service drains
    # between sweeps exactly as it does live.
    capsys.readouterr()
    beat = _station_beat()
    moment = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
    ran = []
    for index in range(3):
        ran.append(beat.request(PROJECT, SESSION, moment=moment, changed=True))
        if index < 2:
            _drain()
    lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if FAILURE_PREFIX in line and REVIEW_RUN_ID in line
    ]

    # Record both counts before asserting, so the base arm's log carries them
    # even though its first assertion fails with them: a base line count of
    # zero would mean this harness never reaches the runner the live station
    # runs, which is reported rather than passed over.
    print(f"record searches over three sweeps: {three_sweep_searches}")
    print(f"record searches after the run directory moved: {moved_searches}")
    print(f"station failure lines: {len(lines)}")

    assert refusals, "no refusal was recorded to compare against the base"
    assert refusals[0]["promoted"] is False
    assert reason == EXPECTED_REFUSAL, reason
    assert three_sweep_searches == 1, three_sweep_searches
    assert moved_searches == 2, moved_searches
    assert len(lines) == 1, lines
