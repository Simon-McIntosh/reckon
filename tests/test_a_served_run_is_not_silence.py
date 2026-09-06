"""A completed run is service evidence, even without a utilisation figure.

The constructed ledger orderings are the point: completion must displace only
an earlier exhaustion on the same backend. A quiet promotion, a failed run, and
a refusal remain silence; a later numeric reading remains the operative fact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _backends, budget, ledger

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
CONFIG = {
    "backends": {
        "alpha": {"launch": "cli", "command": "alpha"},
        "beta": {"launch": "cli", "command": "beta"},
    },
    "budget": {
        "utilisation_ceiling_pct": 95,
        "resume_reserve_pct": 0,
        "exhausted_statuses": ["exhausted"],
    },
}


def _stamp(offset_minutes: int) -> str:
    return (NOW + timedelta(minutes=offset_minutes)).isoformat().replace("+00:00", "Z")


def _exhausted() -> dict:
    block = _backends.unknown_budget("the backend reported exhaustion")
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": 100.0,
            "threshold_status": "exhausted",
            "resets_at": _stamp(120),
        }
    )
    return block


def _known(utilisation_pct: float) -> dict:
    block = _backends.unknown_budget("the backend reported utilisation")
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": utilisation_pct,
            "resets_at": _stamp(120),
        }
    )
    return block


def _record(
    root: Path,
    *,
    run_id: str,
    backend_name: str,
    completed_offset: int,
    budget_block: dict,
    gate: str = "passed",
    completed_at_source: str = "terminal_event",
    failure_classification: str = "",
) -> None:
    record = ledger.build_record(
        run_id=run_id,
        plan="plan-a",
        gate=gate,
        failure_classification=failure_classification,
        backend=backend_name,
        completed_at=_stamp(completed_offset),
        completed_at_source=completed_at_source,
        budget=budget_block,
    )
    ledger.append_run("project", record, root=root)


def _verdict(root: Path, backend_name: str = "alpha") -> dict:
    return budget.preflight(
        "project",
        CONFIG,
        root=root,
        backends=[backend_name],
        now=NOW,
    )["backends"][0]


def test_a_later_completed_run_displaces_an_earlier_exhaustion(
    tmp_path: Path,
) -> None:
    _record(
        tmp_path,
        run_id="exhausted-run",
        backend_name="alpha",
        completed_offset=-20,
        budget_block=_exhausted(),
    )
    _record(
        tmp_path,
        run_id="served-run",
        backend_name="alpha",
        completed_offset=-10,
        budget_block=_backends.unknown_budget("no utilisation published"),
    )

    verdict = _verdict(tmp_path)

    assert verdict["held"] is False
    assert verdict["state"]["headroom"] == "unknown"
    assert "completed run 'served-run'" in verdict["reason"]
    assert "displaced the earlier recorded exhaustion" in verdict["reason"]


def test_completion_evidence_does_not_cross_backends(tmp_path: Path) -> None:
    _record(
        tmp_path,
        run_id="exhausted-run",
        backend_name="alpha",
        completed_offset=-20,
        budget_block=_exhausted(),
    )
    _record(
        tmp_path,
        run_id="served-elsewhere",
        backend_name="beta",
        completed_offset=-10,
        budget_block=_backends.unknown_budget("no utilisation published"),
    )

    assert _verdict(tmp_path)["held"] is True


def test_exhaustion_without_a_later_completion_keeps_its_reason(
    tmp_path: Path,
) -> None:
    _record(
        tmp_path,
        run_id="exhausted-run",
        backend_name="alpha",
        completed_offset=-20,
        budget_block=_exhausted(),
    )

    verdict = _verdict(tmp_path)

    assert verdict["held"] is True
    assert verdict["reason"] == (
        "backend reports threshold status 'exhausted', which policy counts as "
        "exhausted regardless of utilisation; utilisation 100% with burn "
        "multiple unknown"
    )


@pytest.mark.parametrize(
    ("gate", "failure_classification"),
    [("failed", "negative-result"), ("not-run", "work-rejected")],
)
def test_a_run_that_did_not_complete_is_not_service(
    tmp_path: Path,
    gate: str,
    failure_classification: str,
) -> None:
    _record(
        tmp_path,
        run_id="exhausted-run",
        backend_name="alpha",
        completed_offset=-20,
        budget_block=_exhausted(),
    )
    _record(
        tmp_path,
        run_id="unserved-run",
        backend_name="alpha",
        completed_offset=-10,
        budget_block=_backends.unknown_budget("no utilisation published"),
        gate=gate,
        failure_classification=failure_classification,
    )

    assert _verdict(tmp_path)["held"] is True


def test_a_completion_before_the_exhaustion_does_not_clear_it(tmp_path: Path) -> None:
    _record(
        tmp_path,
        run_id="served-run",
        backend_name="alpha",
        completed_offset=-20,
        budget_block=_backends.unknown_budget("no utilisation published"),
    )
    _record(
        tmp_path,
        run_id="exhausted-run",
        backend_name="alpha",
        completed_offset=-10,
        budget_block=_exhausted(),
    )

    assert _verdict(tmp_path)["held"] is True


@pytest.mark.parametrize(("utilisation_pct", "held"), [(60.0, False), (99.0, True)])
def test_a_later_numeric_reading_governs(
    tmp_path: Path,
    utilisation_pct: float,
    held: bool,
) -> None:
    _record(
        tmp_path,
        run_id="exhausted-run",
        backend_name="alpha",
        completed_offset=-30,
        budget_block=_exhausted(),
    )
    _record(
        tmp_path,
        run_id="served-run",
        backend_name="alpha",
        completed_offset=-20,
        budget_block=_backends.unknown_budget("no utilisation published"),
    )
    _record(
        tmp_path,
        run_id="measured-run",
        backend_name="alpha",
        completed_offset=-10,
        budget_block=_known(utilisation_pct),
    )

    verdict = _verdict(tmp_path)

    assert verdict["held"] is held
    assert verdict["state"]["headroom"] == "known"
    assert verdict["state"]["utilisation_pct"] == utilisation_pct
    assert "completed run" not in verdict["reason"]


def test_a_quiet_promotion_remains_silence(tmp_path: Path) -> None:
    """Promotion time alone is not evidence that the backend served a run."""
    _record(
        tmp_path,
        run_id="exhausted-run",
        backend_name="alpha",
        completed_offset=-20,
        budget_block=_exhausted(),
    )
    _record(
        tmp_path,
        run_id="quiet-promotion",
        backend_name="alpha",
        completed_offset=-10,
        budget_block=_backends.unknown_budget("no utilisation published"),
        completed_at_source="promotion_time",
    )

    assert _verdict(tmp_path)["held"] is True
