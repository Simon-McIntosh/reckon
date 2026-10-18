"""A watch producer sweeps only when the crew home changes.

One producer cycle used to list the fleet, walk run directories and call git
on a fixed beat whether or not anything had moved. The sweep now rides a
stat-only change signal over the crew home, so a quiet cycle lists nothing and
spawns no git, a single pointer that moves pulls exactly one sweep, and the
per-live-run liveness probe stays on the beat so a run's death is still
delivered with no file touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from reckon.crew import recovery_classification, recovery_watch, runs

PROJECT = "sweep-on-change"
FOREIGN_HOST = "a-login-node-that-is-not-this-one"
THREE = 3


class _StopTickingError(Exception):
    """Ends the producer after the case's steps have all run."""


def _git(tree: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(tree), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def _worktree(tmp_path: Path) -> tuple[Path, str]:
    """A checkout one commit past its base, so the git arm has work to count."""
    tree = tmp_path / "worktree"
    tree.mkdir()
    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "fixture@example.invalid")
    _git(tree, "config", "user.name", "fixture")
    (tree / "delivery.txt").write_text("base\n")
    _git(tree, "add", "delivery.txt")
    _git(tree, "commit", "-q", "-m", "base")
    base = _git(tree, "rev-parse", "HEAD")
    (tree / "delivery.txt").write_text("delivered\n")
    _git(tree, "add", "delivery.txt")
    _git(tree, "commit", "-q", "-m", "delivered")
    return tree, base


def _pointer(run_id: str, *, worktree: Path, base_sha: str) -> dict:
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": f"node-{run_id}", "plan": "plan-a"},
        "phase": "working",
        "launcher_host": FOREIGN_HOST,
        "process_alive": False,
        "backend": "claude",
        "launch": "cli",
        "command": "claude",
        "manifest_path": str(directory / "manifest.md"),
        "log_path": str(directory / "stream.jsonl"),
        "worktree": str(worktree),
        "base_sha": base_sha,
    }


def _seed(record: dict) -> None:
    """Write a run's stream, manifest and pointer from its record."""
    Path(record["log_path"]).write_text(
        json.dumps(
            {
                "type": "assistant",
                "session_id": record["run_id"],
                "timestamp": "2026-01-01T00:00:00.000Z",
                "message": {"content": [{"type": "text", "text": "x"}]},
            }
        )
        + "\n"
    )
    Path(record["manifest_path"]).write_text(
        f"---\nnode: {record['run_id']}\nstatus: running\n---\n\nbody\n"
    )
    pointer = runs.pointer_path(record["run_id"])
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(record))


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", "600")
    # Substitute this host's process table for a flag the case flips, so a
    # death with no file moved is exercised rather than observed.
    state = {"alive": True}
    monkeypatch.setattr(
        recovery_watch, "local_liveness", lambda record: (state["alive"], True)
    )
    monkeypatch.setattr(
        recovery_classification, "local_liveness", lambda record: (state["alive"], True)
    )
    # No run holds a worker record here, so the descendant probe never runs;
    # pin it so the case cannot read this host's process table either.
    monkeypatch.setattr(recovery_watch, "_live_descendant", lambda pid: None)
    tree, base = _worktree(tmp_path)
    records = [
        _pointer(f"r-s{index}", worktree=tree, base_sha=base) for index in range(THREE)
    ]
    for record in records:
        _seed(record)
    return {"tree": tree, "base": base, "records": records, "state": state}


def _install_spawn_counter(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every git subprocess launched while the producer runs."""
    spawns: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
            spawns.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    return spawns


def _install_list_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    """Count every fleet listing the producer takes."""
    lists: list[int] = []
    real_list_live = recovery_watch.list_live

    def counting_list_live(*args, **kwargs):
        lists.append(1)
        return real_list_live(*args, **kwargs)

    monkeypatch.setattr(recovery_watch, "list_live", counting_list_live)
    return lists


def _drive(
    steps: list[Callable[[], None]],
    *,
    on_boundary: Callable[[], None],
) -> list[dict]:
    """Run the producer, invoking ``steps[i]`` at the i-th quiet boundary."""
    events: list[dict] = []
    clock = {"i": 0}

    def sleeper(_interval: float) -> None:
        index = clock["i"]
        clock["i"] += 1
        on_boundary()
        if index >= len(steps):
            raise _StopTickingError
        steps[index]()

    generator = recovery_watch.watch_ticker(PROJECT, poll_interval=0.0, sleeper=sleeper)
    with contextlib.suppress(_StopTickingError):
        events.extend(generator)
    return events


def test_quiet_cycles_list_nothing_and_spawn_no_git(
    fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll over a fleet nobody moved takes neither a listing nor git."""
    spawns = _install_spawn_counter(monkeypatch)
    lists = _install_list_counter(monkeypatch)
    marks: dict[str, list[int]] = {"spawn": [], "list": []}

    def on_boundary() -> None:
        marks["spawn"].append(len(spawns))
        marks["list"].append(len(lists))

    _drive([lambda: None, lambda: None, lambda: None], on_boundary=on_boundary)

    assert len(marks["list"]) >= 3, marks
    # The producer's own first writes to the watch directory move the token
    # once, so the opening settles within a cycle or two; after that the home
    # is still and no cycle may list the fleet or spawn git again.
    assert marks["list"][0] < 3, "the opening must settle rather than sweep every beat"
    assert marks["list"][1] == marks["list"][0], "a quiet cycle must not list"
    assert marks["list"][2] == marks["list"][0], "a quiet cycle must not list"
    assert marks["spawn"][0] >= 1, "the opening sweep must have exercised git"
    assert marks["spawn"][1] == marks["spawn"][0], "a quiet cycle must spawn no git"
    assert marks["spawn"][2] == marks["spawn"][0], "a quiet cycle must spawn no git"


def test_a_touched_pointer_sweeps_exactly_once(
    fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pointer whose mtime moves pulls one sweep, and only one."""
    lists = _install_list_counter(monkeypatch)
    marks: list[int] = []

    def on_boundary() -> None:
        marks.append(len(lists))

    def touch() -> None:
        pointer = runs.pointer_path(fleet["records"][0]["run_id"])
        os.utime(pointer, None)

    _drive([touch, lambda: None, lambda: None], on_boundary=on_boundary)

    assert len(marks) >= 3, marks
    assert marks[1] - marks[0] == 1, "the touched pointer must pull exactly one sweep"
    assert marks[2] == marks[1], "the beat after it stays quiet again"


def test_a_death_is_delivered_within_one_beat_with_no_file_change(fleet) -> None:
    """A run that dies with every file still must still transition."""
    state = fleet["state"]

    def kill() -> None:
        state["alive"] = False

    events = _drive([kill, lambda: None, lambda: None], on_boundary=lambda: None)
    transitions = [event for event in events if event.get("event") == "transition"]

    assert transitions, "a death with no file moved must still be delivered"
    assert {event.get("run_id") for event in transitions} == {
        record["run_id"] for record in fleet["records"]
    }
