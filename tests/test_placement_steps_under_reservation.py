"""A published reservation places steps for every worker that declares the lane.

A worker is placed by reading the project's published reservation and asking the
scheduler whether the job it names is still around. The two answers that matter
are not the same answer: a scheduler that reports the job in the system holds
it, and a scheduler that answers it knows no such job has released it — but a
question that could not be asked at all (no reporting client on the PATH, a
non-zero exit, a controller that did not answer inside the query's bound), and
that third case used to be read as the job having left. Reading it that way
sends every worker of the placement down the fallback path, where the declared
wrapping mints an allocation of its own: one reservation per worker, silently,
for as long as the query stays unanswerable.

These cases pin the reading the other way round. A published reservation stands
while the question is unanswerable, is used while the scheduler reports it, and
is only released when the scheduler actually answers for it. The scheduler is a
recording fake on the PATH, so what ran — the query, and nothing else — is
asserted from its own log rather than inferred, and the shared crew state is
redirected to a temporary home whose sibling in the real config home is
asserted untouched afterwards.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon._backends import LaunchPlan
from reckon.crew import placement

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# A project name no live project carries, so a record that leaked out of the
# temporary home could not collide with a real reservation.
SYNTHETIC_PROJECT = "steps-under-reservation-fixture"
RESERVATION_ID = "1274051"
RESOLVED_EXECUTABLE = "/opt/backends/bin/codex"

# Every fake scheduler name appends its own argv to this log, so a case can
# assert which questions were asked and which commands were submitted. The name
# comes from shell expansion rather than basename: the PATH these run under
# holds nothing but these stubs, so an external command would not resolve.
_RECORDING_SCHEDULER = """#!/bin/sh
name=${0##*/}
printf '%s\\t%s\\n' "$name" "$*" >> "$FAKE_SCHEDULER_LOG"
if [ "$name" = "squeue" ]; then
  printf '%s\\n' "${FAKE_SCHEDULER_STATE:-}"
fi
exit 0
"""


def _recording_scheduler(directory: Path, names: tuple[str, ...]) -> Path:
    """Executables named ``names`` on PATH that record their own argv.

    Written inside the synthesised config home rather than beside the real one,
    so a case that reaches outside its temporary home cannot record into it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        executable = directory / name
        executable.write_text(_RECORDING_SCHEDULER, encoding="utf-8")
        executable.chmod(0o755)
    return directory


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the shared crew state at a temporary home, and return it."""
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("FAKE_SCHEDULER_LOG", str(home / "scheduler.log"))
    return home


def _asked() -> list[str]:
    """The questions and commands the recording scheduler answered."""
    log = Path(_log_path())
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def _log_path() -> str:
    return os.environ["FAKE_SCHEDULER_LOG"]


def _ambient_placement_dir() -> Path:
    """Where a record for the synthetic project lands if the home is not redirected.

    Read before the temporary home is installed, so the value is the ambient
    config home this host actually uses. Deriving it afterwards would name the
    temporary home instead and the isolation check would pass on the wrong
    directory, so the path is pinned first and carried through the case.
    """
    return placement.reservation_path(SYNTHETIC_PROJECT).parent


def _listing(directory: Path) -> list[str]:
    """What a directory holds, with an absent one reading as empty."""
    if not directory.exists():
        return []
    return sorted(entry.name for entry in directory.iterdir())


def _plan(bin_dir: Path) -> LaunchPlan:
    return LaunchPlan(
        backend="alpha",
        dialect="codex",
        argv=[RESOLVED_EXECUTABLE, "exec", "--task", "t"],
        cwd="/work/tree",
        stdin_text="",
        environment={"PATH": str(bin_dir)},
        final_message_path=None,
        resumed_session=None,
    )


def _placed_backend() -> dict:
    return {
        "launch": "cli",
        "command": "codex",
        "placement": {"scheduler": "srun", "options": ["--partition=all"]},
    }


def _worker_argv(
    bin_dir: Path, *, project: str | None = SYNTHETIC_PROJECT
) -> list[str]:
    """The argv one worker of the placement launches with."""
    plan = dispatch_module.apply_backend_placement(
        _plan(bin_dir), _placed_backend(), project
    )
    return list(plan.argv)


def _step_job_id(argv: list[str]) -> str:
    """The reservation id a placed worker's argv declares."""
    ids = [
        item.removeprefix("--jobid=") for item in argv if item.startswith("--jobid=")
    ]
    assert len(ids) == 1, f"expected exactly one --jobid in {argv}"
    return ids[0]


def _published(project: str | None = SYNTHETIC_PROJECT) -> None:
    placement.publish_reservation(
        {
            "job_id": RESERVATION_ID,
            "scheduler": "salloc",
            "step_scheduler": "srun",
            "options": [],
            "roster_limit": placement.RESERVATION_ROSTER_LIMIT,
        },
        project,
    )


def test_two_workers_share_one_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The property the one-reservation design exists for.

    Two workers of one placement, dispatched while the scheduler cannot be
    asked about the job, declare the same published reservation id and the same
    step form. A reading that treats an unanswerable query as a released job
    gives the second worker an allocation of its own instead, which is the
    silent scatter this case exists to fail on.
    """
    ambient_dir = _ambient_placement_dir()
    ambient = _listing(ambient_dir)
    home = _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(home / "bin", ("srun", "salloc"))
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("squeue", path=str(bin_dir)) is None
    _published()

    first = _worker_argv(bin_dir)
    second = _worker_argv(bin_dir)

    assert _step_job_id(first) == RESERVATION_ID
    assert _step_job_id(second) == RESERVATION_ID
    # One step form, asked once per worker, and none of them an allocation.
    assert (
        first[:3] == second[:3] == [first[0], "--overlap", f"--jobid={RESERVATION_ID}"]
    )
    assert Path(first[0]).name == "srun"
    assert not [item for item in first + second if item == "--no-shell"]
    # The resolved launch is carried through behind the step unchanged.
    launch = _plan(bin_dir).argv
    assert first[-len(launch) :] == launch
    assert second[-len(launch) :] == launch
    # The record stays the one reservation, in the temporary home.
    record = placement.read_reservation(SYNTHETIC_PROJECT)
    assert record is not None and record["job_id"] == RESERVATION_ID
    assert str(placement.reservation_path(SYNTHETIC_PROJECT)).startswith(str(home))
    # And the ambient config home carries no trace of the run.
    assert _listing(ambient_dir) == ambient
    assert not (ambient_dir / "reservation.json").exists()


def test_a_held_reservation_is_used_after_the_scheduler_reports_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordinary path, and the query it rests on is shown to have run.

    A reservation stands on a question that was asked and answered in the
    system, so this case asserts the answer from the recording scheduler's own
    log: the standing reading is not an assumption that skips the scheduler.
    """
    home = _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(home / "bin", ("srun", "salloc", "squeue"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_SCHEDULER_STATE", "RUNNING")
    _published()

    argv = _worker_argv(bin_dir)

    assert _step_job_id(argv) == RESERVATION_ID
    assert [line for line in _asked() if line.startswith("squeue\t")] == [
        f"squeue\t-h -j {RESERVATION_ID} -o %T"
    ]


def test_a_reservation_the_scheduler_answers_has_left_is_released(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An answer that names no job releases the reservation.

    The neighbouring case to the tolerant reading, and the reason the empty
    answer must stay the statement it is: a job that has left the queue is a
    scheduler that ran and named nothing, so a worker must fall back to the
    declared wrapping rather than step into a finished allocation — while a
    query that never ran has said nothing about the job at all.
    """
    home = _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(home / "bin", ("srun", "salloc", "squeue"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_SCHEDULER_STATE", "")
    _published()

    argv = _worker_argv(bin_dir)

    assert not [item for item in argv if item.startswith("--jobid=")]
    assert [Path(argv[0]).name, *argv[1:3]] == [
        "srun",
        "--partition=all",
        RESOLVED_EXECUTABLE,
    ]
    # The release rests on the answer, so the question is shown to have run and
    # to have been answered with nothing.
    assert [line for line in _asked() if line.startswith("squeue\t")] == [
        f"squeue\t-h -j {RESERVATION_ID} -o %T"
    ]


def test_no_published_reservation_leaves_the_declared_wrapping_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No record is not an unanswerable record, and holds nothing back."""
    home = _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(home / "bin", ("srun", "salloc", "squeue"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_SCHEDULER_STATE", "RUNNING")

    argv = _worker_argv(bin_dir)

    assert not [item for item in argv if item.startswith("--jobid=")]
    assert [Path(argv[0]).name, *argv[1:3]] == [
        "srun",
        "--partition=all",
        RESOLVED_EXECUTABLE,
    ]
    assert _asked() == [], "nothing is asked when no record names a job"


def test_the_ensure_command_starts_nothing_when_the_query_cannot_be_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same reading on the holding side, where a second allocation is spent.

    The ensure command is idempotent because the second call must not mint a
    second reservation. A query that could not be asked leaves that idempotence
    intact: it reports the record it read and submits nothing, which the
    recording scheduler's empty log shows directly.
    """
    home = _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(home / "bin", ("srun", "salloc"))
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("squeue", path=str(bin_dir)) is None
    _published()

    result = placement.ensure_reservation(
        project=SYNTHETIC_PROJECT, session="s-1", runner=subprocess.run
    )

    assert result["started"] is False
    assert result["job_id"] == RESERVATION_ID
    assert result["reason"] == "already-held"
    assert _asked() == [], "a second allocation would be recorded here"
    assert placement.read_reservation(SYNTHETIC_PROJECT)["job_id"] == RESERVATION_ID
