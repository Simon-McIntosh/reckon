"""A composed review waits for a placement-roster seat instead of being refused.

A plan review is composed before the lane admits it: the report directory, its
brief, the plan snapshot and the sidecar are written first, and the dispatch
then asks the placement reservation for a worker. When every seat the
allocation was sized for is held, that question used to end the composition
with a refusal and no pointer, so the run the review was owed waited for a
coordinator to compose the review again by hand once a seat freed. The cases
here hold both halves plus the admission: a composition made while the roster is
full leaves a queued pointer that names the brief its composed directory holds,
and once a resident worker ends the queued review is admitted and launches.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import plan_review, queue_admission, recovery, runs
from reckon.crew import placement as placement_module

PLACED = {"scheduler": "srun", "options": ["--ntasks=1"]}

RESERVATION = {
    "job_id": "1277272",
    "size": {"cores": 64, "memory_gb": 128},
    "partition": "all",
}


def _config() -> dict:
    worker = {
        "launch": "cli",
        "command": "codex",
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "time_budget": "20m",
        "placement": {
            **PLACED,
            "state_query": ["squeue", "-h", "-j", "{job}", "-o", "%T"],
            "reason_query": ["squeue", "-h", "-j", "{job}", "-o", "%r"],
        },
    }
    return {
        "default_backend": "worker",
        "local_backend": "worker",
        "backends": {"worker": worker},
        "roles": {"review": {"backend": "worker", "execution_capable": True}},
        "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    path = plans / "fixture.html"
    path.write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="3">'
        '</head><body><h2 id="delivery">Delivery</h2>'
        "<p>Extend the existing mechanism.</p></body></html>",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a committed plan."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"sample": str(repo / "docs")}))
    return home, repo, path


def _subject():
    return recovery.plan_review_subject("sample", "fixture", "coordinator")


def _seed_resident_worker(run_id: str) -> Path:
    """One placed live run whose worker is this process, so the seat reads held.

    Recording this process as the pid makes the roster's liveness read count the
    seat wherever the real probe runs, so the cap is exercised without stubbing
    the process table.
    """
    pointer = {
        "run_id": run_id,
        "project": "sample",
        "backend": "worker",
        "launcher_host": os.uname().nodename,
        "pid": os.getpid(),
        "phase": "working",
        "placement": {
            **PLACED,
            "state_query": ["squeue", "-h", "-j", "{job}", "-o", "%T"],
            "reason_query": ["squeue", "-h", "-j", "{job}", "-o", "%r"],
        },
    }
    path = runs.pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer), encoding="utf-8")
    return path


def _fill_the_roster() -> list[Path]:
    """Fill every seat the roster cap grants, so one more worker cannot be placed."""
    placement_module.publish_reservation(dict(RESERVATION))
    return [
        _seed_resident_worker(f"r-resident-{index}")
        for index in range(placement_module.RESERVATION_ROSTER_LIMIT)
    ]


def _queued_reviews() -> list[dict]:
    return [
        pointer
        for pointer in runs.list_live(project="sample")
        if str(pointer.get("phase") or "") == "queued"
    ]


def test_a_composed_review_on_a_full_roster_leaves_a_queued_pointer(
    project: tuple[Path, Path, Path],
) -> None:
    _home, _repo, _path = project
    _fill_the_roster()
    subject = _subject()
    directory = plan_review.review_report_directory(
        "sample", "fixture", subject["run_id"]
    )

    result = recovery.dispatch_review_for_run(subject, config=_config())

    assert result["dispatched"] is False
    assert result.get("queued"), result
    queued = _queued_reviews()
    assert [pointer["run_id"] for pointer in queued] == [result["queued"]["run_id"]]
    pointer = queued[0]
    assert pointer["backend"] == "worker"
    # The queued pointer launches from the brief this composition wrote, so the
    # composed directory must survive the hold that queued it.
    assert Path(str((pointer.get("node") or {}).get("brief") or "")).parent == directory
    assert (directory / "brief.md").is_file()


def test_a_freed_seat_admits_the_queued_review(
    project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, _repo, _path = project
    seats = _fill_the_roster()
    subject = _subject()
    composed = recovery.dispatch_review_for_run(subject, config=_config())
    queued_run_id = composed["queued"]["run_id"]
    assert queued_run_id
    queued_node_id = str((runs.read_pointer(queued_run_id).get("node") or {}).get("id"))

    # The roster's cap is counted over resident workers, so ending one seat is
    # the whole of what frees it: no other bound in this fixture is spent.
    seats[0].unlink()

    dispatch = importlib.import_module("reckon.crew.dispatch")
    launched: list[dict] = []

    def launch(**kwargs):
        launched.append(dict(kwargs))
        identifier = f"r-admitted-review-{len(launched)}"
        runs._write_json(
            runs.pointer_path(identifier),
            {
                "run_id": identifier,
                "project": "sample",
                "backend": identifier and "worker",
                "phase": "starting",
                "node": (kwargs["node"]).as_dict(),
            },
        )
        return {"run_id": identifier, "phase": "starting"}

    monkeypatch.setattr(dispatch, "dispatch", launch)
    config = _config()

    result = queue_admission.admit_once(
        run_id=queued_run_id,
        launcher=lambda *args, **kwargs: 1,
        config_for=lambda pointer: config,
    )

    assert result["launched"], result
    assert result["launched"] == ["r-admitted-review-1"]
    assert launched, "the queued review reached the dispatch path"
    assert launched[0]["node"].id == queued_node_id
    # The launched run's own pointer records the request it was admitted from,
    # so the review the fleet was too busy to place is the one that launched.
    admitted = runs.read_pointer("r-admitted-review-1")
    assert admitted.get("admitted_at")


def test_a_direct_local_dispatch_on_a_full_roster_is_queued(
    project: tuple[Path, Path, Path],
) -> None:
    """A direct dispatch that meets a full roster is stored as queued, not lost.

    The dispatch printed a run id and then refused, leaving no pointer, so
    nothing launched when a seat freed and the coordinator had to retry by hand.
    A full roster is a wait for a seat, so the request is queued as a router hold
    queues it.
    """
    from reckon.crew.dispatch_admission import LanePaused
    from reckon.crew.dispatching import preparation
    from reckon.crew.node import TaskNode

    _home, repo, _path = project
    _fill_the_roster()
    brief = repo / "brief.md"
    brief.write_text("Review the delivery.\n", encoding="utf-8")
    node = TaskNode(
        id="review-direct",
        goal="review the delivered work",
        plan="fixture",
        section="delivery",
        brief=str(brief),
        role="review",
        spec_level="exact",
        done_when=(
            "the review stores a parsed record scoring all 6 dimensions in the range "
            "0..20 at docs/evidence/review-direct.html, and the manifest reads complete"
        ),
        write_paths=["docs/evidence/review-direct.html"],
        time_budget="20m",
    )

    with pytest.raises(LanePaused) as held:
        preparation.dispatch(
            node=node,
            project="sample",
            repo=repo,
            config=_config(),
            session="coordinator",
            local=True,
            unreviewed_plan_override=True,
            unreconciled_override=True,
            watch_override=True,
        )

    queued = getattr(held.value, "queued", None)
    assert queued, held.value
    assert [pointer["run_id"] for pointer in _queued_reviews()] == [queued["run_id"]]
    assert "roster is full" in str(queued.get("reason") or "")


def _fill_the_admitted_cores(cores: int) -> None:
    """Spend every core a small reservation admits, under the roster cap.

    One placed worker holds one core, so ``cores`` residents spend the
    allocation while the roster still has seats, and the cores bound is the one
    that binds. A parked request on the same backend holds no core.
    """
    placement_module.publish_reservation(
        {**RESERVATION, "size": {"cores": cores, "memory_gb": 128}}
    )
    for index in range(cores):
        _seed_resident_worker(f"r-core-holder-{index}")
    parked = {
        "run_id": "r-parked-awaiting-upstream",
        "project": "sample",
        "backend": "worker",
        "phase": "awaiting-upstream",
        "pid": None,
        "launched_at": None,
    }
    path = runs.pointer_path(parked["run_id"])
    path.write_text(json.dumps(parked), encoding="utf-8")


def test_a_spent_cores_bound_is_a_seat_wait_naming_the_core_holders(
    project: tuple[Path, Path, Path],
) -> None:
    """The cores bound raises the queueable seat wait and names only core holders.

    It refused with a plain error before the roster check ever ran, so every
    dispatch past the admitted cores was lost even after the roster cap queued,
    and its list of occupying runs was the backend's whole population, parked
    requests included, which read as though a parked request held a core.
    """
    from reckon.crew.dispatch_admission import (
        RosterFull,
        _refuse_over_concurrency_ceiling,
    )

    _fill_the_admitted_cores(3)
    with pytest.raises(RosterFull) as held:
        _refuse_over_concurrency_ceiling(
            "worker", _config()["backends"]["worker"], "sample"
        )

    assert sorted(held.value.occupying) == [
        f"r-core-holder-{index}" for index in range(3)
    ]
    assert held.value.occupancy == 3
    assert "r-parked-awaiting-upstream" not in str(held.value)
    assert "partition admitted cores bound" in str(held.value)


def test_a_direct_local_dispatch_past_the_admitted_cores_is_queued(
    project: tuple[Path, Path, Path],
) -> None:
    from reckon.crew.dispatch_admission import LanePaused
    from reckon.crew.dispatching import preparation
    from reckon.crew.node import TaskNode

    _home, repo, _path = project
    _fill_the_admitted_cores(3)
    brief = repo / "brief.md"
    brief.write_text("Review the delivery.\n", encoding="utf-8")
    node = TaskNode(
        id="review-cores",
        goal="review the delivered work",
        plan="fixture",
        section="delivery",
        brief=str(brief),
        role="review",
        spec_level="exact",
        done_when=(
            "the review stores a parsed record scoring all 6 dimensions in the range "
            "0..20 at docs/evidence/review-cores.html, and the manifest reads complete"
        ),
        write_paths=["docs/evidence/review-cores.html"],
        time_budget="20m",
    )

    with pytest.raises(LanePaused) as held:
        preparation.dispatch(
            node=node,
            project="sample",
            repo=repo,
            config=_config(),
            session="coordinator",
            local=True,
            unreviewed_plan_override=True,
            unreconciled_override=True,
            watch_override=True,
        )

    queued = getattr(held.value, "queued", None)
    assert queued, held.value
    assert queued["run_id"] in [pointer["run_id"] for pointer in _queued_reviews()]
