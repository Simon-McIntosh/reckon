"""A dropped request strands no waiter.

An admission pass re-derives a queued request's waits from its plan, because the
plan is what a coordinator steers a request through. The request's own ``run:``
refs come from its authored ``after``, so the pass keeps a stored ``run:`` wait
only when the request named it: a wait the request never asked for is dropped,
wherever it came from.

Keeping every stored ``run:`` ref instead makes a stored wait permanent, and a
wait on work the request never named is a wait nobody chose. Discarding the run
it names then closes the waiter as ``blocked-upstream``, a phase no pass lifts
and the queue's own drop refuses, so the request can only be thrown away.

Each case runs against a synthesised crew home and checkout, and every case
asserts the real crew home is untouched: an isolated read does not prove an
isolated write.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import promotion, queue_admission, runs
from reckon.crew.node import TaskNode

PROJECT = "proj"
PLAN = "plan-a"
WAITING_SECTION = "s2"
DECLARED = f"{PLAN}#s1"
WAITER = "r-waiter"
DROPPED = "r-dropped-request"
QUEUED_SIBLING = "r-queued-sibling"


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _plan_html(declarations: dict[str, str]) -> str:
    depends = json.dumps({WAITING_SECTION: [DECLARED]}).replace('"', "&quot;")
    statuses = json.dumps(declarations).replace('"', "&quot;")
    return "\n".join(
        [
            "<!doctype html>",
            f'<meta name="docs-project" content="{PROJECT}">',
            '<meta name="reckon-type" content="plan">',
            f'<meta name="plan-slug" content="{PLAN}">',
            f'<meta name="plan-section-depends-on" content="{depends}">',
            f'<meta name="plan-section-declarations" content="{statuses}">',
            '<h2 id="s1">One</h2>',
            f'<h2 id="{WAITING_SECTION}">Two</h2>',
            "",
        ]
    )


@pytest.fixture()
def queue_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "queue-repository"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plan = repository / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        _plan_html({"s1": "implementable", WAITING_SECTION: "implementable"}),
        encoding="utf-8",
    )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed the queue repository")

    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _request(
    repository: Path,
    run_id: str,
    *,
    section: str,
    after: list[str],
    waits: list[str],
    wait_projects: dict[str, str] | None = None,
) -> dict:
    node = TaskNode(
        id=f"node-{run_id}",
        goal="wait on the declared section only",
        plan=PLAN,
        section=section,
        done_when="the admission pass drops a wait this request never named",
        write_paths=[f"{run_id}.txt"],
        time_budget="20m",
        spec_level="exact",
        after=list(after),
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": "coordinator-a",
        "repo": str(repository),
        "node": node.as_dict(),
        "phase": queue_admission.AWAITING_UPSTREAM_PHASE,
        "queued_at": joined,
        "created_at": joined,
        "waits": list(waits),
        "wait_projects": dict(wait_projects or {}),
    }


def _store(repository: Path, pointer: dict) -> None:
    runs._write_json(runs.pointer_path(str(pointer["run_id"])), dict(pointer))


def _pass_over(*run_ids: str) -> dict:
    return queue_admission.advance_waiting_requests(
        [runs.read_pointer(run_id) for run_id in run_ids]
    )


def _land_declared_wait(repository: Path) -> None:
    plan = repository / "docs" / "plans" / f"{PLAN}.html"
    plan.write_text(
        _plan_html({"s1": "done", WAITING_SECTION: "implementable"}),
        encoding="utf-8",
    )


def _real_crew_home_entries(home: Path) -> list[tuple[str, int | None]]:
    """The real home's paths these run ids could have written, with mtimes."""
    touched = []
    for run_id in (WAITER, DROPPED, QUEUED_SIBLING):
        touched.append(home / "live" / f"{run_id}.json")
        touched.append(home / "runs" / run_id)
    touched.append(home / "discards" / f"{DROPPED}.json")
    return [
        (str(path), path.stat().st_mtime_ns if path.exists() else None)
        for path in touched
    ]


@pytest.fixture(autouse=True)
def _real_crew_home_untouched(monkeypatch: pytest.MonkeyPatch):
    """Assert the shared home this test's run ids would land in is untouched."""
    real_home = Path(os.path.expanduser("~/.config/reckon/crew"))
    before = _real_crew_home_entries(real_home)
    yield
    assert _real_crew_home_entries(real_home) == before


def test_an_unnamed_wait_on_a_dropped_request_is_dropped_and_the_waiter_lifts(
    queue_repo: Path,
) -> None:
    """A pass drops a stored run: wait the request never named, and it lifts."""
    waiter = _request(
        queue_repo,
        WAITER,
        section=WAITING_SECTION,
        after=[],
        waits=[DECLARED, f"run:{DROPPED}"],
        wait_projects={DROPPED: PROJECT},
    )
    dropped = _request(
        queue_repo, DROPPED, section="s1", after=[], waits=[f"{PLAN}#s1"]
    )
    _store(queue_repo, waiter)
    _store(queue_repo, dropped)

    promotion.discard(DROPPED)
    assert promotion.discard_recorded(DROPPED)

    _pass_over(WAITER)

    stored = runs.read_pointer(WAITER)
    assert stored["waits"] == [DECLARED], stored["waits"]
    assert stored["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE, stored["phase"]

    _land_declared_wait(queue_repo)
    _pass_over(WAITER)

    lifted = runs.read_pointer(WAITER)
    assert lifted["phase"] == queue_admission.QUEUED_PHASE, lifted["phase"]


def test_an_unnamed_wait_on_a_queued_request_is_not_waited_on(
    queue_repo: Path,
) -> None:
    """A pass drops the wait even while the request it names still waits."""
    waiter = _request(
        queue_repo,
        WAITER,
        section=WAITING_SECTION,
        after=[],
        waits=[DECLARED, f"run:{QUEUED_SIBLING}"],
        wait_projects={QUEUED_SIBLING: PROJECT},
    )
    sibling = _request(
        queue_repo,
        QUEUED_SIBLING,
        section="s1",
        after=[],
        waits=[DECLARED],
    )
    _store(queue_repo, waiter)
    _store(queue_repo, sibling)

    _pass_over(WAITER, QUEUED_SIBLING)

    stored = runs.read_pointer(WAITER)
    assert f"run:{QUEUED_SIBLING}" not in stored["waits"], stored["waits"]
    assert stored["waits"] == [DECLARED], stored["waits"]
    assert stored["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE, stored["phase"]

    _land_declared_wait(queue_repo)
    _pass_over(WAITER, QUEUED_SIBLING)

    lifted = runs.read_pointer(WAITER)
    assert lifted["phase"] == queue_admission.QUEUED_PHASE, lifted["phase"]


def test_a_wait_the_request_named_still_blocks_when_its_run_is_discarded(
    queue_repo: Path,
) -> None:
    """A run: wait named through the request's own after keeps today's answer."""
    waiter = _request(
        queue_repo,
        WAITER,
        section=WAITING_SECTION,
        after=[DROPPED],
        waits=[DECLARED, f"run:{DROPPED}"],
        wait_projects={DROPPED: PROJECT},
    )
    dropped = _request(
        queue_repo, DROPPED, section="s1", after=[], waits=[f"{PLAN}#s1"]
    )
    _store(queue_repo, waiter)
    _store(queue_repo, dropped)

    promotion.discard(DROPPED)
    assert promotion.discard_recorded(DROPPED)

    _pass_over(WAITER)

    stored = runs.read_pointer(WAITER)
    assert f"run:{DROPPED}" in stored["waits"], stored["waits"]
    assert stored["phase"] == queue_admission.BLOCKED_UPSTREAM_PHASE, stored["phase"]
