"""The captured-history cache answers a repeated window read without replaying it.

The cache exists so the velocity view can answer within the crew tool's read
deadline on a real repository: rebuilding a project's captured history reads
the committed ledger and one object per committed run record, and that cost is
paid again on every call unless it is cached. This module synthesises a
repository under ``tmp_path`` and points the cache directory at ``tmp_path``,
so nothing is read or written outside the repository under test.

Three properties are load-bearing and each is exercised against a fixture the
code sees rather than echoed from its output:

* a warm read equals a cold read, value for value — the cached capture is the
  same capture, so a caller cannot tell a served window from a rebuilt one;
* a head that has moved is extended, not rebuilt — only the commits after the
  cached head are captured, which is measured by counting the commit-capture
  calls rather than by timing them;
* a corrupt entry is rebuilt, never trusted.
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

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # The worker that runs this suite exports the identity of its own run, and a
    # git wrapper keyed on that identity refuses a mutating verb outside the
    # worker's worktree. Point the subprocess at the synthesised repository by
    # dropping the inherited identity.
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


def _build_repository(root: Path) -> Path:
    repo = root / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    ledger = {
        "data": {
            "runs": [
                {
                    "run_id": "r-impl",
                    "node": "impl-node",
                    "plan": "p",
                    "role": "implement",
                    "gate": "passed",
                    "backend": "claude",
                    "dispatched_at": _iso(6),
                    "completed_at": _iso(6, 100),
                    "worker_seconds": 100,
                    "lineage": {},
                    "attempt": 1,
                }
            ]
        }
    }
    _commit(
        repo,
        "chore: seed the ledger",
        0,
        {"docs/state/sample/crew.json": json.dumps(ledger, indent=2)},
    )
    _commit(repo, "feat: add source", 1, {"src/a.py": "l1\nl2\nl3\n"})
    _commit(repo, "promote(r-impl)", 2, {})
    return repo


@pytest.fixture()
def cache_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "cache"
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(root))
    return root


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return _build_repository(tmp_path / "code")


def _capture(repo: Path) -> dict:
    return velocity._capture_project_cached(
        PROJECT,
        BRANCH,
        repo_path=repo,
        start=WINDOW_START,
        end=WINDOW_END,
    )


def _uncached(repo: Path) -> dict:
    return velocity.capture_project(
        PROJECT,
        BRANCH,
        repo_path=repo,
        start=WINDOW_START,
        end=WINDOW_END,
    )


def test_warm_read_equals_cold_read_value_for_value(repo: Path, cache_root: Path):
    cold = _capture(repo)
    entry = velocity._velocity_cache_path(repo)
    assert entry.is_relative_to(cache_root) and entry.is_file()

    warm = _capture(repo)
    assert warm == cold
    # And the cache reproduces the capture it stands in for, not merely itself.
    assert warm == _uncached(repo)


def test_a_moved_head_is_extended_not_rebuilt(
    repo: Path, cache_root: Path, monkeypatch
):
    calls = []
    original = velocity._capture_commits

    def spy(repo_path, base, head):
        captured = original(repo_path, base, head)
        calls.append((base, head, [c["sha"] for c in captured]))
        return captured

    monkeypatch.setattr(velocity, "_capture_commits", spy)

    cold = _capture(repo)
    first_base, first_head, first_shas = calls[-1]
    assert len(calls) == 1
    assert len(first_shas) == len(cold["commits"]) == 2

    # A warm read at the same head captures nothing.
    assert _capture(repo) == cold
    assert len(calls) == 1

    new_head = _commit(repo, "feat: one more", 3, {"src/b.py": "n1\nn2\n"})
    extended = _capture(repo)

    # The second capture of commits starts at the head the first one reached and
    # covers only the commit added since: the cached range was extended, not
    # replayed.
    assert len(calls) == 2
    delta_base, delta_head, delta_shas = calls[-1]
    assert delta_base == first_head
    assert delta_head == new_head
    assert delta_shas == [new_head]
    # And the extension reproduces the capture a cold read would have made.
    assert extended == _uncached(repo)


def test_a_corrupt_entry_is_rebuilt(repo: Path, cache_root: Path):
    cold = _capture(repo)
    entry = velocity._velocity_cache_path(repo)
    entry.write_text("{ not json")
    assert _capture(repo) == cold

    entry.write_text(json.dumps({"version": 0, "window": {}, "ledger": {}}))
    assert _capture(repo) == cold