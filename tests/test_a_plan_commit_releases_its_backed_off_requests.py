"""A plan commit releases the requests that were backing off on it.

A queued launch is refused while its plan's working tree differs from base, and
the refusal is recorded with a back-off. The dependency guard fingerprints the
plan file's own stamp, which a commit does not move: the file on disk holds the
same bytes before and after, and only HEAD's blob changes. So a request refused
for an uncommitted plan waited out its whole back-off while the lane, and the
request, were ready — the plan having moved under it in the one way the guard
could not see.

So the plan's committed content is fingerprinted at failure time and compared on
every pass, for a request refused by the plan's own visibility and for no other
refusal. The check below is the production one: each attempt asks
``require_plan_section_visible``, the call the launch itself makes, and reads
the refusal it raises.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon._timestamps import parse_utc
from reckon.crew import queue_admission, routing, runs
from reckon.crew.node import TaskNode
from tests.helpers.git import git as _git
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    PROJECT,
    _config_for,
    _lane_document,
    _queue,
    admit_repo,
)

REFUSED_PLAN = "r-plan"
OTHER_REFUSAL = "r-other"


class _LaunchProbe:
    """Stand in for the launch so the pass reaches it without dispatching.

    It counts every attempt, which is the measure: a request whose back-off was
    released reaches the launch, and one held by its back-off never does.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.attempted: list[str] = []
        self.answer: Callable[..., Any] | None = None
        monkeypatch.setattr(queue_admission, "_launch_queued", self)

    def __call__(self, pointer, **_):
        self.attempted.append(str(pointer["run_id"]))
        if self.answer is None:
            raise AssertionError("the probe was reached with no answer bound")
        return self.answer(pointer)


def _node(node_id: str) -> TaskNode:
    return TaskNode(
        id=node_id,
        goal="admit this queued node",
        plan="plan-a",
        section="dispatch",
        done_when="pytest tests/test_a_plan_commit_releases_its_backed_off_requests.py",
        write_paths=[f"{node_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )


def _edit_plan_uncommitted(repository: Path) -> None:
    """An authored plan edit the commit has not landed yet."""
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.write_text(
        plan.read_text(encoding="utf-8") + "<!-- authored, not yet committed -->\n",
        encoding="utf-8",
    )


def _commit_plan(repository: Path) -> str:
    _git(repository, "add", "--", "docs/plans/plan-a.html")
    _git(repository, "commit", "-q", "-m", "docs: land the plan section")
    return _git(repository, "rev-parse", "HEAD:docs/plans/plan-a.html")


def _plan_refusal(repository: Path) -> Callable[..., Any]:
    """The refusal the launch itself raises while the plan is uncommitted.

    It is the production check, so the failure the pass records carries the
    refusal's own words rather than a copy of them, and a launch that would now
    pass proves it in the same call, so an arm that expects a launch really
    launches.
    """

    def answer(pointer, **_):
        authority = routing.resolve_dispatch_authority(PROJECT, repository)
        routing.require_plan_section_visible(
            node=_node(str(pointer["run_id"])),
            project=PROJECT,
            repo=repository,
            base="HEAD",
            authority=authority,
        )
        return {"run_id": pointer["run_id"], "phase": "starting"}

    return answer


def _verdicts(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["run_id"]): row for row in result["verdicts"]}


def _commit_an_unrelated_file(repository: Path) -> None:
    """Move HEAD without touching the plan beside it."""
    unrelated = repository / "docs" / "state" / PROJECT / "index.json"
    unrelated.write_text(
        '{"project": "proj", "data": {"_version": 1}}\n', encoding="utf-8"
    )
    _git(repository, "add", "--", f"docs/state/{PROJECT}/index.json")
    _git(repository, "commit", "-q", "-m", "chore: move the project index")


def test_a_plan_commit_releases_the_request_it_refused(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal cured by the commit is retried at once, before its retry_at."""
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=10)
    )
    _edit_plan_uncommitted(admit_repo)
    runs.queue_dispatch(_queue(admit_repo, node_id="plan-node", run_id=REFUSED_PLAN))
    probe = _LaunchProbe(monkeypatch)
    probe.answer = _plan_refusal(admit_repo)

    first = queue_admission.admit_once(config_for=resolve)

    assert probe.attempted == [REFUSED_PLAN], "the pass attempts the queued request"
    assert first["failed"] == [REFUSED_PLAN]
    recorded = runs.read_pointer(REFUSED_PLAN)
    assert (
        "PlanVisibilityError: plan file docs/plans/plan-a.html differs from base"
        in str(recorded["failure"])
    ), recorded["failure"]
    retry_at = parse_utc(recorded["retry_at"])
    before_retry = retry_at - timedelta(seconds=1)

    # Still uncommitted, the request keeps its back-off: nothing has changed in
    # the world the refusal named.
    held = queue_admission.admit_once(config_for=resolve, now=before_retry)
    assert probe.attempted == [REFUSED_PLAN], "an uncommitted plan holds the back-off"
    assert _verdicts(held)[REFUSED_PLAN]["verdict"] == "held-on-retry-backoff"
    assert held["failed"] == []

    # A commit that leaves the plan alone is not the change the refusal named.
    _commit_an_unrelated_file(admit_repo)
    still_held = queue_admission.admit_once(config_for=resolve, now=before_retry)
    assert probe.attempted == [REFUSED_PLAN], "another commit releases nothing"
    assert _verdicts(still_held)[REFUSED_PLAN]["verdict"] == "held-on-retry-backoff"

    blob = _commit_plan(admit_repo)
    released = queue_admission.admit_once(config_for=resolve, now=before_retry)

    assert probe.attempted == [REFUSED_PLAN, REFUSED_PLAN], (
        "the plan's commit releases the request at the next pass, before retry_at"
    )
    assert released["launched"] == [REFUSED_PLAN], released
    assert _verdicts(released)[REFUSED_PLAN]["verdict"] == "launched"
    assert blob != "", "the plan's committed blob is what moved"


def test_another_refusal_keeps_its_backoff_across_a_plan_commit(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal the plan did not cause is not released by committing it."""
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=10)
    )
    _edit_plan_uncommitted(admit_repo)
    runs.queue_dispatch(_queue(admit_repo, node_id="other-node", run_id=OTHER_REFUSAL))

    def refuse(pointer, **_):
        raise ValueError("the lane refused this launch")

    probe = _LaunchProbe(monkeypatch)
    probe.answer = refuse

    queue_admission.admit_once(config_for=resolve)
    recorded = runs.read_pointer(OTHER_REFUSAL)
    retry_at = parse_utc(recorded["retry_at"])
    assert "failure_plan_commit" not in recorded, (
        "only a plan-refused request fingerprints the plan's committed content"
    )

    _commit_plan(admit_repo)
    held = queue_admission.admit_once(
        config_for=resolve, now=retry_at - timedelta(seconds=1)
    )

    assert probe.attempted == [OTHER_REFUSAL], (
        "a refusal the plan did not cause keeps its back-off through the commit"
    )
    assert _verdicts(held)[OTHER_REFUSAL]["verdict"] == "held-on-retry-backoff"


def test_a_request_whose_plan_stays_uncommitted_keeps_backing_off(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp is the plan's committed blob, and it does not move on its own."""
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=10)
    )
    _edit_plan_uncommitted(admit_repo)
    runs.queue_dispatch(_queue(admit_repo, node_id="plan-node", run_id=REFUSED_PLAN))
    probe = _LaunchProbe(monkeypatch)
    probe.answer = _plan_refusal(admit_repo)

    queue_admission.admit_once(config_for=resolve)
    recorded = runs.read_pointer(REFUSED_PLAN)
    retry_at = parse_utc(recorded["retry_at"])
    committed = _git(admit_repo, "rev-parse", "HEAD:docs/plans/plan-a.html")
    assert recorded["failure_plan_commit"] == committed, (
        "the stamp must be the plan's committed blob, which the working edit "
        "has not replaced"
    )

    held = queue_admission.admit_once(
        config_for=resolve, now=retry_at - timedelta(seconds=1)
    )
    assert probe.attempted == [REFUSED_PLAN]
    assert _verdicts(held)[REFUSED_PLAN]["verdict"] == "held-on-retry-backoff"
    assert runs.read_pointer(REFUSED_PLAN)["phase"] == "queued"
