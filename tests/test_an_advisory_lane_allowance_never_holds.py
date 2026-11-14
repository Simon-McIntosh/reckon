"""A backend whose lane allowance is advisory is never held by a congested lane.

The router's gate keeps requests past its width off the engine, in its own
queue. A dispatcher that also holds when the router grants no extra workers
keeps that queue shallow, so the lane drains as soon as coordinators stop
feeding it. A backend declared ``lane_allowance: advisory`` records the
router's figures but launches past them. A paused gate still holds, because a
pause admits nothing.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from reckon.crew import lane_document

dispatch_module = importlib.import_module("reckon.crew.dispatch")

SESSION = "advisory-coord"


def _document(path: Path, *, verdict: str) -> Path:
    """A lane document whose router grants this session no extra workers."""
    document = {
        "headroom": -6,
        lane_document.ADMISSION_KEY: {
            lane_document.ADMISSION_WORKER_SLOTS_KEY: 0,
            lane_document.ADMISSION_NEW_SESSION_WORKER_SLOTS_KEY: 0,
            lane_document.ADMISSION_OBSERVED_SECONDS_KEY: 900,
            lane_document.ADMISSION_SESSIONS_KEY: {
                SESSION: {
                    lane_document.SESSION_LIVE_RUNS_KEY: 12,
                    lane_document.SESSION_WORKER_SLOTS_KEY: 0,
                }
            },
            "headroom": -6,
            "verdict": verdict,
        },
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _decide(path: Path, **backend: object) -> dict[str, object]:
    return dispatch_module._dispatch_lane_allowance(
        {"lane_document": str(path), **backend}, session=SESSION
    )


def test_a_binding_lane_holds_when_the_router_grants_nothing(tmp_path: Path) -> None:
    decision = _decide(_document(tmp_path / "lane.json", verdict="congested"))

    assert decision["held"] is True


def test_an_advisory_lane_launches_past_a_congested_router(tmp_path: Path) -> None:
    decision = _decide(
        _document(tmp_path / "lane.json", verdict="congested"),
        lane_allowance="advisory",
    )

    assert decision["held"] is False
    assert decision["state"] == "advisory"
    assert decision["allowance"] is None
    assert decision["router_allowance"] == 0
    assert "router's queue" in str(decision["reason"])


def test_an_advisory_lane_still_holds_on_a_pause(tmp_path: Path) -> None:
    decision = _decide(
        _document(tmp_path / "lane.json", verdict="paused"),
        lane_allowance="advisory",
    )

    assert decision["held"] is True


def test_admission_attempts_a_request_on_an_advisory_lane(tmp_path: Path) -> None:
    """The admission pass reads no zero allowance from an advisory lane."""
    from reckon.crew.admission import holds

    path = _document(tmp_path / "lane.json", verdict="congested")
    config = {
        "backends": {
            "clive": {"lane_document": str(path), "lane_allowance": "advisory"}
        }
    }
    allowance, _source = holds._session_allowance_read(
        {"backend": "clive", "session": SESSION}, config_for=lambda _pointer: config
    )

    assert allowance is None
