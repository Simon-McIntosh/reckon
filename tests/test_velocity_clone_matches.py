"""The velocity view counts promotion's recorded clone matches per week.

The ledger row carries a ``clone_matches`` field: a measured run carries the
list of matches the detector found (empty when its functions copied nothing),
and an unmeasured run carries the ``{"status": "unmeasured", ...}`` marker, so
the two never read alike. This module drives a synthesised repository whose
ledger spans two ISO weeks — one run with two matches and one measured-empty run
in the first week, one unmeasured run in the second — and asserts the per-week
figures ``velocity.report`` derives from that field. Every expectation is
hand-computable from the fixture, so a counter that folds the unmeasured state
into the measured-empty count moves a figure and fails the assertion rather than
passing silently.
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
# The window spans ISO weeks 2026-W36 (Mon 2026-08-31) and 2026-W37 (Mon
# 2026-09-07), ending before W38's Monday so exactly two cells are produced.
WINDOW_START = velocity.iso(BASE + 1 * DAY)  # 2026-09-02, a Wednesday
WINDOW_END = velocity.iso(BASE + 12 * DAY)  # 2026-09-13, a Sunday
PROJECT = "sample"
BRANCH = "main"


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
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)


def _match(run_path: str, run_line: int, existing_path: str) -> dict:
    return {
        "run_function": {"path": run_path, "line": run_line, "name": "f"},
        "existing_function": {"path": existing_path, "line": 10, "name": "parse_utc"},
        "window_line": 1,
    }


def _row(run_id: str, day: int, clone_matches: object) -> dict:
    return {
        "run_id": run_id,
        "node": run_id + "-node",
        "plan": "p",
        "role": "implement",
        "gate": "passed",
        "backend": "claude",
        "dispatched_at": _iso(day),
        "completed_at": _iso(day, 100),
        "worker_seconds": 100,
        "lineage": {},
        "attempt": 1,
        "clone_matches": clone_matches,
    }


def _build_repository(root: Path) -> Path:
    repo = root / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    ledger = {
        "data": {
            "runs": [
                _row(
                    "r-two",
                    1,
                    [
                        _match("reckon/x.py", 4, "reckon/_timestamps.py"),
                        _match("reckon/x.py", 20, "reckon/other.py"),
                    ],
                ),
                _row("r-empty", 3, []),
                _row(
                    "r-unmeasured",
                    8,
                    {"status": "unmeasured", "reason": "no revision pair"},
                ),
            ]
        }
    }
    # The ledger is committed before the window opens so no window commit lands
    # a docs/state line that would be read as a clone report.
    _commit(
        repo,
        "chore: seed the ledger",
        0,
        {"docs/state/sample/crew.json": json.dumps(ledger, indent=2)},
    )
    _commit(repo, "promote(r-two)", 1, {})
    _commit(repo, "promote(r-empty)", 3, {})
    _commit(repo, "promote(r-unmeasured)", 8, {})
    return repo


@pytest.fixture()
def report(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(tmp_path / "cache"))
    repo = _build_repository(tmp_path / "code")
    return velocity.report(
        {PROJECT: str(repo)},
        start=WINDOW_START,
        end=WINDOW_END,
        run_store_db=None,
    )


def test_clone_matches_are_counted_per_iso_week(report: dict):
    weeks = report["clones"]["weeks"]
    assert [week["iso_week"] for week in weeks] == ["2026-W36", "2026-W37"]
    assert weeks[0] == {
        "iso_week": "2026-W36",
        "week_start": "2026-08-31",
        "runs_with_match": 1,
        "matches": 2,
        "runs_no_match": 1,
        "runs_unmeasured": 0,
    }
    assert weeks[1] == {
        "iso_week": "2026-W37",
        "week_start": "2026-09-07",
        "runs_with_match": 0,
        "matches": 0,
        "runs_no_match": 0,
        "runs_unmeasured": 1,
    }


def test_an_unmeasured_run_is_not_a_measured_run_with_no_match(report: dict):
    # The second week holds one run and it carries the unmeasured marker, which
    # must land in runs_unmeasured and leave runs_no_match at zero; folding the
    # two states together moves both and fails here.
    week = report["clones"]["weeks"][1]
    assert week["runs_unmeasured"] == 1
    assert week["runs_no_match"] == 0
    assert week["runs_with_match"] == 0
    assert week["matches"] == 0
