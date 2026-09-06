"""A budget reading describes only the backend identities declared on it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon import budget

MOMENT = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
POLICY = {
    "utilisation_ceiling_pct": 100.0,
    "resume_reserve_pct": 5.0,
    "exhausted_statuses": [],
}
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


def _reading(
    backend_name: str,
    utilisation_pct: float,
    *,
    covered_backends: tuple[str, ...] = (),
) -> budget._Reading:
    return budget._Reading(
        backend=backend_name,
        budget={
            "headroom": "known",
            "utilisation_pct": utilisation_pct,
            "resets_at": (MOMENT + timedelta(hours=1)).isoformat(),
        },
        observed_at=MOMENT.isoformat(),
        when=MOMENT,
        source="constructed-reading",
        age_source="constructed-reading",
        covered_backends=covered_backends,
    )


@pytest.mark.parametrize(
    "backend_name",
    ["codex-spark", "codex-luna", "codex-terra"],
)
def test_a_reading_does_not_spread_across_a_backend_name_family(
    backend_name: str,
) -> None:
    observed = _reading("codex", 95.0)

    state = budget.state_for(backend_name, recorded=observed, now=MOMENT)
    verdict = budget.decide(state, POLICY, now=MOMENT)

    assert state.headroom == "unknown"
    assert state.utilisation_pct is None
    assert verdict["held"] is False


def test_declared_coverage_applies_to_exactly_the_named_backends() -> None:
    observed = _reading(
        "codex",
        95.0,
        covered_backends=("codex", "codex-luna"),
    )
    readings = budget._RecordedReadings({"codex": observed})

    assert readings.for_backend("codex") is observed
    assert readings.for_backend("codex-luna") is observed
    assert readings.for_backend("codex-spark") is None
    assert readings.for_backend("codex-terra") is None


@pytest.mark.parametrize(
    ("backend_name", "utilisation_pct", "held"),
    [("codex", 95.0, True), ("codex-spark", 94.0, False)],
)
def test_a_backends_own_reading_keeps_its_existing_verdict(
    backend_name: str,
    utilisation_pct: float,
    held: bool,
) -> None:
    observed = _reading(backend_name, utilisation_pct)
    state = budget.state_for(backend_name, recorded=observed, now=MOMENT)

    assert budget.decide(state, POLICY, now=MOMENT)["held"] is held


def test_an_own_reading_outranks_another_readings_declared_coverage() -> None:
    family = _reading(
        "codex",
        95.0,
        covered_backends=("codex", "codex-spark"),
    )
    spark = _reading("codex-spark", 94.0)
    readings = budget._RecordedReadings({"codex": family, "codex-spark": spark})

    assert readings.for_backend("codex-spark") is spark
    state = budget.state_for(
        "codex-spark",
        recorded=readings.for_backend("codex-spark"),
        now=MOMENT,
    )
    assert budget.decide(state, POLICY, now=MOMENT)["held"] is False


def test_each_configured_backend_resolves_to_one_reading_or_none_by_identity() -> None:
    codex = _reading(
        "codex",
        50.0,
        covered_backends=("codex", "codex-luna"),
    )
    spark = _reading("codex-spark", 50.0)
    claude = _reading(
        "claude",
        50.0,
        covered_backends=("claude", "claude-opus"),
    )
    readings = budget._RecordedReadings(
        {"codex": codex, "codex-spark": spark, "claude": claude}
    )

    resolved = {
        backend_name: readings.for_backend(backend_name)
        for backend_name in CONFIGURED_BACKENDS
    }

    assert resolved == {
        "codex": codex,
        "codex-spark": spark,
        "codex-luna": codex,
        "codex-terra": None,
        "clive": None,
        "clive-glm": None,
        "claude": claude,
        "claude-opus": claude,
        "native": None,
    }
    assert resolved["codex"] is codex
    assert resolved["codex-spark"] is spark
    assert resolved["codex-luna"] is codex
    assert resolved["claude-opus"] is claude
