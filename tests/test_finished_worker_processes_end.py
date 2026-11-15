"""A terminal run releases its whole launch tree within one minute."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch, runs

ROOT = Path(__file__).resolve().parents[1]
BOUND_SECONDS = 60.0
STAND_IN = (
    r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['RECKON_PROBE_ROOT'])
mode = os.environ['RECKON_PROBE_MODE']
if mode != 'natural':
    child = subprocess.Popen([sys.executable, '-c', """
    + repr(r"""
import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen([sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)'],
    start_new_session=True)
Path(os.environ['RECKON_PROBE_ROOT'], 'descendants.json').write_text(
    __import__('json').dumps([os.getpid(), grandchild.pid]))
time.sleep(600)
""")
    + r"""], start_new_session=True)
    while not (root / 'descendants.json').exists():
        time.sleep(.02)
    if mode != 'root-exits-on-term':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
manifest = Path(os.environ['RECKON_MANIFEST'])
manifest.write_text('node: stub\nstatus: complete\ncommits: []\n')
(root / 'ready').write_text(str(os.getpid()))
if mode == 'natural':
    sys.exit(0)
if mode == 'rewriting':
    while True:
        manifest.write_text('node: stub\nstatus: complete\ncommits: []\n')
        time.sleep(.1)
time.sleep(600)
"""
)


def _running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat[stat.rindex(")") + 2 :].split()[0] != "Z"


def _wait(predicate, seconds: float, detail: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), detail


@pytest.fixture
def launch(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_FLEET_SPAWN", "")
    monkeypatch.delenv(dispatch_launch.TERMINAL_MANIFEST_GRACE_ENV, raising=False)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    processes = []
    owned = []

    def start(mode):
        run_id = "r-terminal-tree"
        directory = runs.run_dir(run_id)
        directory.mkdir(parents=True)
        manifest = tmp_path / "manifest.md"
        prompt = tmp_path / "prompt.txt"
        prompt.write_text("stand-in worker")
        stamp = runs._utc_now()
        pointer = {
            "run_id": run_id,
            "phase": "starting",
            "attempt": 1,
            "manifest_path": str(manifest),
            "attempt_started_at": stamp,
        }
        runs._write_json(runs.pointer_path(run_id), pointer)
        runs._write_json(
            directory / dispatch_launch.ATTEMPT_RECORD_NAME, {"attempt": 1}
        )
        spec = {
            "run_id": run_id,
            "run_directory": str(directory),
            "attempt": 1,
            "attempt_started_at": stamp,
            "worktree": str(tmp_path),
            "repo": str(tmp_path),
            "fenced": False,
            "environment": {},
            "prompt_path": str(prompt),
            "log_path": str(directory / "stream.jsonl"),
            "stderr_path": str(directory / "stderr.log"),
            "plan": {
                "argv": [sys.executable, "-c", STAND_IN],
                "cwd": str(tmp_path),
                "environment": {
                    "RECKON_PROBE_ROOT": str(tmp_path),
                    "RECKON_PROBE_MODE": mode,
                },
                "dialect": "claude",
                "backend": "stand-in",
            },
        }
        path = directory / "supervisor.json"
        path.write_text(json.dumps(spec))
        environment = dict(os.environ)
        environment.pop("RECKON_RUN_ID", None)
        with (directory / "supervisor.stderr.log").open("w") as stderr:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "reckon.crew.supervisor_main",
                    "--spec",
                    str(path),
                ],
                cwd=ROOT,
                env=environment,
                stderr=stderr,
                start_new_session=True,
            )
        processes.append(process)
        owned.append(process.pid)
        _wait((tmp_path / "ready").exists, 20, "worker did not start")
        owned.append(int((tmp_path / "ready").read_text()))
        descendants = tmp_path / "descendants.json"
        if descendants.exists():
            owned.extend(json.loads(descendants.read_text()))
        assert mode == "natural" or all(_running(pid) for pid in owned), owned
        return run_id, directory, process, owned

    yield start
    # These are enumerated fixture-owned pids only; peers and services are outside it.
    survivors = [pid for pid in owned if _running(pid)]
    print("fixture cleanup targets:", survivors)
    for pid in survivors:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    for process in processes:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
    for pid in survivors:
        if _running(pid):
            print("fixture cleanup SIGKILL:", pid)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    for process in processes:
        process.wait(timeout=5)


@pytest.mark.parametrize("mode", ["lingering", "root-exits-on-term", "rewriting"])
def test_terminal_pointer_ends_every_launched_process(launch, mode):
    run_id, directory, process, owned = launch(mode)
    runs._mutate_pointer(run_id, lambda record: {**record, "phase": "complete"})
    assert runs.read_pointer(run_id)["phase"] == "complete"
    started = time.monotonic()
    _wait(
        lambda: not any(_running(pid) for pid in owned),
        BOUND_SECONDS,
        f"terminal run still owns live processes after one minute: {owned}",
    )
    elapsed = time.monotonic() - started
    process.wait(timeout=5)
    receipt = json.loads((directory / dispatch_launch.EXIT_RECORD_NAME).read_text())
    assert receipt["recorded_by"] == "supervisor"
    assert set(owned[1:]).issubset(receipt["ended_pids"])
    assert receipt["forced_exit"]["reason"] == "terminal-run"
    assert receipt["forced_exit"]["phase"] == "complete"
    senders = [
        json.loads(line)
        for line in (directory / "senders.jsonl").read_text().splitlines()
    ]
    assert any(row.get("outcome") == "delivered" for row in senders)
    print(f"whole launch tree gone after {elapsed:.3f}s; receipt={receipt}")


def test_natural_worker_exit_has_no_forced_exit_record(launch):
    run_id, directory, process, owned = launch("natural")
    process.wait(timeout=20)
    assert not any(_running(pid) for pid in owned)
    receipt = json.loads((directory / dispatch_launch.EXIT_RECORD_NAME).read_text())
    assert receipt["exit_code"] == 0
    assert receipt["ended_pids"] == []
    assert "forced_exit" not in receipt
    assert not (directory / "senders.jsonl").exists()
    assert runs.read_pointer(run_id)["phase"] == "complete"
