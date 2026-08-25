"""Cold-start watcher lifetime across delivery and reconciliation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.recovery import watch_ticker
from reckon.crew.runs import project_watch_visibility


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


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move all crew state into the test's temporary directory."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    """Create the smallest repository accepted by the dispatch boundary."""
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-ship" / "scripts"
    scripts.mkdir(parents=True)
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "delivery.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="delivery">'
        '<h2 id="dispatch">Dispatch</h2>',
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/delivery.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"sample": str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_stale_terminal_pointer(home: Path, *, repo: Path | None = None) -> dict:
    """Park one delivered pointer before the watcher is armed."""
    stream = home / "streams" / "owner.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text('{"type":"turn.completed"}\n', encoding="utf-8")
    manifest = home / "manifests" / "owner.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "node: owner\nstatus: complete\ncommits: HEAD\nblockers: none\n",
        encoding="utf-8",
    )
    stale_at = time.time() - 600
    os.utime(stream, (stale_at, stale_at))
    os.utime(manifest, (stale_at, stale_at))
    record = {
        "run_id": "run-owner",
        "project": "sample",
        "repo": str(repo or "/temporary/repository"),
        "node": {
            "id": "owner",
            "plan": "delivery",
            "time_budget": "20m",
            "write_paths": ["reckon/owner.py"],
        },
        "phase": "complete",
        "created_at": "2026-08-25T05:00:00+00:00",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "process_alive": False,
    }
    crew._write_json(crew.pointer_path(record["run_id"]), record)
    return record


def _next_node(home: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="next-node",
        goal="Hold the project watch while an unreconciled pointer remains",
        plan="delivery",
        section="dispatch",
        done_when="tests/test_crew_watch_lifetime.py reports 3 passed and 0 failed",
        write_paths=["reckon/next.py"],
        time_budget="20m",
        manifest_path=str(home / "manifests" / "next.md"),
    )


class _ControlledPoll:
    """Expose one completed poll without letting the watcher run ahead."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()

    def __call__(self, _seconds: float) -> None:
        self.reached.set()
        assert self.release.wait(timeout=5)


def _assert_seat_held_after_poll(poll: _ControlledPoll, future) -> None:
    assert poll.reached.wait(timeout=5)
    visibility = project_watch_visibility("sample")
    assert future.done() is False
    assert visibility["watcher_live"] is True
    assert visibility["pointer_count"] == 1


def _finish_after_reconciliation(ticker, pool: ThreadPoolExecutor) -> None:
    with pytest.raises(StopIteration):
        pool.submit(next, ticker).result(timeout=5)


def test_cold_watch_holds_an_already_stale_terminal_pointer(home: Path) -> None:
    record = _write_stale_terminal_pointer(home)
    source_root = Path(__file__).parents[1]
    producer = subprocess.Popen(
        [
            str(Path(sys.executable).with_name("reckon")),
            "crew",
            "watch",
            "--project",
            "sample",
            "--json",
        ],
        env={**os.environ, "PYTHONPATH": str(source_root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert producer.stdout is not None
        baseline = json.loads(producer.stdout.readline())
        assert (baseline["to_state"], baseline["unpromoted"]) == ("complete", 1)
        time.sleep(0.25)

        visibility = project_watch_visibility("sample")
        assert producer.poll() is None
        assert visibility["watcher_live"] is True
        assert visibility["pointer_count"] == 1
        crew.pointer_path(record["run_id"]).unlink()
        assert producer.stdout is not None
        promoted = json.loads(producer.stdout.readline())
        assert (promoted["from_state"], promoted["to_state"]) == (
            "complete",
            "promoted",
        )
        assert producer.wait(timeout=5) == 0
    finally:
        if producer.poll() is None:
            producer.terminate()
            producer.wait(timeout=5)

    assert project_watch_visibility("sample")["watcher_live"] is False


def test_dispatch_is_admitted_during_the_stale_terminal_window(
    home: Path, repo: Path
) -> None:
    record = _write_stale_terminal_pointer(home, repo=repo)
    poll = _ControlledPoll()
    ticker = watch_ticker("sample", stall_window="1h", poll_interval=0, sleeper=poll)

    with ThreadPoolExecutor(max_workers=1) as pool:
        baseline = pool.submit(next, ticker).result(timeout=5)
        assert baseline["to_state"] == "complete"
        waiting = pool.submit(next, ticker)
        _assert_seat_held_after_poll(poll, waiting)

        accepted = crew.dispatch(
            node=_next_node(home),
            project="sample",
            repo=repo,
            config=CONFIG,
            session="consumer",
            launcher=lambda *args, **kwargs: 4242,
            watch_required=True,
        )

        assert accepted["watch"]["watcher_live"] is True
        assert accepted["watch_override"] is None
        crew.pointer_path(record["run_id"]).unlink()
        crew.pointer_path(accepted["run_id"]).unlink()
        poll.release.set()
        assert waiting.result(timeout=5)["to_state"] == "promoted"
        _finish_after_reconciliation(ticker, pool)


def test_empty_fleet_still_waits_for_its_first_pointer(home: Path) -> None:
    poll = _ControlledPoll()
    ticker = watch_ticker("sample", stall_window="1h", poll_interval=0, sleeper=poll)

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(next, ticker)
        assert poll.reached.wait(timeout=5)
        visibility = project_watch_visibility("sample")
        assert waiting.done() is False
        assert visibility["watcher_live"] is True
        assert visibility["pointer_count"] == 0

        record = _write_stale_terminal_pointer(home)
        poll.release.set()
        baseline = waiting.result(timeout=5)
        assert baseline["to_state"] == "complete"
        crew.pointer_path(record["run_id"]).unlink()
        promoted = pool.submit(next, ticker).result(timeout=5)
        assert promoted["to_state"] == "promoted"
        _finish_after_reconciliation(ticker, pool)
