"""A subscription-billed lane records no per-token spend.

The claude lanes run on the operator's Claude subscription, and the codex lanes
on the codex ``codex-sub`` group, neither of which charges per token. The
harness still emits a ``total_cost_usd`` figure for them, so recording that
figure as spend makes a subscription lane read as the dearest one on any
surface that ranks or sums cost. Which lanes are subscription-billed is
declared in the model catalogue (``subscription_groups``); the ledger and the
cost surfaces read that declaration rather than a backend set in code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import capabilities, ledger


def _catalogue_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "docs/state/reckon/model-catalogue.yaml"
    )


@pytest.fixture(autouse=True)
def _point_at_this_checkouts_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the catalogue under test, whatever tree ``reckon`` imports from."""
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(_catalogue_path()))


def test_a_claude_lane_budget_records_no_spend_and_the_flag() -> None:
    record = ledger.build_record(
        run_id="r-claude",
        plan="plan-a",
        gate="passed",
        backend="claude",
        budget={"cost_usd": 1.61, "cost_usd_cumulative": 1.61},
    )

    budget = record["budget"]
    assert budget["cost_usd"] is None
    assert budget["cost_usd_cumulative"] is None
    assert budget["cost_usd_imputed"] is True
    assert budget["billing"] == "subscription"
    assert budget["budget_group"] == "claude-sub"
    # The harness figure is preserved, under a name that cannot be summed as
    # spend.
    assert budget["harness_reported_cost_usd"] == 1.61


def test_a_clive_budget_is_unchanged() -> None:
    record = ledger.build_record(
        run_id="r-clive",
        plan="plan-a",
        gate="passed",
        backend="clive",
        budget={"cost_usd": 21.59, "cost_usd_cumulative": 21.59},
    )

    budget = record["budget"]
    assert budget["cost_usd"] is None
    assert budget["cost_usd_cumulative"] is None
    assert budget["cost_usd_imputed"] is True
    # The local lane keeps its original shape: no billing marker is added.
    assert "billing" not in budget


def test_a_lane_outside_every_subscription_group_keeps_its_cost() -> None:
    record = ledger.build_record(
        run_id="r-metered",
        plan="plan-a",
        gate="passed",
        backend="gemini-pro",
        budget={"cost_usd": 1.61, "cost_usd_cumulative": 1.61},
    )

    budget = record["budget"]
    assert budget["cost_usd"] == 1.61
    assert budget["cost_usd_cumulative"] == 1.61
    assert "cost_usd_imputed" not in budget
    assert "billing" not in budget


def test_an_existing_stored_claude_row_reads_as_subscription() -> None:
    """A row committed before the declaration is reinterpreted, not rewritten."""

    stored = {"backend": "claude", "budget": {"cost_usd": 1.61}}

    assert ledger.is_subscription_backend("claude") is True
    # Not a local lane: the subscription path is distinct from the unmetered
    # backend set.
    assert ledger.is_unmetered_backend("claude") is False

    assert capabilities._backend_billing(stored) == "subscription"
    assert capabilities._cost_usd(stored) is None
    assert capabilities._cost_usd_imputed(stored) is True


def test_a_stored_metered_row_keeps_its_cost() -> None:
    stored = {"backend": "gemini-pro", "budget": {"cost_usd": 1.61}}

    assert capabilities._backend_billing(stored) is None
    assert capabilities._cost_usd(stored) == 1.61
    assert capabilities._cost_usd_imputed(stored) is False
