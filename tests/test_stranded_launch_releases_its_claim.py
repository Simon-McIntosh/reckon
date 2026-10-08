"""A launch cut off before its worker spawned releases the claim it holds.

The review reflex recognises a standing review by its pointer alone. A review
whose launch died between composing its record and spawning a worker leaves a
pointer that never advances and never ends: nothing is in flight, nothing was
delivered, and the scoring run is told its review is covered forever. A pointer
at a pre-spawn phase with no pid and no launch evidence in its run directory,
past the bound, is a stranded launch; the reflex releases it by discarding it
with the reason recorded and composes the review again. A pointer inside the
bound is still a young launch and stays in flight.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_review_dispatch, runs

PROJECT = "stranded-fixture"
SESSION = "coordinator-fixture"
SOURCE_RUN = "r-scoring-source"
NOW = 1_800_000_000.0


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised crew home; the fixture never touches the operator's."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    (config_home / "live").mkdir()
    return config_home


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _review_pointer(review_run_id: str, *, started_at: str) -> dict:
    """A review pointer holding a pre-spawn phase, with nothing else written."""
    runs.run_dir(review_run_id).mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": review_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "starting",
        "created_at": started_at,
        "attempt_started_at": started_at,
    }
    runs._write_json(runs.pointer_path(review_run_id), record)
    return record


def _scoring_pointer(crew_home: Path, review_run_id: str) -> dict:
    """A completed run whose dispatch record names the review it was given."""
    manifest = crew_home / "manifests" / f"{SOURCE_RUN}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {SOURCE_RUN}\nstatus: complete\ncommits: none\n")
    record = {
        "run_id": SOURCE_RUN,
        "project": PROJECT,
        "session": SESSION,
        "phase": "starting",
        "process_alive": False,
        "manifest_path": str(manifest),
        "node": {
            "id": "source-node",
            "plan": "fixture-plan",
            "section": "s",
            "write_paths": ["seed.txt"],
        },
        recovery.REVIEW_DISPATCH_FIELD: {
            "run_id": review_run_id,
            "status": "dispatched",
            "head": "",
        },
    }
    runs._write_json(runs.pointer_path(SOURCE_RUN), record)
    return record


def test_a_launch_that_recorded_nothing_is_stranded_past_the_bound(
    crew_home: Path,
) -> None:
    review = _review_pointer(
        "r-review-stranded",
        started_at=_at(NOW - recovery.STRANDED_LAUNCH_BOUND_SECONDS - 60),
    )

    assert recovery._stranded_launch(review, now_seconds=NOW) is True
    row = recovery.classify_pointer(review, now_seconds=NOW)
    assert row["classification"] == "launch-failed"
    assert "stranded launch" in row["detail"]


def test_a_launch_inside_the_bound_stays_in_flight(crew_home: Path) -> None:
    review = _review_pointer("r-review-young", started_at=_at(NOW - 60))

    assert recovery._stranded_launch(review, now_seconds=NOW) is False
    row = recovery.classify_pointer(review, now_seconds=NOW)
    assert row["classification"] != "launch-failed"


def test_the_reflex_releases_a_stranded_review_and_composes_again(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = "r-review-stranded"
    _scoring_pointer(crew_home, review)
    _review_pointer(
        review,
        started_at=_at(time.time() - 2 * recovery.STRANDED_LAUNCH_BOUND_SECONDS),
    )
    # The composed review is refused where it would dispatch from a fixture
    # home; what this test measures is that the refusal is reached at all,
    # because the standing stranded claim no longer returns in flight.
    monkeypatch.setattr(recovery_review_dispatch, "carry_review_forward", lambda *a, **k: None)

    report = recovery.dispatch_review_for_run(runs.read_pointer(SOURCE_RUN))

    assert report["reason"] != "a review is already in flight as a live run"
    assert not runs.pointer_path(review).exists()
    assert recovery._review_in_flight(runs.read_pointer(SOURCE_RUN)) == ""
    marker = json.loads(
        (runs.run_dir(review) / "discard.json").read_text(encoding="utf-8")
    )
    assert marker["run_id"] == review
    assert marker["phase"] == "starting"
    assert "stranded launch" in marker["reason"]


def test_the_reflex_leaves_a_young_review_in_flight(crew_home: Path) -> None:
    review = "r-review-young"
    _scoring_pointer(crew_home, review)
    _review_pointer(review, started_at=_at(time.time() - 60))

    report = recovery.dispatch_review_for_run(runs.read_pointer(SOURCE_RUN))

    assert report == {
        "run_id": SOURCE_RUN,
        "dispatched": False,
        "reason": "a review is already in flight as a live run",
        "review_run_id": review,
    }
    assert runs.pointer_path(review).exists()
    assert recovery._review_in_flight(runs.read_pointer(SOURCE_RUN)) == review
