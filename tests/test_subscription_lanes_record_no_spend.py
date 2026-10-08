"""A subscription-billed lane records no per-token spend.

The claude lanes run on the operator's Claude subscription, and the codex lanes
on the codex ``codex-sub`` group, neither of which charges per token. The
harness still emits a ``total_cost_usd`` figure for them, so recording that
figure as spend makes a subscription lane read as the dearest one on any
surface that ranks or sums cost. Which lanes are subscription-billed is
declared in resolved flight config as a backend's catalogue ``budget_group``
(a group whose name ends in ``-sub``); the ledger and the cost surfaces read
that declaration rather than a backend set in code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import capabilities, flight, ledger
from reckon.crew.picker import outcomes

# A host layer declaring each backend under test. The catalogue only fills keys
# for a backend another layer already defines, so the catalogue's budget groups
# reach resolution only through a host layer that names the backend. The claude
# lane's group is left unset here so the catalogue supplies ``claude-sub``.
HOST_LAYER = """\
version: 1

backends:
  claude:
    command: claude
  clive:
    command: clive
  gemini-pro:
    command: gemini
"""

# The same host layer, but declaring the claude lane's group itself, so a
# resolved-config reader must prefer it over the catalogue's ``claude-sub``.
HOST_LAYER_OVERRIDING_CLAUDE = """\
version: 1

backends:
  claude:
    command: claude
    budget_group: claude-metered
"""


# A project-layer flight that moves one lane the host left metered into a
# subscription group. The catalogue only fills keys for a backend another layer
# already defines, so the project names the backend the host declares.
PROJECT_FLIGHT = """\
version: 1

backends:
  gemini-pro:
    budget_group: research-sub
"""


def _write_project_flight(project: str) -> None:
    """Write a project layer where the resolver reads it, without a mount."""
    path = flight.project_config_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROJECT_FLIGHT)


def _catalogue_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "docs/state/reckon/model-catalogue.yaml"
    )


@pytest.fixture()
def _hermetic_flight(isolated_reckon_home, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Resolve against this checkout's catalogue and a host layer we own.

    The suite isolates the configuration home per test; this writes the host
    layer into it and points the catalogue override at the checkout's own
    catalogue, so the real configuration home is never read and a declaration
    there cannot make an assertion pass or fail.
    """
    (isolated_reckon_home / "flight.yaml").write_text(HOST_LAYER)
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(_catalogue_path()))
    return isolated_reckon_home


pytestmark = pytest.mark.usefixtures("_hermetic_flight")


def _budget(backend: str, cost: float = 1.61) -> dict:
    record = ledger.build_record(
        run_id=f"r-{backend}",
        plan="plan-a",
        gate="passed",
        backend=backend,
        budget={"cost_usd": cost, "cost_usd_cumulative": cost},
    )
    return record["budget"]


def test_a_claude_lane_budget_records_no_spend_and_the_flag() -> None:
    budget = _budget("claude")

    assert budget["cost_usd"] is None
    assert budget["cost_usd_cumulative"] is None
    assert budget["cost_usd_imputed"] is True
    assert budget["billing"] == "subscription"
    assert budget["budget_group"] == "claude-sub"
    # The harness figure is preserved, under a name that cannot be summed as
    # spend.
    assert budget["harness_reported_cost_usd"] == 1.61


def test_a_clive_budget_is_unchanged() -> None:
    budget = _budget("clive", cost=21.59)

    assert budget["cost_usd"] is None
    assert budget["cost_usd_cumulative"] is None
    assert budget["cost_usd_imputed"] is True
    # The local lane keeps its original shape: no billing marker is added.
    assert "billing" not in budget


def test_a_lane_outside_every_subscription_group_keeps_its_cost() -> None:
    budget = _budget("gemini-pro")

    assert budget["cost_usd"] == 1.61
    assert budget["cost_usd_cumulative"] == 1.61
    assert "cost_usd_imputed" not in budget
    assert "billing" not in budget


def test_a_host_override_of_a_budget_group_wins_over_the_catalogue(
    isolated_reckon_home: Path,
) -> None:
    """The catalogue declares ``claude`` on ``claude-sub``; the host layer here
    declares it on a metered group instead, and a resolution-reading lookup must
    report the host's declaration rather than the catalogue's.
    """
    (isolated_reckon_home / "flight.yaml").write_text(HOST_LAYER_OVERRIDING_CLAUDE)

    assert ledger.backend_budget_group("claude") == "claude-metered"
    assert ledger.is_subscription_backend("claude") is False
    assert _budget("claude")["cost_usd"] == 1.61


def test_a_project_flight_moves_a_backend_into_a_subscription_group() -> None:
    """A project-layer override reaches billing on that project's rows only.

    The host layer leaves ``gemini-pro`` metered; one project's flight layer
    moves it into a subscription group. A row for that project must record no
    per-token spend — nulled, flagged imputed, and naming the group — while the
    same backend on another project's row keeps its cost. The override is a
    property of the project, not of the lane, so both directions are asserted.
    """
    _write_project_flight("moved")

    moved = ledger.build_record(
        run_id="r-moved",
        plan="plan-a",
        project="moved",
        gate="passed",
        backend="gemini-pro",
        budget={"cost_usd": 1.61, "cost_usd_cumulative": 1.61},
    )["budget"]
    elsewhere = ledger.build_record(
        run_id="r-elsewhere",
        plan="plan-a",
        project="elsewhere",
        gate="passed",
        backend="gemini-pro",
        budget={"cost_usd": 1.61, "cost_usd_cumulative": 1.61},
    )["budget"]

    assert moved["cost_usd"] is None
    assert moved["cost_usd_cumulative"] is None
    assert moved["cost_usd_imputed"] is True
    assert moved["billing"] == "subscription"
    assert moved["budget_group"] == "research-sub"

    assert elsewhere["cost_usd"] == 1.61
    assert elsewhere["cost_usd_cumulative"] == 1.61
    assert "billing" not in elsewhere
    assert "cost_usd_imputed" not in elsewhere


def test_a_read_row_reads_its_project_layer() -> None:
    """A row read back under its project is billed by that project's override.

    The read surfaces resolve billing at read time, so a row stamped with the
    project whose flight moved the lane into a subscription group is labelled a
    subscription there and metered under any other project.
    """
    _write_project_flight("moved")

    moved = {
        "backend": "gemini-pro",
        "budget": {"cost_usd": 1.61},
        "_project": "moved",
    }
    elsewhere = {
        "backend": "gemini-pro",
        "budget": {"cost_usd": 1.61},
        "_project": "elsewhere",
    }

    assert capabilities._backend_billing(moved) == "subscription"
    assert capabilities._cost_usd(moved) is None
    assert capabilities._cost_usd_imputed(moved) is True

    assert capabilities._backend_billing(elsewhere) is None
    assert capabilities._cost_usd(elsewhere) == 1.61


def test_a_malformed_catalogue_leaves_a_lane_metered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A catalogue that does not parse must not stop a promotion.

    ``read_layer_file`` raises ``FlightConfigError`` on a duplicate key. The
    ledger reads the catalogue at every promotion, so an unhandled raise there
    would stop all promotions on one bad edit; it falls back to no declaration,
    which leaves the lane metered and its recorded cost untouched. The host
    layer still declares the claude lane, so a catalogue that had parsed would
    have made it a subscription and this assertion would fail.
    """
    malformed = tmp_path / "catalogue.yaml"
    malformed.write_text(
        "version: 1\n"
        "\n"
        "backends:\n"
        "  claude:\n"
        "    budget_group: claude-sub\n"
        "    budget_group: claude-sub\n"
    )
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(malformed))

    budget = _budget("claude")

    assert budget["cost_usd"] == 1.61
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


def test_picker_outcomes_name_subscription_backends_not_dollars() -> None:
    """The picker's metered_spend reports subscription lanes as subscription.

    ``summarize`` names every backend
    billed as a subscription in the
    ``billing`` block, so a reader of metered spend is told which lanes the
    dollars exclude rather than summing a subscription lane's harness figure.
    """
    rows = {
        "demo": [
            _picker_row("r-claude", "claude"),
            _picker_row("r-meter", "gemini-pro"),
        ]
    }

    report = outcomes.summarize(rows, {})
    billing = report["metered_spend"]["billing"]

    assert billing["subscription_backends"] == ["claude"]
    assert "gemini-pro" not in billing["subscription_backends"]


def test_backend_billing_labels_a_subscription_lane() -> None:
    """The billing class a row's backend carries is the catalogue's declaration.

    :func:`capabilities._backend_billing` — which the routing row's ``billing``
    key and its ``median_cost_usd`` block read — labels a subscription lane so
    a reader is told which lanes the dollars exclude, and nulls its cost.
    """
    subscription = {"backend": "claude", "budget": {"cost_usd": 1.61}}
    metered = {"backend": "gemini-pro", "budget": {"cost_usd": 1.61}}

    assert capabilities._backend_billing(subscription) == "subscription"
    assert capabilities._backend_billing(metered) is None
    # A subscription sample contributes no dollar figure to the median.
    assert capabilities._cost_usd(subscription) is None
    assert capabilities._cost_usd(metered) == 1.61


def _picker_row(name: str, backend: str) -> dict:
    return {
        "run_id": name,
        "node": name,
        "plan": "sample",
        "role": "implement",
        "spec_level": "guided",
        "backend": backend,
        "route_mode": "picker",
        "gate": "passed",
        "outcome": "",
        "review": {"total": 80},
        "wall_seconds": 100,
        "dispatched_at": "2026-10-03T04:00:00Z",
        "completed_at": "2026-10-03T04:00:00Z",
        "picker_selection": {
            "action": "route",
            "backend": backend,
            "confidence": 0.4,
            "latency_ms": 100,
            "fallback_reason": None,
            "offered": [
                {"backend": backend, "family": "local", "burn_multiple": 1.5},
            ],
        },
    }
