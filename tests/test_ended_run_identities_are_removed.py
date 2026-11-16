"""The crew's own maintenance pass removes the identities of ended executions.

An ``exec-<pid>.identity`` file is published by a dispatch while it holds a
node claim and retired with that claim, so one outlives its execution only when
the process dies holding it. Nothing removed those on a schedule: the sweep
existed behind ``gc --sweep-exec-identities`` and the installed retention timer
never passed the flag, so the files accumulated in the shared crew home at one
per dispatch process. The maintenance pass now makes the same judgment every
hour, and it keeps every identity a live execution is still writing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import gc

ROOT = Path(__file__).resolve().parents[1]

# A live holder keeps its identity file open for the test's whole duration. It
# takes a real node claim, because that is what publishes an identity file.
_HOLD_CLAIM = """\
import os, sys
from reckon.crew import dispatch_claims as claims
held = claims._claim_node_dispatch(
    project='identity-fixture',
    worktree_identity='identity-worktree',
    node_id=sys.argv[1],
    run_id=sys.argv[1],
    session='identity-session',
)
print(os.getpid(), flush=True)
sys.stdin.read()
held.release()
"""


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, and the claims directory beneath it."""
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    claims = home / "crew" / "claims"
    claims.mkdir(parents=True)
    return home


def _identities(home: Path) -> list[Path]:
    return sorted((home / "crew" / "claims").glob("exec-*.identity"))


def _dead_pids(count: int) -> list[int]:
    """Pids the kernel has already freed, one exited process each."""
    pids: list[int] = []
    for _ in range(count):
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        pids.append(finished.pid)
    return pids


def _plant_ended(home: Path, count: int) -> list[Path]:
    directory = home / "crew" / "claims"
    return [
        _write_identity(directory / f"exec-{pid}.identity") for pid in _dead_pids(count)
    ]


def _write_identity(path: Path) -> Path:
    path.write_text(f"{os.uname().nodename}\ndan-ended-execution\n", encoding="utf-8")
    return path


def _hold_live_holders(count: int) -> list[subprocess.Popen[str]]:
    """Start ``count`` live holders, each publishing its own identity file."""
    holders: list[subprocess.Popen[str]] = []
    for index in range(count):
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLD_CLAIM, f"live-holder-{index}"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        line = holder.stdout.readline() if holder.stdout else ""
        assert line.strip().isdigit(), line
        holders.append(holder)
    return holders


def test_one_pass_removes_every_ended_identity_and_keeps_every_live_one(
    crew_home: Path,
) -> None:
    """The measure: ended runs' identities go, live runs' identities stay.

    Both populations are present before the pass, so the assertion reads a
    removal and a preservation in the same pass rather than an absence in a
    directory that held nothing to begin with.
    """
    ended = _plant_ended(crew_home, 3)
    holders = _hold_live_holders(2)
    directory = crew_home / "crew" / "claims"
    live = [directory / f"exec-{holder.pid}.identity" for holder in holders]
    try:
        assert all(path.is_file() for path in live), live
        assert sorted(_identities(crew_home)) == sorted(ended + live)

        report = gc.retention_sweep(apply=True)

        assert report["exec_identities"]["removed"] == len(ended), report
        assert report["exec_identities"]["kept"] == len(holders), report
        assert [path for path in ended if path.exists()] == []
        assert [path for path in live if path.is_file()] == live
    finally:
        for holder in holders:
            if holder.stdin is not None:
                holder.stdin.close()
            holder.wait(timeout=60)


def test_the_pass_removes_nothing_while_a_live_identity_is_being_written(
    crew_home: Path,
) -> None:
    """A live execution rewriting its file is never mistaken for a ended one.

    The writer replaces the file atomically in a loop for the pass's whole
    duration, so the sweep judges a name whose writer is active at every
    instant. One ended execution is planted in the same directory in the same
    pass, which is what separates a pass that kept the live file from a pass
    that removed nothing at all.
    """
    live = crew_home / "crew" / "claims" / f"exec-{os.getpid()}.identity"
    _write_identity(live)
    ended = _plant_ended(crew_home, 1)[0]
    stop = threading.Event()
    written = 0

    def rewrite() -> None:
        nonlocal written
        while not stop.is_set():
            temporary = live.with_name(f"{live.name}.writing")
            _write_identity(temporary)
            os.replace(temporary, live)
            written += 1

    writer = threading.Thread(target=rewrite)
    writer.start()
    try:
        deadline = time.monotonic() + 5.0
        while written < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        report = gc.retention_sweep(apply=True)
    finally:
        stop.set()
        writer.join()

    assert written >= 2, written
    assert report["exec_identities"]["removed"] == 1, report
    assert not ended.exists()
    assert live.is_file()
