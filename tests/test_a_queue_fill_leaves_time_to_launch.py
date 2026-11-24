"""A pass's queue fill stops at its budget, so the pass still launches.

The queue fill runs before a pass launches anything. A fresh service image
remembers no plan, so its first fill judged every plan's review coverage, and on
24 November that took over half an hour while the local lane sat at zero
workers with queued work ready. The fill now stops once its budget is spent and
defers the remaining plans to later passes. It always files at least one plan,
so a later pass makes progress, and a deferred plan is filed by the next pass
rather than forgotten.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import flight
from reckon.crew.admission import queueing
from tests.test_the_service_queues_what_may_be_queued import _plan_html_text

PROJECT = "proj"
PLANS = ("plan-a", "plan-b", "plan-c", "plan-d")


@pytest.fixture()
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(flight, "service_queues_plans", lambda config: True)
    root = tmp_path / "checkout" / "docs"
    (root / "plans").mkdir(parents=True)
    for slug in PLANS:
        (root / "plans" / f"{slug}.html").write_text(
            _plan_html_text(slug), encoding="utf-8"
        )
    return root


def _filling(seconds_per_plan: float):
    now = [0.0]
    stored: list[str] = []

    def queue(project, slug, **_kwargs):
        stored.append(slug)
        now[0] += seconds_per_plan
        return {"queued": 0, "skipped": 0, "refused": 0, "results": []}

    return now, stored, queue


def test_a_fill_past_its_budget_defers_the_rest(docs: Path) -> None:
    now, stored, queue = _filling(60.0)
    report = queueing.PlanQueue().fill(
        config_for=lambda project: {},
        queue=queue,
        projects={PROJECT: docs},
        budget_seconds=100.0,
        clock=lambda: now[0],
    )
    assert stored == ["plan-a", "plan-b"], stored
    assert report["deferred"] is True
    deferred = [
        row["plan"]
        for row in report["projects"][0]["plans"]
        if str(row.get("reason", "")).startswith("deferred")
    ]
    assert deferred == ["plan-c", "plan-d"]


def test_the_next_pass_files_what_was_deferred(docs: Path) -> None:
    now, stored, queue = _filling(60.0)
    filler = queueing.PlanQueue()
    for _ in range(2):
        filler.fill(
            config_for=lambda project: {},
            queue=queue,
            projects={PROJECT: docs},
            budget_seconds=100.0,
            clock=lambda: now[0],
        )
    assert stored == list(PLANS), stored


def test_a_fill_always_files_one_plan(docs: Path) -> None:
    now, stored, queue = _filling(500.0)
    queueing.PlanQueue().fill(
        config_for=lambda project: {},
        queue=queue,
        projects={PROJECT: docs},
        budget_seconds=1.0,
        clock=lambda: now[0],
    )
    assert stored == ["plan-a"]


def test_a_fill_inside_its_budget_files_every_plan(docs: Path) -> None:
    now, stored, queue = _filling(1.0)
    report = queueing.PlanQueue().fill(
        config_for=lambda project: {},
        queue=queue,
        projects={PROJECT: docs},
        budget_seconds=100.0,
        clock=lambda: now[0],
    )
    assert stored == list(PLANS)
    assert "deferred" not in report
