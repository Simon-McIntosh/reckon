"""An admission verdict reads one plan's history rather than the project's.

A pass resolves a section's landing by matching committed rows on the plan and
section they name, so a project-wide read decodes every run the project ever
recorded to reach the handful that can match, and keeps the rows for the whole
pass. The read names its plan and the pass cache is keyed by it, so two plans
judged in one pass each pay for their own rows and neither reads the other's.
"""

from __future__ import annotations

from typing import Any

import pytest

from reckon import ledger
from reckon.crew.admission import readiness


@pytest.fixture()
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every ``ledger.load`` the code under test makes, with its arguments."""
    calls: list[dict[str, Any]] = []

    def load(project, root=None, picker_only=False, **kwargs):
        calls.append({"project": project, "kwargs": kwargs})
        return {"runs": []}, 1

    monkeypatch.setattr(ledger, "load", load)
    return calls


def test_the_read_names_the_plan_it_is_asked_about(recorded):
    assert readiness._ledger_run_rows("proj", "plan-a", None) == []
    assert recorded == [{"project": "proj", "kwargs": {"plan": "plan-a"}}]


def test_two_plans_in_a_pass_read_their_own_rows_once(recorded):
    cache: dict[str, Any] = {}
    readiness._ledger_run_rows("proj", "plan-a", cache)
    readiness._ledger_run_rows("proj", "plan-b", cache)
    readiness._ledger_run_rows("proj", "plan-a", cache)
    assert [call["kwargs"]["plan"] for call in recorded] == ["plan-a", "plan-b"]


def test_a_failed_read_is_cached_under_its_plan(recorded, monkeypatch):
    def unreadable(*_arguments, **_keywords):
        raise OSError("the store cannot be read")

    monkeypatch.setattr(ledger, "load", unreadable)
    cache: dict[str, Any] = {}
    assert readiness._ledger_run_rows("proj", "plan-a", cache) is None
    assert readiness._ledger_run_rows("proj", "plan-b", cache) is None
    assert ("section-runs", "proj", "plan-a") in cache
    assert ("section-runs", "proj", "plan-b") in cache
