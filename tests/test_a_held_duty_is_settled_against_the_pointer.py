"""A held duty is settled against the run's current pointer.

A session's duties are computed once per station tick and served for as many
minutes as the tick runs. Anything the fleet did in that window is invisible to
the held answer: a review launched against a run's head, a resumed run back in
a working phase, a stopped run whose manifest a resume has replaced. The row
was true when it was computed and is contradicted by the pointer now, so the
answer is settled against the pointer when it is read.

Every case here writes its rows into one snapshot, then moves the fleet, then
reads that same stored snapshot back through
:func:`reckon.crew.obligation_snapshot.without_departed_run_duties`. The first
reading of each fixture, taken before the pointers move, is the positive
control that the row exists at all.

Four facts are measured:

* a review-missing row is dropped once the run's pointer records a review
  dispatched for the head it names;
* the same row is kept when no review has been dispatched, or the dispatch
  stands for an earlier head;
* a promotable-stale row is dropped once the run's pointer reads working after
  a resume, as is a blocked or review-missing row whose resume is recorded
  after its manifest was written;
* a review-missing row with no dispatched review, and a promotable-stale row
  whose run still reads complete, are both kept -- the settlement drops nothing
  a pointer does not answer.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot

PROJECT = "held-duty-fixture"
SESSION = "held-duty-session"
REVIEWED_RUN = "r-reviewed-run"
REVIEW_RUN = "r-launched-review"
HEAD = "7c094e8e2a1b3c4d5e6f708192a3b4c5d6e7f809"
EARLIER_HEAD = "0bcd3d5b52a8a1c2d3e4f5061728394a5b6c7d8e"
MANIFEST_STATUS = "status: blocked\n"


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, so no real pointer is read or written."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _write_pointer(run_id: str, record: dict[str, Any]) -> None:
    path = obligation_snapshot.live_pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _review_dispatch(
    *, status: str, head: str, review_run_id: str = REVIEW_RUN
) -> dict[str, Any]:
    """The entry the reflex records on a run's pointer when it acts for it.

    A launch writes ``dispatched``; an attempt the lane held or refused writes
    its own status, and only ``dispatched`` stands for a review that has been
    started.
    """
    return {
        "status": status,
        "reason": "fixture",
        "run_id": review_run_id,
        "backend": "fixture-lane",
        "head": head,
        "at": datetime.now(tz=UTC).isoformat(),
        "attempt": 1,
    }


def _manifest_file(tmp_path: Path, *, written_at: datetime) -> Path:
    """A manifest written at one stated instant, so the ordering is explicit."""
    path = tmp_path / "manifest.md"
    path.write_text(MANIFEST_STATUS, encoding="utf-8")
    stamp = written_at.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _run_pointer(
    run_id: str,
    *,
    phase: str,
    manifest: Path | None = None,
    resumed_at: datetime | None = None,
    superseded_head: str = "",
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "phase": phase,
    }
    if manifest is not None:
        record["manifest_path"] = str(manifest)
    if resumed_at is not None:
        record["resume_superseded_head"] = {
            "head": superseded_head,
            "at": resumed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
    if review is not None:
        record["review_dispatch"] = dict(review)
    return record


def _producer() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
        "started_at": datetime.now(tz=UTC).isoformat(),
        "code_stamp": obligation_snapshot.source_code_stamp(),
    }


def _publish(rows: list[dict[str, Any]]) -> None:
    """Store one held answer, as a station tick would."""
    payload = {
        "project": PROJECT,
        "session": SESSION,
        "obligations": rows,
        "acknowledged": [],
        "summary": {"count": len(rows), "oldest_age_seconds": 180},
    }
    document = obligation_snapshot.document_for(
        payload,
        computed_at=datetime.now(tz=UTC),
        stream_offset=0,
        producer=_producer(),
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _served_rows() -> list[dict[str, Any]]:
    """The rows a reader acts on, settled against the pointers as they read now."""
    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None, "this case needs a published snapshot"
    return obligation_snapshot.without_departed_run_duties(
        obligation_snapshot.live_payload(document)
    )["obligations"]


def _review_missing_row(*, head: str = HEAD) -> dict[str, Any]:
    return {
        "kind": "review-missing",
        "run_id": REVIEWED_RUN,
        "node": "reviewed-node",
        "plan": "fixture-plan",
        "age_seconds": 180,
        "head": head,
        "next_command": (
            f"reckon crew dispatch --batch --plan fixture-plan --run {REVIEWED_RUN}"
        ),
        "reason": "no stored review covers the delivered head",
    }


def _promotable_row() -> dict[str, Any]:
    return {
        "kind": "promotable-stale",
        "run_id": REVIEWED_RUN,
        "node": "reviewed-node",
        "plan": "fixture-plan",
        "age_seconds": 3600,
        "head": HEAD,
        "next_command": f"reckon crew promote --run {REVIEWED_RUN}",
    }


def _blocked_row() -> dict[str, Any]:
    return {
        "kind": "blocked",
        "run_id": REVIEWED_RUN,
        "node": "reviewed-node",
        "plan": "fixture-plan",
        "age_seconds": 600,
        "next_command": f"reckon crew resume --run {REVIEWED_RUN} --advice '<advice>'",
        "reason": "the worker stopped without a verdict",
    }


def test_a_launched_review_answers_the_review_missing_row(crew_home: Path) -> None:
    """The row is kept at the sweep and dropped once a review is dispatched."""
    _write_pointer(REVIEWED_RUN, _run_pointer(REVIEWED_RUN, phase="complete"))
    _publish([_review_missing_row()])

    assert [row["kind"] for row in _served_rows()] == ["review-missing"]

    _write_pointer(
        REVIEWED_RUN,
        _run_pointer(
            REVIEWED_RUN,
            phase="complete",
            review=_review_dispatch(status="dispatched", head=HEAD),
        ),
    )

    assert _served_rows() == []


def test_a_queued_review_or_an_earlier_head_keeps_the_review_missing_row(
    crew_home: Path,
) -> None:
    """Neither a held or refused attempt nor one reading an older head answers."""
    _write_pointer(REVIEWED_RUN, _run_pointer(REVIEWED_RUN, phase="complete"))
    _publish([_review_missing_row()])

    _write_pointer(
        REVIEWED_RUN,
        _run_pointer(
            REVIEWED_RUN,
            phase="complete",
            review=_review_dispatch(status="awaiting-lane", head=HEAD),
        ),
    )
    assert [row["kind"] for row in _served_rows()] == ["review-missing"]

    _write_pointer(
        REVIEWED_RUN,
        _run_pointer(
            REVIEWED_RUN,
            phase="complete",
            review=_review_dispatch(status="dispatched", head=EARLIER_HEAD),
        ),
    )
    assert [row["kind"] for row in _served_rows()] == ["review-missing"]


def test_a_resume_answers_the_promotion_and_the_halt(
    crew_home: Path, tmp_path: Path
) -> None:
    """A run resumed onto a new attempt owes neither the promotion nor the halt.

    The held answer was computed while the pointer read complete, with the
    manifest the previous attempt wrote. Both rows name state that resume has
    replaced.
    """
    now = datetime.now(tz=UTC)
    manifest = _manifest_file(tmp_path, written_at=now - timedelta(minutes=20))
    _write_pointer(REVIEWED_RUN, _run_pointer(REVIEWED_RUN, phase="complete"))
    _publish([_promotable_row(), _blocked_row()])

    assert sorted(row["kind"] for row in _served_rows()) == [
        "blocked",
        "promotable-stale",
    ]

    _write_pointer(
        REVIEWED_RUN,
        _run_pointer(
            REVIEWED_RUN,
            phase="working",
            manifest=manifest,
            resumed_at=now,
            superseded_head=HEAD,
        ),
    )

    assert _served_rows() == []


def test_a_review_missing_row_is_answered_by_a_resume_too(
    crew_home: Path, tmp_path: Path
) -> None:
    """The row naming the head a resume replaced is withdrawn by that resume."""
    now = datetime.now(tz=UTC)
    manifest = _manifest_file(tmp_path, written_at=now - timedelta(minutes=20))
    _write_pointer(REVIEWED_RUN, _run_pointer(REVIEWED_RUN, phase="complete"))
    _publish([_review_missing_row()])

    assert [row["kind"] for row in _served_rows()] == ["review-missing"]

    _write_pointer(
        REVIEWED_RUN,
        _run_pointer(
            REVIEWED_RUN,
            phase="starting",
            manifest=manifest,
            resumed_at=now,
            superseded_head=HEAD,
        ),
    )

    assert _served_rows() == []


def test_a_pointer_that_does_not_answer_the_row_keeps_it(crew_home: Path) -> None:
    """A held or refused reviewer attempt and a complete run settle nothing."""
    _write_pointer(REVIEWED_RUN, _run_pointer(REVIEWED_RUN, phase="complete"))
    _publish([_review_missing_row(), _promotable_row()])

    _write_pointer(
        REVIEWED_RUN,
        _run_pointer(
            REVIEWED_RUN,
            phase="complete",
            review=_review_dispatch(status="awaiting-lane", head=HEAD),
        ),
    )

    kinds = sorted(row["kind"] for row in _served_rows())
    assert kinds == ["promotable-stale", "review-missing"]


def test_a_pointer_carrying_no_phase_keeps_the_promotion_row(crew_home: Path) -> None:
    """A pointer that states no phase states no move, so it settles nothing."""
    _write_pointer(REVIEWED_RUN, {"run_id": REVIEWED_RUN, "project": PROJECT})
    _publish([_promotable_row()])

    assert [row["kind"] for row in _served_rows()] == ["promotable-stale"]


def test_a_resume_older_than_the_manifest_keeps_the_halt(
    crew_home: Path, tmp_path: Path
) -> None:
    """A manifest written after the resume belongs to the new attempt.

    The comparison is against the manifest's own write, so a resumed run whose
    worker has already delivered a fresh manifest is read against that
    delivery rather than against the one the row was built from.
    """
    now = datetime.now(tz=UTC)
    manifest = _manifest_file(tmp_path, written_at=now + timedelta(minutes=5))
    _write_pointer(REVIEWED_RUN, _run_pointer(REVIEWED_RUN, phase="complete"))
    _publish([_blocked_row()])

    _write_pointer(
        REVIEWED_RUN,
        _run_pointer(
            REVIEWED_RUN,
            phase="working",
            manifest=manifest,
            resumed_at=now,
            superseded_head=HEAD,
        ),
    )

    assert [row["kind"] for row in _served_rows()] == ["blocked"]
