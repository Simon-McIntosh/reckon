"""A relaunch of an existing run id is refused while another is in flight.

The window this closes: a launch writes ``attempt.json`` for its own attempt and
its supervisor writes ``worker.json`` a moment later. Between the two, every
liveness read sees the older, ended attempt, so a second door prepares the same
attempt and two workers run in one worktree.

The reservation is keyed on the newest attempt receipt in the run's own
directory plus one — never the pointer, the ledger or the node's newest
committed run — taken before the worktree is touched and freed by its own
launch's end. Two doors racing for one attempt therefore hold one reservation
between them, and a launch that ended or was killed never leaves the run
unlaunchable.

Every case works against a temporary crew home and repository, and asserts
afterwards that the operator's own crew home is untouched.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import dispatch_launch as dispatch_launch_module
from reckon.crew import process_liveness
from reckon.crew import routing as routing_module
from reckon.crew import runs as runs_module
from reckon.crew.dispatch_sessions import record_resumption
from reckon.crew.node import CrewError
from reckon.crew.recovery_liveness import _reading_host
from reckon.crew.resumption import LAUNCH_WINDOW_SECONDS

RUN_ID = "r-concurrent-launch"
NODE_ID = "concurrent-node"
SESSION = "dispatch-session"
JOB_ID = "1291586"
PLACEMENT = {"partition": "betelgeuse", "nodes": 1}

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

HARNESS_CONFIG = {
    **CONFIG,
    "backends": {"alpha": {**CONFIG["backends"]["alpha"], "launch": "in-harness"}},
}

dispatch_module = importlib.import_module("reckon.crew.dispatch")

IN_FLIGHT = "in flight"


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def real_home_untouched() -> None:
    """Every case leaves the operator's own crew home exactly as it found it."""
    real = runs_module.crew_home()
    before = _listing(real)
    yield
    assert _listing(real) == before, "the case wrote into the real crew home"


def _listing(directory: Path) -> list[str] | None:
    try:
        return sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return None


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding one run that has launched before."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: None)
    directory = runs_module.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs_module.live_dir().mkdir(parents=True, exist_ok=True)
    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer())
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")
    return directory


def _pointer(**extra: object) -> dict:
    return {
        "run_id": RUN_ID,
        "project": "sample",
        "phase": "starting",
        "backend": "probe",
        "launch": "cli",
        "launcher_host": _reading_host(),
        "attempt": 2,
        "attempt_kind": "resume",
        "session": "session-1",
        "worktree": str(runs_module.run_dir(RUN_ID)),
        "manifest_path": str(runs_module.run_dir(RUN_ID) / "manifest.md"),
        "log_path": str(runs_module.run_dir(RUN_ID) / "stream.jsonl"),
        "stderr_path": str(runs_module.run_dir(RUN_ID) / "stderr.log"),
        "placement": dict(PLACEMENT),
        "job_id": JOB_ID,
        "node": {"id": NODE_ID, "plan": "fixture", "section": "guard"},
        **extra,
    }


def _retired_exit(directory: Path, attempt: int) -> None:
    runs_module._write_json(
        directory / f"attempt-{attempt}-exit.json",
        {"run_id": RUN_ID, "attempt": attempt, "exit_code": 0, "detail": "ended"},
    )


def _retired_worker(directory: Path, attempt: int, pid: int) -> None:
    runs_module._write_json(
        directory / f"attempt-{attempt}-worker.json",
        {"run_id": RUN_ID, "attempt": attempt, "pid": pid, "argv": []},
    )


def _reservation(
    directory: Path,
    attempt: int,
    *,
    pid: int,
    supervisor_pid: int | None = None,
    age: float = 0.0,
) -> Path:
    payload = {
        "run_id": RUN_ID,
        "attempt": attempt,
        "kind": "dispatch",
        "pid": pid,
        "pid_start_time": runs_module._process_start_time(pid),
        "at": "2026-11-08T20:00:00Z",
    }
    if supervisor_pid is not None:
        payload["supervisor_pid"] = supervisor_pid
        payload["supervisor_pid_start_time"] = runs_module._process_start_time(
            supervisor_pid
        )
    path = directory / f"attempt-{attempt}.reservation"
    runs_module._write_json(path, payload)
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


def _live_child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _dead_pid() -> int:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    return gone.pid


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)


def _refused(call) -> CrewError:
    with pytest.raises(CrewError) as refused:
        call()
    return refused.value


# ── the key ─────────────────────────────────────────────────────────────────


def test_the_key_is_the_newest_receipt_plus_one(home: Path) -> None:
    _retired_exit(home, 1)
    _retired_worker(home, 2, _dead_pid())
    assert dispatch_launch_module.launch_reservation_key(home) == 3


def test_the_key_ignores_the_pointer(home: Path) -> None:
    _retired_exit(home, 1)
    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer(attempt=9))
    assert dispatch_launch_module.launch_reservation_key(home) == 2


def test_a_fresh_run_keys_at_one(tmp_path: Path) -> None:
    assert dispatch_launch_module.launch_reservation_key(tmp_path / "absent") == 1


def test_the_marker_names_the_attempt_a_first_launch_keeps(home: Path) -> None:
    """With no receipt on disk, a launch keeps the attempt it was dispatched with.

    A paired shadow arm derives its attempt from the node's own committed runs,
    so its first launch is already dispatched with an attempt above one. The
    marker is the only record of that attempt, and the key must read it rather
    than restart at one.
    """
    runs_module.pointer_path(RUN_ID).unlink(missing_ok=True)
    runs_module._write_json(
        home / "attempt.json",
        {"run_id": RUN_ID, "attempt": 2, "attempt_kind": "shadow"},
    )
    assert dispatch_launch_module.launch_reservation_key(home) == 2
    taken = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="dispatch")
    assert taken.attempt == 2
    dispatch_launch_module.release_launch_reservation(taken)


def test_the_pointer_names_the_attempt_a_compatibility_launch_keeps(
    home: Path,
) -> None:
    """A launch carrying no trace but the pointer keys on the pointer's attempt.

    The compatibility seam starts a ``resume-*`` launch without a reservation of
    its own, so the pointer is the only on-disk reading. Keying at one there
    would launch attempt one over a run whose pointer already names attempt
    three, duplicating a number the run has used.
    """
    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer(attempt=3))
    assert dispatch_launch_module.launch_reservation_key(home) == 3


def test_an_attempt_never_retired_into_receipts_still_takes_the_next_number(
    home: Path,
) -> None:
    """A first attempt that left only canonical records has still taken one.

    The run ends without a later launch archiving ``worker.json`` into an
    ``attempt-1-worker.json`` receipt, so a key read from the retired names
    alone would hand the relaunch the number that attempt already used and the
    reserved and launched attempts would disagree.
    """
    runs_module._write_json(
        home / "worker.json", {"run_id": RUN_ID, "attempt": 1, "pid": _dead_pid()}
    )
    assert dispatch_launch_module.launch_reservation_key(home) == 2
    reservation = dispatch_launch_module.reserve_launch_attempt(
        home, RUN_ID, kind="dispatch"
    )
    assert reservation.attempt == 2
    dispatch_launch_module.release_launch_reservation(reservation)


def test_a_record_written_before_the_attempt_field_is_the_first_attempt(
    home: Path,
) -> None:
    runs_module._write_json(
        home / "worker.json", {"run_id": RUN_ID, "pid": _dead_pid()}
    )
    runs_module._write_json(
        home / "exit.json", {"run_id": RUN_ID, "worker_pid": _dead_pid()}
    )
    assert dispatch_launch_module.launch_reservation_key(home) == 2


# ── one holder, one key ─────────────────────────────────────────────────────


def test_two_doors_for_one_attempt_leave_one_reservation(home: Path) -> None:
    _retired_exit(home, 1)
    first = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="dispatch")
    assert first.attempt == 2
    refused = _refused(
        lambda: dispatch_launch_module.reserve_launch_attempt(
            home, RUN_ID, kind="resume"
        )
    )
    assert "attempt 2" in str(refused) and IN_FLIGHT in str(refused)
    held = sorted(path.name for path in home.glob("attempt-*.reservation"))
    assert held == ["attempt-2.reservation"], held


def test_a_resume_and_a_dispatch_take_the_same_key(home: Path) -> None:
    _retired_exit(home, 1)
    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer(attempt=2))
    reserved = dispatch_launch_module.reserve_launch_attempt(
        home, RUN_ID, kind="resume"
    )
    refused = _refused(
        lambda: dispatch_launch_module.reserve_launch_attempt(
            home, RUN_ID, kind="dispatch"
        )
    )
    assert reserved.attempt == 2
    assert "attempt 2" in str(refused)


def test_a_resume_and_a_dispatch_racing_on_a_marker_only_run_share_one_key(
    home: Path,
) -> None:
    """A run with no receipt at all: both doors still compute one attempt."""
    runs_module.pointer_path(RUN_ID).unlink(missing_ok=True)
    runs_module._write_json(home / "attempt.json", {"run_id": RUN_ID, "attempt": 1})
    reserved = dispatch_launch_module.reserve_launch_attempt(
        home, RUN_ID, kind="resume"
    )
    refused = _refused(
        lambda: dispatch_launch_module.reserve_launch_attempt(
            home, RUN_ID, kind="dispatch"
        )
    )
    assert reserved.attempt == 1
    assert "attempt 1" in str(refused) and IN_FLIGHT in str(refused)


def test_a_marker_only_run_resumes_attempt_one_and_its_exit_frees_it(
    home: Path,
) -> None:
    """The only trace is the marker, so the resume launches the attempt it names."""
    runs_module.pointer_path(RUN_ID).unlink(missing_ok=True)
    runs_module._write_json(home / "attempt.json", {"run_id": RUN_ID, "attempt": 1})
    taken = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="resume")
    assert taken.attempt == 1
    assert dispatch_launch_module.held_launch_reservation(home) is not None
    _retired_exit(home, 1)
    assert dispatch_launch_module.held_launch_reservation(home) is None, (
        "the attempt's own exit receipt did not free its reservation"
    )


# ── freed by liveness, never by age or the pointer ──────────────────────────


def test_an_old_reservation_whose_launcher_lives_still_refuses(home: Path) -> None:
    _retired_exit(home, 1)
    _reservation(home, 2, pid=os.getpid(), age=LAUNCH_WINDOW_SECONDS * 10)
    refused = _refused(
        lambda: dispatch_launch_module.reserve_launch_attempt(
            home, RUN_ID, kind="dispatch"
        )
    )
    assert "attempt 2" in str(refused)


def test_a_pointer_at_the_reserved_attempt_does_not_free_it(home: Path) -> None:
    _retired_exit(home, 1)
    _reservation(home, 2, pid=os.getpid())
    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer(attempt=2))
    refused = _refused(
        lambda: dispatch_launch_module.reserve_launch_attempt(
            home, RUN_ID, kind="dispatch"
        )
    )
    assert "attempt 2" in str(refused)


def test_a_dead_launcher_with_the_pointer_at_the_attempt_admits(home: Path) -> None:
    _retired_exit(home, 1)
    _reservation(home, 2, pid=_dead_pid())
    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer(attempt=2))
    taken = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="dispatch")
    assert taken.attempt == 2


def test_a_dead_launcher_with_a_live_supervisor_refuses(home: Path) -> None:
    _retired_exit(home, 1)
    supervisor = _live_child()
    try:
        _reservation(home, 2, pid=_dead_pid(), supervisor_pid=supervisor.pid)
        refused = _refused(
            lambda: dispatch_launch_module.reserve_launch_attempt(
                home, RUN_ID, kind="dispatch"
            )
        )
        assert "attempt 2" in str(refused)
        assert str(supervisor.pid) in str(refused)
    finally:
        _stop(supervisor)


def test_a_recorded_supervisor_that_exited_frees_the_reservation(home: Path) -> None:
    _retired_exit(home, 1)
    _reservation(home, 2, pid=os.getpid(), supervisor_pid=_dead_pid())
    taken = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="dispatch")
    assert taken.attempt == 2


def test_a_refused_launch_releases_its_reservation(home: Path) -> None:
    _retired_exit(home, 1)
    taken = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="dispatch")
    dispatch_launch_module.release_launch_reservation(taken, only_when_unspawned=True)
    again = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="dispatch")
    assert again.attempt == taken.attempt


def test_a_spawned_supervisor_keeps_the_reservation(home: Path) -> None:
    _retired_exit(home, 1)
    taken = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="dispatch")
    supervisor = _live_child()
    try:
        dispatch_launch_module.record_reservation_supervisor(taken, supervisor.pid)
        dispatch_launch_module.release_launch_reservation(
            taken, only_when_unspawned=True
        )
        assert taken.path.exists(), "a spawned launch released its reservation"
    finally:
        _stop(supervisor)


def test_an_attempts_exit_receipt_frees_the_admitted_runs_door(home: Path) -> None:
    """A hold naming a live process is freed by the attempt's own ending.

    The admit service outlives every run it launches, so a reservation left
    naming it would keep the run unlaunchable for the service's whole life. The
    attempt's exit receipt is written to shared storage by the launch itself and
    frees the hold whatever process the reservation names.
    """
    _reservation(home, 2, pid=os.getpid())
    assert dispatch_launch_module.held_launch_reservation(home) is not None
    _retired_exit(home, 2)
    assert dispatch_launch_module.held_launch_reservation(home) is None
    taken = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="dispatch")
    assert taken.attempt == 3
    dispatch_launch_module.release_launch_reservation(taken)


def test_an_attempts_exit_receipt_frees_the_in_harness_door(home: Path) -> None:
    """The reader a non-cli launch takes answers on the same facts."""
    _reservation(home, 2, pid=os.getpid())
    assert "in flight" in str(
        _refused(lambda: dispatch_launch_module.refuse_held_launch(RUN_ID, home))
    )
    _retired_exit(home, 2)
    dispatch_launch_module.refuse_held_launch(RUN_ID, home)


def test_a_run_less_exit_receipt_frees_the_admitted_runs_door(home: Path) -> None:
    """A receipt carrying no run id stands where one goes; it frees nothing."""
    _reservation(home, 2, pid=os.getpid())
    runs_module._write_json(home / "attempt-2-exit.json", {"exit_code": 0})
    assert dispatch_launch_module.held_launch_reservation(home) is not None
    refused = _refused(
        lambda: dispatch_launch_module.reserve_launch_attempt(
            home, RUN_ID, kind="dispatch"
        )
    )
    assert "attempt 2" in str(refused)


def test_a_run_less_exit_receipt_frees_the_in_harness_door(home: Path) -> None:
    _reservation(home, 2, pid=os.getpid())
    runs_module._write_json(home / "attempt-2-exit.json", {"exit_code": 0})
    assert "in flight" in str(
        _refused(lambda: dispatch_launch_module.refuse_held_launch(RUN_ID, home))
    )


def test_a_truncated_exit_receipt_frees_neither_door(home: Path) -> None:
    _reservation(home, 2, pid=os.getpid())
    (home / "attempt-2-exit.json").write_text(
        f'{{"run_id": "{RUN_ID}", "exit_c', encoding="utf-8"
    )
    assert dispatch_launch_module.held_launch_reservation(home) is not None
    assert "in flight" in str(
        _refused(
            lambda: dispatch_launch_module.reserve_launch_attempt(
                home, RUN_ID, kind="dispatch"
            )
        )
    )
    assert "in flight" in str(
        _refused(lambda: dispatch_launch_module.refuse_held_launch(RUN_ID, home))
    )


def test_the_newest_receipt_decides(home: Path) -> None:
    _retired_exit(home, 1)
    live = _live_child()
    try:
        _retired_worker(home, 2, live.pid)
        record = json.loads(runs_module.pointer_path(RUN_ID).read_text())
        assert dispatch_launch_module._retired_attempt_ended(record) is False
    finally:
        _stop(live)


# ── the spawn guard ─────────────────────────────────────────────────────────


def _spawn_spec(home: Path, attempt: int) -> dict:
    return {
        "run_id": RUN_ID,
        "run_directory": str(home),
        "attempt": attempt,
        "attempt_kind": "resume",
        "attempt_started_at": "2026-11-08T20:00:00Z",
        "prompt_path": str(home / "prompt.txt"),
        "log_path": str(home / "resume-1.jsonl"),
        "stderr_path": str(home / "resume-1.stderr.log"),
        "plan": {
            "argv": [sys.executable, "-c", "pass"],
            "cwd": str(home),
            "environment": {},
            "dialect": "",
        },
    }


def _ended_previous_attempt(home: Path) -> None:
    runs_module._write_json(
        home / "attempt.json",
        {"run_id": RUN_ID, "attempt": 2, "attempt_kind": "resume"},
    )
    _retired_worker(home, 1, _dead_pid())
    _retired_exit(home, 1)
    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer(pid=_dead_pid()))


def test_the_spawn_guard_admits_its_own_attempt_and_refuses_another(
    home: Path,
) -> None:
    _ended_previous_attempt(home)
    taken = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="resume")
    assert taken.attempt == 2
    pid = dispatch_launch_module._supervisor_spawn_worker(_spawn_spec(home, 2))
    try:
        assert pid > 0, "the guard refused the attempt its own reservation holds"
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)

    # The spawn guard admitted its own attempt; another attempt of the same
    # run is refused by the reservation it does not hold. A second door is
    # refused too, even though the run's receipts have moved past the held
    # attempt.
    refused = _refused(
        lambda: dispatch_launch_module.reserve_launch_attempt(
            home, RUN_ID, kind="dispatch"
        )
    )
    assert "attempt 2" in str(refused) and IN_FLIGHT in str(refused)
    # A reservation at a later attempt than the spec's guards the spawn. One at
    # an earlier attempt does not: that launch has been superseded, so refusing
    # on it would leave a resumed run unlaunchable for good.
    _reservation(home, 3, pid=os.getpid())
    refused_guard = _refused(
        lambda: dispatch_launch_module._supervisor_spawn_worker(_spawn_spec(home, 2))
    )
    assert "attempt 3" in str(refused_guard) and IN_FLIGHT in str(refused_guard)


def test_an_earlier_attempts_reservation_does_not_guard_a_later_launch(
    home: Path,
) -> None:
    """A superseded attempt's reservation cannot refuse the launch after it."""
    _ended_previous_attempt(home)
    # Attempt 3's launch is still alive, but the spec launches attempt 4: the
    # earlier reservation belongs to a launch that attempt 4 supersedes.
    _reservation(home, 3, pid=os.getpid())
    pid = dispatch_launch_module._supervisor_spawn_worker(_spawn_spec(home, 4))
    try:
        # The guard admitted the launch: the worker receipt it writes names the
        # process it started, which a refused launch never reaches.
        receipts = sorted(home.glob("attempt-*-worker.json"))
        assert receipts, "the guard admitted no worker"
        worker = json.loads(receipts[-1].read_text(encoding="utf-8"))
        assert worker["pid"] == pid, (
            "the guard refused on a reservation older than the launch"
        )
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


def test_a_reservation_whose_attempt_already_ended_is_skipped(home: Path) -> None:
    """An ending on the reserved attempt frees it however its processes read."""
    _ended_previous_attempt(home)
    # The reservation is at a later attempt than the spec's, which refuses on
    # its own; the recorded ending is what makes it not a hold.
    _reservation(home, 3, pid=os.getpid())
    _retired_exit(home, 3)
    pid = dispatch_launch_module._supervisor_spawn_worker(_spawn_spec(home, 2))
    try:
        receipts = sorted(home.glob("attempt-*-worker.json"))
        assert receipts, "the guard admitted no worker"
        worker = json.loads(receipts[-1].read_text(encoding="utf-8"))
        assert worker["pid"] == pid, (
            "the guard refused on an attempt that had already ended"
        )
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


# ── a reserved attempt is launched ──────────────────────────────────────────


def test_a_resume_launches_the_attempt_it_reserved(home: Path) -> None:
    _retired_exit(home, 1)
    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer(attempt=2))
    taken = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="resume")
    record_resumption(
        RUN_ID,
        pid=4242,
        attempt=taken.attempt,
        turn=1,
        log_path=home / "resume-1.jsonl",
        stderr_path=home / "resume-1.stderr.log",
        attempt_started_at="2026-11-08T20:00:00Z",
    )
    pointer = json.loads(runs_module.pointer_path(RUN_ID).read_text())
    assert pointer["attempt"] == taken.attempt


# ── the dispatch door ───────────────────────────────────────────────────────


@pytest.fixture()
def repo_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKTREE_ROOT", str(tmp_path / "trees"))

    repo = tmp_path / "repo"
    (repo / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (repo / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "docs" / "plans" / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", ".gitignore", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


def _node(manifest_path: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        goal="record one dispatch guard result",
        plan="fixture",
        section="guard",
        spec_level="guided",
        done_when="pytest reports one passing guard case",
        write_paths=["src/candidate.py"],
        time_budget="20m",
        manifest_path=str(manifest_path),
    )


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _stand_in_tree(repo: Path) -> tuple[Path, str, Path]:
    path = routing_module._launch_worktree_path(repo, SESSION, NODE_ID)
    routing_module._create_worktree(repo, SESSION, NODE_ID, "HEAD")
    note = path / "worker-note.log"
    note.write_text("in progress\n", encoding="utf-8")
    return path, _git(path, "rev-parse", "HEAD"), note


def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    if not directory.is_dir():
        return {}
    return {
        str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _dispatch(
    repo: Path,
    manifest: Path,
    *,
    run_id: str = RUN_ID,
    config: dict = CONFIG,
    launcher=lambda *a, **_: 4242,
) -> dict:
    return crew.dispatch(
        node=_node(manifest),
        project="proj",
        repo=repo,
        config=config,
        session=SESSION,
        run_id=run_id,
        base="HEAD",
        launcher=launcher,
    )


def _window_run(home_root: Path, worktree: Path) -> None:
    """The window: attempt 1 ended, attempt 2 is named, no worker record."""
    directory = runs_module.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    _retired_exit(directory, 1)
    runs_module._write_json(
        directory / "attempt.json",
        {"run_id": RUN_ID, "attempt": 2, "attempt_kind": "dispatch"},
    )
    runs_module._write_json(
        runs_module.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "proj",
            "node": {"id": NODE_ID, "plan": "fixture", "section": "guard"},
            "phase": "starting",
            "worktree": str(worktree),
            "pid": _dead_pid(),
            "launcher_host": socket.gethostname(),
            "attempt": 2,
        },
    )


def _hold_a_live_supervisor(monkeypatch: pytest.MonkeyPatch) -> subprocess.Popen:
    """Make a CLI launch record a supervisor that outlives the dispatch."""
    supervisor = _live_child()
    monkeypatch.setattr(
        dispatch_launch_module,
        "_spawn_detached_supervisor",
        lambda argv, stderr_path: supervisor.pid,
    )
    monkeypatch.setattr(
        dispatch_launch_module,
        "_confirm_supervisor_survived",
        lambda pid, run_directory, run_id: None,
    )
    return supervisor


def test_the_resume_compatibility_path_launches_the_pointer_attempt(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compatibility launch carries no reservation, so it keys on the pointer.

    The ``resume-*`` seam starts the supervisor without a reservation of its
    own. The pointer names attempt three and no receipt exists, so the launch
    reserves and starts attempt three rather than restarting at one.
    """
    from reckon import _backends

    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer(attempt=3))
    supervisor = _live_child()
    specs: list[Path] = []

    def capture(spec_path, run_directory, run_id):
        specs.append(Path(spec_path))
        return supervisor.pid

    monkeypatch.setattr(dispatch_launch_module, "_start_supervisor", capture)
    monkeypatch.setattr(
        dispatch_launch_module,
        "_confirm_supervisor_survived",
        lambda pid, run_directory, run_id: None,
    )
    plan = _backends.LaunchPlan(
        backend="probe",
        dialect="claude",
        argv=[sys.executable, "-c", "pass"],
        cwd=str(home),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )
    try:
        dispatch_launch_module._spawn(
            plan,
            log_path=home / "resume-1.jsonl",
            stderr_path=home / "resume-1.stderr.log",
            prompt_path=home / "prompt.txt",
        )
    finally:
        _stop(supervisor)

    assert specs, "the compatibility seam started no supervisor"
    spec = json.loads(specs[0].read_text(encoding="utf-8"))
    assert spec["attempt"] == 3, (
        "the launch did not start the attempt the pointer names"
    )
    assert (home / "attempt-3.reservation").exists(), "the launch took no reservation"


def test_a_failed_supervisor_rewrite_on_a_supervised_launch_is_reported(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rewrite that does not land is reported on stderr and stored nowhere.

    A door with no launch result — a resume, a review dispatch, the
    compatibility seam — has no result dict to return the failure in, so it
    warns. The failure is never written to the run's pointer: a pointer field
    outlives the failure it describes, because no later launch clears it.
    """
    from reckon import _backends

    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer(attempt=3))
    supervisor = _live_child()
    monkeypatch.setattr(
        dispatch_launch_module,
        "_start_supervisor",
        lambda spec_path, run_directory, run_id: supervisor.pid,
    )
    monkeypatch.setattr(
        dispatch_launch_module,
        "_confirm_supervisor_survived",
        lambda pid, run_directory, run_id: None,
    )
    original_write = dispatch_launch_module._write_json

    def failing_write(path, payload, **keywords):
        if str(path).endswith(dispatch_launch_module.RESERVATION_SUFFIX):
            raise OSError("the reservation could not be rewritten")
        return original_write(path, payload, **keywords)

    monkeypatch.setattr(dispatch_launch_module, "_write_json", failing_write)
    plan = _backends.LaunchPlan(
        backend="probe",
        dialect="claude",
        argv=[sys.executable, "-c", "pass"],
        cwd=str(home),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )
    try:
        dispatch_launch_module._spawn(
            plan,
            log_path=home / "resume-1.jsonl",
            stderr_path=home / "resume-1.stderr.log",
            prompt_path=home / "prompt.txt",
        )
    finally:
        _stop(supervisor)

    reported = capsys.readouterr().err
    assert dispatch_launch_module.SUPERVISOR_UNRECORDED_WARNING in reported, (
        "the failed rewrite was dropped instead of reported"
    )
    assert str(RUN_ID) in reported, "the warning names no run"
    pointer = json.loads(runs_module.pointer_path(RUN_ID).read_text(encoding="utf-8"))
    assert "reservation_supervisor_recorded" not in pointer, (
        "the failure was stored on the pointer, where no later launch clears it"
    )
    assert (home / "attempt-3.reservation").exists(), (
        "the failed rewrite removed the hold"
    )


def test_a_landed_supervisor_rewrite_reports_no_failure(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rewrite that lands leaves no failure report anywhere.

    The warning exists to make a dropped rewrite visible, so it must not fire
    when the rewrite landed. A report that is emitted unconditionally would
    pass every test that only ever fails the write, and would leave a reader
    unable to tell a succeeded rewrite from a failed one.
    """
    from reckon import _backends

    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer(attempt=3))
    supervisor = _live_child()
    monkeypatch.setattr(
        dispatch_launch_module,
        "_start_supervisor",
        lambda spec_path, run_directory, run_id: supervisor.pid,
    )
    monkeypatch.setattr(
        dispatch_launch_module,
        "_confirm_supervisor_survived",
        lambda pid, run_directory, run_id: None,
    )
    plan = _backends.LaunchPlan(
        backend="probe",
        dialect="claude",
        argv=[sys.executable, "-c", "pass"],
        cwd=str(home),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )
    try:
        dispatch_launch_module._spawn(
            plan,
            log_path=home / "resume-1.jsonl",
            stderr_path=home / "resume-1.stderr.log",
            prompt_path=home / "prompt.txt",
        )
    finally:
        _stop(supervisor)

    reported = capsys.readouterr().err
    assert dispatch_launch_module.SUPERVISOR_UNRECORDED_WARNING not in reported, (
        "a rewrite that landed still reported a failure"
    )
    pointer = json.loads(runs_module.pointer_path(RUN_ID).read_text(encoding="utf-8"))
    assert "reservation_supervisor_recorded" not in pointer
    held = json.loads((home / "attempt-3.reservation").read_text(encoding="utf-8"))
    assert held["supervisor_pid"] == supervisor.pid, (
        "the landed rewrite did not name the supervisor"
    )


def test_the_window_is_refused_before_the_worktree_is_touched(
    repo_context: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config_home, repo = repo_context
    tree, _head, _note = _stand_in_tree(repo)
    _window_run(tmp_path, tree)

    supervisor = _hold_a_live_supervisor(monkeypatch)
    try:
        _dispatch(repo, tmp_path / "manifests" / "first.md", launcher=None)
        # The first dispatch re-cut the stand-in tree, as a relaunch does; the
        # file this case protects is written into the tree the live launch now
        # holds.
        marker = tree / "live-marker.txt"
        marker.write_text("in progress\n", encoding="utf-8")
        before = _snapshot(tree)
        head_before = _git(tree, "rev-parse", "HEAD")

        calls: list[str] = []
        original = dispatch_module._create_worktree

        def spy(*arguments, **keywords):
            calls.append("cut")
            return original(*arguments, **keywords)

        dispatch_module._create_worktree = spy
        try:
            refused = _refused(
                lambda: _dispatch(repo, tmp_path / "manifests" / "second.md")
            )
        finally:
            dispatch_module._create_worktree = original
    finally:
        _stop(supervisor)

    # Two guards defend this window and which one speaks depends on how far the
    # first launch had got: the reservation while the worker is still unrecorded,
    # the live-worker guard once it is. Both refuse before the tree is touched.
    assert "attempt 2" in str(refused) and (
        IN_FLIGHT in str(refused) or "live worker" in str(refused)
    ), str(refused)
    assert calls == [], "the refused launch cut the worktree"
    assert _snapshot(tree) == before, "the refused launch touched the live tree"
    assert _git(tree, "rev-parse", "HEAD") == head_before
    assert marker.read_text() == "in progress\n"


def test_an_in_harness_relaunch_is_refused_while_a_cli_launch_holds_one(
    repo_context: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config_home, repo = repo_context
    tree, _head, _note = _stand_in_tree(repo)
    _window_run(tmp_path, tree)

    supervisor = _hold_a_live_supervisor(monkeypatch)
    try:
        _dispatch(repo, tmp_path / "manifests" / "cli.md", launcher=None)

        calls: list[str] = []
        original = dispatch_module._create_worktree

        def spy(*arguments, **keywords):
            calls.append("cut")
            return original(*arguments, **keywords)

        dispatch_module._create_worktree = spy
        try:
            refused = _refused(
                lambda: _dispatch(
                    repo,
                    tmp_path / "manifests" / "harness.md",
                    config=HARNESS_CONFIG,
                )
            )
        finally:
            dispatch_module._create_worktree = original
    finally:
        _stop(supervisor)

    assert "attempt 2" in str(refused) and (
        IN_FLIGHT in str(refused) or "live worker" in str(refused)
    ), str(refused)
    assert calls == [], "the in-harness relaunch cut the worktree"


def test_a_dispatch_is_held_by_the_supervisor_it_recorded(
    repo_context: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch leaves behind a reservation naming the supervisor it started.

    The admitting process is this one and stays alive for the whole case, as the
    admit service's does. What frees the run must be the supervisor's own end,
    so the reservation the dispatch leaves on disk has to name that supervisor
    rather than the door that took it.
    """
    _config_home, repo = repo_context
    tree, _head, _note = _stand_in_tree(repo)
    _window_run(tmp_path, tree)
    directory = runs_module.run_dir(RUN_ID)

    supervisor = _live_child()
    try:
        monkeypatch.setattr(
            dispatch_launch_module,
            "_spawn_detached_supervisor",
            lambda argv, stderr_path: supervisor.pid,
        )
        monkeypatch.setattr(
            dispatch_launch_module,
            "_confirm_supervisor_survived",
            lambda pid, run_directory, run_id: None,
        )
        _dispatch(repo, tmp_path / "manifests" / "first.md", launcher=None)

        held = dispatch_launch_module.held_launch_reservation(directory)
        assert held is not None
        assert held["supervisor_pid"] == supervisor.pid, (
            "the reservation names the door that took it, not the supervisor"
        )
        _refused(
            lambda: dispatch_launch_module.reserve_launch_attempt(
                directory, RUN_ID, kind="dispatch"
            )
        )
    finally:
        _stop(supervisor)

    assert dispatch_launch_module.held_launch_reservation(directory) is None, (
        "a supervisor that ended leaves the run launchable while the door lives"
    )
    again = dispatch_launch_module.reserve_launch_attempt(
        directory, RUN_ID, kind="dispatch"
    )
    dispatch_launch_module.release_launch_reservation(again)


def test_a_relaunch_rewrites_its_marker_with_the_reserved_attempt(
    repo_context: tuple[Path, Path], tmp_path: Path
) -> None:
    """The marker always names the attempt the launch runs.

    A run holding an earlier attempt receipt but no ledger row is dispatched
    with attempt one and reserves attempt two. A marker left naming one would
    duplicate that attempt's number while a receipt for it already exists, and
    a later reader would take the reserved number for an attempt that never ran.
    """
    _config_home, repo = repo_context
    _tree, _head, _note = _stand_in_tree(repo)
    directory = runs_module.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    _retired_exit(directory, 1)
    runs_module._write_json(
        directory / "attempt.json",
        {"run_id": RUN_ID, "attempt": 1, "attempt_kind": "redispatch"},
    )
    runs_module._write_json(
        runs_module.pointer_path(RUN_ID), _pointer(attempt=1, pid=_dead_pid())
    )
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")

    record = _dispatch(repo, tmp_path / "manifests" / "again.md")

    assert record["attempt"] == 2, "the launch did not reserve the next attempt"
    marker = json.loads((directory / "attempt.json").read_text(encoding="utf-8"))
    assert marker["attempt"] == 2, "the marker names an attempt the launch did not run"


def test_a_failed_supervisor_rewrite_is_reported_and_still_holds(
    repo_context: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reservation that cannot name its supervisor keeps refusing.

    The rewrite lands after the supervisor is spawned, so releasing the hold
    when it fails would invite a relaunch over a live worker. The failure is
    reported on stderr instead, and the attempt's own exit receipt is what
    frees the hold in its own time.
    """
    _config_home, repo = repo_context
    tree, _head, _note = _stand_in_tree(repo)
    _window_run(tmp_path, tree)
    directory = runs_module.run_dir(RUN_ID)

    supervisor = _live_child()
    monkeypatch.setattr(
        dispatch_launch_module,
        "_spawn_detached_supervisor",
        lambda argv, stderr_path: supervisor.pid,
    )
    monkeypatch.setattr(
        dispatch_launch_module,
        "_confirm_supervisor_survived",
        lambda pid, run_directory, run_id: None,
    )
    original_write = dispatch_launch_module._write_json

    def failing_write(path, payload, **keywords):
        if str(path).endswith(dispatch_launch_module.RESERVATION_SUFFIX):
            raise OSError("the reservation could not be rewritten")
        return original_write(path, payload, **keywords)

    monkeypatch.setattr(dispatch_launch_module, "_write_json", failing_write)
    try:
        record = _dispatch(repo, tmp_path / "manifests" / "first.md", launcher=None)
    finally:
        _stop(supervisor)

    assert (
        dispatch_launch_module.SUPERVISOR_UNRECORDED_WARNING in capsys.readouterr().err
    ), "the failed rewrite was not reported"
    assert "reservation_supervisor_recorded" not in record, (
        "the failure was stored on the launch record, which is merged onto the pointer"
    )
    refused = _refused(
        lambda: dispatch_launch_module.reserve_launch_attempt(
            directory, RUN_ID, kind="dispatch"
        )
    )
    assert "attempt 2" in str(refused) and IN_FLIGHT in str(refused)


def test_a_fresh_run_ids_reservation_ends_with_its_launch(
    repo_context: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first launch reserves the attempt it starts and drops the hold at its end.

    The marker is written before the reservation block, so the run always holds
    an attempt record when the door keys and every launch reserves. A launch
    that ends leaves no reservation behind, so the run stays launchable.
    """
    _config_home, repo = repo_context
    taken: list[int] = []
    original = dispatch_launch_module.reserve_launch_attempt

    def spy(run_directory, run_id, *, kind):
        reservation = original(run_directory, run_id, kind=kind)
        taken.append(reservation.attempt)
        return reservation

    monkeypatch.setattr(dispatch_launch_module, "reserve_launch_attempt", spy)
    record = _dispatch(repo, tmp_path / "manifests" / "fresh.md", run_id="r-fresh-run")
    directory = runs_module.run_dir("r-fresh-run")

    assert taken == [1], "a fresh launch took no reservation"
    assert record["attempt"] == 1, "the ledger attempt was not kept"
    assert not list(directory.glob("attempt-*.reservation")), (
        "a launch that ended left its reservation behind"
    )
