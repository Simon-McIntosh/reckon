"""A section closes only when none of its runs can still land.

A section is released by the section landing, and one section can own several
runs. One of them ending ``failed`` therefore says nothing about the same
section while a sibling is still queued, held or working, and nothing while
that failed run has a resume already on its way. Reading the first failed
pointer as the section's end turned every request waiting on such a section
into ``blocked-upstream`` and offered to recover or drop requests that were
correctly queued.

Each case runs against a synthesised crew home and checkout, and every case
asserts the real crew home is untouched: an isolated read does not prove an
isolated write.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import queue_admission, resumption, runs
from reckon.crew.node import TaskNode
from reckon.crew.recovery_classification import classify_pointer

PROJECT = "proj"
PLAN = "plan-a"
SECTION = "s1"
WAITING_SECTION = "s2"

WAITER = "r-waiter"
FAILED = "r-waiter-failed"
SIBLING = "r-sibling"

DECLARED = f"{PLAN}#{SECTION}"


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _attribute(value: object) -> str:
    return json.dumps(value).replace('"', "&quot;")


def _plan_html(declarations: dict[str, str]) -> str:
    depends = _attribute({WAITING_SECTION: [DECLARED]})
    statuses = _attribute(declarations)
    return "\n".join(
        [
            "<!doctype html>",
            '<meta name="docs-project" content="' + PROJECT + '">',
            '<meta name="reckon-type" content="plan">',
            '<meta name="plan-slug" content="' + PLAN + '">',
            f'<meta name="plan-section-depends-on" content="{depends}">',
            f'<meta name="plan-section-declarations" content="{statuses}">',
            f'<h2 id="{SECTION}">One</h2>',
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
        _plan_html({SECTION: "implementable", WAITING_SECTION: "implementable"}),
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
    phase: str,
    waits: list[str] | None = None,
) -> dict:
    node = TaskNode(
        id=f"node-{run_id}",
        goal="wait on the declared section only",
        plan=PLAN,
        section=section,
        done_when="the request follows the section it waits on",
        write_paths=[f"{run_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": "coordinator-a",
        "repo": str(repository),
        "node": node.as_dict(),
        "phase": phase,
        "queued_at": joined,
        "created_at": joined,
        "waits": list(waits or ()),
    }


def _store(pointer: dict) -> None:
    runs._write_json(runs.pointer_path(str(pointer["run_id"])), dict(pointer))


def _pass_over(*run_ids: str) -> dict:
    return queue_admission.advance_waiting_requests(
        [runs.read_pointer(run_id) for run_id in run_ids]
    )


def _waiter(repository: Path) -> dict:
    return _request(
        repository,
        WAITER,
        section=WAITING_SECTION,
        phase=queue_admission.AWAITING_UPSTREAM_PHASE,
        waits=[DECLARED],
    )


def _failed_run(repository: Path) -> dict:
    return _request(repository, FAILED, section=SECTION, phase="failed")


def _sibling_run(repository: Path) -> dict:
    return _request(repository, SIBLING, section=SECTION, phase="queued")


@contextlib.contextmanager
def _a_resume_in_flight(run_id: str) -> Iterator[None]:
    """Mark a run as resuming exactly as the resume door marks it.

    The mark the door writes names the process that is launching the resume,
    and every reader of it is a different process. A test is one process, so
    the mark names a live process this one started and will stop.
    """
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        pointer = runs.read_pointer(run_id)
        pointer["resume_launch_in_flight"] = {
            "pid": holder.pid,
            "pid_start_time": resumption.process_start_time(holder.pid),
            "at": datetime.now(UTC).isoformat(),
        }
        runs._write_json(runs.pointer_path(run_id), pointer)
        assert resumption.launch_in_flight(runs.read_pointer(run_id))
        yield
    finally:
        holder.kill()
        holder.wait()


def _real_crew_home_entries(home: Path) -> list[tuple[str, int | None]]:
    """The real home's paths these run ids could have written, with mtimes."""
    touched = []
    for run_id in (WAITER, FAILED, SIBLING):
        touched.append(home / "live" / f"{run_id}.json")
        touched.append(home / "runs" / run_id)
    return [
        (str(path), path.stat().st_mtime_ns if path.exists() else None)
        for path in touched
    ]


@pytest.fixture(autouse=True)
def _real_crew_home_untouched():
    """Assert the shared home this test's run ids would land in is untouched."""
    real_home = Path(os.path.expanduser("~/.config/reckon/crew"))
    before = _real_crew_home_entries(real_home)
    yield
    assert _real_crew_home_entries(real_home) == before


def test_a_failed_run_beside_a_live_sibling_keeps_its_waiter_waiting(
    queue_repo: Path,
) -> None:
    """One failed node of a section is not the section's end."""
    _store(_waiter(queue_repo))
    _store(_failed_run(queue_repo))
    _store(_sibling_run(queue_repo))

    _pass_over(WAITER, FAILED, SIBLING)

    waiter = runs.read_pointer(WAITER)
    assert waiter["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert waiter["upstream_live"] == [SIBLING]
    row = classify_pointer(waiter)
    assert row["classification"] == "awaiting-upstream"
    assert "can no longer land" not in row["detail"]
    assert SIBLING in row["detail"]


def test_a_resumed_failed_run_returns_its_waiters_to_waiting(
    queue_repo: Path,
) -> None:
    """The mark a resume writes on the failed pointer lifts the section again."""
    _store(_waiter(queue_repo))
    _store(_failed_run(queue_repo))
    _pass_over(WAITER, FAILED)

    blocked = runs.read_pointer(WAITER)
    assert blocked["phase"] == queue_admission.BLOCKED_UPSTREAM_PHASE
    blocked_row = classify_pointer(blocked)
    assert "can no longer land" in blocked_row["detail"]
    assert blocked_row["next_action"].startswith("recover or drop")

    with _a_resume_in_flight(FAILED):
        _pass_over(WAITER, FAILED)

    resumed = runs.read_pointer(WAITER)
    assert resumed["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert resumed["upstream_live"] == [FAILED]


def test_a_section_no_run_of_which_can_land_still_reads_dead(queue_repo: Path) -> None:
    """The judgment this change narrows keeps its one honest case."""
    _store(_waiter(queue_repo))
    _store(_failed_run(queue_repo))

    _pass_over(WAITER, FAILED)

    waiter = runs.read_pointer(WAITER)
    assert waiter["phase"] == queue_admission.BLOCKED_UPSTREAM_PHASE
    assert not waiter.get("upstream_live")
    row = classify_pointer(waiter)
    assert "can no longer land" in row["detail"]
    assert DECLARED in row["detail"]
