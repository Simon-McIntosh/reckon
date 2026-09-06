"""Range utilisation itself so burn floors cannot mask a ceiling regression.

The range is the point: configured backends can all sit below the ceiling and
therefore cannot falsify a broken hold. Burn floors govern whether a projected
exhaustion instant is emitted, not whether utilisation is a known reading.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon import budget

MOMENT = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
PERIOD_MINUTES = 10_080.0
POLICY = {
    "utilisation_ceiling_pct": 95.0,
    "resume_reserve_pct": 0.0,
    "exhausted_statuses": [],
}
UTILISATION_VERDICTS = (
    (8.0, False),
    (60.0, False),
    (94.0, False),
    (96.0, True),
    (99.5, True),
)
ELAPSED_FRACTIONS = (0.0247, 0.30)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _state(
    *,
    utilisation_pct: float | None,
    elapsed_fraction: float,
) -> budget.BudgetState:
    period_seconds = PERIOD_MINUTES * 60.0
    seconds_until_reset = int(period_seconds * (1.0 - elapsed_fraction))
    block = {
        "headroom": "known",
        "utilisation_pct": utilisation_pct,
        "rate_limit_period_minutes": PERIOD_MINUTES,
        "resets_at": _stamp(MOMENT + timedelta(seconds=seconds_until_reset)),
    }
    return budget._from_block(
        "lane",
        block,
        observed_at=_stamp(MOMENT),
        source="constructed-account-surface",
        age_source="account-observation",
        now=MOMENT,
    )


@pytest.mark.parametrize(("utilisation_pct", "held"), UTILISATION_VERDICTS)
def test_ceiling_verdict_is_identical_on_both_sides_of_the_burn_elapsed_floor(
    utilisation_pct: float,
    held: bool,
) -> None:
    verdicts = [
        budget.decide(
            _state(
                utilisation_pct=utilisation_pct,
                elapsed_fraction=elapsed_fraction,
            ),
            POLICY,
            now=MOMENT,
        )
        for elapsed_fraction in ELAPSED_FRACTIONS
    ]

    assert [verdict["held"] for verdict in verdicts] == [held, held]
    assert [verdict["state"]["headroom"] for verdict in verdicts] == [
        "known",
        "known",
    ]
    assert f"utilisation {utilisation_pct:g}%" in verdicts[0]["reason"]
    assert "projected exhaustion withheld" in verdicts[0]["reason"]


@pytest.mark.parametrize("elapsed_fraction", ELAPSED_FRACTIONS)
def test_absent_utilisation_stays_unknown_and_never_holds(
    elapsed_fraction: float,
) -> None:
    verdict = budget.decide(
        _state(utilisation_pct=None, elapsed_fraction=elapsed_fraction),
        POLICY,
        now=MOMENT,
    )

    assert verdict["state"]["headroom"] == "unknown"
    assert verdict["state"]["utilisation_pct"] is None
    assert verdict["held"] is False


@pytest.mark.parametrize(
    "utilisation_pct", [value for value, _held in UTILISATION_VERDICTS]
)
def test_projection_is_withheld_below_the_floor_and_emitted_above_it(
    utilisation_pct: float,
) -> None:
    below, above = [
        budget.decide(
            _state(
                utilisation_pct=utilisation_pct,
                elapsed_fraction=elapsed_fraction,
            ),
            POLICY,
            now=MOMENT,
        )["state"]
        for elapsed_fraction in ELAPSED_FRACTIONS
    ]

    assert below["projected_exhaustion_at"] is None
    assert above["projected_exhaustion_at"] is not None
