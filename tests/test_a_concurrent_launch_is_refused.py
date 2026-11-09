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
    refused_guard = _refused(
        lambda: dispatch_launch_module._supervisor_spawn_worker(_spawn_spec(home, 3))
    )
    assert "attempt 2" in str(refused_guard) and IN_FLIGHT in str(refused_guard)


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


def test_the_window_is_refused_before_the_worktree_is_touched(
    repo_context: tuple[Path, Path], tmp_path: Path
) -> None:
    _config_home, repo = repo_context
    tree, _head, _note = _stand_in_tree(repo)
    _window_run(tmp_path, tree)

    _dispatch(repo, tmp_path / "manifests" / "first.md")
    # The first dispatch re-cut the stand-in tree, as a relaunch does; the file
    # this case protects is written into the tree the live launch now holds.
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

    assert "attempt 2" in str(refused) and IN_FLIGHT in str(refused)
    assert calls == [], "the refused launch cut the worktree"
    assert _snapshot(tree) == before, "the refused launch touched the live tree"
    assert _git(tree, "rev-parse", "HEAD") == head_before
    assert marker.read_text() == "in progress\n"


def test_an_in_harness_relaunch_is_refused_while_a_cli_launch_holds_one(
    repo_context: tuple[Path, Path], tmp_path: Path
) -> None:
    _config_home, repo = repo_context
    tree, _head, _note = _stand_in_tree(repo)
    _window_run(tmp_path, tree)
    _dispatch(repo, tmp_path / "manifests" / "cli.md")

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

    assert "attempt 2" in str(refused) and IN_FLIGHT in str(refused)
    assert calls == [], "the in-harness relaunch cut the worktree"


def test_a_fresh_run_id_takes_no_reservation(
    repo_context: tuple[Path, Path], tmp_path: Path
) -> None:
    _config_home, repo = repo_context
    record = _dispatch(repo, tmp_path / "manifests" / "fresh.md", run_id="r-fresh-run")
    directory = runs_module.run_dir("r-fresh-run")
    assert not list(directory.glob("attempt-*.reservation"))
    assert record["attempt"] == 1, "the ledger attempt was not kept"
