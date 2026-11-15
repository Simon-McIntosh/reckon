"""One classify pass walks the process table once, however long its pass takes.

The station's tick classifies every live pointer in the store, and the liveness
read of a pointer whose recorded pids have gone falls through to the
environment scan. That scan is answered from a walk of the whole ``/proc``
table, and the sharing that made it cheap -- one walk per one-second beat --
bounds a poller's pass rather than a sweep's. A pass on a loaded host runs for
minutes, so nearly every pointer's liveness read fell in a beat of its own and
walked the table again.

The pass now opens a region that holds one walk for its whole duration. Every
liveness read inside it is answered from that one table, however many pointers
the pass covers and however long it takes.

The fixture is a synthesised crew home of 40 live pointers whose pointer pid
and worker-record pid are both dead, so every one of them reaches the
environment scan. The process table is a fake that counts its own walks, and it
is shown to answer before its count is believed: one further pointer's mark is
carried by a live child of this process, so the table is observed to report
that run alive while a pointer with no mark reads dead. The beat clock steps
past ``BEAT_SECONDS`` between reads, as a pass of minutes does.

The declared negative control is the base revision under the same fixture: with
no region, the pass walks the table once per pointer, and the recording
stand-in counts 40.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import process_signal as ps
from reckon.crew import recovery_liveness, runs, station_store
from reckon.crew.process_signal import RUN_ID_ENV
from reckon.crew.run_paths import crew_home
from tests.helpers.git import git as _git

NEGATIVE_CONTROL_MUTATION = (
    "at the base revision the recording stand-in counts 40 walks over 40 pointers"
)

PROJECT = "classify-walk-fixture"
SESSION = "coordinator-fixture"
POINTERS = 40
STALL_SECONDS = 900
RECKON_HOME_ENV = "RECKON_HOME"
# Far longer than any read in this file waits; every child is reaped.
_CHILD_SLEEP_SECONDS = 300.0


def _unique_run_id(index: int, label: str) -> str:
    """A run id no other pointer or process in this fixture carries."""
    return f"r-classify-{label}-{index:02d}-{os.urandom(5).hex()}"


def _dead_pid(low: int) -> int:
    """A pid no process holds, so a recorded pid reads as one that has gone."""
    for candidate in range(low, low + 1_000):
        if not Path(f"/proc/{candidate}").exists():
            return candidate
    raise AssertionError(f"no absent pid found from {low}")


def _marked_child_env(run_id: str, home: Path) -> dict[str, str]:
    """The environment a launcher writes into a run's processes."""
    env = dict(os.environ)
    env.pop(RECKON_HOME_ENV, None)
    env[RUN_ID_ENV] = run_id
    env[RECKON_HOME_ENV] = str(home)
    return env


def _start_marked_child(run_id: str, home: Path) -> subprocess.Popen:
    """A live child carrying ``run_id`` with this fixture's crew home."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({_CHILD_SLEEP_SECONDS})"],
        env=_marked_child_env(run_id, home),
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

    The record's presence is what leaves the environment scan as the run's last
    reach: the supervisor's worker is gone, so only a process carrying the run
    mark can still say the run lives.
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


def _pointer(
    run_id: str, dead_pid: int, manifest: Path, repo: Path, moment: float
) -> dict[str, Any]:
    """A live pointer whose recorded pids have both gone."""
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(moment) - 60))
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": "",
        "process_alive": False,
        "launch": "in-harness",
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": [],
        },
        "role": "implement",
        "phase": "working",
        "manifest_path": str(manifest),
        "pid": dead_pid,
        "pid_start_time": None,
        "launcher_host": socket.gethostname(),
        "created_at": started,
    }


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A synthesised crew home holding 40 live pointers that reach the scan."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv(RECKON_HOME_ENV, str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    plan = root / "docs" / "plans" / "fixture-plan.html"
    plan.write_text(
        f'<meta name="docs-project" content="{PROJECT}">\n<h2 id="s1">fixture</h2>\n',
        encoding="utf-8",
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", "docs/plans/fixture-plan.html"),
        ("commit", "-q", "-m", "test: seed the classify-walk fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    moment = time.time()
    pointers: list[dict[str, Any]] = []
    for index in range(POINTERS):
        run_id = _unique_run_id(index, "dead")
        manifest = crew_home() / "runs" / run_id / "manifest.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {run_id}\nstatus: in-progress\ncheckpoint: fixture\n",
            encoding="utf-8",
        )
        _write_worker_record(run_id, _dead_pid(2_500_000 + index * 10))
        pointer = _pointer(
            run_id, _dead_pid(2_000_000 + index * 10), manifest, root, moment
        )
        runs._write_json(runs.pointer_path(run_id), pointer)
        pointers.append(pointer)
    return {
        "home": str(crew_home()),
        "crew_home": str(crew_home()),
        "config_home": str(config_home),
        "repo": root,
        "pointers": pointers,
        "moment": moment,
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
    if pinned is not None and hasattr(pinned, "set"):
        pinned.set(None)
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
    """A fake table counting its walks, and a clock that steps on every read.

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
    # A pass over a loaded host spends its time on a pointer rather than on the
    # separate reads one pointer costs, so every table read steps the clock
    # past a beat: no two reads share the beat the base revision keys on.
    clock = {"now": 1_000.0}

    def stepped() -> float:
        clock["now"] += ps.BEAT_SECONDS * 2
        return clock["now"]

    monkeypatch.setattr(ps, "_beat_clock", stepped)
    return walks


def _classify(fleet: dict[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    """One classify pass over the fixture, on a store of its own."""
    store = station_store.StationStore(root)
    store.tick(home=fleet["home"])
    return store.classify_tick(now_seconds=fleet["moment"], stall_seconds=STALL_SECONDS)


def test_one_pass_walks_the_table_once(
    fleet: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One classify pass over 40 live pointers walks the table once."""
    live_run = fleet["pointers"][0]["run_id"]
    live_child = _start_marked_child(live_run, Path(fleet["config_home"]))
    try:
        _wait_for_mark(live_child.pid, live_run)
        walks = _table_and_walks(monkeypatch, fleet, {live_run: [live_child.pid]})
        _clear_process_table_state()
        head = _classify(fleet, tmp_path / "station-head")
        head_walks = len(walks)
        # The table was seen before the count is believed: the live child's mark
        # reads alive and a pointer with no mark reads dead. Were the
        # environment scan not consulting the table, both would agree.
        live_reading = recovery_liveness.local_liveness(fleet["pointers"][0])
        dead_reading = recovery_liveness.local_liveness(fleet["pointers"][1])
        assert live_reading[:2] == (True, True), (
            "the live child's mark did not read alive, so the walk proves"
            f" nothing: {live_reading}"
        )
        assert dead_reading[0] is False, (
            "a pointer with no mark did not read dead, so the base arm's count"
            f" would be a walk count and not a liveness reading: {dead_reading}"
        )
        assert head_walks == 1, (
            f"one classify pass over {POINTERS} live pointers walked the"
            f" process table {head_walks} times"
        )

        # The verdicts the base revision reaches, measured by neutralising the
        # region: without it the pass is the base revision's, since the
        # beat-keyed map steps past a beat between every pointer's read.
        with monkeypatch.context() as patch:
            base_walks = _table_and_walks(patch, fleet, {live_run: [live_child.pid]})
            patch.setattr(ps, "process_table_walk_once", contextlib.nullcontext)
            _clear_process_table_state()
            base = _classify(fleet, tmp_path / "station-base")
    finally:
        _reap(live_child)
    assert base == head, (
        f"the pass's verdicts differ from the base revision's: head={head} base={base}"
    )
    # The live carrier is the one row the two arms could disagree about if the
    # region hid a process the beat's map had not seen: it must read alive in
    # both.
    assert head[live_run].get("process_alive") is True, (
        f"the live-carrier pointer did not read alive in the head arm: {head[live_run]}"
    )
    assert base[live_run].get("process_alive") is True, (
        f"the live-carrier pointer did not read alive in the base arm: {base[live_run]}"
    )
    assert len(base_walks) == POINTERS, (
        f"the base revision walked the pass's table {len(base_walks)} times over"
        f" {POINTERS} pointers, so this fixture no longer reproduces the defect"
    )
