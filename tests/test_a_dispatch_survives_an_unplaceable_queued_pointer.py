"""A dispatch's yield check survives a queued pointer with no readable arrival time.

A dispatch that finds room on its lane first asks for its session's oldest
waiting request, through the same ordering the admission pass ranks by. The
ordering refuses a queued pointer whose ``queued_at`` cannot be read, and one
such pointer -- a record read mid-write was enough on 14 November -- refused
the whole order, so the dispatch crashed. The admission pass and the live view
already leave such a pointer out; the dispatch now does the same.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import queue_admission, queue_order
from reckon.crew import runs as runs_module
from reckon.crew.dispatching import requests

SESSION = "s-yield"
LANE = "clive"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _queued(run_id: str, *, queued_at: datetime | None) -> dict:
    pointer = {
        "run_id": run_id,
        "project": "yield-proj",
        "session": SESSION,
        "backend": LANE,
        "phase": "queued",
        "node": {"id": run_id},
    }
    if queued_at is not None:
        pointer["queued_at"] = queued_at.isoformat()
    return pointer


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    pointers: list[dict] = []
    monkeypatch.setattr(runs_module, "list_live", lambda *a, **k: list(pointers))
    monkeypatch.setattr(queue_order, "_roadmap_priority_keys", lambda *_a: {})
    monkeypatch.setattr(queue_admission, "_retry_backoff_hold", lambda *a, **k: None)
    monkeypatch.setattr(
        queue_admission, "_earlier_overlapping_request", lambda *a, **k: None
    )
    return pointers


def test_an_unplaceable_pointer_does_not_refuse_the_dispatch(live):
    live.append(_queued("r-unplaceable", queued_at=None))
    live.append(_queued("r-placed", queued_at=NOW - timedelta(minutes=5)))
    chosen = requests._session_older_admissible_request(
        session=SESSION, backend_name=LANE, moment=NOW
    )
    assert chosen is not None
    assert chosen["run_id"] == "r-placed"


def test_the_ordering_itself_still_refuses_such_a_pointer():
    with pytest.raises(ValueError, match="no readable queued_at"):
        queue_order.admission_order(
            [_queued("r-unplaceable", queued_at=None)], [], now=NOW
        )
