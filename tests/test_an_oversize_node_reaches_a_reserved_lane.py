"""A node no unreserved lane can fit is offered the lane a policy reserves.

The reservation keeps ordinary work off the orchestrators' lane, and its tier
and attempts terms are what keep an ordinary node on a lane anyone may use. A
node larger than every unreserved lane's window is the case those terms were
never meant to settle: no lane outside the policy's coverage can run it at all,
so withholding the reserved lane refuses the dispatch outright and the work
never reaches the queue. The waiver that admits it is decided over the whole
"candidate set, holds every other term -- the lane's window ceiling, its budget
verdict and its own context fit -- and records itself in the candidate's note
rather than its exclusions.
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
#: the lane is withheld from it on the tier term and never by default.
ROUTINE: dict = {}
LOCAL_WINDOW = 230_144
CODEX_WINDOW = 400_000
WAIVER_NOTE = "offered because no open lane fits the node's context"


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
    """No unreserved lane fits the node, so the policy's tier term is waived."""

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
    assert offered["codex-astra"].note == WAIVER_NOTE
    # The pick admits a candidate by an empty reasons list; the exception is
    # recorded in the note, where pick()'s split cannot read it as an exclusion.
    assert offered["codex-astra"].reasons == []


def test_a_node_within_the_local_window_is_not_offered_the_reserved_lane(
    live, monkeypatch
) -> None:
    """An ordinary node keeps the reservation: nothing needed waiving."""

    _windows(monkeypatch)
    options = _candidates(live, _request(8_000))
    offered = {
        candidate.backend: candidate for candidate in options if not candidate.reasons
    }
    assert "local-served" in offered
    assert offered["local-served"].note is None
    assert "codex-astra" not in offered
    assert any(
        reason.startswith("reserved-lane: ")
        for reason in _reasons(options, "codex-astra")
    )


def test_an_oversize_node_is_refused_when_the_lane_window_is_at_its_ceiling(
    live, monkeypatch
) -> None:
    """The window ceiling is not waived: a spent lane still withholds itself."""

    _windows(monkeypatch)
    options = _candidates(live, _request(300_000), view=_view(codex_pct=60.0))
    offered = {
        candidate.backend: candidate for candidate in options if not candidate.reasons
    }
    assert "codex-astra" not in offered
    candidate = next(c for c in options if c.backend == "codex-astra")
    assert candidate.note is None
    # The waiver is what the node is judged on, and the window term it never
    # reaches today is the one that holds: the tier term is gone and the
    # candidate is still withheld by its own account window.
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
    assert "codex-astra" not in offered
    assert any(
        reason.startswith("reserved-lane: ")
        for reason in _reasons(options, "codex-astra")
    )


def test_the_policy_is_keyed_by_the_lane_the_state_reads() -> None:
    """The waiver reads the same lane derivation the policy is keyed by."""

    config = _config()
    assert dispatch_picker.lane_family(config, "codex-astra") == "codex"
    assert dispatch_picker.lane_family(config, "local-served") == "local"
