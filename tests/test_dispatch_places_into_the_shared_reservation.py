"""A placed dispatch holds the one shared reservation, then runs its worker in it.

A dispatch that declares a placement runs inside the one allocation every worker
of the host shares. Resolution must reach the ensure rather than prefix a bare
step client: the bare client carries no job id and publishes nothing for the
next dispatch to find, so every dispatch mints an allocation of its own, and
twenty workers ask for twenty allocations with most of them queueing. Holding
the shared one instead creates it once, by whichever dispatch arrives first, and
every later dispatch reads the published job id and joins it.

The scheduler is a recording stub on the PATH throughout, so the property under
test is reckon's plumbing — how many allocations are asked for and which job id
a worker is placed under — and no real scheduling client is reached. The shared
crew state is a temporary home, so no case touches the ambient config home.

An allocation-obtaining request is what the name says: an ``salloc`` the ensure
runs, or a wrapping that would ask the scheduler for an allocation of its own —
a step client carrying no job id. A step that names a job id is placed into an
allocation rather than asking for one, so it is not counted.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

from reckon._backends import LaunchPlan
from reckon.crew import placement

dispatch_module = importlib.import_module("reckon.crew.dispatch")

RESOLVED_EXECUTABLE = "/opt/backends/bin/codex"
GRANTED_ID = "55600001"

# Every stub appends its own argv to this log, so a case shows which questions
# were asked and how many allocations were obtained.
_RECORDING_SCHEDULER = """#!/bin/sh
name=${0##*/}
printf '%s\\t%s\\n' "$name" "$*" >> "$FAKE_SCHEDULER_LOG"
case "$name" in
  salloc)
    printf 'salloc: Granted job allocation %s\\n' "${FAKE_JOB:-55600001}"
    ;;
  squeue)
    printf '%s\\n' "${FAKE_SQUEUE_STATE-RUNNING}"
    ;;
esac
exit 0
"""


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the shared crew state at a temporary home before anything is written."""
    home = tmp_path / "config"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("FAKE_SCHEDULER_LOG", str(home / "scheduler.log"))
    return home


def _recording_scheduler(directory: Path) -> Path:
    """Executables named for the scheduler verbs, each recording its own argv."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("salloc", "srun", "sbatch", "squeue"):
        executable = directory / name
        executable.write_text(_RECORDING_SCHEDULER, encoding="utf-8")
        executable.chmod(0o755)
    return directory


def _scheduler_path(bin_dir: Path) -> str:
    """The recording stubs first on the system PATH, so they shadow any client."""
    return os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])


def _asked(name: str) -> list[str]:
    """The recorded invocations of one scheduler verb."""
    log = Path(os.environ["FAKE_SCHEDULER_LOG"])
    if not log.exists():
        return []
    return [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith(name + "\t")
    ]


def _step_job_id(argv: list[str]) -> str | None:
    """The reservation id a placed worker's argv declares, or None."""
    ids = [
        item.removeprefix("--jobid=") for item in argv if item.startswith("--jobid=")
    ]
    assert len(ids) <= 1, f"expected at most one --jobid in {argv}"
    return ids[0] if ids else None


def _allocation_requests(*plans: LaunchPlan) -> int:
    """Requests that ask the scheduler for an allocation, across the plans given.

    The ensure's ``salloc`` is one; a wrapping whose step client carries no job
    id is the other, because that is exactly the bare form the next dispatch
    cannot find and so mints an allocation of its own. A wrapping that names a
    job id is placed into an allocation the caller already holds.
    """
    count = len(_asked("salloc"))
    for plan in plans:
        argv = plan.argv
        if Path(argv[0]).name == "srun" and _step_job_id(argv) is None:
            count += 1
    return count


def _plan(environment: dict) -> LaunchPlan:
    return LaunchPlan(
        backend="alpha",
        dialect="codex",
        argv=[RESOLVED_EXECUTABLE, "exec", "--task", "t"],
        cwd="/work/tree",
        stdin_text="",
        environment=environment,
        final_message_path=None,
        resumed_session=None,
    )


def _placed_backend() -> dict:
    return {
        "launch": "cli",
        "command": "codex",
        "placement": {"scheduler": "srun", "options": ["--partition=all"]},
    }


def _dispatch(bin_dir: Path, project: str) -> LaunchPlan:
    return dispatch_module.resolve_backend_placement(
        _plan({"PATH": _scheduler_path(bin_dir)}), _placed_backend(), project
    )


def test_a_dispatch_with_no_reservation_holds_one_and_places_into_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolution holds the shared allocation once and steps the worker into it.

    With no readable reservation the dispatch must not fall back to a bare step
    client: it holds the shared allocation, publishes its job id unkeyed where
    every project reads it, and runs its worker as an overlapping step carrying
    that id.
    """
    _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    launch = _plan({"PATH": _scheduler_path(bin_dir)})

    wrapped = _dispatch(bin_dir, "alpha")

    assert len(_asked("salloc")) == 1
    record = placement.read_reservation()
    assert record is not None and record["job_id"] == GRANTED_ID
    assert "--overlap" in wrapped.argv
    assert _step_job_id(wrapped.argv) == GRANTED_ID
    # The resolved launch is carried through behind the step unchanged.
    assert wrapped.argv[-len(launch.argv) :] == launch.argv


def test_a_second_project_joins_the_one_allocation_across_both_dispatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The second project reads the published record and starts no allocation.

    The property the shared design exists for, and one dispatch cannot show it.
    Across a dispatch under one project and a dispatch under another, sharing
    nothing but the published record, exactly one allocation-obtaining request
    is made and both workers are placed under the one job id the first dispatch
    published.
    """
    _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))

    first = _dispatch(bin_dir, "alpha")
    second = _dispatch(bin_dir, "beta")

    assert _allocation_requests(first, second) == 1
    assert len(_asked("salloc")) == 1
    assert _step_job_id(first.argv) == GRANTED_ID
    # The second dispatch started nothing of its own: the published record is
    # still the first dispatch's job id, unchanged.
    assert _step_job_id(second.argv) == GRANTED_ID
    assert placement.read_reservation()["job_id"] == GRANTED_ID


def test_a_backend_declaring_no_placement_is_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No placement means no reservation: nothing held, wrapping unchanged."""
    _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    plan = _plan({"PATH": _scheduler_path(bin_dir)})

    wrapped = dispatch_module.resolve_backend_placement(
        plan, {"launch": "cli", "command": "codex"}, "alpha"
    )

    assert wrapped is plan
    assert _asked("salloc") == []
    assert _allocation_requests(wrapped) == 0
    assert wrapped.argv == plan.argv


def test_a_legacy_per_project_record_is_placed_into_without_a_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An existing per-project record is honoured; no allocation is obtained.

    The on-disk shape ``placement/<project>/reservation.json`` is read for its
    own project, its job id is placed into, and the ensure finds it already held
    rather than obtaining another.
    """
    _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    legacy = placement.legacy_reservation_path("imas-ambix")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"job_id": "99000001", "scheduler": "salloc"}),
        encoding="utf-8",
    )
    assert not placement.reservation_path().exists()

    wrapped = _dispatch(bin_dir, "imas-ambix")

    assert _step_job_id(wrapped.argv) == "99000001"
    assert _asked("salloc") == []
