"""One live read composes every row's lane admission from one lane reading.

A request waiting on a lane stores the verdict sentence its dispatcher composed
at the moment it was queued. The live view used to replay that recorded sentence
for each row, so three rows of one session dispatched minutes apart reported the
allowances their own dispatches had seen -- 0, 63 and 65 extra workers in one
response read at 16:47Z -- and a reader had no way to tell which sentence
described the lane in front of them.

The view now reads the lane document once for the whole composition and gives
every row that one reading, so rows of one session agree on the lane's present
allowance. A stored sentence is still carried, because it is what the row said
when it was written, but only dated by the moment it was recorded: undated, a
day-old sentence is indistinguishable from a present one.

Both cases fail if the view goes back to replaying the stored sentences, which
is the mutation this module exists to catch.
"""

from __future__ import annotations

from typing import Any

from reckon.crew import query

SESSION = "s-coordinator"
OBSERVED_AT = "2026-11-02T16:47:00Z"
LIVE_ALLOWANCE = 4


def _lane_document() -> dict[str, Any]:
    """One lane reading: the router grants this session four extra workers."""
    return {
        "state": "measured",
        "observed_at": OBSERVED_AT,
        "headroom": 3,
        "running": 20,
        "waiting": 2,
        "admission": {
            "worker_slots": 5,
            "new_session_worker_slots": 4,
            "observed_seconds": 900,
            "sessions": {
                SESSION: {"worker_slots": LIVE_ALLOWANCE, "live_runs": 6},
            },
        },
    }


def _queued_record(index: int, stored_allowance: int) -> dict[str, Any]:
    """One queued request carrying the sentence its own dispatch recorded."""
    recorded_at = f"2026-11-02T16:4{index}:00+00:00"
    return {
        "run_id": f"r-{index}",
        "project": "sample",
        "backend": "clive",
        "session": SESSION,
        "phase": "queued",
        "created_at": recorded_at,
        "queued_at": recorded_at,
        "reason": (
            "the lane's router grants "
            f"{stored_allowance} extra workers to session {SESSION} "
            "(the session's own worker slots); the router's own verdict is full"
        ),
        "node": {"id": f"node-{index}", "plan": "a-plan", "section": "s1"},
    }


def _records() -> list[dict[str, Any]]:
    return [_queued_record(index, stored) for index, stored in enumerate((0, 63, 65))]


def _declare_one_lane(monkeypatch, reads: list[str], path: str) -> None:
    """Point every row at one lane document and count how often it is read."""
    monkeypatch.setattr(
        query,
        "_lane_config_for",
        lambda record, configs: {"backends": {"clive": {"lane_document": path}}},
    )

    def read(path: str) -> dict[str, Any]:
        reads.append(path)
        return _lane_document()

    monkeypatch.setattr(query, "_read_lane_document", read)


def test_one_lane_reading_composes_one_allowance(monkeypatch, tmp_path) -> None:
    """Three rows of one session report the lane's allowance in one read."""
    reads: list[str] = []
    lane_path = str(tmp_path / "lane.json")
    _declare_one_lane(monkeypatch, reads, lane_path)

    rows = query.project_live_rows(
        _records(),
        fields=(
            "run_id",
            "detail",
            "lane_allowance",
            "lane_reading_observed_at",
        ),
    )

    # The whole composition costs one read of the lane document, taken once for
    # the three rows rather than once per row.
    assert reads == [lane_path]
    assert [row["lane_allowance"] for row in rows] == [LIVE_ALLOWANCE] * 3
    assert [row["lane_reading_observed_at"] for row in rows] == [OBSERVED_AT] * 3
    for row in rows:
        assert f"grants {LIVE_ALLOWANCE} extra workers" in row["detail"]

    # The reading the rows were composed from is dated once, not once per row.
    _readings, documents = query._live_lane_readings(
        [dict(record) for record in _records()]
    )
    statement = query._lane_reading_statement(documents)
    assert statement == {
        "documents": [{"path": lane_path, "observed_at": OBSERVED_AT}],
        "observed_at": OBSERVED_AT,
    }


def test_each_stored_sentence_is_shown_with_its_recorded_time(
    monkeypatch, tmp_path
) -> None:
    """A row's stored verdict is dated by the moment it was recorded."""
    lane_path = str(tmp_path / "lane.json")
    _declare_one_lane(monkeypatch, [], lane_path)

    rows = query.project_live_rows(_records(), fields=("run_id", "detail"))
    by_run = {row["run_id"]: row["detail"] for row in rows}

    for index, stored in enumerate((0, 63, 65)):
        detail = by_run[f"r-{index}"]
        assert (
            f"the verdict recorded at 2026-11-02T16:4{index}:00+00:00 read:" in detail
        )
        assert f"grants {stored} extra workers" in detail

    # A row whose lane publishes nothing has no current reading to report, and
    # its stored sentence is still dated rather than served as if it were now.
    monkeypatch.setattr(query, "_lane_config_for", lambda record, configs: {})
    unread = query.project_live_rows(
        [dict(record) for record in _records()], fields=("run_id", "detail")
    )
    for index, row in enumerate(unread):
        assert f"(recorded at 2026-11-02T16:4{index}:00+00:00)" in row["detail"]
