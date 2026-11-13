"""A reserved lane is offered to every node its own context fit admits.

The reservation states when the orchestrators' shared lane is meant to be
spent: its tier floor, its attempt threshold and its window ceiling are
preferences the router weighs, so a node below the floor is offered the lane
with the policy's own words beside it. Context fit, a serving refusal, a paused
lane and a budget verdict that holds still remove the lane, because those make
it unable to run the node.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from reckon import flight
from reckon.crew import dispatch_picker
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, snapshot

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)

#: A node declaring no requirement at all: it reaches no reserved-lane floor, so
#: the lane is a preference for it rather than an exclusion.
ROUTINE: dict = {}
LOCAL_WINDOW = 230_144
CODEX_WINDOW = 400_000


def _config() -> dict:
    return {
        "default_backend": "codex-astra",
        "local_backend": "local-served",
        "budget": {"pace_multiple": 1.1},
        "roles": {"implement": {}, "review": {}},
        "picker_reserved_lanes": {"codex": {"window_ceiling": 0.6}},
        "backends": {
            "local-served": {
                "launch": "cli",
                "command": "codex",
                "model": "local",
                "effort": "high",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            },
            "codex-astra": {
                "launch": "cli",
                "command": "codex",
                "model": "astra",
                "effort": "high",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            },
        },
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


def _node() -> TaskNode:
    return TaskNode(
        id="n",
        goal="g",
        plan="example",
        role="implement",
        spec_level="guided",
        done_when="d",
        time_budget="45m",
    )


def _request(estimated_context: int) -> PickRequest:
    return PickRequest(
        "example",
        _node(),
        capability=ROUTINE,
        estimated_context=estimated_context,
    )


def _fresh(pct: float) -> dict:
    return {
        "headroom": "known",
        "utilisation_pct": pct,
        "burn_multiple": 1.0,
        "resets_at": None,
        "expired": False,
        "observed_at": NOW.isoformat(),
        "source": "account-surface",
    }


def _view(*, local_pct: float = 20.0, codex_pct: float = 20.0) -> dict:
    return {
        "groups": [],
        "backends": [
            {"backend": "local-served", "held": False, "state": _fresh(local_pct)},
            {"backend": "codex-astra", "held": False, "state": _fresh(codex_pct)},
        ],
    }


def _windows(monkeypatch) -> None:
    """Each lane's own window, so context fit is measured per candidate."""

    def verdict(*, resolution, repo):
        window = LOCAL_WINDOW if resolution.backend == "local-served" else CODEX_WINDOW
        return {"allowed": True, "window_tokens": window}

    monkeypatch.setattr(snapshot.routing, "_context_fit_verdict", verdict)


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(tmp_path / "lane.json"))
    monkeypatch.setattr(snapshot.routing, "shared_verdict_inputs", lambda *a: {})
    monkeypatch.setattr(snapshot.budget, "burn_blocks", lambda *a, **k: {})
    monkeypatch.setattr(snapshot.budget, "group_pace", lambda *a, **k: [])
    monkeypatch.setattr(
        snapshot.budget,
        "state_for",
        lambda name, *a, **k: snapshot.budget.BudgetState(name),
    )
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda project, name, backend, **k: {"status": "served"},
    )
    monkeypatch.setattr(
        snapshot.routing, "_competence_verdict", lambda **k: {"allowed": True}
    )
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot, "_lane", lambda *a: (3, {"waiting": 0}, {"held": False})
    )
    return tmp_path


def _candidates(root, request, view=None):
    return snapshot.candidates(
        request,
        _config(),
        root,
        records=[],
        verdict_inputs={},
        budget_snapshot=_view() if view is None else view,
    )


def _offered(options) -> dict:
    return {
        candidate.backend: candidate for candidate in options if not candidate.reasons
    }


def _reasons(options, backend) -> list[str]:
    return next(c.reasons for c in options if c.backend == backend)


def test_an_oversize_node_with_no_tier_is_offered_the_reserved_lane(
    live, monkeypatch
) -> None:
    """No unreserved lane fits the node, and the reserved lane is offered."""

    _windows(monkeypatch)
    options = _candidates(live, _request(300_000))
    offered = {
        candidate.backend: candidate for candidate in options if not candidate.reasons
    }
    assert "local-served" not in offered
    assert any(
        reason.startswith("context-fit: ")
        for reason in _reasons(options, "local-served")
    )
    assert "codex-astra" in offered
    assert offered["codex-astra"].reserved
    # The reservation is evidence: it is never in the reasons list pick() reads
    # as an exclusion.
    assert offered["codex-astra"].reasons == []
    assert any(
        preference.startswith("reserved-lane: ")
        for preference in offered["codex-astra"].preferences
    )


def test_a_node_within_the_local_window_is_still_offered_the_reserved_lane(
    live, monkeypatch
) -> None:
    """An ordinary node is offered the lane with the reservation stated."""

    _windows(monkeypatch)
    options = _candidates(live, _request(8_000))
    offered = {
        candidate.backend: candidate for candidate in options if not candidate.reasons
    }
    assert "local-served" in offered
    assert "codex-astra" in offered
    assert offered["codex-astra"].reserved
    assert any(
        "hardest tier" in preference
        for preference in offered["codex-astra"].preferences
    )


def test_an_oversize_node_reads_the_lane_window_it_is_at_its_ceiling_on(
    live, monkeypatch
) -> None:
    """A spent window is stated on the reservation rather than applied."""

    _windows(monkeypatch)
    options = _candidates(live, _request(300_000), view=_view(codex_pct=60.0))
    offered = {
        candidate.backend: candidate for candidate in options if not candidate.reasons
    }
    assert "codex-astra" in offered
    # The window term is stated beside the lane rather than applied to it: the
    # candidate is offered, its reservation records the ceiling it is over, and
    # the router decides.
    reservation = offered["codex-astra"].reservation
    assert reservation["ceiling_pct"] == 60.0
    assert reservation["window_utilisation_pct"] == 60.0
    assert reservation["withheld"]
    policy = flight.reserved_lane_policies(_config())["codex"]
    withheld = snapshot._reserved_lane_exclusion(
        "codex",
        policy,
        declared={},
        attempts=0,
        utilisation=60.0,
        waive_tier=True,
    )
    assert withheld is not None
    assert "ceiling" in withheld


def test_an_oversize_node_is_not_offered_a_lane_whose_own_window_it_exceeds(
    live, monkeypatch
) -> None:
    """The waiver admits the policy, not the lane: its own context fit governs."""

    def verdict(*, resolution, repo):
        window = LOCAL_WINDOW if resolution.backend == "local-served" else 250_000
        return {"allowed": True, "window_tokens": window}

    monkeypatch.setattr(snapshot.routing, "_context_fit_verdict", verdict)
    options = _candidates(live, _request(300_000))
    offered = {
        candidate.backend: candidate for candidate in options if not candidate.reasons
    }
    assert "codex-astra" not in offered
    assert any(
        reason.startswith("context-fit: ")
        for reason in _reasons(options, "codex-astra")
    )


def test_a_lane_excluded_for_another_reason_earns_no_waiver(live, monkeypatch) -> None:
    """Availability is not context fit, so the reservation stands."""

    # The local lane's own window fits the node, so context fit is not why it is
    # removed: the serving probe is.
    def verdict(*, resolution, repo):
        return {"allowed": True, "window_tokens": 400_000}

    monkeypatch.setattr(snapshot.routing, "_context_fit_verdict", verdict)
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda project, name, backend, **k: (
            {"status": "refused"} if name == "local-served" else {"status": "served"}
        ),
    )
    options = _candidates(live, _request(300_000))
    offered = {
        candidate.backend: candidate for candidate in options if not candidate.reasons
    }
    assert _reasons(options, "local-served") == ["availability: refused"]
    # A lane held by another rule is unaffected by the reservation: the
    # reserved lane is offered because nothing about it removes it.
    assert "codex-astra" in offered
    assert offered["codex-astra"].reserved


def test_the_waiver_removes_only_the_policys_own_term(live, monkeypatch) -> None:
    """A lane held by another rule stays held: only the tier term is waived."""

    _windows(monkeypatch)
    monkeypatch.setattr(
        snapshot,
        "_dispatch_lane_gate",
        lambda backend: (
            {"state": "paused"}
            if backend.get("model") == "astra"
            else {"state": "open"}
        ),
    )
    options = _candidates(live, _request(300_000))
    offered = {
        candidate.backend: candidate for candidate in options if not candidate.reasons
    }
    assert "codex-astra" not in offered
    reasons = _reasons(options, "codex-astra")
    assert "lane-gate: paused" in reasons
    # The paused gate is the whole reason the lane is gone; the reservation's
    # own terms are stated beside it rather than added to the exclusion.
    assert not any(reason.startswith("reserved-lane: ") for reason in reasons)


def test_the_policy_is_keyed_by_the_lane_the_state_reads() -> None:
    """The waiver reads the same lane derivation the policy is keyed by."""

    config = _config()
    assert dispatch_picker.lane_family(config, "codex-astra") == "codex"
    assert dispatch_picker.lane_family(config, "local-served") == "local"
