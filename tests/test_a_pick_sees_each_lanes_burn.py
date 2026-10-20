"""Each metered lane's row says what it is spending and what it has left.

A lane's window ceiling, its whole-window burn average and its reset say
nothing about how fast the account is spending now or what it can afford over
the hours ahead, so a router weighing a node's priority against the lane's
availability has no figure for either. The burn block is that figure, derived
once in ``reckon.budget`` from the lane's own recorded readings as a series and
rendered by the picker on every metered candidate.

The figures are the ones that motivated the work: a lane that went from 4% to
20% in two hours is spending 8 points an hour whether its window is two percent
through or half spent, and a lane at 96% with six and a half days left may
afford about 0.15 points over the next six hours. Under a 10,000-credit lift
whose reserve is a tenth, a balance fallen from 60,559.94 to 59,559.94 leaves an
8,000-credit pool drawn at 2,000 an hour, which is four hours of runway.

The negative controls derive the window rate from the whole-window average
instead of the trailing span -- which the 2%-elapsed case must fail -- and leave
the reserve out of the pool, which the pool case must fail.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import budget
from reckon.crew import budget_lift as bl
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, prompts, snapshot
from tests.real_home_traces import traces_naming

PROJECT = "reckon"
GROUP = "codex-sub"
LOCAL = "local-lane"
LANE = "codex-lane"
NOW = datetime(2026, 10, 20, 21, 0, tzinfo=UTC)
PERIOD_MINUTES = 10_080
WEEK_HOURS = PERIOD_MINUTES / 60.0
CAP = 10_000.0
BALANCE_AT_GRANT = 60_559.94
ENDPOINT = "burn-fixture"


def _config() -> dict[str, Any]:
    return {
        "local_backend": LOCAL,
        "default_backend": LOCAL,
        "roles": {"implement": {}},
        "routing": {"budget_period_hours": 6},
        "budget": {
            "pace_multiple": 1.1,
            "utilisation_ceiling_pct": 100.0,
            "resume_reserve_pct": 5.0,
        },
        "backends": {
            LOCAL: {"launch": "cli", "command": "clive", "model": "m-local"},
            LANE: {
                "launch": "cli",
                "command": "codex",
                "model": "m-codex",
                "budget_group": GROUP,
            },
        },
    }


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the crew home, and assert the real one is left untouched."""

    home = Path.home() / ".config" / "reckon"
    before = traces_naming(home, ("crew/live",), (PROJECT, ENDPOINT))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield
    after = traces_naming(home, ("crew/live",), (PROJECT, ENDPOINT))
    assert after == before, f"a test wrote the live crew home: {after}"


def _record(
    home: Path,
    *,
    run_id: str,
    when: datetime,
    utilisation_pct: float,
    hours_to_reset: float,
    balance: float | None = None,
    backend: str = LANE,
) -> None:
    """One recorded budget block, as a live run's pointer carries it."""

    block: dict[str, Any] = {
        "headroom": "known",
        "utilisation_pct": utilisation_pct,
        "rate_limit_type": "primary",
        "rate_limit_period_minutes": PERIOD_MINUTES,
        "resets_at": (when + timedelta(hours=hours_to_reset)).isoformat(),
        "threshold_status": "ok",
    }
    if balance is not None:
        block["credits"] = {
            "has_credits": True,
            "unlimited": False,
            "balance": balance,
        }
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "backend": backend,
        "observed_at": when.isoformat(),
        "budget": block,
    }
    path = home / "crew" / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer), encoding="utf-8")


def _lift_block() -> dict[str, Any]:
    """The credit lift as the effective budget block carries it."""

    return {
        "id": "lift-credit-fixture",
        "cap": CAP,
        "balance_at_grant": BALANCE_AT_GRANT,
        "reserve_fraction": 0.1,
    }


def _burn(config: dict | None = None) -> dict[str, Any]:
    return budget.burn_block(
        PROJECT, GROUP, config=_config() if config is None else config, now=NOW
    )


def _grant_ending(config: dict, hours: float) -> dict:
    return bl.grant(
        config,
        group=GROUP,
        reason="spend purchased credits on high-level work",
        form=bl.CREDITS,
        credits=CAP,
        balance_at_grant=BALANCE_AT_GRANT,
        ends={"kind": "at", "at": (NOW + timedelta(hours=hours)).isoformat()},
        now=NOW,
        environ={"USER": "lead"},
    )


# --- the window's own rate ---------------------------------------------------


def test_a_young_window_still_reports_what_it_is_spending_now(tmp_path: Path):
    """4% then 20% two hours later is 8 points an hour at 2% elapsed."""

    hours_to_reset = WEEK_HOURS * 0.98
    _record(
        tmp_path,
        run_id="young-a",
        when=NOW - timedelta(hours=2),
        utilisation_pct=4.0,
        hours_to_reset=hours_to_reset + 2,
    )
    _record(
        tmp_path,
        run_id="young-b",
        when=NOW,
        utilisation_pct=20.0,
        hours_to_reset=hours_to_reset,
    )

    burn = _burn()
    assert burn["window_rate_pct_per_hour"] == pytest.approx(8.0, rel=0.001)
    # The density the floor withholds: at 2% elapsed the whole-window average
    # reads null, where the trailing rate does not.
    assert (
        budget._burn_multiple(20.0, PERIOD_MINUTES, int(hours_to_reset * 3600)) is None
    )


def test_the_window_even_share_is_the_remainder_over_the_hours_to_reset(
    tmp_path: Path,
):
    """96% with 6.5 days left affords about 0.15 points over six hours."""

    _record(
        tmp_path,
        run_id="even-share",
        when=NOW,
        utilisation_pct=96.0,
        hours_to_reset=6.5 * 24,
    )

    burn = _burn()
    assert burn["window_even_share_pct"] == pytest.approx(0.15, abs=0.01)
    assert burn["window_rate_pct_per_hour"] is None


# --- the credits lift --------------------------------------------------------


def test_the_pool_is_the_cap_less_its_reserve_less_the_credits_drawn(
    tmp_path: Path,
):
    config = _config()
    _grant_ending(config, 48.0)
    _record(
        tmp_path,
        run_id="pool",
        when=NOW,
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn(config)
    assert burn["credit_pool_remaining"] == pytest.approx(8_000.0)
    assert burn["credit_pool_cap"] == pytest.approx(10_000.0)


def test_the_credit_rate_and_runway_follow_the_balance_fall(tmp_path: Path):
    config = _config()
    _grant_ending(config, 48.0)
    _record(
        tmp_path,
        run_id="credit-a",
        when=NOW - timedelta(hours=1),
        utilisation_pct=3.0,
        hours_to_reset=6.5 * 24 + 1,
        balance=BALANCE_AT_GRANT + 1000.0,
    )
    _record(
        tmp_path,
        run_id="credit-b",
        when=NOW,
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn(config)
    assert burn["credit_rate_per_hour"] == pytest.approx(2_000.0, rel=0.001)
    assert burn["runway_hours"] == pytest.approx(4.0, rel=0.001)


def test_the_credit_even_share_is_the_pool_over_the_hours_to_the_end(
    tmp_path: Path,
):
    config = _config()
    _grant_ending(config, 48.0)
    _record(
        tmp_path,
        run_id="pool-even",
        when=NOW,
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn(config)
    assert burn["credit_even_share"] == pytest.approx(1_000.0, rel=0.001)


def test_the_credit_fields_are_absent_with_no_credits_lift(tmp_path: Path):
    _record(
        tmp_path,
        run_id="no-lift",
        when=NOW,
        utilisation_pct=96.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn()
    assert "credit_pool_remaining" not in burn
    assert "credit_pool_cap" not in burn
    assert "credit_rate_per_hour" not in burn
    assert "credit_even_share" not in burn
    assert burn["window_even_share_pct"] == pytest.approx(0.15, abs=0.01)


def test_one_reading_rates_nothing(tmp_path: Path):
    config = _config()
    _grant_ending(config, 48.0)
    _record(
        tmp_path,
        run_id="single",
        when=NOW,
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn(config)
    assert burn["window_rate_pct_per_hour"] is None
    assert burn["credit_rate_per_hour"] is None
    assert burn["runway_hours"] is None


def test_two_readings_under_the_minimum_span_are_one_sample(tmp_path: Path):
    config = _config()
    config["budget"] = {**config["budget"], "credit_lift": _lift_block()}
    _record(
        tmp_path,
        run_id="close-a",
        when=NOW - timedelta(minutes=10),
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24 + 0.2,
        balance=BALANCE_AT_GRANT,
    )
    _record(
        tmp_path,
        run_id="close-b",
        when=NOW,
        utilisation_pct=20.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn(config)
    assert burn["window_rate_pct_per_hour"] is None
    assert burn["credit_rate_per_hour"] is None
    assert burn["credit_pool_remaining"] == pytest.approx(8_000.0)


# --- the picker renders it ---------------------------------------------------


def _request() -> PickRequest:
    return PickRequest(
        project=PROJECT,
        node=TaskNode(
            id="burn-node",
            goal="see each lane's burn",
            plan="lane-and-model-routing",
            role="implement",
            spec_level="exact",
            done_when="each metered lane's row carries its burn block",
            write_paths=[],
        ),
        session="s",
        capability={},
        estimated_context=8000,
    )


def _view(observed_at: str) -> dict[str, Any]:
    return {
        "groups": [
            {
                "group": GROUP,
                "members": [LANE],
                "allowance": {
                    "state": budget.OBSERVED,
                    "utilisation": 0.20,
                    "burn_multiple": 1.0,
                    "effective_limit": None,
                    "resets_at": (NOW + timedelta(hours=6.5 * 24)).isoformat(),
                    "reset_available": False,
                    "observed_at": observed_at,
                },
            }
        ],
        "backends": [
            {"backend": LOCAL, "held": False, "state": {"headroom": "unknown"}},
            {
                "backend": LANE,
                "held": False,
                "state": {
                    "headroom": "known",
                    "utilisation_pct": 20.0,
                    "burn_multiple": 1.0,
                    "resets_at": (NOW + timedelta(hours=6.5 * 24)).isoformat(),
                    "expired": False,
                    "observed_at": observed_at,
                    "source": "account-surface",
                },
            },
        ],
    }


@pytest.fixture
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


@pytest.mark.usefixtures("_no_fleet_reads")
def test_a_picks_rendered_state_carries_the_burn_block_on_metered_candidates(
    tmp_path: Path,
):
    # A pick reads the wall clock rather than a moment handed to it, so this
    # case dates its readings from now; the pure budget cases above state the
    # moment they are judged at instead.
    live = datetime.now(UTC)
    _record(
        tmp_path,
        run_id="rendered-a",
        when=live - timedelta(hours=2),
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24 + 2,
    )
    _record(
        tmp_path,
        run_id="rendered-b",
        when=live,
        utilisation_pct=20.0,
        hours_to_reset=6.5 * 24,
    )
    request = _request()
    options = snapshot.candidates(
        request,
        _config(),
        Path("/nonexistent-repo"),
        records=[],
        verdict_inputs={},
        budget_snapshot=_view(NOW.isoformat()),
        cached_only=True,
    )
    state = prompts.build_state(
        node=request.node,
        capability={},
        estimated_context=8000,
        comment="",
        candidates=options,
        lane={"return_times": {}, "lanes": {}, "local_lane": {}},
    )
    rows = state["candidates"]
    metered = next(row for row in rows.values() if row["backend"] == LANE)
    local = next(row for row in rows.values() if row["backend"] == LOCAL)
    assert metered["burn"]["window_rate_pct_per_hour"] == pytest.approx(8.0, rel=0.001)
    assert "burn" not in local
