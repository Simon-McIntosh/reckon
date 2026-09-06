"""Burn-rate evidence becomes a dated exhaustion projection for admission."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon import budget

MOMENT = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
PERIOD_MINUTES = 10_080.0
POLICY = {
    "utilisation_ceiling_pct": 100.0,
    "resume_reserve_pct": 5.0,
    "exhausted_statuses": [],
}

# The host flight layer currently exposes these nine lanes. The verdict is
# deliberately constructed rather than read from the workstation account: this
# test protects admission semantics without turning the suite into a monitor.
CONFIGURED_BACKENDS = (
    "codex",
    "codex-spark",
    "codex-luna",
    "codex-terra",
    "clive",
    "clive-glm",
    "claude",
    "claude-opus",
    "native",
)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _window_state(
    *,
    backend: str = "lane",
    utilisation_pct: float,
    elapsed_fraction: float,
    observed_at: str | None = None,
) -> budget.BudgetState:
    period_seconds = PERIOD_MINUTES * 60.0
    seconds_until_reset = int(period_seconds * (1.0 - elapsed_fraction))
    burn_multiple = (utilisation_pct / 100.0) / elapsed_fraction
    return budget.BudgetState(
        backend=backend,
        headroom="known",
        utilisation_pct=utilisation_pct,
        burn_multiple=burn_multiple,
        rate_limit_period_minutes=PERIOD_MINUTES,
        resets_at=_stamp(MOMENT + timedelta(seconds=seconds_until_reset)),
        seconds_until_reset=seconds_until_reset,
        observed_at=_stamp(MOMENT) if observed_at is None else observed_at,
        source="constructed-reading",
    )


def _preflight_for(monkeypatch: pytest.MonkeyPatch, state: budget.BudgetState) -> dict:
    monkeypatch.setattr(
        budget,
        "latest_recorded",
        lambda *args, **kwargs: budget._RecordedReadings({}),
    )
    monkeypatch.setattr(budget, "state_for", lambda *args, **kwargs: state)
    return budget.preflight(
        "project",
        {
            "backends": {state.backend: {}},
            "budget": POLICY,
        },
        backends=[state.backend],
        now=MOMENT,
    )


def test_opening_window_quantisation_is_not_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = budget.BudgetState(
        backend="lane",
        headroom="known",
        utilisation_pct=3.0,
        burn_multiple=(3.0 / 100.0) / (10_234 / (PERIOD_MINUTES * 60.0)),
        rate_limit_period_minutes=PERIOD_MINUTES,
        resets_at=_stamp(MOMENT + timedelta(seconds=594_566)),
        seconds_until_reset=594_566,
        observed_at=_stamp(MOMENT),
        source="constructed-reading",
    )

    report = _preflight_for(monkeypatch, state)
    admitted = report["backends"][0]["state"]

    assert admitted["headroom"] == "unknown"
    assert admitted["projected_exhaustion_at"] is None
    assert report["held"] is False
    assert "1.69% elapsed is below the 5% floor" in report["summary"]


def test_admissible_burn_emits_an_earlier_exhaustion_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _window_state(utilisation_pct=55.0, elapsed_fraction=0.30)

    report = _preflight_for(monkeypatch, state)
    admitted = report["backends"][0]["state"]
    projected = datetime.fromisoformat(admitted["projected_exhaustion_at"])
    reset = datetime.fromisoformat(admitted["resets_at"])
    expected = MOMENT + timedelta(
        seconds=state.seconds_until_reset / state.burn_multiple
    )

    assert projected == expected.replace(microsecond=0)
    assert projected < reset
    why_line = next(
        line for line in report["summary"].splitlines() if line.startswith("WHY")
    )
    assert "utilisation 55%" in why_line
    assert f"projected exhaustion {admitted['projected_exhaustion_at']}" in why_line
    assert "burn multiple is report-only and never holds" not in report["summary"]


def test_sustainable_burn_projects_the_reset_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _window_state(utilisation_pct=20.0, elapsed_fraction=0.30)

    report = _preflight_for(monkeypatch, state)
    admitted = report["backends"][0]["state"]

    assert state.burn_multiple <= 1.0
    assert admitted["projected_exhaustion_at"] == admitted["resets_at"]


def test_unstamped_burn_has_no_projection_and_does_not_raise() -> None:
    state = _window_state(
        utilisation_pct=55.0,
        elapsed_fraction=0.30,
        observed_at="",
    )

    verdict = budget.decide(state, POLICY, now=MOMENT)

    assert verdict["state"]["projected_exhaustion_at"] is None
    assert verdict["held"] is False


def test_a_rolled_over_window_has_no_projection() -> None:
    state = _window_state(utilisation_pct=55.0, elapsed_fraction=0.30)

    verdict = budget.decide(
        state,
        POLICY,
        now=datetime.fromisoformat(state.resets_at) + timedelta(seconds=1),
    )

    assert verdict["state"]["headroom"] == "unknown"
    assert verdict["state"]["projected_exhaustion_at"] is None
    assert verdict["held"] is False


@pytest.mark.parametrize("backend_name", CONFIGURED_BACKENDS)
@pytest.mark.parametrize(
    ("utilisation_pct", "held"),
    [(94.0, False), (95.0, True)],
)
def test_existing_utilisation_admission_is_unchanged_for_each_configured_backend(
    backend_name: str,
    utilisation_pct: float,
    held: bool,
) -> None:
    state = budget.BudgetState(
        backend=backend_name,
        headroom="known",
        utilisation_pct=utilisation_pct,
    )

    verdict = budget.decide(state, POLICY, now=MOMENT)

    assert verdict["held"] is held
    assert verdict["effective_ceiling_pct"] == 95.0
