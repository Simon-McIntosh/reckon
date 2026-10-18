"""The velocity view answers for every scope, including a quiet repository.

``capture_project`` took ``selected[0]`` without checking that the window
selected any commit, so one repository quiet in the window failed the
all-project view with an ``IndexError``. The same index took ``first[-1]`` when
the window started at a repository's first commit, which Python reads as the
last commit rather than as no base, so that first commit's changes were
measured from the wrong revision and counted as zero.

This module drives ``velocity.report`` — the composition the ``*`` view reads —
over three synthesised repositories under ``tmp_path``: one quiet in the window,
one whose first commit is inside it, and one ordinary. Each project's figure is
read from the composed view.

The declared mutation restores the unguarded ``selected[0]`` index, so the
quiet repository raises and its case goes red.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

import pytest

from reckon import velocity

DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
WINDOW_START = velocity.iso(BASE + 10 * DAY)  # 2026-09-11
WINDOW_END = velocity.iso(BASE + 20 * DAY)  # 2026-09-21
BRANCH = "main"

QUIET = "quiet"
FIRST = "first-commit"
ORDINARY = "ordinary"


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # The worker that runs this suite exports the identity of its own run, and a
    # git wrapper keyed on that identity refuses a mutating verb outside the
    # worker's worktree. Point the subprocess at the synthesised repository by
    # dropping the inherited identity.
    base = {**os.environ, **(env or {})}
    for name in ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT"):
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, day: int, changes: dict[str, str]) -> None:
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
    _git(repo, "commit", "-q", "-m", message, env=env)


def _source(lines: int) -> str:
    return "".join(f"line {index}\n" for index in range(lines))


def _build(root: Path) -> dict[str, Path]:
    repos = {}
    for name in (QUIET, FIRST, ORDINARY):
        repo = root / name
        repos[name] = repo
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", BRANCH)
    # Quiet: two commits, both before the window opens, none inside it.
    _commit(repos[QUIET], "feat: early", 1, {"src/early.py": _source(5)})
    _commit(repos[QUIET], "feat: early again", 2, {"src/early.py": _source(6)})
    # First commit inside the window: the window opens at the repository's root,
    # so its changes are measured from the empty tree.
    _commit(repos[FIRST], "feat: first", 12, {"src/first.py": _source(7)})
    _commit(repos[FIRST], "feat: later", 14, {"src/later.py": _source(3)})
    # Ordinary: one commit before the window and one inside it.
    _commit(repos[ORDINARY], "feat: before", 5, {"src/before.py": _source(6)})
    _commit(repos[ORDINARY], "feat: inside", 14, {"src/inside.py": _source(4)})
    return repos


@pytest.fixture()
def view(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    repos = _build(tmp_path / "code")
    return velocity.report(
        {name: str(repo) for name, repo in repos.items()},
        start=WINDOW_START,
        end=WINDOW_END,
        run_store_db=None,
    )


def _cell(view: dict, project: str) -> dict:
    return next(cell for cell in view["by_project"] if cell["project"] == project)


def _added(cell: dict) -> int:
    return sum(line["added"] for line in cell["metrics"]["lines"].values())


def test_the_all_project_view_returns_every_repository(view: dict):
    projects = sorted(cell["project"] for cell in view["by_project"])
    assert projects == sorted([QUIET, FIRST, ORDINARY])
    assert sorted(entry["project"] for entry in view["coverage"]) == projects
    assert len(view["provenance"]["branches"]) == 3


def test_a_quiet_repository_reports_zero_for_every_metric(view: dict):
    cell = _cell(view, QUIET)
    assert _added(cell) == 0
    assert cell["metrics"]["lines"]["source"]["added"] == 0
    assert cell["metrics"]["primary_commits"]["denominator"] == 0
    assert cell["metrics"]["promoted_nodes"]["denominator"] == 0


def test_a_window_starting_at_the_first_commit_measures_from_the_empty_tree(
    view: dict,
):
    cell = _cell(view, FIRST)
    # The root commit's seven source lines are counted from nothing and the
    # later commit's three are added to them. The unguarded index measured from
    # the last commit, an empty range, and reported zero.
    assert cell["metrics"]["lines"]["source"]["added"] == 10


def test_an_ordinary_repository_counts_only_its_windowed_commit(view: dict):
    cell = _cell(view, ORDINARY)
    # Only the in-window commit's four lines count; the six added before the
    # window opens are outside it.
    assert cell["metrics"]["lines"]["source"]["added"] == 4
