"""A widen says when the paths it just added take effect.

A running worker's mounts are fixed when its sandbox starts, so a widened claim
reaches the worker only on its next attempt. The widen result used to list the
added paths and say nothing about that, so a coordinator read ``ok`` as a fence
that had already moved — measured on a run whose three paths read as widened
while the live worker still could not write them.

The sentence is built from one liveness reading that carries the arm it was
taken from, so it states what the run's records support: a worker read alive by
its recorded pid is named with that pid; a worker found only by its session walk
is named by its session and never with a pid; a placed run's recorded pid is the
scheduler client's and is never presented as the worker's; a proven death says
the next resume or launch mounts the paths; an unreadable liveness says only
that it could not be read on this host.

The declared mutation, applied verbatim in a scratch copy: dropping the liveness
read makes the first case read as the second; naming the session-walk arm's pid
as the worker's fails the third; deciding the boolean apart from the reading
fails the fifth.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import click.testing
import pytest

from reckon.cli import main as cli_main
from reckon.crew import process_liveness, runs
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "fixture-project"
HOST = socket.gethostname()

_REAL_HOME = Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"
_RUN_IDS = (
    "r-widen-live-worker",
    "r-widen-stopped-run",
    "r-widen-session-walk",
    "r-widen-unknown-liveness",
    "r-widen-fence-field",
)


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    ceiling = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    return ceiling + 4096


@contextmanager
def _live_worker(*extra_argv: str):
    """A real, running child, bounded by its own clock and reaped by its pid."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", *extra_argv],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


def _case_artifacts(crew_home: Path) -> list[Path]:
    """The paths this file's cases leave under ``crew_home``.

    Named for the run ids above, so the same list describes the real crew home
    and a stand-in: the assertion in the fixture is the receipt that this file
    wrote nothing into a live fleet's home while it ran.
    """
    return [
        *(crew_home / "runs" / run_id for run_id in _RUN_IDS),
        *(crew_home / "live" / f"{run_id}.json" for run_id in _RUN_IDS),
    ]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    landed = [path for path in _case_artifacts(_REAL_HOME) if path.exists()]
    assert not landed, f"the real crew home carries this file's run paths: {landed}"


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    pid: int | None,
    session_id: str = "fixture-session",
    attempt: int = 1,
    placed: bool = False,
) -> dict:
    """A run whose write claim can be widened, differing in its liveness facts."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / f"{run_id}-tree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / f"{run_id}.md"
    manifest.write_text("status: working\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "backend": "alpha",
        "launch": "cli",
        "repo": str(worktree),
        "role": "implement",
        "session_id": session_id,
        "phase": "working",
        "attempt": attempt,
        "manifest_path": str(manifest),
        "log_path": str(tmp_path / f"{run_id}.jsonl"),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "worktree": str(worktree),
        "pid": pid,
        "launcher_host": HOST,
        "node": {
            "id": run_id,
            "role": "implement",
            "write_paths": ["src/original.py"],
        },
    }
    if placed:
        record["job_id"] = "4242"
        record["placement"] = {
            "state_query": ["squeue", "-h", "-j", "{job}", "-o", "%T"],
        }
    _write_json(pointer_path(run_id), record)
    return record


def _widen(
    run_id: str,
    path: str,
    *,
    reason: str = "",
    self_widen: bool = False,
) -> dict:
    """Run ``crew widen`` as the CLI emits it, and decode its one document."""
    argv = ["crew", "widen", "--run", run_id, "--write-path", path]
    if reason:
        argv += ["--reason", reason]
    previous = os.environ.get("RECKON_RUN_ID")
    os.environ.pop("RECKON_RUN_ID", None)
    if self_widen:
        os.environ["RECKON_RUN_ID"] = run_id
    try:
        result = click.testing.CliRunner().invoke(cli_main, argv)
    finally:
        os.environ.pop("RECKON_RUN_ID", None)
        if previous is not None:
            os.environ["RECKON_RUN_ID"] = previous
    assert result.exit_code == 0, (result.stdout, result.stderr, result.exception)
    return json.loads(result.stdout)


# --- The cases the widen result must show ----------------------------------


def test_a_self_widen_beside_a_live_worker_names_the_attempt_and_pid(home) -> None:
    """The worker is alive by its recorded pid, so it keeps the mounts it has."""
    with _live_worker() as worker_pid:
        _pointer(home, "r-widen-live-worker", pid=worker_pid, attempt=2)
        result = _widen(
            "r-widen-live-worker",
            "src/added-by-worker.py",
            reason="the worker reached a path its dispatch did not name",
            self_widen=True,
        )
    text = result["takes_effect"]
    assert "src/added-by-worker.py" in result["added"]
    assert "attempt 2" in text
    assert f"pid {worker_pid}" in text
    assert "mounts are unchanged" in text
    assert "next attempt" in text


def test_a_widen_of_a_stopped_run_says_the_next_attempt_mounts_the_paths(
    home,
) -> None:
    """A proven death means nothing is holding the old fence."""
    _pointer(home, "r-widen-stopped-run", pid=_absent_pid())
    result = _widen("r-widen-stopped-run", "src/added-by-coordinator.py")
    text = result["takes_effect"]
    assert "src/added-by-coordinator.py" in result["added"]
    assert "next resume or launch mounts the added paths" in text
    assert "node.write_paths" in text


def test_a_session_walk_worker_is_named_by_its_session_and_never_by_a_pid(
    home,
) -> None:
    """A worker found through its session has no pid this host read as its own."""
    session_id = "walked-session-r-widen"
    with _live_worker(session_id) as stand_in:
        _pointer(
            home,
            "r-widen-session-walk",
            pid=_absent_pid(),
            session_id=session_id,
            attempt=3,
        )
        result = _widen(
            "r-widen-session-walk",
            "src/added-under-walk.py",
            reason="the worker reached a path its dispatch did not name",
            self_widen=True,
        )
    text = result["takes_effect"]
    assert session_id in text
    assert "attempt 3" in text
    assert "no pid is named for it" in text
    assert not re.search(r"pid \d", text), text
    assert str(stand_in) not in text
    assert "mounts are unchanged" in text


def test_an_unreadable_liveness_never_says_a_launch_mounts_the_paths(home) -> None:
    """No pid recorded and no live process: the widen knows nothing to claim."""
    _pointer(home, "r-widen-unknown-liveness", pid=None, session_id="no-such-session")
    result = _widen("r-widen-unknown-liveness", "src/guarded.py")
    text = result["takes_effect"]
    assert "could not be read on this host" in text
    assert "next launch" not in text
    assert "next resume" not in text
    assert "mounts the added paths" not in text


def test_the_fence_field_carries_the_widened_paths_after_the_call(home) -> None:
    """The field the sentence names is the field the launch fence reads.

    This guards behaviour the widening already had: the claim writer stores the
    added paths under ``node.write_paths``, which is what a launch reads for its
    fence. It passes at the base revision by design.
    """
    _pointer(home, "r-widen-fence-field", pid=_absent_pid())
    result = _widen("r-widen-fence-field", "src/fence-field.py")
    assert "src/fence-field.py" in result["added"]
    reread = runs.read_pointer("r-widen-fence-field")
    fenced = (reread.get("node") or {}).get("write_paths") or ()
    assert "src/fence-field.py" in fenced
    assert result["write_paths"] == list(fenced)


# --- The boolean is derived from the one reading ---------------------------


@pytest.mark.parametrize(
    ("arm", "alive", "facts", "expected"),
    [
        ("recorded-pid", True, {"pid": 1234}, True),
        ("session-walk", True, {"session_id": "s"}, True),
        ("placed", True, {"job_id": "1"}, True),
        ("placed", False, {"job_id": "1"}, False),
        ("dead", False, {}, False),
        ("unknown", None, {}, None),
    ],
)
def test_record_process_alive_answers_the_boolean_the_reading_implies(
    monkeypatch: pytest.MonkeyPatch, arm: str, alive, facts: dict, expected
) -> None:
    """The arm and the boolean come from one decision, never two."""
    reading = process_liveness.LivenessReading(arm, alive, **facts)
    monkeypatch.setattr(
        process_liveness, "record_process_liveness", lambda *a, **k: reading
    )
    record = {"run_id": "r-any", "launch": "cli", "pid": 1234}
    assert process_liveness.record_process_alive(record) is expected
