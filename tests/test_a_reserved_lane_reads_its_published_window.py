"""A reserved lane reads the window its account publishes, and a lift releases it.

The account surface reports one operative window and names it ``primary``,
whatever its length, so neither of the two clocks the picker knew by name is
observed. The picker's window term read only the five-hour clock, took the
absence for no headroom, and withheld the reserved lane on a reading that was
in fact fresh -- the pace row already fell back to the operative window. The
window term now reads that same fill window through one reader.

A lift in force releases the window term whatever its form. A credits lift did
so on its own, and a session lift is more specific than a global one, so a
stronger lift used to shadow the release a weaker one gave. Any lift now
releases it, and the lift-aware budget verdict governs the lane instead.

The negative controls remove the operative-window fallback, which fails the
first case, and restrict the release to credits lifts, which fails the uncapped
session-lift case.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import budget, pace_report
from reckon.crew import budget_lift as bl
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, snapshot
from tests.real_home_traces import traces_naming

CODEX_GROUP = "codex-sub"
CAP = 10_000.0
BALANCE_AT_GRANT = 60_000.0
OPERATIVE_MINUTES = 10_080

ROUTINE: dict = {}
HARDEST = {
    "class": "general",
    "requirements": {
        "reasoning": "deep",
        "verification": "strict",
        "risk": "critical",
    },
}

CONFIG: dict[str, Any] = {
    "default_backend": "local-lane",
    "local_backend": "local-lane",
    "roles": {"implement": {}, "verification": {}},
    "picker_reserved_lanes": {"codex": {}},
    "budget": {
        "pace_multiple": 1.1,
        "utilisation_ceiling_pct": 100.0,
        "resume_reserve_pct": 0.0,
        "coordinator_reserve_pct": 0.0,
    },
    "backends": {
        "local-lane": {"launch": "cli", "command": "clive", "model": "m-local"},
        "codex-lane": {
            "launch": "cli",
            "command": "codex",
            "model": "m-codex",
            "budget_group": CODEX_GROUP,
        },
    },
}


def _moment() -> datetime:
    return datetime.now(UTC)


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


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
    monkeypatch.setattr(
        snapshot.paid_lanes, "refresh_reserved_window", lambda *a, **k: False
    )


def _request(*, capability: dict | None = None, session: str = "s") -> PickRequest:
    return PickRequest(
        project="reckon",
        node=TaskNode(
            id="reserved-window-node",
            goal="read the window the reserved lane publishes",
            plan="lane-and-model-routing",
            role="implement",
            spec_level="exact",
            done_when="the lane reads its published window",
            write_paths=[],
        ),
        session=session,
        capability=ROUTINE if capability is None else capability,
        estimated_context=8000,
    )


def _not_published(period: str) -> dict[str, Any]:
    return {
        "period": period,
        "state": budget.NOT_PUBLISHED,
        "utilisation": None,
        "age_seconds": None,
        "observed_at": None,
        "resets_at": None,
        "window_minutes": None,
    }


def _observed_clock(period: str, pct: float, *, now: datetime) -> dict[str, Any]:
    return {
        "period": period,
        "state": budget.OBSERVED,
        "utilisation": pct / 100,
        "age_seconds": 0.0,
        "observed_at": _stamp(now),
        "resets_at": _stamp(now + timedelta(days=5)),
        "window_minutes": 300,
    }


def _allowance(pct: float, *, now: datetime) -> dict[str, Any]:
    return {
        "state": budget.OBSERVED,
        "utilisation": pct / 100,
        "burn_multiple": 1.0,
        "effective_limit": 1.0,
        "observed_at": _stamp(now),
        "resets_at": _stamp(now + timedelta(days=5)),
        "window_minutes": OPERATIVE_MINUTES,
        "reset_available": False,
    }


def _state(*, pct: float | None, now: datetime, fresh: bool = True) -> dict[str, Any]:
    if pct is None:
        return {"headroom": "unknown", "detail": "no reading"}
    return {
        "headroom": "known",
        "utilisation_pct": pct,
        "burn_multiple": 1.0,
        "resets_at": _stamp(now + timedelta(days=5)),
        "expired": not fresh,
        "observed_at": _stamp(now),
        "source": "account-surface",
    }


def _operative_view(
    *,
    pct: float | None,
    fresh: bool = True,
    clocks: Mapping[str, Any] | None = None,
    now: datetime,
) -> dict[str, Any]:
    """One live budget view whose codex group publishes only an operative window.

    ``clocks`` overrides the two named clocks the group reports, so a case can
    put an observed five-hour clock beside the operative window.
    """
    group: dict[str, Any] = {
        "group": CODEX_GROUP,
        "members": ["codex-lane"],
        "source": "account-surface",
        "clocks": (
            clocks
            if clocks is not None
            else {
                "five_hour": _not_published("five_hour"),
                "seven_day": _not_published("seven_day"),
            }
        ),
    }
    if pct is not None and fresh:
        group["allowance"] = _allowance(pct, now=now)
    elif pct is not None:
        stale = _allowance(pct, now=now - timedelta(hours=2))
        group["allowance"] = {**stale, "observed_at": _stamp(now - timedelta(hours=2))}
    return {
        "checked_at": _stamp(now),
        "groups": [group],
        "backends": [
            {"backend": "local-lane", "held": False, "state": {"headroom": "unknown"}},
            {
                "backend": "codex-lane",
                "held": False,
                "state": _state(pct=pct, now=now, fresh=fresh),
            },
        ],
    }


def _candidates(
    config: dict, view: dict, *, capability: dict | None = None, session: str = "s"
) -> list:
    return snapshot.candidates(
        _request(capability=capability, session=session),
        config,
        Path("/nonexistent-repo"),
        records=[],
        verdict_inputs={},
        budget_snapshot=view,
        cached_only=False,
    )


def _offered(options) -> set[str]:
    return {item.backend for item in options if not item.reasons}


def _reasons(options, backend: str) -> list[str]:
    return next(item.reasons for item in options if item.backend == backend)


def _has_reason(options, backend: str, prefix: str) -> bool:
    return any(reason.startswith(prefix) for reason in _reasons(options, backend))


def _grant_uncapped(config: dict, *, session: str | None) -> dict:
    return bl.grant(
        config,
        group=CODEX_GROUP,
        reason="spend the shared account past the pace it reserved",
        form=bl.UNCAPPED,
        scope="session" if session else bl.GLOBAL,
        session=session,
        now=_moment(),
        environ={"USER": "lead"},
    )


def _grant_credits(config: dict) -> dict:
    return bl.grant(
        config,
        group=CODEX_GROUP,
        reason="spend purchased credits on high-level work",
        form=bl.CREDITS,
        credits=CAP,
        balance_at_grant=BALANCE_AT_GRANT,
        now=_moment(),
        environ={"USER": "lead"},
    )


# --- the window the account publishes -----------------------------------------


def test_an_operative_window_offers_the_lane_and_the_tier_keeps_it_from_an_ordinary_node():
    """Neither named clock observed: the operative window's headroom is read."""

    now = _moment()
    view = _operative_view(pct=0.0, now=now)

    hardest = _candidates(CONFIG, view, capability=HARDEST)
    assert "codex-lane" in _offered(hardest)

    ordinary = _candidates(CONFIG, view)
    assert "codex-lane" not in _offered(ordinary)
    assert _has_reason(ordinary, "codex-lane", "reserved-lane:")
    assert "hardest tier" in _reasons(ordinary, "codex-lane")[0]


def test_an_operative_window_past_the_ceiling_is_withheld_by_the_window_term():
    """The operative window's 70% is judged against the 60% reserved ceiling."""

    now = _moment()
    options = _candidates(
        CONFIG, _operative_view(pct=70.0, now=now), capability=HARDEST
    )
    assert "codex-lane" not in _offered(options)
    withheld = next(
        reason
        for reason in _reasons(options, "codex-lane")
        if reason.startswith("reserved-lane:")
    )
    assert "70" in withheld
    assert "60" in withheld


def test_an_operative_window_past_its_shelf_life_is_withheld_as_stale():
    """A reading older than the shelf life is unknown headroom, not headroom."""

    now = _moment()
    options = _candidates(
        CONFIG, _operative_view(pct=0.0, fresh=False, now=now), capability=HARDEST
    )
    assert "codex-lane" not in _offered(options)
    withheld = next(
        reason
        for reason in _reasons(options, "codex-lane")
        if reason.startswith("reserved-lane:")
    )
    assert "unknown or stale" in withheld


def test_an_observed_five_hour_clock_governs_over_the_operative_window():
    """30% on the named clock beside a 90% operative window offers the lane."""

    now = _moment()
    view = _operative_view(
        pct=90.0,
        now=now,
        clocks={
            "five_hour": _observed_clock("five_hour", 30.0, now=now),
            "seven_day": _not_published("seven_day"),
        },
    )
    options = _candidates(CONFIG, view, capability=HARDEST)
    assert "codex-lane" in _offered(options)


# --- any lift releases the window term ----------------------------------------


def test_an_uncapped_session_lift_releases_a_window_the_lane_never_published():
    """No window, a session lift governs, and the tier term alone decides."""

    config = {
        **CONFIG,
        "budget": {**CONFIG["budget"], "lift": {"max_multiple": 5.0}},
    }
    _grant_uncapped(config, session="s")
    _grant_credits(config)
    now = _moment()
    # No decision about headroom at all: the group publishes nothing.
    view = _operative_view(pct=None, now=now)

    hardest = _candidates(config, view, capability=HARDEST, session="s")
    assert "codex-lane" in _offered(hardest)

    ordinary = _candidates(config, view, session="s")
    assert "codex-lane" not in _offered(ordinary)
    assert _has_reason(ordinary, "codex-lane", "reserved-lane:")
    assert any("hardest tier" in reason for reason in _reasons(ordinary, "codex-lane"))


def test_a_session_lift_does_not_release_a_window_for_another_session():
    """A lift naming one session leaves another session's pick withheld."""

    config = {
        **CONFIG,
        "budget": {**CONFIG["budget"], "lift": {"max_multiple": 5.0}},
    }
    _grant_uncapped(config, session="other")
    now = _moment()
    view = _operative_view(pct=None, now=now)

    options = _candidates(config, view, capability=HARDEST, session="s")
    assert "codex-lane" not in _offered(options)
    withheld = next(
        reason
        for reason in _reasons(options, "codex-lane")
        if reason.startswith("reserved-lane:")
    )
    assert "unknown or stale" in withheld


def test_a_drain_by_lift_leaves_the_budget_verdict_to_withhold_the_lane():
    """A drain-by lift releases the window term; a spent window still holds."""

    config = {
        **CONFIG,
        "budget": {**CONFIG["budget"], "lift": {"max_multiple": 5.0}},
    }
    now = _moment()
    bl.grant(
        config,
        group=CODEX_GROUP,
        reason="drain the account onto the work it is reserved for",
        form=bl.DRAIN_BY,
        target=now + timedelta(days=2),
        now=now,
        environ={"USER": "lead"},
    )
    view = _operative_view(pct=100.0, now=now)

    options = _candidates(config, view, capability=HARDEST)
    assert "codex-lane" not in _offered(options)
    assert _has_reason(options, "codex-lane", "budget-ceiling:")
    # The lift released the window term, so the policy's own term is silent.
    assert not _has_reason(options, "codex-lane", "reserved-lane:")


def test_a_fresh_operative_window_sends_no_reserved_window_probe(monkeypatch):
    """A group publishing a fresh operative window is not probed again."""

    calls: list[str] = []

    def _record(*args, **kwargs):
        calls.append(str(args[1] if len(args) > 1 else "?"))
        return False

    monkeypatch.setattr(snapshot.paid_lanes, "refresh_reserved_window", _record)
    now = _moment()
    _candidates(CONFIG, _operative_view(pct=0.0, now=now), capability=HARDEST)
    assert calls == []


# --- the pace row keeps the clocks it reported --------------------------------


def test_pace_row_fills_the_five_hour_clock_from_the_operative_window(monkeypatch):
    """The row reports the operative window in the clock it always filled."""

    now = _moment()
    entry = {
        "group": CODEX_GROUP,
        "state": budget.OBSERVED,
        "member": "codex-lane",
        "source": budget.WINDOW_SOURCE_RECORDED,
        "clocks": {
            "five_hour": _not_published("five_hour"),
            "seven_day": _not_published("seven_day"),
        },
        "allowance": _allowance(0.0, now=now),
        "bar": {"recommendations": [{}]},
    }
    monkeypatch.setattr(pace_report, "group_pace", lambda *a, **k: [dict(entry)])
    monkeypatch.setattr(pace_report, "_account_surface_readings", lambda *a, **k: {})
    monkeypatch.setattr(budget, "recorded_windows", lambda *a, **k: {})

    row = pace_report.pace_row(
        CONFIG,
        project="reckon",
        lane="codex-lane",
        node="reserved-window-node",
        score=1.0,
        now=now,
    )
    clocks = row["clocks"]
    assert clocks["five_hour"] == {
        "period": "primary",
        "state": budget.OBSERVED,
        "utilisation": 0.0,
        "age_seconds": pytest.approx(0.0, abs=2.0),
        "observed_at": _stamp(now),
        "resets_at": _stamp(now + timedelta(days=5)),
        "window_minutes": OPERATIVE_MINUTES,
    }
    assert clocks["seven_day"]["state"] == budget.NOT_PUBLISHED
