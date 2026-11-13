"""The router decides on preferences; only a hard constraint removes a lane.

A reservation states when the orchestrators' shared lane is meant to be spent,
and a pacing ceiling below the account's own limit states the even share the
operator planned. Neither makes a lane unable to run the node, so neither
removes it: both travel beside the offered candidate and the router weighs
them. Only a lane that cannot run the node is excluded -- one the node's
context does not fit, one that is not serving, one the operator paused, one a
budget verdict the account would refuse, one a review may not use.

The deterministic fallback is the one place the preferences decide, because it
has no router to ask.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, pick, snapshot

NOW = datetime(2026, 11, 3, 6, 5, 0, tzinfo=UTC)
PROJECT = "reckon"

#: The floor a reservation prefers. A node declaring no reasoning requirement
#: sits below it, which is the guided implement node the lead saw held.
HARD = {"reasoning": "deep", "risk": "elevated", "verification": "strict"}
ROUTINE: dict = {}

LOCAL_WINDOW = 230_144
CODEX_WINDOW = 400_000
LIFT_ID = "codex-sub-1793686984-4a0250"


def _config(*, lifted: bool) -> dict:
    budget: dict = {"pace_multiple": 1.1}
    if lifted:
        # The lift rides on the lane's effective budget block, which is where
        # the picker and a dispatch both read it.
        budget.update(
            {
                "lift_id": LIFT_ID,
                "form": "uncapped",
                "reason": "spend the shared account freely",
                "granted_by": "lead",
            }
        )
    return {
        "default_backend": "clive",
        "local_backend": "clive",
        "budget": budget,
        "roles": {"implement": {}, "review": {}},
        "picker_reserved_lanes": {"codex": {"window_ceiling": 0.6}},
        "backends": {
            "clive": {
                "launch": "cli",
                "command": "clive",
                "model": "local-model",
                "effort": "high",
            },
            "codex-astra": {
                "launch": "cli",
                "command": "codex",
                "model": "astra",
                "effort": "high",
            },
        },
    }


def _node(*, hard: bool = False, role: str = "implement", size: int = 8_000) -> TaskNode:
    return TaskNode(
        id=f"n-{role}-{int(hard)}",
        goal="g",
        plan="example",
        role=role,
        spec_level="guided",
        done_when="d",
        time_budget="45m",
    )


def _request(*, hard: bool = False, role: str = "implement", size: int = 8_000):
    return PickRequest(
        PROJECT,
        _node(hard=hard, role=role, size=size),
        capability=HARD if hard else ROUTINE,
        estimated_context=size,
    )


def _state(pct: float = 20.0) -> dict:
    return {
        "headroom": "known",
        "utilisation_pct": pct,
        "burn_multiple": 1.0,
        "resets_at": None,
        "expired": False,
        "observed_at": NOW.isoformat(),
        "source": "account-surface",
    }


def _view() -> dict:
    return {
        "groups": [],
        "backends": [
            {"backend": "clive", "held": False, "state": _state()},
            {"backend": "codex-astra", "held": False, "state": _state()},
        ],
    }


@pytest.fixture
def lane(monkeypatch):
    """A stub lane document: the local lane's room, and every lane serving.

    ``room`` is the figure the picker reads as this session's worker allowance
    on the local lane -- the one number the fallback's own rule turns on. A
    paused lane and a lane that fails context fit are set by the tests that
    need them.
    """

    class Lane:
        def __init__(self):
            self.room: int | None = 3
            self.paused: set[str] = set()

    stub = Lane()

    def _lane_reading(backend, session, documents=None):
        local = backend.get("model") == "local-model"
        slots = stub.room if local else 2
        return slots, {"running": 1, "waiting": 0, "stale": False}, {}

    monkeypatch.setattr(snapshot, "_lane", _lane_reading)
    monkeypatch.setattr(
        snapshot,
        "_dispatch_lane_gate",
        lambda backend: (
            {"state": "paused"}
            if backend.get("model") in stub.paused
            else {"state": "open"}
        ),
    )
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda *a, **k: {"status": "served"},
    )
    monkeypatch.setattr(
        snapshot.routing,
        "shared_verdict_inputs",
        lambda *a: {"capability_cache": {}, "cache_status": "unknown"},
    )
    monkeypatch.setattr(snapshot.budget, "burn_blocks", lambda *a, **k: {})
    monkeypatch.setattr("reckon.crew.picker._record_pick_timings", lambda *a, **k: None)
    return stub


def _windows(monkeypatch, *, codex_window: int = CODEX_WINDOW) -> None:
    def verdict(*, resolution, repo):
        window = LOCAL_WINDOW if resolution.backend == "clive" else codex_window
        return {"allowed": True, "window_tokens": window}

    monkeypatch.setattr(snapshot.routing, "_context_fit_verdict", verdict)


def _pick(request, config, *, tmp_path, choice=None):
    """Run one pick with a stub Jev that records the set it was shown."""

    seen: dict = {}

    def caller(state, questions, **kwargs):
        keys = [key for key in questions["route"]["criteria"] if key != "hold"]
        seen["candidates"] = state["candidates"]
        seen["keys"] = keys
        seen["state"] = state
        chosen = choice or keys[0]
        share = 1.0 / (len(keys) + 1)
        probabilities = dict.fromkeys([*keys, "hold"], share)
        return {
            "model": "stub",
            "usage": {},
            "answers": {
                "route": {
                    "choice": chosen,
                    "confidence": 0.7,
                    "probabilities": probabilities,
                }
            },
        }

    selection = pick(
        request,
        config,
        repo=tmp_path,
        records=[],
        budget_snapshot=_view(),
        caller=caller,
    )
    return selection, seen


def _codex(seen: dict) -> dict:
    return next(
        entry
        for entry in seen["candidates"].values()
        if entry["backend"] == "codex-astra"
    )


def _first_case(tmp_path, monkeypatch) -> None:
    """A lifted reserved lane reaches the router and is placed when chosen."""

    _windows(monkeypatch)
    selection, seen = _pick(
        _request(), _config(lifted=True), tmp_path=tmp_path, choice="codex:astra:codex-astra"
    )
    assert "codex:astra:codex-astra" in seen["keys"]
    codex = _codex(seen)
    assert codex["reserved"] is True
    assert codex["reservation"]["lift_in_force"] is True
    assert selection.backend == "codex-astra"


def test_a_lifted_reserved_lane_reaches_the_router(tmp_path, monkeypatch, lane):
    _first_case(tmp_path, monkeypatch)


def test_without_a_lift_the_reserved_lane_is_still_offered(tmp_path, monkeypatch, lane):
    _windows(monkeypatch)
    _, seen = _pick(_request(), _config(lifted=False), tmp_path=tmp_path)
    assert "codex:astra:codex-astra" in seen["keys"]
    codex = _codex(seen)
    assert codex["reserved"] is True
    assert codex["reservation"]["lift_in_force"] is False
    assert codex["preferences"]


def _third_case(tmp_path, monkeypatch, lane) -> None:
    """A node too large for the local window never reaches Jev with it."""

    _windows(monkeypatch)
    _, seen = _pick(_request(size=300_000), _config(lifted=True), tmp_path=tmp_path)
    assert "local:local-model:clive" not in seen["keys"]
    assert "codex:astra:codex-astra" in seen["keys"]


def test_a_node_too_large_for_local_is_never_offered_it(tmp_path, monkeypatch, lane):
    _third_case(tmp_path, monkeypatch, lane)


def test_a_paused_lane_is_never_offered(tmp_path, monkeypatch, lane):
    _windows(monkeypatch)
    lane.paused.add("local-model")
    _, seen = _pick(_request(), _config(lifted=True), tmp_path=tmp_path)
    assert "local:local-model:clive" not in seen["keys"]


def test_the_fallback_keeps_a_reservation_and_prefers_local(tmp_path, monkeypatch, lane):
    _windows(monkeypatch)

    def unavailable(state, questions, **kwargs):
        raise RuntimeError("jev is down")

    selection = pick(
        _request(hard=True),
        _config(lifted=False),
        repo=tmp_path,
        records=[],
        budget_snapshot=_view(),
        caller=unavailable,
    )
    # An ordinary node is not placed on an unlifted reservation.
    assert selection.backend == "clive"
    assert selection.action == "fallback"

    low_think = pick(
        _request(),
        _config(lifted=False),
        repo=tmp_path,
        records=[],
        budget_snapshot=_view(),
        caller=unavailable,
    )
    assert low_think.backend == "clive"


def test_a_review_is_never_offered_a_metered_lane(tmp_path, monkeypatch, lane):
    _windows(monkeypatch)
    config = {
        **_config(lifted=False),
        "review_excluded_backends": ["codex-astra"],
    }
    _, seen = _pick(_request(role="review"), config, tmp_path=tmp_path)
    assert "codex:astra:codex-astra" not in seen["keys"]


# --- negative controls --------------------------------------------------------


def test_restoring_the_reserved_exclusion_fails_the_first_case(
    tmp_path, monkeypatch, lane
):
    """The ruling is load-bearing: put the policy back in ``reasons`` and fail."""

    monkeypatch.setattr(
        snapshot, "_reserved_lane_evidence", lambda reason: ([reason], [])
    )
    with pytest.raises(AssertionError):
        _first_case(tmp_path, monkeypatch)


def test_offering_a_lane_that_fails_context_fit_fails_the_third_case(
    tmp_path, monkeypatch, lane
):
    """Context fit is a hard constraint: drop it and the node fits locally."""

    def _fit_stub(*args, **kwargs):
        return [], []

    monkeypatch.setattr(snapshot, "_fit", _fit_stub)
    with pytest.raises(AssertionError):
        _third_case(tmp_path, monkeypatch, lane)