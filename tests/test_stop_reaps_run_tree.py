"""``crew stop`` ends the run's whole tree, so a following discard succeeds.

A run's wrapper can ignore SIGTERM. ``crew stop`` signals the run's processes
and gave up once its settle window expired, so the recorded pid stayed alive
and the next ``crew discard`` refused with ``recorded pid ... is alive`` -- the
run could be neither stopped nor discarded. The stop therefore signals the tree
it resolved, waits a bounded grace, escalates to SIGKILL for whatever is still
alive, and reports the pids it ended and any that survived.

The cases below build a fake run tree: a parent wrapper and a child wrapper that
both ignore SIGTERM, plus a bystander in its own session that carries no
ownership mark. The stop must end the parent and the child, name their pids, and
leave the bystander alone, after which the discard must succeed.

The negative control sets ``RECKON_STOP_ESCALATION=off``, which drops the
escalation. The parent then survives the graceful signal, and the discard is
refused exactly as it was before the fix.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

dispatch_sessions = importlib.import_module("reckon.crew.dispatch_sessions")
promotion = importlib.import_module("reckon.crew.promotion")
runs = importlib.import_module("reckon.crew.runs")

# A wrapper and its child, both ignoring SIGTERM, so nothing in the tree ends
# without the escalation. The parent writes both pids for the case to read.
IGNORING_TREE = (
    "import os, signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "child = subprocess.Popen([sys.executable, '-c',\n"
    "  'import signal,time\\n'\n"
    "  'signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n'\n"
    "  'time.sleep(300)'])\n"
    "open(os.environ['PIDFILE'], 'w').write(f'{os.getpid()} {child.pid}')\n"
    "time.sleep(300)\n"
)

# The tree outlives any bounded wait in a case, so an exit can only be the
# stop's own signal.
BYSTANDER = "import time\ntime.sleep(300)\n"

RUN_ID = "r-stop-reaps-tree"


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


def _kill(pid: int | None) -> None:
    if not pid:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A live pointer over a wrapper tree that ignores SIGTERM, and a bystander."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    run_directory = runs.run_dir(RUN_ID)
    run_directory.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    pidfile = tmp_path / "pids"
    environment = dict(os.environ, PIDFILE=str(pidfile), RECKON_RUN_ID=RUN_ID)
    parent = subprocess.Popen(
        [sys.executable, "-c", IGNORING_TREE],
        env=environment,
        start_new_session=True,
    )
    _wait_until(pidfile.is_file, timeout=20, detail="the wrapper's pidfile")
    parent_pid, child_pid = (int(word) for word in pidfile.read_text().split())

    bystander = subprocess.Popen(
        [sys.executable, "-c", BYSTANDER], start_new_session=True
    )

    pointer: dict[str, Any] = {
        "run_id": RUN_ID,
        "project": "stop-reaps-tree-fixture",
        "repo": str(worktree),
        "worktree": str(worktree),
        "phase": "running",
        "pid": parent_pid,
        "pid_start_time": dispatch_sessions._process_start_time(parent_pid),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-10-22T00:00:00Z",
        "created_at": "2026-10-22T00:00:00Z",
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)
    return {
        "parent": parent_pid,
        "child": child_pid,
        "parent_process": parent,
        "bystander": bystander.pid,
        "run_directory": run_directory,
    }


def _cleanup(fixture: dict[str, Any]) -> None:
    for key in ("parent", "child", "bystander"):
        _kill(fixture[key])
    with contextlib.suppress(Exception):
        fixture["parent_process"].wait(timeout=5)


def test_a_stop_ends_the_whole_tree_and_the_discard_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    try:
        record = dispatch_sessions.terminate(RUN_ID)

        # The graceful signal was answered by nothing, so only the escalation
        # can have ended these. The behavioural claim comes first: a stop that
        # leaves the wrapper alive is the defect, whatever the record says.
        assert not _running(fixture["parent"]), record.get("detail")
        assert not _running(fixture["child"]), record.get("detail")
        assert record["killed_pids"], record.get("detail")

        # The tree is printed: both pids are resolved before the signal and
        # named in the record without indentation.
        assert set(record["target_pids"]) >= {fixture["parent"], fixture["child"]}
        pretty = json.dumps(record, indent=2)
        for pid in (fixture["parent"], fixture["child"]):
            assert str(pid) in pretty

        # Nothing of the run is left for the discard to refuse on.
        result = promotion.discard(RUN_ID)
        assert result["pointer_removed"] is True

        # The reach stayed inside the run's own tree.
        assert _running(fixture["bystander"])
    finally:
        _cleanup(fixture)


def test_the_escalation_off_leaves_the_wrapper_alive_and_the_discard_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared negative control: without the escalation the defect returns."""
    monkeypatch.setenv(dispatch_sessions.STOP_ESCALATION_ENV, "off")
    fixture = _fixture(tmp_path, monkeypatch)
    try:
        record = dispatch_sessions.terminate(RUN_ID)

        assert not record["killed_pids"]
        assert record["alive_after"]
        assert _running(fixture["parent"])

        with pytest.raises(promotion.CrewError) as refused:
            promotion.discard(RUN_ID)
        assert "is alive" in str(refused.value)
    finally:
        _cleanup(fixture)
