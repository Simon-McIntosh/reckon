"""The lanes view is the one carrier of the calling session's lane share.

A coordinator reading ``crew(view="lanes", session=...)`` learns its own
published share of the local lane, how many runs the router counts against it,
and how old the document those figures came from is. The share is read through
the same function a dispatch refusal quotes, so a coordinator reading the lane
and a dispatch the lane refused cannot disagree about one number. The directory
and the ready-set view carry no copy of these figures: one carrier, so a second
read cannot drift from the first.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import mcp_views
from reckon.crew import directory, obligations, paid_lanes

SESSION = "11111111-2222-3333-4444-555555555555"
UNLISTED_SESSION = "99999999-8888-7777-6666-555555555555"
OBSERVED = datetime(2030, 1, 2, 3, 0, 0, tzinfo=UTC)
SHELF_LIFE_SECONDS = 45
SESSION_WORKER_SLOTS = 7
SESSION_LIVE_RUNS = 2
LANE_WORKER_SLOTS = 41


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _lane_document(
    path: Path,
    *,
    observed: datetime = OBSERVED,
    shelf_life_seconds: int | None = SHELF_LIFE_SECONDS,
) -> Path:
    """A lane document of the shape the router publishes, with one listed session."""
    document: dict[str, Any] = {
        "state": "measured",
        "observed_at": _stamp(observed),
        "headroom": 9,
        "running": 3,
        "admission": {
            "verdict": "open",
            "worker_slots": LANE_WORKER_SLOTS,
            "sessions": {
                SESSION: {
                    "live_runs": SESSION_LIVE_RUNS,
                    "worker_slots": SESSION_WORKER_SLOTS,
                }
            },
        },
    }
    if shelf_life_seconds is not None:
        document["suggested_shelf_life_seconds"] = shelf_life_seconds
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture()
def lane_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _lane_document(tmp_path / "lane.json")
    monkeypatch.setenv(paid_lanes.LOCAL_LANE_DOCUMENT_ENV, str(path))
    return path


def _view(composed: datetime, **kwargs: Any) -> dict[str, Any]:
    return mcp_views.crew_lanes_view({}, [], composed_at=_stamp(composed), **kwargs)


def _share(composed: datetime, session: str = SESSION) -> dict[str, Any]:
    return _view(composed, session=session)["session_share"]


def test_the_lanes_view_carries_the_calling_sessions_share(
    lane_document: Path,
) -> None:
    """The share, the live runs and the document's vintage arrive together."""

    share = _share(OBSERVED + timedelta(seconds=5))

    assert share["session"] == SESSION
    assert share["worker_slots"] == SESSION_WORKER_SLOTS
    # The figure is the one a dispatch refusal quotes, not a second reading.
    assert share["worker_slots"] == obligations._published_worker_slots(SESSION)
    assert share["live_runs"] == SESSION_LIVE_RUNS
    assert share["observed_at"] == _stamp(OBSERVED)
    assert share["age_seconds"] == 5
    assert share["shelf_life_seconds"] == SHELF_LIFE_SECONDS
    assert share["stale"] is False


def test_a_document_older_than_its_publication_period_reads_stale(
    lane_document: Path,
) -> None:
    """A stale document is marked stale rather than carried as current.

    Negative control: dropping the staleness comparison leaves ``stale`` false
    here, because nothing else in the block looks at the age.
    """

    share = _share(OBSERVED + timedelta(seconds=SHELF_LIFE_SECONDS + 1))

    assert share["stale"] is True
    assert share["age_seconds"] == SHELF_LIFE_SECONDS + 1
    assert share["age_seconds"] > share["shelf_life_seconds"]
    assert "older than its" in share["detail"]


def test_a_document_without_a_shelf_life_is_not_called_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freshness the document does not state is unjudged, never fresh by default."""

    path = _lane_document(tmp_path / "lane.json", shelf_life_seconds=None)
    monkeypatch.setenv(paid_lanes.LOCAL_LANE_DOCUMENT_ENV, str(path))

    share = _share(OBSERVED + timedelta(days=30))

    assert share["stale"] is False
    assert share["shelf_life_seconds"] is None
    assert "no shelf life" in share["detail"]


def test_an_unlisted_session_reads_unknown_not_zero(lane_document: Path) -> None:
    """A session the router has not admitted has no live-run count yet."""

    share = _share(OBSERVED, session=UNLISTED_SESSION)

    assert share["live_runs"] == "unknown"
    # The share itself falls back exactly as the dispatch refusal reads it.
    assert share["worker_slots"] == LANE_WORKER_SLOTS
    assert share["worker_slots"] == obligations._published_worker_slots(
        UNLISTED_SESSION
    )


def test_the_share_appears_only_when_a_session_is_named(
    lane_document: Path,
) -> None:
    assert "session_share" not in _view(OBSERVED)


def test_the_other_views_copy_no_figure(lane_document: Path) -> None:
    """The directory and the ready-set view name no figure of their own."""

    ready = mcp_views.ready_set_view({"project": "probe"})
    board = directory.directory(pointers=[])

    for payload in (ready, board):
        assert "session_share" not in payload
        assert "worker_slots" not in payload
        assert "live_runs" not in payload


def test_the_ready_set_view_derives_no_coordinator_of_its_own() -> None:
    """The unreachable ledger derivation is gone; an un-injected read is empty."""

    assert not hasattr(mcp_views, "_ready_set_coordinators")

    payload = mcp_views.ready_set_view(
        {"project": "probe", "immediate_roadmap": [{"slug": "plan#s1"}]},
        ready_idle=True,
    )

    assert payload["ready"][0]["coordinator"] is None
    assert payload["ready"][0]["plan_owner"] == ""
