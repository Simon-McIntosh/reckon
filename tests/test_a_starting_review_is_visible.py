"""A review run that has started is visible as its subject's review.

A composed review is dispatched before it files anything, and while it is
starting the store holds no record for it. A surface reading only the store
therefore read the subject as still owed a review, printed the dispatch, and
sent the coordinator to a command the lane refuses because the review run
already holds it. The review run's own pointer is the evidence that exists in
that window, so the subject's row reads it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import query, recovery, runs

PROJECT = "sample-project"
SESSION = "sample-session"
SUBJECT_RUN = "source-run"
SUBJECT_NODE = "source-node"
REVIEW_RUN = "review-run"


def _write_pointer(pointer: dict) -> None:
    directory = runs.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pointer['run_id']}.json").write_text(
        json.dumps(pointer), encoding="utf-8"
    )


def _subject_pointer(config_home: Path) -> dict:
    """A completed run whose worker manifest reports completion."""
    manifest = config_home / "runs" / SUBJECT_RUN / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: source-node\nstatus: complete\ncommits: abc123\n", encoding="utf-8"
    )
    return {
        "run_id": SUBJECT_RUN,
        "project": PROJECT,
        "session": SESSION,
        "node": {"id": SUBJECT_NODE, "plan": "delivery-plan", "section": "delivery"},
        "phase": "complete",
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
        "log_path": str(config_home / "runs" / SUBJECT_RUN / "stream.jsonl"),
        "process_alive": False,
        "base_sha": "abc123",
    }


def _review_record_path(config_home: Path) -> Path:
    """Where a composed run review writes the record it is granted."""
    return config_home / "runs" / SUBJECT_RUN / "review-records" / f"{SUBJECT_RUN}.json"


def _review_pointer(config_home: Path, *, phase: str) -> dict:
    """A review run dispatched for the subject, naming the record it will write."""
    return {
        "run_id": REVIEW_RUN,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "node": {
            "id": f"review-of-{SUBJECT_NODE}",
            "write_paths": [str(_review_record_path(config_home))],
        },
        "launcher_host": "another-host",
    }


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the crew home the live pointers and the store are read from."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def test_a_starting_review_counts_as_the_runs_review_in_flight(
    config_home: Path,
) -> None:
    """The subject names the review standing for it, and offers no dispatch."""
    subject = _subject_pointer(config_home)
    _write_pointer(subject)
    _write_pointer(_review_pointer(config_home, phase="starting"))

    row = recovery.classify_pointer(subject)

    assert row["review_present"] is True
    assert row["review_run_id"] == REVIEW_RUN
    assert row["review_run_phase"] == "starting"
    assert "reckon crew dispatch" not in row["next_action"]
    assert REVIEW_RUN in row["next_action"]
    assert REVIEW_RUN in row["detail"] and "starting" in row["detail"]


def test_the_live_view_lists_the_starting_review(config_home: Path) -> None:
    """The review's own row is in the live view, and the subject points at it."""
    subject = _subject_pointer(config_home)
    review = _review_pointer(config_home, phase="starting")
    _write_pointer(subject)
    _write_pointer(review)

    rows = {row["run_id"]: row for row in query.project_live_rows([subject, review])}

    assert rows[REVIEW_RUN]["phase"] == "starting"
    assert rows[SUBJECT_RUN]["review_run_id"] == REVIEW_RUN
    assert rows[SUBJECT_RUN]["review_run_phase"] == "starting"
    # Both names are in the live vocabulary the pre-flight reads, so a caller
    # narrowing its read to them is served rather than refused.
    narrowed = query.project_live_rows(
        [subject], fields=["review_run_id", "review_run_phase"]
    )
    assert narrowed == [
        {
            "run_id": SUBJECT_RUN,
            "review_run_id": REVIEW_RUN,
            "review_run_phase": "starting",
        }
    ]


def test_a_run_without_a_review_still_prints_the_dispatch(config_home: Path) -> None:
    """The dispatch survives for the run that has no review run at all."""
    subject = _subject_pointer(config_home)
    _write_pointer(subject)

    row = recovery.classify_pointer(subject)

    assert row["review_present"] is False
    assert row["review_run_id"] is None
    assert row["next_action"].startswith("reckon crew dispatch ")
    assert f"--node review-of-{SUBJECT_NODE}" in row["next_action"]
