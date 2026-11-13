"""One station refresh walks the process table once, however long its pass takes.

A station refresh classifies every live pointer in the project, and the
liveness read of a pointer whose recorded pids have gone falls through to the
environment scan: the reach that finds a reparented worker by the run mark its
launcher wrote into it. That scan is answered from a walk of the whole
``/proc`` table, and the sharing that made it cheap - one walk per one-second
beat - bounds a poller's pass rather than a sweep's. A refresh on a loaded host
runs for minutes, so nearly every pointer's liveness read fell in a beat of its
own and walked the table again.

The refresh now opens a region that holds one walk for the whole derivation.
Every liveness read inside it is answered from that one table, however many
pointers the refresh covers and however long it takes.

The fixture is a synthesised crew home of 30 live pointers whose pointer pid and
worker-record pid are both dead, so every one of them reaches the environment
scan. The process table is a fake that counts its own walks and is shown to be
seen: one pointer's mark is carried by a live child of this process (the run's
worker reparented away from the dead recorded pids) and one's mark names a
suspended child this case then reaps, so the table is observed to answer both
alive and dead before either count is believed. The beat clock steps past
``BEAT_SECONDS`` between reads, as a tick of minutes does.

The declared negative control is the base revision under the same fixture: with
no region, the refresh walks the table once per pointer.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, recovery_liveness, runs
from reckon.crew import process_signal as ps
from reckon.crew.process_signal import RUN_ID_ENV
from tests.helpers.git import git as _git

NEGATIVE_CONTROL_MUTATION = (
    "one station refresh over 30 live pointers walks the process table once"
)

PROJECT = "process-walk-fixture"
SESSION = "coordinator-fixture"
POINTERS = 30
# One pointer's liveness is read three times in a refresh: once for the row the
# sweep classifies, then once for the drain row and once inside the classifier
# that drain row builds. The beat a tick keeps steps once per pointer, so these
# three reads share it.
READS_PER_POINTER = 3
OBSERVED_AT = datetime(2026, 11, 13, 6, 30, tzinfo=UTC)
RECKON_HOME_ENV = "RECKON_HOME"
# Far longer than any single read in this file waits; every child is reaped.
_CHILD_SLEEP_SECONDS = 300.0


def _unique_run_id(index: int) -> str:
    """A run id no other pointer or process in this fixture carries."""
    return f"r-walk-{index:02d}-{os.urandom(5).hex()}"


def _dead_pid(low: int) -> int:
    """A pid no process holds, so a recorded pid reads as one that has gone."""
    for candidate in range(low, low + 1_000):
        if not Path(f"/proc/{candidate}").exists():
            return candidate
    raise AssertionError(f"no absent pid found from {low}")


def _child_env(run_id: str, home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop(RECKON_HOME_ENV, None)
    env[RUN_ID_ENV] = run_id
    env[RECKON_HOME_ENV] = str(home)
    return env


def _start_marked_child(run_id: str, home: Path) -> subprocess.Popen:
    """A live child carrying ``run_id`` and its crew home's config home."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({_CHILD_SLEEP_SECONDS})"],
        env=_child_env(run_id, home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(child: subprocess.Popen) -> None:
    """Stop a child and wait for it, escalating only if it will not end."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def _wait_for_mark(pid: int, run_id: str) -> None:
    """Wait until a child's own environment is readable and carries the mark.

    The positive control the fake table's live entry rests on: a child spawned
    but not yet executed carries no readable environment, so naming it would
    assert a liveness the kernel table does not yet show.
    """
    needle = f"{RUN_ID_ENV}={run_id}".encode()
    deadline = 10.0
    waited = 0.0
    while waited < deadline:
        try:
            if needle in (Path("/proc") / str(pid) / "environ").read_bytes():
                return
        except OSError:
            pass
        time.sleep(0.02)
        waited += 0.02
    raise AssertionError(f"pid {pid} never showed the mark for {run_id!r}")


def _write_worker_record(run_id: str, pid: int) -> None:
    """A worker record naming a pid no process holds.

    The record's presence is what leaves the environment scan as the last reach
    for the run: the supervisor's worker is gone, so only a process carrying
    the run mark can still say the run lives.
    """
    directory = Path(runs.run_dir(run_id))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "worker.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": pid,
                "pid_start_time": None,
                "launched_at": "2026-11-13T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def _pointer(run_id: str, dead_pid: int, manifest: Path, repo: Path) -> dict[str, Any]:
    """A live pointer whose recorded pids have both gone."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": "",
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "pid": dead_pid,
        "pid_start_time": None,
        "launcher_host": socket.gethostname(),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": [],
        },
    }


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A synthesised crew home holding 30 live pointers that reach the scan."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the process-walk fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    head = _git(root, "rev-parse", "HEAD")
    pointers: list[dict[str, Any]] = []
    for index in range(POINTERS):
        run_id = _unique_run_id(index)
        dead_pid = _dead_pid(2_000_000 + index * 10)
        manifest = config_home / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {run_id}\nstatus: working\ncommits: [{head}]\n", encoding="utf-8"
        )
        _write_worker_record(run_id, _dead_pid(2_500_000 + index * 10))
        pointer = _pointer(run_id, dead_pid, manifest, root)
        runs._write_json(runs.pointer_path(run_id), pointer)
        pointers.append(pointer)
    return {
        "home": config_home,
        "repo": root,
        "pointers": pointers,
        "crew_home": str(config_home / "crew"),
    }


def _clear_process_table_state() -> None:
    """Forget the beat's map and any pinned walk.

    The pinned walk is cleared through ``getattr`` so this fixture also runs
    against the base revision, where no region exists: the case then reaches
    its own assertions instead of failing in setup, so the count it records is
    the walk count and not an import error.
    """
    ps._MARKED_PROCESSES_BEAT.clear()
    pinned = getattr(ps, "_PINNED_PROCESSES", None)
    if pinned is not None:
        pinned[:] = [None]
    ps._ENVIRON_INDEX.clear()


@pytest.fixture(autouse=True)
def cold_process_table() -> Any:
    """Each case starts and ends with no beat map and nothing pinned."""
    _clear_process_table_state()
    yield
    _clear_process_table_state()


def _table_and_walks(
    monkeypatch: pytest.MonkeyPatch,
    fleet: dict[str, Any],
    entries: dict[str, list[int]],
) -> list[int]:
    """A fake table counting its walks, and the clock a tick of minutes keeps.

    The table maps each run's mark and this fixture's crew home to the pids
    that carry it. ``walks`` records one entry per walk, so a zero is the
    instrument and not the code, and the liveness assertions below show the
    same table answering alive and dead before either count is believed.
    """
    walks: list[int] = []
    table = {
        (run_id, fleet["crew_home"]): list(pids) for run_id, pids in entries.items()
    }

    def fake_walk() -> dict[tuple[str, str], list[int]]:
        walks.append(1)
        return table

    monkeypatch.setattr(ps, "_walk_marked_processes", fake_walk)
    # A tick of minutes spends its time on a pointer, not on the separate reads
    # one pointer costs, so the clock steps once per pointer: every third read
    # here is a new pointer's first, and the two after it belong to the same
    # pointer and the same beat.
    clock = {"now": 1_000.0, "reads": 0}

    def stepped() -> float:
        clock["reads"] += 1
        if clock["reads"] % READS_PER_POINTER == 1:
            clock["now"] += ps.BEAT_SECONDS * 2
        return clock["now"]

    monkeypatch.setattr(ps, "_beat_clock", stepped)
    return walks


def _liveness_one_pointer_at_a_time(fleet: dict[str, Any]) -> dict[str, Any]:
    """The answer a refresh of 30 separate beats gives each pointer.

    Each pointer's read starts from a cold beat, which is exactly what the base
    revision's refresh does: by the time the next pointer is classified the
    previous walk is more than a beat old, so nothing is shared.
    """
    answers: dict[str, Any] = {}
    for pointer in fleet["pointers"]:
        ps._MARKED_PROCESSES_BEAT.clear()
        answers[pointer["run_id"]] = recovery_liveness.local_liveness(pointer)
    return answers


def test_one_refresh_walks_the_table_once(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One station refresh over 30 live pointers walks the table once."""
    run_live = fleet["pointers"][0]["run_id"]
    run_exited = fleet["pointers"][1]["run_id"]
    live_child = _start_marked_child(run_live, fleet["home"])
    exited_child = _start_marked_child(run_exited, fleet["home"])
    try:
        _wait_for_mark(live_child.pid, run_live)
        _wait_for_mark(exited_child.pid, run_exited)
        _reap(exited_child)
        walks = _table_and_walks(
            monkeypatch,
            fleet,
            {run_live: [live_child.pid], run_exited: [exited_child.pid]},
        )
        state = obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)
        rows = {str(row.get("run_id") or ""): row for row in state.rows}
        # The scan was reached for every pointer and the table was seen: the
        # mark carried by the live child reads alive, the mark naming the exited
        # child reads dead, and the pointers with no mark read dead. Were the
        # table not consulted, both liveness answers would be the same.
        assert rows[run_live]["process_alive"] is True, (
            "the live child's mark did not read alive, so the environment scan"
            " never consulted the table and the count proves nothing"
        )
        assert rows[run_exited]["process_alive"] is not True, (
            "the exited child's mark read alive, so the table's answer is not"
            " the process table's"
        )
        answered = sum(1 for row in state.rows if row.get("process_alive") is not None)
        assert answered >= POINTERS, (
            f"only {answered} pointers reached a proven liveness read, so the"
            " refresh did not cover the fixture"
        )
        assert len(walks) == 1, (
            f"one refresh over {POINTERS} live pointers walked the process table"
            f" {len(walks)} times"
            f" ({len(walks) // POINTERS} per pointer)"
        )
    finally:
        _reap(exited_child)
        _reap(live_child)


def test_every_pointer_reads_as_it_does_pointer_by_pointer(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sharing one walk leaves each pointer's liveness as separate reads give it."""
    run_live = fleet["pointers"][0]["run_id"]
    run_exited = fleet["pointers"][1]["run_id"]
    live_child = _start_marked_child(run_live, fleet["home"])
    exited_child = _start_marked_child(run_exited, fleet["home"])
    try:
        _wait_for_mark(live_child.pid, run_live)
        _wait_for_mark(exited_child.pid, run_exited)
        _reap(exited_child)
        _table_and_walks(
            monkeypatch,
            fleet,
            {run_live: [live_child.pid], run_exited: [exited_child.pid]},
        )
        apart = _liveness_one_pointer_at_a_time(fleet)
        state = obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)
        shared = {
            str(row.get("run_id") or ""): (
                row.get("process_alive"),
                row.get("liveness_proven"),
            )
            for row in state.rows
        }
        assert shared[run_live] == (True, True), (
            f"the live child's pointer read {shared[run_live]} in the refresh, so"
            " the comparison rests on no proven-alive answer"
        )
        assert apart == shared, (
            "the refresh's liveness answers differ from the per-pointer reads:"
            f" refresh={shared} per-pointer={apart}"
        )
    finally:
        _reap(exited_child)
        _reap(live_child)
