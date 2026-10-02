"""After ``crew stop``, the stopped run's supervisor exits within a stated bound.

``crew stop`` signals the worker's whole process group, so the supervisor and
the worker both receive the stop. The supervisor's SIGTERM/SIGHUP handler only
*records* that stop; without a bound the supervisor then blocks in ``waitpid``
for as long as the worker lives. A worker that ignores the group signal
therefore held the supervisor alive about 45 s after the stop was recorded
(measured on ``r-20261002T052254559858``).

These cases drive a real detached supervisor for a stub worker that ignores
SIGTERM, stop the run through ``dispatch.terminate`` -- the call ``crew stop``
makes -- and measure how long the supervisor's pid survives. The supervisor is
expected to give the worker the configured stop grace to end on its own, then
SIGKILL it and write the exit record, so its pid is gone within
``stop_grace + 3 * poll + slack`` of the stop.

The worker's stub sleep is far larger than the bound, so an exit inside the
bound can only be the supervisor's act. The declared mutation withholds the
stop-grace escalation: the supervisor then waits on the worker's own exit, its
pid is still alive past the bound, and the test fails.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

dispatch_module = importlib.import_module("reckon.crew.dispatch")
runs = importlib.import_module("reckon.crew.runs")
routing = importlib.import_module("reckon.crew.routing")

# A stub worker that ignores the group stop, so an exit is only ever the
# supervisor's escalation or a signal the test itself sends.
STUB_IGNORES_STOP = (
    "import os, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "from pathlib import Path\n"
    "Path(os.environ['RECKON_MANIFEST']).write_text(\n"
    "    'node: stub-node\\nstatus: in-progress\\ncommits: []\\n'\n"
    ")\n"
    "time.sleep(float(os.environ['RECKON_STUB_SLEEP']))\n"
)

# A stub worker that ends on the group stop, so the supervisor needs no
# escalation: the positive control that the bound is met without a kill.
STUB_ENDS_ON_STOP = (
    "import os, time\n"
    "from pathlib import Path\n"
    "Path(os.environ['RECKON_MANIFEST']).write_text(\n"
    "    'node: stub-node\\nstatus: in-progress\\ncommits: []\\n'\n"
    ")\n"
    "time.sleep(float(os.environ['RECKON_STUB_SLEEP']))\n"
)

NEGATIVE_CONTROL = os.environ.get("RECKON_STOP_SUPERVISOR_NEGATIVE_CONTROL", "").strip()

# The bound the cases assert. The grace is the configured stop grace; the poll
# is how often the supervisor's reap loop wakes, paid once to notice the stop
# and once more to notice the kill; the slack covers process startup and a
# loaded machine. The stub's sleep is far larger, so an exit inside the bound
# can only be the supervisor's act.
POLL_SECONDS = dispatch_module._WORKER_MANIFEST_POLL_SECONDS
STOP_GRACE_SECONDS = 1.0
WORKER_SLEEP_SECONDS = 120.0
SLACK_SECONDS = 8.0
BOUND_SECONDS = STOP_GRACE_SECONDS + 3 * POLL_SECONDS + SLACK_SECONDS
PROMPT_BOUND_SECONDS = 2 * POLL_SECONDS + SLACK_SECONDS


def _stop_grace_environment() -> str:
    """The stop grace the launched supervisor reads.

    The mutation withholds the escalation by giving the supervisor a grace far
    larger than the test's window, so it waits on the worker's own exit.
    """
    if NEGATIVE_CONTROL:
        return "600"
    return str(STOP_GRACE_SECONDS)


def _repo_root() -> Path:
    return Path(dispatch_module.__file__).resolve().parents[2]


def _running(pid: int | None) -> bool:
    """Whether a pid is a live (non-zombie) process."""
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat[stat.rindex(")") + 2 :].split()[0] != "Z"


def _wait_until(predicate: Callable[[], Any], *, timeout: float, detail: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{detail} not met within {timeout:g}s")


def _kill_group(pid: int | None) -> None:
    if not pid:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid) or pid, signal.SIGKILL)


def _worker_pid(run_id: str) -> int | None:
    path = runs.run_dir(run_id) / dispatch_module.WORKER_RECORD_NAME
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _sender_reasons(run_directory: Path) -> list[str]:
    path = run_directory / routing.SENDER_RECORD_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [
        str(json.loads(line).get("reason") or "")
        for line in lines
        if line.strip()
    ]


def _launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stub: str
) -> dict[str, Any]:
    """A live pointer and a real detached supervisor supervising a stub worker."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv(dispatch_module.STOP_GRACE_ENV, _stop_grace_environment())
    monkeypatch.setenv("RECKON_FLEET_SPAWN", "")
    monkeypatch.setenv("PYTHONPATH", str(_repo_root()))

    run_id = "r-stop-supervisor"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manifest = tmp_path / "manifest.md"
    stream = directory / "stream.jsonl"
    prompt = directory / "prompt.txt"
    prompt.write_text("stub prompt\n", encoding="utf-8")

    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": "stop-supervisor-fixture",
        "repo": str(worktree),
        "worktree": str(worktree),
        "backend": "alpha",
        "launch": "cli",
        "dialect": "claude",
        "phase": "starting",
        "pid": None,
        "session": "coordinator-fixture",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-10-02T00:00:00Z",
        "created_at": "2026-10-02T00:00:00Z",
    }
    runs._write_json(directory / dispatch_module.ATTEMPT_RECORD_NAME, {"attempt": 1})
    runs._write_json(runs.pointer_path(run_id), pointer)

    spec = {
        "run_id": run_id,
        "run_directory": str(directory),
        "repo": str(worktree),
        "worktree": str(worktree),
        "fenced": False,
        "prompt_path": str(prompt),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-10-02T00:00:00Z",
        "environment": {},
        "plan": {
            "argv": [sys.executable, "-c", stub],
            "cwd": str(worktree),
            "environment": {
                "RECKON_STUB_SLEEP": str(WORKER_SLEEP_SECONDS),
            },
            "dialect": "claude",
            "backend": "alpha",
        },
    }
    spec_path = directory / "supervisor.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    supervisor_pid = dispatch_module._start_supervisor(spec_path, directory, run_id)

    def set_pid(record: dict[str, Any]) -> dict[str, Any]:
        record["pid"] = supervisor_pid
        record["pid_start_time"] = dispatch_module._process_start_time(supervisor_pid)
        return record

    runs._mutate_pointer(run_id, set_pid)
    return {
        "run_id": run_id,
        "directory": directory,
        "supervisor_pid": supervisor_pid,
        "manifest": manifest,
    }


def _stop(fixture: dict[str, Any]) -> float:
    """Wait for the stub to be up, stop the run, return the stop instant."""
    run_id = fixture["run_id"]
    _wait_until(
        lambda: _worker_pid(run_id) is not None,
        timeout=15,
        detail="the stub worker's record",
    )
    _wait_until(
        fixture["manifest"].is_file,
        timeout=15,
        detail="the stub worker's manifest",
    )
    started = time.monotonic()
    dispatch_module.terminate(run_id)
    return started


def test_the_supervisor_exits_within_the_bound_after_a_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _launch(tmp_path, monkeypatch, stub=STUB_IGNORES_STOP)
    supervisor_pid = fixture["supervisor_pid"]
    try:
        started = _stop(fixture)
        _wait_until(
            lambda: not _running(supervisor_pid),
            timeout=BOUND_SECONDS + 5.0,
            detail="the supervisor's exit",
        )
        elapsed = time.monotonic() - started
        assert elapsed <= BOUND_SECONDS, (
            f"the supervisor outlived the stop bound: {elapsed:.2f}s > "
            f"{BOUND_SECONDS:.2f}s"
        )
        assert not _running(_worker_pid(fixture["run_id"]))
        assert (fixture["directory"] / dispatch_module.EXIT_RECORD_NAME).is_file()
        # The end was attributable: the run's own directory names the kill.
        assert "worker-ignored-the-stop-grace-signal" in _sender_reasons(
            fixture["directory"]
        )
    finally:
        _kill_group(supervisor_pid)


def test_a_worker_that_ends_on_the_stop_ends_the_supervisor_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _launch(tmp_path, monkeypatch, stub=STUB_ENDS_ON_STOP)
    supervisor_pid = fixture["supervisor_pid"]
    try:
        started = _stop(fixture)
        _wait_until(
            lambda: not _running(supervisor_pid),
            timeout=PROMPT_BOUND_SECONDS + 5.0,
            detail="the supervisor's exit",
        )
        elapsed = time.monotonic() - started
        assert elapsed <= PROMPT_BOUND_SECONDS
        # The worker ended on the group stop, so no escalation was warranted.
        assert "worker-ignored-the-stop-grace-signal" not in _sender_reasons(
            fixture["directory"]
        )
    finally:
        _kill_group(supervisor_pid)
