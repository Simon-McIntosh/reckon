"""A discard keeps the queued request's admission record.

A queued pointer carries what dispatch wrote when it held the request and what
the admission pass stamped when a queued launch was attempted and refused. A
request is usually discarded because it waited too long, which is exactly when
someone needs to read why it waited — so those fields must not die with the
pointer. The discard marker already records the departure; these tests assert
it records the admission record beside it, wherever the marker lands, and that
a key the pointer never carried stays out of the record rather than appearing
as null.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import promotion
from reckon.crew.queue_admission import _stamp_failure


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move pointers, run directories and the discards spool into a temp home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _admission_record(run_id: str, *, guard: bool) -> dict:
    """A queued pointer carrying the admission and failure fields a real one has.

    The failure keys are written by the producer's own stamper so the test
    cannot drift from the fields an attempted launch actually leaves. ``guard``
    selects whether the stamp carries a failure guard — a stamp without one
    leaves ``failure_guard`` off the pointer, which is the absent key the
    discard record must not invent.
    """
    record = {
        "run_id": run_id,
        "project": "proj",
        "plan": "plan-a",
        "section": "3",
        "session": "session-1",
        "node": {"id": "queued-node", "plan": "plan-a", "time_budget": "20m"},
        "backend": "local",
        "phase": "queued",
        "queued_at": "2026-10-18T08:54:00+00:00",
        "created_at": "2026-10-18T08:54:00+00:00",
        "hold": {"held": True, "reason": "session-allowance", "session_slots": 0},
        "reason": "session-allowance",
    }
    _stamp_failure(
        record,
        "brief for the composed review is not readable",
        moment=datetime(2026, 10, 18, 9, 0, tzinfo=UTC),
        guard={"reason": "gate-congested"} if guard else None,
    )
    return record


def _store_pointer(run_id: str, record: dict) -> None:
    crew._write_json(crew.pointer_path(run_id), record)


def _spool_record(run_id: str) -> dict:
    return json.loads(promotion.discard_spool_path(run_id).read_text())


def _run_dir_record(run_id: str) -> dict:
    return json.loads(promotion.discard_record_path(run_id).read_text())


def test_a_queued_discard_keeps_the_admission_record_in_the_spool(home: Path) -> None:
    """A request with no run directory records its admission fields in the spool."""
    run_id = "r-queued"
    record = _admission_record(run_id, guard=False)
    _store_pointer(run_id, record)
    assert not crew.run_dir(run_id).exists()

    crew.discard(run_id)

    assert not crew.pointer_path(run_id).exists()
    written = _spool_record(run_id)

    for key in (
        "queued_at",
        "session",
        "backend",
        "reason",
        "failure",
        "failure_at",
        "failure_attempts",
        "retry_at",
        "failure_reported_at",
    ):
        assert key in written, f"the discard record dropped {key!r}"
        assert written[key] == record[key], f"{key!r} did not survive the discard"

    # The nested allowance travels whole, so the gate reading it was queued
    # under still reads at the discard record.
    assert written["hold"] == record["hold"]

    # The pointer never carried a guard, so the record must not invent one.
    assert "failure_guard" not in record
    assert "failure_guard" not in written


def test_a_run_with_a_directory_records_the_fields_where_it_lives(home: Path) -> None:
    """The same fields land in a run directory's record, guard included."""
    run_id = "r-run-dir"
    record = _admission_record(run_id, guard=True)
    crew.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _store_pointer(run_id, record)

    crew.discard(run_id)

    assert not promotion.discard_spool_path(run_id).exists()
    written = _run_dir_record(run_id)

    assert written["hold"] == record["hold"]
    assert written["failure"] == record["failure"]
    assert written["retry_at"] == record["retry_at"]
    assert written["failure_guard"] == record["failure_guard"]


def test_a_discarded_request_still_reads_as_discarded(home: Path) -> None:
    """Keeping the admission record does not change what a reader is told."""
    run_id = "r-still-discarded"
    record = _admission_record(run_id, guard=False)
    _store_pointer(run_id, record)

    assert promotion.discard_recorded(run_id) is False
    crew.discard(run_id)
    assert promotion.discard_recorded(run_id) is True
