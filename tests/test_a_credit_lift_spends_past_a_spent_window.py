"""A credit lift admits work past a spent window until its credit cap is drawn.

The provider keeps serving after the weekly window reads 100% while the account
holds purchased credits, so the window ceiling alone would hold a lane that is
still serving. A credit lift records the balance at grant and admits work until
the credits drawn since then reach its cap. A fresh dispatch stops short of the
cap by the lift's reserve; a resume may spend the reserve.

The negative control is the same spent-window state judged with no credit lift
in the policy, which the ceiling must hold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _backends
from reckon import budget as budget_module
from reckon.crew import budget_lift as bl
from tests.real_home_traces import traces_naming

GROUP = "credit-lift-fixture"
NOW = datetime(2026, 10, 20, 20, 45, tzinfo=UTC)
ENV = {"USER": "lead"}
CAP = 10_000.0
AT_GRANT = 60_564.37


def _config() -> dict:
    return {
        "backends": {GROUP: {"budget_group": GROUP}},
        "budget": {
            "pace_multiple": 1.1,
            "utilisation_ceiling_pct": 100.0,
            "resume_reserve_pct": 5.0,
            "coordinator_reserve_pct": 3.0,
        },
    }


def _spent_window(balance: float | None, *, has_credits: bool = True):
    block = {
        "headroom": "known",
        "utilisation_pct": 100.0,
        "rate_limit_type": "primary",
        "rate_limit_period_minutes": 10080,
        "resets_at": "2026-10-27T06:32:42Z",
        "threshold_status": "rate_limit_reached",
    }
    if balance is not None:
        block["credits"] = {
            "has_credits": has_credits,
            "unlimited": False,
            "balance": balance,
        }
    return budget_module._from_block(
        GROUP,
        block,
        observed_at="2026-10-20T20:45:00Z",
        source="account-surface",
        now=NOW,
    )


def _policy(**lift: object) -> dict:
    block = dict(_config()["budget"])
    block["credit_lift"] = {
        "id": "lift-credit-fixture",
        "cap": CAP,
        "balance_at_grant": AT_GRANT,
        "reserve_fraction": 0.1,
        **lift,
    }
    return budget_module.policy({"budget": block})


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = Path.home() / ".config" / "reckon"
    before = traces_naming(home, (bl.LIFTS_LEAF,), (GROUP,))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield tmp_path
    after = traces_naming(home, (bl.LIFTS_LEAF,), (GROUP,))
    assert after == before, f"a test wrote the live budget-lifts.json: {after}"


def test_the_account_answer_carries_the_credit_balance():
    response = {
        "result": {
            "rateLimits": {
                "primary": {
                    "usedPercent": 100,
                    "windowDurationMins": 10080,
                    "resetsAt": 1793082762,
                },
                "credits": {
                    "hasCredits": True,
                    "unlimited": False,
                    "balance": "60564.3720820000",
                },
                "rateLimitReachedType": "rate_limit_reached",
            }
        }
    }
    budget = _backends._CodexDialect().read_probe(response)
    assert budget["credits"] == {
        "has_credits": True,
        "unlimited": False,
        "balance": pytest.approx(60564.372082),
    }


def test_an_unreadable_balance_records_no_credits():
    assert _backends._credit_reading({"hasCredits": True, "balance": "n/a"}) is None


def test_a_spent_window_with_no_credit_lift_is_held():
    verdict = budget_module.decide(
        _spent_window(AT_GRANT - 100), budget_module.policy(_config()), now=NOW
    )
    assert verdict["held"] is True


def test_a_credit_lift_admits_a_dispatch_under_its_cap():
    verdict = budget_module.decide(_spent_window(AT_GRANT - 100), _policy(), now=NOW)
    assert verdict["held"] is False
    assert "lift-credit-fixture" in verdict["reason"]


def test_a_dispatch_stops_at_the_cap_less_its_reserve():
    stop = CAP * 0.9
    under = budget_module.decide(_spent_window(AT_GRANT - stop + 1), _policy(), now=NOW)
    over = budget_module.decide(_spent_window(AT_GRANT - stop), _policy(), now=NOW)
    assert under["held"] is False
    assert over["held"] is True


def test_a_resume_may_spend_the_reserve_but_not_past_the_cap():
    inside_reserve = _spent_window(AT_GRANT - CAP * 0.95)
    assert (
        budget_module.decide(inside_reserve, _policy(), purpose="resume", now=NOW)[
            "held"
        ]
        is False
    )
    spent = _spent_window(AT_GRANT - CAP)
    assert (
        budget_module.decide(spent, _policy(), purpose="resume", now=NOW)["held"]
        is True
    )


def test_a_reading_with_no_balance_falls_back_to_the_ceiling():
    verdict = budget_module.decide(_spent_window(None), _policy(), now=NOW)
    assert verdict["held"] is True


def test_an_account_without_usable_credits_is_held():
    verdict = budget_module.decide(
        _spent_window(AT_GRANT - 100, has_credits=False), _policy(), now=NOW
    )
    assert verdict["held"] is True


def test_a_credit_grant_needs_a_balance_to_measure_from():
    with pytest.raises(bl.LiftRefusedError):
        bl.grant(
            _config(),
            group=GROUP,
            reason="spend credits",
            form=bl.CREDITS,
            credits=CAP,
            balance_at_grant=None,
            now=NOW,
            environ=ENV,
        )


def test_a_credit_grant_reaches_the_policy_the_ceiling_reads():
    config = _config()
    lift = bl.grant(
        config,
        group=GROUP,
        reason="spend credits",
        form=bl.CREDITS,
        credits=CAP,
        balance_at_grant=AT_GRANT,
        now=NOW,
        environ=ENV,
    )
    block = bl.effective_budget(config, group=GROUP, readings=None, now=NOW)
    assert block["credit_lift"]["id"] == lift["id"]
    judged = budget_module.policy({**config, "budget": block})
    admitted = budget_module.decide(_spent_window(AT_GRANT - 500), judged, now=NOW)
    held = budget_module.decide(_spent_window(AT_GRANT - CAP), judged, now=NOW)
    assert admitted["held"] is False
    assert held["held"] is True
