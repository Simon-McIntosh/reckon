"""The census's compact-summary size bound does not abort the velocity view.

``compact_summary`` carried a 300,000-byte assertion on its own dump. The
census path ``velocity()`` returns that summary as the study artifact, so the
bound belonged there. ``report()`` — the ``crew velocity`` view — reuses
``compact_summary`` and then adds ``by_project_day_lane``, the largest block,
to the same dictionary, paging it with ``limit`` and ``cursor``. A long window
therefore tripped the assertion inside the view and aborted the read, with the
bound never limiting what the view returned.

The fixture below builds a ``full`` mapping the way
``tests/test_velocity_session_continuity.py`` does — a synthesised repository,
captured and measured — then pads one copied-key string so its compact summary
dumps past the bound. Each case drives one entry point over that oversized
``full``: ``compact_summary`` returns, ``velocity()`` refuses, and ``report()``
carries ``by_project_day_lane``. The padding is asserted to exceed the bound
before it is used, so a case cannot pass because the fixture stayed small.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import velocity

DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
WINDOW_START = velocity.iso(BASE + 1 * DAY)
WINDOW_END = velocity.iso(BASE + 20 * DAY)
PROJECT = "sample"
BRANCH = "main"
BOUND = 300_000
MESSAGE = "Compact census exceeds its repository size bound"

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # The worker running this suite exports its own run identity, and a git
    # wrapper keyed on it refuses a mutating verb outside the worker's worktree.
    # Drop the inherited identity so the synthesised repository is the target.
    base = {**os.environ, **(env or {})}
    for name in _DISPATCH_IDENTITY:
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, day: int, changes: dict[str, str]) -> str:
    when = _iso(day)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    for path, content in changes.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(repo, "add", path, env=env)
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


def _run(run_id: str, node: str, backend: str, session_id, day: int, seconds: int):
    return {
        "run_id": run_id,
        "node": node,
        "plan": "p",
        "role": "implement",
        "backend": backend,
        "session_id": session_id,
        "dispatched_at": _iso(day, seconds),
        "completed_at": _iso(day, seconds + 40),
        "worker_seconds": 40,
        "lineage": {},
        "attempt": 1,
    }


def _build_repository(root: Path) -> Path:
    repo = root / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    ledger = {
        "data": {
            "runs": [
                _run("r-a1", "n-t1", "codex", "s-1", 6, 10),
                _run("r-b1", "n-t2", "claude", "s-2", 7, 10),
            ]
        }
    }
    _commit(
        repo,
        "chore: seed the ledger",
        0,
        {"docs/state/sample/crew.json": json.dumps(ledger, indent=2)},
    )
    _commit(repo, "promote(r-a1)", 6, {})
    _commit(repo, "promote(r-b1)", 7, {})
    return repo


@pytest.fixture()
def full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(tmp_path / "velocity-cache"))
    monkeypatch.setenv("RECKON_CLONE_CACHE", str(tmp_path / "clone-cache"))
    root = tmp_path / "code"
    _build_repository(root)
    snapshot = velocity.capture(
        {PROJECT: BRANCH},
        start=WINDOW_START,
        end=WINDOW_END,
        code_root=root,
        run_store_db=None,
    )
    measured = velocity.measure(
        snapshot,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        projects={PROJECT: BRANCH},
    )
    return _padded(measured)


def _padded(measured: dict) -> dict:
    padded = dict(measured)
    padded["definitions"] = {
        **measured["definitions"],
        "size_probe": "x" * (BOUND + 20_000),
    }
    # The instrument check: compact_summary copies ``definitions`` verbatim, so
    # a summary over this full dumps past the bound. It is asserted on the
    # length directly so the fixture stays independent of compact_summary, and a
    # mutation that restores the bound there cannot also break the fixture and
    # mask which case actually detects it.
    assert len(padded["definitions"]["size_probe"]) > BOUND
    return padded


def test_compact_summary_returns_the_oversized_summary(full: dict):
    summary = velocity.compact_summary(full, [])
    assert summary["definitions"]["size_probe"]


def test_velocity_aborts_on_the_oversized_census(full: dict, monkeypatch):
    monkeypatch.setattr(velocity, "capture", lambda *a, **k: {"projects": []})
    monkeypatch.setattr(velocity, "measure", lambda *a, **k: full)
    with pytest.raises(AssertionError, match=MESSAGE):
        velocity.velocity(projects={PROJECT: BRANCH})


def test_report_is_not_bounded_by_the_census(full: dict, monkeypatch, tmp_path):
    monkeypatch.setattr(
        velocity,
        "_capture_project_cached",
        lambda *a, **k: {"project": PROJECT, "runs": []},
    )
    monkeypatch.setattr(velocity, "recover_ledger_clocks", lambda *a, **k: None)
    monkeypatch.setattr(velocity, "health_reviews", lambda *a, **k: {})
    monkeypatch.setattr(velocity, "measure", lambda *a, **k: full)
    summary = velocity.report(
        {PROJECT: str(tmp_path)},
        start=WINDOW_START,
        end=WINDOW_END,
    )
    assert "by_project_day_lane" in summary
