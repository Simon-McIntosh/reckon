"""The station's duty pass resolves every run's plan inside one docs-tree scan.

Each finished run's review duty resolves its plan, and resolving a plan checks
its identity against every sibling plan file in the directory. Outside a scan
scope that check stat-ed the whole plan directory once per run, which a profile
of the live station put at 18% of its per-project refresh. Inside the scope one
identity index serves every resolve. This case observes the scope each duty is
asked under.
"""

from __future__ import annotations

import pytest

from reckon import resources
from reckon.crew import station_subscriber


def test_a_duty_pass_shares_one_resource_scope(monkeypatch: pytest.MonkeyPatch):
    from reckon.crew import obligations

    scopes: list[object] = []

    def owes(pointer, **_kwargs):
        scopes.append(resources._INDEX_SCOPE.get())
        return False

    monkeypatch.setattr(station_subscriber, "run_owes_its_coordinator", owes)
    monkeypatch.setattr(station_subscriber, "_review_grace", lambda project: 0)
    monkeypatch.setattr(obligations, "_live_review_runs", lambda *pair: set())
    pointers = [
        {"run_id": f"r-{index}", "phase": "complete", "project": "p", "session": "s"}
        for index in range(3)
    ]
    station_subscriber.live_work_populations(pointers)
    assert len(scopes) == 3
    assert all(scope is not None for scope in scopes)
    assert len({id(scope) for scope in scopes}) == 1
    assert resources._INDEX_SCOPE.get() is None
