"""Arming serializes launches without lending its lock to a long-lived process."""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import reckon.crew.dispatch_watch as dispatch_watch_module
from reckon.crew import runs

PROJECT = "arm-lock-sample"
SESSION = "lock-reader"
ROOT = Path(__file__).resolve().parents[1]
dispatch = importlib.import_module("reckon.crew.dispatch")
pytestmark = pytest.mark.arms_watch_producer

PRODUCER = r"""import os, signal, time
from pathlib import Path
from reckon.crew import runs
home = Path(os.environ["RECKON_HOME"])
with (home / "launches").open("a") as log:
    log.write(str(os.getpid()) + "\n")
with runs._project_watch_claim("arm-lock-sample", "30s") as (held, record):
    assert held
    if os.environ.get("STUBBORN_PRODUCER"):
        signal.signal(signal.SIGTERM, lambda *_: (home / "term-seen").touch())
        record.update(parent_pid=1, parent_start_time="gone")
        runs._write_watch_record(runs._WATCH_SEAT_HANDLES["arm-lock-sample"], record)
    (home / "ready").touch()
    while not (home / "stop").exists():
        time.sleep(.02)
"""

ARM = """import importlib, json, os
from pathlib import Path
m = importlib.import_module("reckon.crew.dispatch")
w = importlib.import_module("reckon.crew.dispatch_watch")
w._watch_executable = lambda: os.environ["WATCH_DRIVER"]
w.WATCHER_LOAD_BOUND_SECONDS = float(os.environ.get("ARM_BOUND", "15"))
gate = Path(os.environ["RECKON_HOME"], "go")
while not gate.exists():
    m.time.sleep(.01)
try:
    print(json.dumps(m._ensure_watch_producer("arm-lock-sample")), flush=True)
except m.CrewError as exc:
    print(str(exc), flush=True)
    raise SystemExit(3)
"""


def _wait(predicate, *, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("temporary process did not reach its expected state")


def _probe(lock):
    return subprocess.run(
        ["flock", "-n", str(lock), "true"], capture_output=True, timeout=5, check=False
    ).returncode


def _fds(pid, target):
    found = []
    for fd in Path(f"/proc/{pid}/fd").glob("*"):
        try:
            if os.readlink(fd) == str(target):
                found.append(Path(f"/proc/{pid}/fdinfo/{fd.name}").read_text())
        except OSError:
            continue
    return found


@pytest.fixture()
def processes(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv(dispatch.FLEET_SPAWN_ENV, "off")
    driver = tmp_path / "watch_driver.py"
    driver.write_text(f"#!{sys.executable}\n" + PRODUCER)
    driver.chmod(0o755)
    monkeypatch.setattr(dispatch_watch_module, "_watch_executable", lambda: str(driver))
    env = {k: v for k, v in os.environ.items() if not k.startswith("RECKON_")}
    env.update(
        RECKON_HOME=str(home),
        RECKON_WATCH_ARMING="on",
        RECKON_FLEET_SPAWN="off",
        PYTHONPATH=str(ROOT),
        WATCH_DRIVER=str(driver),
    )
    children = []
    supervisors = []
    original = dispatch._start_watch_producer

    def start(project):
        process = original(project)
        supervisors.append(process)
        return process

    monkeypatch.setattr(dispatch_watch_module, "_start_watch_producer", start)

    def spawn(source, *, extra=None, stdout=subprocess.PIPE):
        process = subprocess.Popen(
            [sys.executable, "-c", source],
            env={**env, **(extra or {})},
            stdout=stdout,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        children.append(process)
        return process

    yield home, spawn, supervisors
    # Only these children and the producers naming this temporary home are ours.
    (home / "stop").touch()
    for process in children:
        if process.poll() is None:
            print(f"terminating temporary-home child {process.pid}: {process.args}")
            process.terminate()
        process.communicate(timeout=10)
    for process in supervisors:
        process.wait(timeout=10)
    launches = home / "launches"
    if launches.exists():
        for pid in map(int, launches.read_text().splitlines()):
            _wait(
                lambda pid=pid: (
                    not Path(f"/proc/{pid}").exists()
                    or Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] == "Z"
                )
            )


def test_live_producer_and_follower_leave_arm_lock_free(processes):
    _home, spawn, supervisors = processes
    # A socket is a real streaming consumer without a process-table pipe walk.
    reader, writer = socket.socketpair()
    try:
        follower = spawn(
            "from functools import partial; from reckon import cli; "
            "cli._follow_watch_lines = partial(cli._follow_watch_lines, sweep=None); "
            "cli.main(['crew', 'follow', '--project', 'arm-lock-sample', "
            "'--session', 'lock-reader', '--no-color'])",
            stdout=writer,
        )
        _wait(lambda: runs.follower_state(PROJECT, SESSION)["registered"])
        state = dispatch._ensure_watch_producer(PROJECT, session=SESSION)
        assert state["watcher_live"] and state["session_attached"]
        lock = runs.watch_stream_path(PROJECT).with_suffix(".arm.lock")
        assert follower.poll() is None
        assert supervisors[0].poll() is None
        # Prove the instrument sees the producer's held seat before probing arm.
        assert _probe(runs.watch_lock_path(PROJECT)) == 1
        assert _probe(lock) == 0, "a live child retained the arming lock"
        for pid in (state["watcher"]["pid"], follower.pid, supervisors[0].pid):
            assert not _fds(pid, lock), f"pid {pid} inherited the arm descriptor"
    finally:
        reader.close()
        writer.close()


def test_concurrent_armings_launch_only_one_producer(processes):
    home, spawn, _ = processes
    first, second = spawn(ARM), spawn(ARM)
    (home / "go").touch()
    results = []
    for process in (first, second):
        stdout, stderr = process.communicate(timeout=25)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert all(state["watcher_live"] for state in results)
    assert results[0]["watcher"]["pid"] == results[1]["watcher"]["pid"]
    assert len((home / "launches").read_text().splitlines()) == 1
    assert _probe(runs.watch_stream_path(PROJECT).with_suffix(".arm.lock")) == 0


def test_stubborn_orphan_cannot_hold_arm_lock_past_bound(processes):
    home, spawn, _ = processes
    producer = spawn(PRODUCER, extra={"STUBBORN_PRODUCER": "yes"})
    _wait((home / "ready").exists)
    arm = spawn(ARM, extra={"ARM_BOUND": "2"})
    (home / "go").touch()
    try:
        stdout, stderr = arm.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        lock = runs.watch_stream_path(PROJECT).with_suffix(".arm.lock")
        pytest.fail(
            f"arming exceeded its bound; pid={arm.pid} fdinfo={_fds(arm.pid, lock)}"
        )
    assert arm.returncode == 3, (stdout, stderr)
    assert "release" in stdout and "2s" in stdout
    assert (home / "term-seen").exists(), "the stop request must reach the producer"
    assert producer.poll() is None, "the test producer must ignore the stop signal"
    assert _probe(runs.watch_stream_path(PROJECT).with_suffix(".arm.lock")) == 0


def test_contended_arming_refuses_within_bound(processes):
    import fcntl

    home, spawn, _ = processes
    lock = runs.watch_stream_path(PROJECT).with_suffix(".arm.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        arm = spawn(ARM, extra={"ARM_BOUND": "0.5"})
        (home / "go").touch()
        try:
            stdout, stderr = arm.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail("a contended arming waited without a bound")
        assert arm.returncode == 3, (stdout, stderr)
        assert str(lock) in stdout
        assert not (home / "launches").exists()


def test_delivery_checks_do_not_hold_the_arm_lock(processes, monkeypatch):
    dispatch._ensure_watch_producer(PROJECT)
    lock = runs.watch_stream_path(PROJECT).with_suffix(".arm.lock")
    original = dispatch.watch_state
    observed = []

    def state(project, *, session=None):
        if session is not None:
            observed.append(_probe(lock))
        return original(project, session=session)

    monkeypatch.setattr(dispatch_watch_module, "watch_state", state)
    dispatch._ensure_watch_producer(PROJECT, session=SESSION)
    assert observed == [0], "delivery resolution kept the arming mutex"
