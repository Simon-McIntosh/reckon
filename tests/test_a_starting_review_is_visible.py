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

from reckon.crew import query, recovery, recovery_review_dispatch, runs

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


def _subject_pointer(
    config_home: Path, *, run_id: str = SUBJECT_RUN, node_id: str = SUBJECT_NODE
) -> dict:
    """A completed run whose worker manifest reports completion."""
    manifest = config_home / "runs" / run_id / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {node_id}\nstatus: complete\ncommits: abc123\n", encoding="utf-8"
    )
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "node": {"id": node_id, "plan": "delivery-plan", "section": "delivery"},
        "phase": "complete",
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
        "log_path": str(config_home / "runs" / run_id / "stream.jsonl"),
        "process_alive": False,
        "base_sha": "abc123",
    }


def _review_record_path(config_home: Path, *, subject_run: str = SUBJECT_RUN) -> Path:
    """Where a composed run review writes the record it is granted."""
    return config_home / "runs" / subject_run / "review-records" / f"{subject_run}.json"


def _review_pointer(
    config_home: Path,
    *,
    phase: str,
    run_id: str = REVIEW_RUN,
    subject_run: str = SUBJECT_RUN,
    node_id: str = SUBJECT_NODE,
) -> dict:
    """A review run dispatched for the subject, naming the record it will write."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "node": {
            "id": f"review-of-{node_id}",
            "write_paths": [
                str(_review_record_path(config_home, subject_run=subject_run))
            ],
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
    narrowed = [
        row
        for row in query.project_live_rows(
            [subject, review], fields=["review_run_id", "review_run_phase"]
        )
        if row["run_id"] == SUBJECT_RUN
    ]
    assert narrowed == [
        {
            "run_id": SUBJECT_RUN,
            "review_run_phase": "starting",
            "review_run_id": REVIEW_RUN,
        }
    ]


@pytest.mark.parametrize(
    ("phase", "in_flight"),
    [("queued", True), ("working", True), ("complete", False), ("failed", False)],
)
def test_only_a_dispatched_review_counts_as_in_flight(
    config_home: Path, phase: str, in_flight: bool
) -> None:
    """A review is in flight while it has been dispatched and has not ended.

    Queued and working are dispatched reviews whose record does not exist yet,
    so the subject reads them. A complete or failed review has ended: the store
    answers for a record it filed, and a run left owed a review is offered its
    dispatch again rather than pointed at a review that will write nothing.
    """
    subject = _subject_pointer(config_home)
    _write_pointer(subject)
    _write_pointer(_review_pointer(config_home, phase=phase))

    row = recovery.classify_pointer(subject)

    if in_flight:
        assert row["review_present"] is True
        assert row["review_run_id"] == REVIEW_RUN
        assert row["review_run_phase"] == phase
        assert "reckon crew dispatch" not in row["next_action"]
    else:
        assert row["review_present"] is False
        assert row["review_run_id"] is None
        assert row["next_action"].startswith("reckon crew dispatch ")


def test_one_live_read_lists_pointers_once(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classifying a snapshot must not list the live pointers again per row.

    The live view reads the pointers once and classifies every row from that
    read. Recognising an already-dispatched review is the one classification
    that consults the snapshot, so a view over five rows must leave the
    directory read at one — otherwise the cost grows with the fleet.
    """
    reads: list[str] = []
    real = recovery_review_dispatch.list_live

    def counting(*args: object, **kwargs: object) -> list[dict]:
        reads.append("read")
        return real(*args, **kwargs)

    monkeypatch.setattr(recovery_review_dispatch, "list_live", counting)
    monkeypatch.setattr(runs, "list_live", counting)

    for index in range(5):
        _write_pointer(
            _subject_pointer(
                config_home,
                run_id=f"{SUBJECT_RUN}-{index}",
                node_id=f"{SUBJECT_NODE}-{index}",
            )
        )

    # The view's own read, then one classification pass over what it read.
    records = runs.list_live()
    rows = query.project_live_rows(records)

    assert len(rows) == 5
    assert len(reads) == 1
