"""A waits pass files each idle upstream plan once and resolves its owners once.

The admit pass files the section an idle wait names through the whole-plan
dispatch. It did so for every idle waiter, in every round of the waits stage,
and each filing resolved the project's plan coordinators again over every live
pointer. On 23 November one pass spent hours there. Within one pass a plan is
now filed once however many waits name it, and a project's coordinators are
resolved once.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew import dispatch_batch, plan_coordinator, queue_admission
from reckon.crew.node import TaskNode
from tests.test_an_idle_wait_queues_its_upstream import (  # noqa: F401
    PROJECT,
    SESSION,
    UPSTREAM_PLAN,
    WAITER_PLAN,
    _age,
    _config,
    _declare_waits,
    _live,
    _pointer,
    _queue_waiter,
    _requests_on,
    project_config,
    repository,
)

SECOND_WAITER_PLAN = "plan-wait-two"


def _queue_second_waiter(repository: Path) -> None:
    queue_admission.queue_requests(
        [
            TaskNode(
                id="second-waiter",
                goal="build the second waiting section",
                plan=SECOND_WAITER_PLAN,
                section="s1",
                done_when="the second waiting check passes",
                write_paths=["second.txt"],
                time_budget="20m",
                spec_level="exact",
            )
        ],
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=_config(),
        run_ids={"second-waiter": "r-second-waiter"},
    )


def _counting(monkeypatch, module, name: str) -> list[str]:
    calls: list[str] = []
    real = getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(str(args[1] if len(args) > 1 else kwargs.get("plan", "")))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def test_two_waits_on_one_refused_upstream_file_it_once_and_resolve_once(
    repository: Path,  # noqa: F811 - the imported fixture
    project_config: dict,  # noqa: F811 - the imported fixture
    monkeypatch,
) -> None:
    _declare_waits(repository, WAITER_PLAN, {"s1": [f"{UPSTREAM_PLAN}#s1"]})
    _declare_waits(repository, SECOND_WAITER_PLAN, {"s1": [f"{UPSTREAM_PLAN}#s1"]})
    _queue_waiter(repository)
    _queue_second_waiter(repository)
    _age("r-waiter")
    _age("r-second-waiter")
    # The upstream's review is owed, so it cannot be queued and both waits stay
    # idle: each one reaches the filing, which is where the cost repeated.
    project_config["config"] = _config({"plan_review_gate": "enforce"})
    filings = _counting(monkeypatch, dispatch_batch, "queue_plan")
    resolutions = _counting(monkeypatch, plan_coordinator, "resolve_plan_coordinators")

    queue_admission.advance_waiting_requests(_live())

    assert filings.count(UPSTREAM_PLAN) == 1, filings
    assert len(resolutions) == 1, resolutions
    assert _requests_on(UPSTREAM_PLAN) == [], _live()
    for run_id in ("r-waiter", "r-second-waiter"):
        waiter = _pointer(run_id)
        assert waiter["wait_class"] == "idle", waiter
        assert waiter.get("upstream_unqueued"), waiter
