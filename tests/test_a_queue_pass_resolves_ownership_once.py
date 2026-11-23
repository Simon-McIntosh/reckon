"""A queue pass resolves its project's coordinators once, not once per plan.

The admit service's queue stage files every plan of a project that has moved.
The coordinator resolution and the live pointer set it reads answer for the
whole project, but each plan's store read them again for itself. On 23 November
the first pass after a restart weighed 85 plans and took 6,593 seconds, about
77 seconds a plan, almost all of it rebuilding the session directory per plan,
and no request launched while it ran.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew import plan_coordinator
from tests.test_the_service_queues_what_may_be_queued import (  # noqa: F401
    _pass,
    _plan_html_text,
    _sections_filed,
    repository,
)

PLANS = ("plan-a", "plan-b", "plan-c")


def _counting(monkeypatch, module, name: str) -> list[int]:
    calls: list[int] = []
    real = getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def test_one_pass_over_several_plans_resolves_ownership_once(
    repository: Path,  # noqa: F811 - the imported fixture
    monkeypatch,
) -> None:
    plans_dir = repository / "docs" / "plans"
    for slug in PLANS[1:]:
        (plans_dir / f"{slug}.html").write_text(_plan_html_text(slug), encoding="utf-8")
    resolved = _counting(monkeypatch, plan_coordinator, "resolve_plan_coordinators")

    report = _pass(repository)

    assert report["queued"] == 3 * len(PLANS), report
    assert len(resolved) == 1, f"ownership resolved {len(resolved)} times"
