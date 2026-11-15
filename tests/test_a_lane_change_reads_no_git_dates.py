"""A lane change re-plans without a git history or last-modified lookup.

A lane change re-plans the live run it moves. That re-plan reaches
``plan_dispatch`` by importing it straight from the plan module, so it never
passes through the preview wrapper that enters the suppression scope, and a
document age is a display value on this path as it is on a preview. Each
history query is bounded at ten seconds, so two of them can hold a move for
twenty.

The first case records the suppression flag at the moment the re-plan is
called, so removing the scope reddens it. The second records the flag in the
thread that did not enter the scope while the scope is open, which is what
separates a per-context flag from a module global that every reader shares.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import serve
from reckon.crew import CrewError, dispatch_sessions, runs

PROJECT = "lane-change-fixture"
RUN_ID = "r-20260101T000000000000-lane-change"


def _pointer(repository: Path, worktree: Path) -> dict:
    return {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": "coordinator-fixture",
        "repo": str(repository),
        "worktree": str(worktree),
        "base_sha": "HEAD",
        "backend": "from-lane",
        "launch": "cli",
        "phase": "working",
        "node": {
            "id": "lane-change-node",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }


def _refusing_resolution() -> SimpleNamespace:
    """A resolution the lane change refuses on, so the call stops there."""
    return SimpleNamespace(validation=SimpleNamespace(ok=False, findings=[]))


def test_a_lane_change_replans_inside_the_suppressed_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repository.mkdir()
    worktree.mkdir()
    runs._write_json(runs.pointer_path(RUN_ID), _pointer(repository, worktree))

    seen: list[bool] = []

    def recorded(**kwargs: object) -> SimpleNamespace:
        seen.append(bool(serve._DISCOVERY_WITHOUT_GIT_DATES.get()))
        return _refusing_resolution()

    monkeypatch.setattr(dispatch_sessions, "plan_dispatch", recorded)

    with pytest.raises(CrewError):
        dispatch_sessions.change_lane(
            RUN_ID,
            "to-lane",
            "the lane's window cannot hold the run",
            config={"backends": {}},
            launch=False,
        )

    assert seen == [True], (
        "a lane change re-plans inside the suppression scope, so it makes no "
        f"git history or last-modified lookup: {seen}"
    )


def test_the_suppression_does_not_leak_across_contexts() -> None:
    inside: list[bool] = []
    started = threading.Event()
    release = threading.Event()

    def worker() -> None:
        with serve.discovery_without_git_dates():
            inside.append(bool(serve._DISCOVERY_WITHOUT_GIT_DATES.get()))
            started.set()
            release.wait(10)

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(10), "the worker entered the scope"
    outside = bool(serve._DISCOVERY_WITHOUT_GIT_DATES.get())
    release.set()
    thread.join(10)

    assert inside == [True], "the scope reads as set inside the caller"
    assert outside is False, (
        "the suppression is per context, so a reader on another path does not "
        "inherit it"
    )
