"""A credits-lifted lane is offered as open but scarce.

A codex window that reads 100% is not exhausted while the account holds
purchased credits and a lift admits work against them, so the picker must offer
the lane to the nodes that can use it rather than reading the raw window ceiling
and removing it. The picker therefore gates a candidate on the verdict a
dispatch of the same purpose would take -- through the group's lift-aware budget
block -- and under a credits lift judges a reserved lane by its credit pool
rather than its window, so the tier term alone keeps an ordinary node on the
local lane.

The figures are the ones the lead's grant of 2026-10-20 carried: a 10,000-credit
cap drawn from a balance of 60,000, new dispatches stopping at the 9,000 the
lift reserves. The negative control restores the raw ceiling comparison, and
separately keeps the window term under a lift.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon import budget
from reckon.crew import budget_lift as bl
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, snapshot
from tests.real_home_traces import traces_naming

NOW = datetime(2026, 10, 20, 21, 0, tzinfo=UTC)
ENV = {"USER": "lead"}

CODEX_GROUP = "codex-sub"
PLAIN_GROUP = "claude-sub"
CAP = 10_000.0
BALANCE_AT_GRANT = 60_000.0

ROUTINE: dict = {}
HARD = {
    "class": "general",
    "requirements": {
        "reasoning": "deep",
        "verification": "strict",
        "risk": "elevated",
    },
}

CONFIG: dict[str, Any] = {
    "default_backend": "local-lane",
    "local_backend": "local-lane",
    "roles": {"implement": {}, "review": {}},
    "picker_reserved_lanes": {"codex": {}},
    "budget": {
        "pace_multiple": 1.1,
        "utilisation_ceiling_pct": 100.0,
        "resume_reserve_pct": 5.0,
        "coordinator_reserve_pct": 3.0,
    },
    "backends": {
        "local-lane": {"launch": "cli", "command": "clive", "model": "m-local"},
        "codex-lane": {
            "launch": "cli",
            "command": "codex",
            "model": "m-codex",
            "budget_group": CODEX_GROUP,
        },
        "plain-lane": {
            "launch": "cli",
            "command": "claude",
            "model": "m-plain",
            "budget_group": PLAIN_GROUP,
        },
    },
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the lift store, and assert the real crew home is untouched."""

    home = Path.home() / ".config" / "reckon"
    before = traces_naming(home, (bl.LIFTS_LEAF,), (CODEX_GROUP,))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield tmp_path
    after = traces_naming(home, (bl.LIFTS_LEAF,), (CODEX_GROUP,))
    assert after == before, f"a test wrote the live budget-lifts.json: {after}"


@pytest.fixture(autouse=True)
def _no_fleet_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze every fleet read so a pick touches only the code under test."""

    monkeypatch.setattr(snapshot, "_fit", lambda *a, **k: [])
    monkeypatch.setattr(snapshot, "estimated_context_tokens", lambda *a, **k: 0)
    monkeypatch.setattr(
        snapshot,
        "_serving_observation",
        lambda backend: {"status": "serving", "detail": ""},
    )
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot, "_lane", lambda *a: (3, {"waiting": 0}, {"held": False})
    )


def _request(*, capability: dict | None = None) -> PickRequest:
    return PickRequest(
        project="reckon",
        node=TaskNode(
            id="credit-lift-node",
            goal="judge a lifted lane by its credit pool",
            plan="lane-and-model-routing",
            role="implement",
            spec_level="exact",
            done_when="the lane is offered as open but scarce",
            write_paths=[],
        ),
        session="s",
        capability=ROUTINE if capability is None else capability,
        estimated_context=8000,
    )


def _state(name: str, pct: float, *, balance: float | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "headroom": "known",
        "utilisation_pct": pct,
        "burn_multiple": 1.0,
        "resets_at": "2026-10-27T06:32:42Z",
        "expired": False,
        "observed_at": NOW.isoformat(),
        "source": "account-surface",
    }
    if balance is not None:
        state["credit_balance"] = balance
        state["credits_available"] = True
    return state


def _view(*, codex: dict[str, Any], plain: dict[str, Any]) -> dict[str, Any]:
    """One composed budget snapshot: each lane's window at the stated reading."""

    def allowance(pct: float) -> dict[str, Any]:
        return {
            "state": budget.OBSERVED,
            "utilisation": pct / 100,
            "burn_multiple": 1.0,
            "effective_limit": 0.3,
            "resets_at": None,
            "reset_available": False,
        }

    return {
        "groups": [
            {
                "group": CODEX_GROUP,
                "members": ["codex-lane"],
                "allowance": allowance(codex.get("utilisation_pct") or 0.0),
            },
            {
                "group": PLAIN_GROUP,
                "members": ["plain-lane"],
                "allowance": allowance(plain.get("utilisation_pct") or 0.0),
            },
        ],
        "backends": [
            {"backend": "local-lane", "held": False, "state": {"headroom": "unknown"}},
            {"backend": "plain-lane", "held": False, "state": plain},
            {"backend": "codex-lane", "held": False, "state": codex},
        ],
    }


def _grant(config: dict) -> dict:
    return bl.grant(
        config,
        group=CODEX_GROUP,
        reason="spend purchased credits on high-level work",
        form=bl.CREDITS,
        credits=CAP,
        balance_at_grant=BALANCE_AT_GRANT,
        now=NOW,
        environ=ENV,
    )


def _candidates(config: dict, view: dict, *, capability: dict | None = None) -> list:
    return snapshot.candidates(
        _request(capability=capability),
        config,
        Path("/nonexistent-repo"),
        records=[],
        verdict_inputs={},
        budget_snapshot=view,
        cached_only=True,
    )


def _reasons(options, backend: str) -> list[str]:
    return next(item.reasons for item in options if item.backend == backend)


def _offered(options) -> set[str]:
    return {item.backend for item in options if not item.reasons}


def _has_reason(options, backend: str, prefix: str) -> bool:
    return any(reason.startswith(prefix) for reason in _reasons(options, backend))


# --- the lane a lift opens is offered on its terms ---------------------------


def test_a_credit_lift_offers_the_lane_to_a_hard_node_and_keeps_it_from_an_ordinary_one():
    config = dict(CONFIG)
    _grant(config)
    view = _view(
        codex=_state("codex-lane", 100.0, balance=BALANCE_AT_GRANT - 1000),
        plain=_state("plain-lane", 10.0),
    )

    hard = _candidates(config, view, capability=HARD)
    assert "codex-lane" in _offered(hard)

    routine = _candidates(config, view)
    assert "codex-lane" not in _offered(routine)
    assert any(
        reason.startswith("reserved-lane:")
        for reason in _reasons(routine, "codex-lane")
    )


def test_the_credit_cap_withholds_the_lane_from_every_node():
    config = dict(CONFIG)
    _grant(config)
    view = _view(
        codex=_state("codex-lane", 100.0, balance=BALANCE_AT_GRANT - 9000),
        plain=_state("plain-lane", 10.0),
    )

    for capability in (HARD, ROUTINE):
        options = _candidates(config, view, capability=capability)
        assert "codex-lane" not in _offered(options)
        assert _has_reason(options, "codex-lane", "budget-ceiling:")
        assert any("credit" in reason for reason in _reasons(options, "codex-lane"))


def test_a_spent_window_with_no_lane_offers_nothing():
    config = dict(CONFIG)
    view = _view(
        codex=_state("codex-lane", 100.0),
        plain=_state("plain-lane", 10.0),
    )

    options = _candidates(config, view, capability=HARD)
    assert "codex-lane" not in _offered(options)
    assert _has_reason(options, "codex-lane", "budget-ceiling:")


def test_the_dispatch_reserves_set_the_ceiling_an_unreserved_lane_is_held_at():
    config = dict(CONFIG)

    over = _candidates(
        config,
        _view(
            codex=_state("codex-lane", 91.0),
            plain=_state("plain-lane", 93.0),
        ),
    )
    assert "plain-lane" not in _offered(over)
    assert _has_reason(over, "plain-lane", "budget-ceiling:")

    under = _candidates(
        config,
        _view(
            codex=_state("codex-lane", 91.0),
            plain=_state("plain-lane", 91.0),
        ),
    )
    assert "plain-lane" in _offered(under)


def test_a_reserved_lane_with_no_lift_is_held_by_its_window_term():
    config = dict(CONFIG)
    view = _view(
        codex=_state("codex-lane", 70.0),
        plain=_state("plain-lane", 10.0),
    )

    options = _candidates(config, view, capability=HARD)
    assert "codex-lane" not in _offered(options)
    assert _has_reason(options, "codex-lane", "reserved-lane:")
