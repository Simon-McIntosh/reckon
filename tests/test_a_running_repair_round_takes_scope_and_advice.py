"""A widen reaches a running repair round's next attempt.

A repair round is a resume of the reviewed run rather than a second node, so a
path a coordinator grants while the round's worker is live belongs on the run's
own claim. The claim used to refuse every amendment while any worker was alive,
so a file the round's finding needed never reached the next attempt: measured on
a repair round auto-started before the coordinator's ``crew widen``, whose
``--write-path`` was refused while the worker ran and whose advice could not be
sent either, because the round runs its own turn.

Two behaviours, both on the composed launch rather than on the pointer:

* a widen issued while a repair round's worker is live is recorded, and the
  round's next attempt is fenced with the widened path among its writable binds;
* a widen on a live run that is not in a repair round is still refused, exactly
  as at the base revision.

The declared mutation, applied verbatim in a scratch copy: drop the carry of the
recorded widen into the next attempt's fence composition, which makes the bind
assertion fail.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click.testing
import pytest

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import crew
from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path
from tests import test_a_live_run_never_reads_dead as liveness

PROJECT = "proj"
HOST = socket.gethostname()

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "claude",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@contextmanager
def _live_worker() -> Iterator[int]:
    """A real, running child, bounded by its own clock and reaped by its pid."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the crew config and state home at a temp tree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, crew_home: Path) -> Path:
    """A throwaway repository carrying a plan and the fleet launch script."""
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills/reckon-build/scripts/worktree_fleet.py"
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="s3">§3 — Repair</h2></body></html>
""",
        encoding="utf-8",
    )
    (root / "allowed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "allowed.txt", "docs/plans/plan-a.html", "skills"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *args)
    (crew_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _live_pointer(
    tmp_path: Path,
    repo: Path,
    run_id: str,
    *,
    pid: int,
    repair_round: bool,
) -> dict:
    """A run whose worker is live, differing only in whether a round is open.

    The attempt kind is ``resume`` because that is what a repair round's turn
    is; without a repair round the same run is a live worker whose claim keeps
    the boundary it was dispatched with.
    """
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / f"{run_id}-tree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(f"node: {run_id}\nstatus: working\n", encoding="utf-8")
    prompt_path = directory / "prompt.txt"
    prompt_path.write_text("the original dispatch prompt\n", encoding="utf-8")
    (directory / "attempt.json").write_text(
        json.dumps({"attempt": 2, "attempt_kind": "resume"}), encoding="utf-8"
    )
    (directory / "attempt-2-worker.json").write_text(
        json.dumps({"run_id": run_id, "attempt": 2, "pid": pid}),
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(worktree),
        "launch": "cli",
        "backend": "alpha",
        "dialect": "claude",
        "role": "implement",
        "pid": pid,
        "pid_start_time": None,
        "process_alive": None,
        "session_id": f"sess-{run_id}",
        "session_harness": "claude",
        "launcher_host": HOST,
        "attempt": 2,
        "attempt_kind": "resume",
        "base_sha": "HEAD",
        "created_at": "2026-11-10T22:49:00+00:00",
        "log_path": str(directory / "resume-1.jsonl"),
        "manifest_path": str(manifest),
        "prompt_path": str(prompt_path),
        "phase": "working",
        "node": {
            "id": run_id,
            "plan": "plan-a",
            "section": "§3",
            "time_budget": "25m",
            "write_paths": ["allowed.txt"],
        },
    }
    if repair_round:
        record["repair_rounds"] = {
            "round_id": "round-1-helium",
            "count": 1,
            "attempts": 2,
        }
        record["repair_dispatch"] = {
            "status": "resumed",
            "round_id": "round-1-helium",
            "attempt": 2,
        }
    _write_json(pointer_path(run_id), record)
    return record


def _widen(run_id: str, path: str) -> click.testing.Result:
    """Run ``crew widen`` as a coordinator issues it."""
    argv = ["crew", "widen", "--run", run_id, "--write-path", path]
    return click.testing.CliRunner().invoke(cli_main, argv)


def _bound_writable(argv: list[str], target: Path) -> bool:
    """Whether the composed fence binds ``target`` writable."""
    wanted = str(target)
    for index, word in enumerate(argv):
        if word == "--bind" and list(argv[index + 1 : index + 3]) == [wanted, wanted]:
            return True
    return False


def test_a_live_repair_round_takes_the_widen_and_its_next_attempt_mounts_it(
    crew_home: Path,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The widen is accepted while the round runs, and the next turn is fenced."""
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    # The launch's executable preflight asks a live harness for its own hook
    # identity, which is not what this case measures: it reads the fence the
    # next attempt is composed with.
    monkeypatch.setattr(
        dispatch_sessions_module, "preflight_launch_command", lambda *a, **k: None
    )
    run_id = "r-live-repair-round"
    granted = tmp_path / "granted-outside-the-tree" / "the-finding-s-file.md"
    granted.parent.mkdir(parents=True, exist_ok=True)
    with _live_worker() as worker_pid:
        _live_pointer(tmp_path, repo, run_id, pid=worker_pid, repair_round=True)
        widened = _widen(run_id, str(granted))
        assert widened.exit_code == 0, (
            widened.stdout,
            widened.stderr,
            widened.exception,
        )
        document = json.loads(widened.stdout)
        assert str(granted) in document["added"]

    # The turn the widen was issued during ends, and the round's next attempt
    # is composed. The assertion below reads that composition, not the claim.
    liveness._write_exit_record(run_id)
    plan = crew.resume_plan(run_id, "continue", config=CONFIG)

    argv = [str(token) for token in plan.argv]
    assert _bound_writable(argv, granted.parent), (
        "the next attempt's fence did not bind the widened path writable; "
        f"composed argv was {argv}"
    )


def test_a_live_run_outside_a_repair_round_is_still_refused(
    crew_home: Path,
    repo: Path,
    tmp_path: Path,
) -> None:
    """A live worker that is not a repair round keeps its claim fixed."""
    run_id = "r-live-ordinary-worker"
    with _live_worker() as worker_pid:
        _live_pointer(tmp_path, repo, run_id, pid=worker_pid, repair_round=False)
        refused = _widen(run_id, str(tmp_path / "granted" / "notes.md"))

    assert refused.exit_code != 0
    assert "its worker is still running" in refused.output
    assert str(tmp_path / "granted" / "notes.md") not in json.dumps(
        crew.read_pointer(run_id)
    )
