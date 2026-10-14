"""The pace path dates a live run's rollout figure by its own report.

``recorded_windows`` reconciles one member's windows by recency, and a rollout
receipt keys its quota windows without stamping them. A reader that returned a
receipt alone could only age it by the run record that named the session -- a
stamp no earlier than the moment the provider reported the figure -- so a run
whose record was written after another source's report could make an older
figure the group's pace. The fix is to read the session's rollout through the
event-dated reader the lanes path uses and take the rate-limit event's own
stamp; a rollout with no dated event contributes no candidate at all.

The three sources are laid out EVENT_STAMP < RECEIPT_STAMP < RECORD_STAMP: the
rollout reports at EVENT_STAMP, a committed receipt is observed at
RECEIPT_STAMP, and the live run's record is stamped RECORD_STAMP. Dating the
rollout by the record would make it the newest and win; dating it by its event
leaves the RECEIPT_STAMP receipt to speak, and the rollout candidate itself
carries EVENT_STAMP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon import budget
from reckon.crew import paid_lanes

NOW = datetime(2026, 9, 21, 18, 0, 0, tzinfo=UTC)
EVENT_STAMP = NOW - timedelta(minutes=30)
RECEIPT_STAMP = NOW - timedelta(minutes=20)
RECORD_STAMP = NOW - timedelta(minutes=10)

SESSION = "sess-live"
RESETS_IN = NOW + timedelta(days=3)

CONFIG = {
    "backends": {"solo": {"budget_group": "sol"}},
}


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _reset() -> int:
    return int(RESETS_IN.timestamp())


def _receipt_record(windows: list[tuple[int, float]], observed_at: datetime) -> dict:
    """A promoted run whose lane receipt records ``windows`` at ``observed_at``."""
    return {
        "run_id": "r-committed",
        "backend": "solo",
        "completed_at": _iso(observed_at),
        "lane_receipt": {
            "quota_state": "measured",
            "observed_at": _iso(observed_at),
            "quota_windows": [
                {
                    "window_minutes": minutes,
                    "used_percent": used,
                    "resets_at": _reset(),
                }
                for minutes, used in windows
            ],
        },
    }


def _live_pointer(observed_at: datetime) -> dict:
    """The in-flight run record, stamped later than the receipt and the event."""
    return {
        "run_id": "r-in-flight",
        "project": "demo",
        "backend": "solo",
        "phase": "running",
        "session_id": SESSION,
        "observed_at": _iso(observed_at),
        "created_at": _iso(observed_at),
    }


def _rollout_event(observed_at: datetime, *windows: tuple[int, float]) -> object:
    """A rollout's newest rate-limit reading, as the event-dated reader returns it."""
    names = ("primary", "secondary")
    rate_limits = {
        name: {
            "window_minutes": minutes,
            "used_percent": used_percent,
            "resets_at": _reset(),
        }
        for name, (minutes, used_percent) in zip(names, windows, strict=False)
    }
    return paid_lanes.RolloutEvent(
        session_id=SESSION, observed_at=observed_at, rate_limits=rate_limits
    )


def _reader(observed_at: datetime, *windows: tuple[int, float]):
    def reader(session_id: str) -> object:
        assert session_id == SESSION, "the live run's own session is the one read"
        return _rollout_event(observed_at, *windows)

    return reader


def test_the_event_dated_receipt_beats_the_older_rollout_despite_the_later_record() -> (
    None
):
    """The receipt observed between the two stamps wins, so the record stamp
    cannot lift an older rollout figure above a newer receipt."""
    reading = budget.recorded_windows(
        "demo",
        CONFIG,
        now=NOW,
        records=[_receipt_record([(300, 42.0), (10080, 21.0)], RECEIPT_STAMP)],
        pointers=[_live_pointer(RECORD_STAMP)],
        rollouts=_reader(EVENT_STAMP, (300, 55.0), (10080, 66.0)),
    )["solo"]

    assert reading.figure("five_hour").utilisation == pytest.approx(0.42)
    assert reading.figure("seven_day").utilisation == pytest.approx(0.21)
    assert reading.observed_at == RECEIPT_STAMP, (
        "the RECEIPT_STAMP receipt is the newest, not the EVENT_STAMP rollout lifted by the RECORD_STAMP record"
    )


def test_the_rollout_candidate_is_dated_by_its_event_not_the_run_record() -> None:
    """With the receipt gone, the rollout candidate itself carries the event stamp.

    Nothing here competes with the rollout, so the reading the group reports is
    the rollout candidate: it is dated EVENT_STAMP -- the rate-limit event's own instant --
    and never RECORD_STAMP, the record stamp that named the session.
    """
    reading = budget.recorded_windows(
        "demo",
        CONFIG,
        now=NOW,
        records=[],
        pointers=[_live_pointer(RECORD_STAMP)],
        rollouts=_reader(EVENT_STAMP, (300, 55.0), (10080, 66.0)),
    )["solo"]

    assert reading.figure("five_hour").utilisation == 0.55
    assert reading.figure("seven_day").utilisation == 0.66
    assert reading.observed_at == EVENT_STAMP, (
        "the rollout candidate's observed_at is its event"
    )


def test_a_rollout_with_no_dated_event_contributes_no_candidate() -> None:
    """A session whose rollout carried no dated event leaves the receipt to speak."""
    reading = budget.recorded_windows(
        "demo",
        CONFIG,
        now=NOW,
        records=[_receipt_record([(300, 42.0), (10080, 21.0)], RECEIPT_STAMP)],
        pointers=[_live_pointer(RECORD_STAMP)],
        rollouts=lambda session_id: None,
    )["solo"]

    assert reading.figure("seven_day").utilisation == 0.21
    assert reading.observed_at == RECEIPT_STAMP
