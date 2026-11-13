"""A fleet that has moved to another allocation stays moved.

The old allocation's supervisor outlives the move until it is retired. It must
not publish its own record over the new one, a service copy that outlives
SIGKILL must not gain a replacement beside it, and the move's census must find
the fleet's zellij servers whatever environment launched it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import fleet_migrate, fleet_supervisor


def _environ(tmp_path: Path, own: str) -> dict[str, str]:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return {
        "SLURM_JOB_ID": own,
        "FLEET_STATE_DIR": str(state),
        "FLEET_RUNTIME_DIR": str(tmp_path / "runtime"),
    }


def _publish(environ: dict[str, str], job: str) -> None:
    record = Path(environ["FLEET_STATE_DIR"]) / fleet_supervisor.RECORD_NAME
    record.write_text(json.dumps({"job_id": job, "node": "somewhere"}))


def test_a_record_naming_another_running_job_supersedes_this_one(tmp_path: Path):
    environ = _environ(tmp_path, "100")
    _publish(environ, "200")
    asked: list[str] = []

    def running(job: str) -> str:
        asked.append(job)
        return "RUNNING"

    assert fleet_supervisor.superseding_job(environ, job_state=running) == "200"
    assert asked == ["200"]


@pytest.mark.parametrize("state", [None, "COMPLETED", "CANCELLED", "PENDING"])
def test_a_record_naming_a_job_that_is_not_running_is_taken_over(
    tmp_path: Path, state: str | None
):
    environ = _environ(tmp_path, "100")
    _publish(environ, "200")
    assert fleet_supervisor.superseding_job(environ, job_state=lambda _: state) is None


def test_the_own_record_and_a_missing_record_never_ask_the_scheduler(tmp_path: Path):
    environ = _environ(tmp_path, "100")

    def forbidden(job: str) -> str:
        raise AssertionError(f"scheduler asked about {job}")

    assert fleet_supervisor.superseding_job(environ, job_state=forbidden) is None
    _publish(environ, "100")
    assert fleet_supervisor.superseding_job(environ, job_state=forbidden) is None
    assert (
        fleet_supervisor.superseding_job(
            {**environ, "SLURM_JOB_ID": ""}, job_state=forbidden
        )
        is None
    )


def test_a_restart_whose_stop_leaves_the_copy_alive_starts_no_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    environ = _environ(tmp_path, "100")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manager = fleet_supervisor.DeclaredServices(runtime, environ)
    started: list[str] = []
    monkeypatch.setattr(manager, "_stop_locked", lambda name: False)
    monkeypatch.setattr(
        manager, "_start_locked", lambda name, argv: started.append(name) or 4321
    )
    state = fleet_supervisor._ServiceState(argv=["true"], pid=1234)
    manager._restart_locked("ingest", state)
    assert started == []
    monkeypatch.setattr(manager, "_stop_locked", lambda name: True)
    manager._restart_locked("ingest", state)
    assert started == ["ingest"]


def test_the_census_lists_sessions_in_the_fleet_socket_directory(
    tmp_path: Path, monkeypatch
):
    runtime = tmp_path / "fleet-runtime"
    monkeypatch.setenv("FLEET_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", "/run/user/elsewhere/zellij")
    seen: dict[str, str] = {}

    def invoke(argv, **options):
        seen.update(options["env"])
        return subprocess.CompletedProcess(argv, 0, "alpha-fleet [Created]\n", "")

    assert fleet_migrate._listed_sessions(invoke=invoke) == [("alpha-fleet", False)]
    assert seen["ZELLIJ_SOCKET_DIR"] == str(runtime / "zellij")


def test_runs_launched_on_the_old_node_match_by_short_hostname():
    live = [
        {"run_id": "r-b", "launcher_host": "old-node.example.org", "phase": "working"},
        {"run_id": "r-a", "launcher_host": "old-node", "phase": "a-phase-nobody-knows"},
        {"run_id": "r-c", "launcher_host": "new-node.example.org", "phase": "working"},
        {"run_id": "r-d", "phase": "working"},
        {"run_id": "r-e", "launcher_host": "old-node", "phase": "complete"},
        {"run_id": "r-f", "launcher_host": "old-node", "phase": "promotion-requested"},
        {"run_id": "r-g", "launcher_host": "old-node", "phase": "queued"},
    ]
    assert fleet_migrate._runs_launched_on("old-node", live) == ["r-a", "r-b"]
    assert fleet_migrate._runs_launched_on("", live) == []


def test_lock_holders_are_read_from_proc_locks_and_waiters_are_skipped():
    device = os.makedev(0xFD, 0x2)
    text = (
        "1: FLOCK  ADVISORY  WRITE 2612499 fd:02:4242 0 EOF\n"
        "1: -> FLOCK  ADVISORY  WRITE 3000 fd:02:4242 0 EOF\n"
        "2: FLOCK  ADVISORY  WRITE 1111 fd:02:9999 0 EOF\n"
        "3: POSIX  ADVISORY  WRITE 2222 08:01:4242 0 EOF\n"
        "4: FLOCK  ADVISORY  WRITE 2612499 fd:02:4242 0 EOF\n"
    )
    assert fleet_supervisor.lock_holders_in(text, device, 4242) == [2612499]


def test_a_live_lock_names_the_process_that_holds_it(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = fleet_supervisor.service_lock_path(runtime, "ingest")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time; f = open(sys.argv[1], 'w'); "
                "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); "
                "time.sleep(60)"
            ),
            str(path),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout.readline() == b"held\n"
        assert fleet_supervisor.service_lock_holders("ingest", runtime) == [holder.pid]
    finally:
        holder.kill()
        holder.wait()
    assert fleet_supervisor.service_lock_holders("ingest", runtime) == []
