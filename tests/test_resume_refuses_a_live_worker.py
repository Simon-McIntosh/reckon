"""A resume of a run whose worker still lives is refused, naming that process.

A live worker holds the run's session, so a second attempt started over it
collides with the worker already writing and can reclassify the run from
working to blocked on its way out. Both doors that can start a worker — a
hand-typed ``crew resume`` and the automatic resume sweep — therefore refuse
while the run's process is still there, and the refusal names the pid so the
coordinator reading it can check the process table rather than take the
refusal on faith.

The worker is not always the pid the pointer names. A supervised launch
records the supervisor on the pointer and the worker it spawned in the run
directory's own worker record, and a supervisor that exits ahead of its worker
takes the pointer's pid with it while the worker goes on committing. The
reading here is the same host-gated composition every other surface reads, and
the refusal reports which pid supplied it.

The supervisor's exit record does not license a resume while a worker still
answers for itself: a launcher that ends early can leave a record beside a run
that is still being worked, and the record is an observation of the end only
where nothing is still running.

The declared mutation, applied verbatim in a scratch copy: with the liveness
check removed from the manual resume, these cases launch a second attempt and
fail.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption, runs
from reckon.crew.dispatch import resume_plan
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "With the liveness check removed from the manual resume, "
    "tests/test_resume_refuses_a_live_worker.py launches a second attempt and fails."
)

PROJECT = "fixture-project"
HOST = socket.gethostname()
NOW_SECONDS = datetime(2026, 9, 29, 12, 0, tzinfo=UTC).timestamp()
MANIFEST_NS = int(datetime(2026, 9, 29, 10, 0, tzinfo=UTC).timestamp()) * 1_000_000_000
CONDITION = "the scheduler job has finished"

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "fixture-agent",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    ceiling = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    return ceiling + 4096


@contextmanager
def _live_worker():
    """A real, running child process, bounded by its own timeout and reaped by pid.

    The process sleeps on its own clock, so it ends even if this test's
    teardown never runs, and it is killed and waited on by its pid on the way
    out so no child is left behind for the next case to see.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


_REAL_HOME = Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"
_RUN_IDS = ("r-manual-live-worker", "r-sweep-live-worker", "r-live-under-record")


def _case_artifacts(crew_home: Path) -> list[Path]:
    """The paths this file's cases leave under ``crew_home``.

    Named for the run ids above, so the same list describes the real crew home
    and a stand-in: the assertion below is the receipt that this file wrote
    nothing into a live fleet's home while it ran.
    """
    return [
        *(crew_home / "runs" / run_id for run_id in _RUN_IDS),
        *(crew_home / "live" / f"{run_id}.json" for run_id in _RUN_IDS),
    ]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    landed = [path for path in _case_artifacts(_REAL_HOME) if path.exists()]
    assert not landed, f"the real crew home carries this file's run paths: {landed}"


def _waiting_manifest(path: Path, awaited: Path) -> None:
    path.write_text(
        "status: waiting\n"
        f"wait_condition: {CONDITION}\n"
        f"wait_file: {json.dumps([str(awaited)])}\n"
        "resume_brief: read the job's output and continue\n",
        encoding="utf-8",
    )
    os.utime(path, ns=(MANIFEST_NS, MANIFEST_NS))


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    pid: int | None,
    worker_pid: int | None = None,
    exit_record: bool = False,
    parked: bool = False,
) -> dict:
    """A stopped run, differing only in which process the evidence names."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / f"{run_id}-tree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / f"{run_id}.md"
    if parked:
        awaited = tmp_path / f"{run_id}-condition-met"
        awaited.write_text("done\n", encoding="utf-8")
        _waiting_manifest(manifest, awaited)
    else:
        manifest.write_text(
            "node: resume-refuses-a-live-worker\nstatus: waiting\n", encoding="utf-8"
        )
    if worker_pid is not None:
        (directory / recovery.WORKER_RECORD_NAME).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "attempt": 1,
                    "pid": worker_pid,
                    "pid_start_time": recovery._process_start_time(worker_pid),
                    "launched_at": "2026-09-29T09:59:30Z",
                    "backend": "alpha",
                    "argv": ["fixture-agent", "exec"],
                }
            ),
            encoding="utf-8",
        )
    if exit_record:
        # A record written while a launcher ended early, as the supervisor's
        # signal short-circuit leaves it: the run's worker is still running.
        (directory / recovery.EXIT_RECORD_NAME).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "attempt": 1,
                    "recorded_by": "supervisor",
                    "worker_pid": worker_pid,
                    "launched_at": "2026-09-29T09:59:30Z",
                    "exited_at": "2026-09-29T09:59:33Z",
                    "exit_code": None,
                    "signal": 15,
                    "signal_name": "SIGTERM",
                    "stream_records_seen": 4,
                    "last_record_type": "thread.started",
                    "ended_during": "working",
                }
            ),
            encoding="utf-8",
        )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "backend": "alpha",
        "launch": "cli",
        "argv": ["fixture-agent", "exec"],
        "role": "implement",
        "session_id": "fixture-session",
        "phase": "working",
        "attempt": 1,
        "created_at": "2026-09-29T09:59:00+00:00",
        "manifest_path": str(manifest),
        "log_path": str(tmp_path / f"{run_id}.jsonl"),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "worktree": str(worktree),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid) if pid else None,
        "launcher_host": HOST,
        "node": {
            "id": run_id,
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


def _classification(run_id: str) -> str:
    return str(
        recovery.classify_pointer(runs.read_pointer(run_id)).get("classification")
    )


# --- The manual door: a hand-typed resume ---------------------------------


def test_a_manual_resume_of_a_live_worker_is_refused_naming_its_pid(home) -> None:
    """The worker the run's own record names is alive, so nothing may start."""
    with _live_worker() as worker_pid:
        _pointer(home, "r-manual-live-worker", pid=_absent_pid(), worker_pid=worker_pid)

        with pytest.raises(CrewError) as raised:
            resume_plan("r-manual-live-worker", "continue", config=CONFIG)

        refusal = str(raised.value)
        assert str(worker_pid) in refusal, refusal
        assert "alive" in refusal, refusal
        # The refusal is a refusal and not a stop: the run is still the live
        # worker's, and it must keep reading that way to every later reader.
        assert _classification("r-manual-live-worker") == "running"


def test_a_manual_resume_of_a_live_pointer_process_is_refused(home) -> None:
    """The pointer's own pid answering alive is the case the guard always held."""
    with _live_worker() as pid:
        _pointer(home, "r-live-under-record", pid=pid)

        with pytest.raises(CrewError) as raised:
            resume_plan("r-live-under-record", "continue", config=CONFIG)

        refusal = str(raised.value)
        assert "still has a live process" in refusal, refusal
        assert "before resuming" in refusal, refusal
        assert _classification("r-live-under-record") == "running"


# --- The automatic door: the resume sweep ----------------------------------


def _sweep_launching(monkeypatch: pytest.MonkeyPatch, pointers: list[dict]):
    """Run one real sweep, with a recorded launcher standing in for the spawn."""
    launches: list[dict] = []

    def launcher(plan, **kwargs):
        launches.append({"plan": plan, **kwargs})
        return 424242

    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: list(pointers))
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])
    monkeypatch.setattr(resumption, "dispatch_awaiting_reviews", lambda **_kwargs: {})
    report = resumption.sweep(
        PROJECT,
        config=CONFIG,
        dry_run=False,
        launcher=launcher,
        now=datetime.fromtimestamp(NOW_SECONDS, tz=UTC),
    )
    return report, launches


def test_the_sweep_refuses_a_live_pointer_process_before_any_spawn(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked run whose supervisor still lives is refused before any spawn.

    The message is the launcher's own, held word for word by the prediction
    parity test; what this case pins is that the sweep meets it, starts
    nothing, and leaves the run reading as it did.
    """
    with _live_worker() as pid:
        pointer = _pointer(tmp_path, "r-sweep-live-under-record", pid=pid, parked=True)
        before = _classification("r-sweep-live-under-record")
        report, launches = _sweep_launching(monkeypatch, [pointer])

        assert launches == []
        assert report["resumed"] == []
        skipped = {row["run_id"]: row for row in report["skipped"]}
        entry = skipped["r-sweep-live-under-record"]
        assert entry["reason"] == "resume-refused", entry
        assert "still has a live process" in entry["detail"], entry
        # The refusal starts nothing and stops nothing: the run reads exactly
        # as it did before the sweep touched it, while its worker still lives.
        assert _classification("r-sweep-live-under-record") == before


def test_an_early_exit_record_does_not_license_a_resume_while_the_worker_lives(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record written beside a worker that still runs does not end the attempt.

    The shape is the launcher that ended by signal while the worker it adopted
    went on: the record names the signal and sits beside a run whose own worker
    record still answers alive. The record is evidence of the end only where
    nothing still runs, so the sweep refuses, names the worker's pid, and
    launches nothing.
    """
    with _live_worker() as worker_pid:
        pointer = _pointer(
            tmp_path,
            "r-live-under-record",
            pid=_absent_pid(),
            worker_pid=worker_pid,
            exit_record=True,
            parked=True,
        )
        before = _classification("r-live-under-record")
        report, launches = _sweep_launching(monkeypatch, [pointer])

        assert launches == []
        assert report["resumed"] == []
        entry = next(
            row for row in report["skipped"] if row["run_id"] == "r-live-under-record"
        )
        assert entry["reason"] == "resume-refused", entry
        assert str(worker_pid) in entry["detail"], entry
        assert _classification("r-live-under-record") == before
