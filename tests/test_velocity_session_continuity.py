"""The velocity view reports the per-lane-day share of cross-task session use.

A dispatch continued another task's session when its recorded ``session_id``
equals that of an earlier-dispatched run whose ``(project, plan, node)`` differs.
The fixtures below are synthesised under ``tmp_path`` so this module reads no
live state: a git history, the run ledger committed at its head, and the promote
commits that date each promotion. Every expectation is derived from the fixture
the code sees rather than echoed from the code's own output.

The population carries one case per classification: a same-task resume (not
counted), a cross-task continuation (counted), a run with no recorded
``session_id`` (unmeasured, reported beside the share and never as fresh), and a
fresh session, across two lanes on two days. The same-task case is the load
bearing one: if a same-task resume is ever counted as a continuation the
day-six codex share moves from 0.0 to 0.5, so the assertion below is the check
the declared mutation must fail.
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
    # worker's worktree. Drop the inherited identity so the synthesised
    # repository is always the target, whatever fixture scope happens to run.
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

    # One run per classification. ``r-a1`` opens session ``s-1`` under task
    # (sample, p, n-t1); ``r-a2`` resumes the same task and session on the same
    # day (not a continuation); ``r-b1`` resumes the same session under a
    # different task on the next day (a continuation). ``r-u`` records no
    # session id; ``r-f`` opens a fresh session. Two lanes (codex, claude) on
    # two days (six and seven).
    ledger = {
        "data": {
            "runs": [
                _run("r-a1", "n-t1", "codex", "s-1", 6, 10),
                _run("r-a2", "n-t1", "codex", "s-1", 6, 20),
                _run("r-u", "n-u", "claude", None, 6, 30),
                _run("r-b1", "n-t2", "codex", "s-1", 7, 10),
                _run("r-f", "n-t4", "claude", "s-2", 7, 20),
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
    _commit(repo, "promote(r-a2)", 6, {})
    _commit(repo, "promote(r-u)", 6, {})
    _commit(repo, "promote(r-b1)", 7, {})
    _commit(repo, "promote(r-f)", 7, {})
    return repo


@pytest.fixture()
def full(tmp_path: Path) -> dict:
    root = tmp_path / "code"
    _build_repository(root)
    snapshot = velocity.capture(
        {PROJECT: BRANCH},
        start=WINDOW_START,
        end=WINDOW_END,
        code_root=root,
        run_store_db=None,
    )
    return velocity.measure(
        snapshot,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        projects={PROJECT: BRANCH},
    )


def _cell(full: dict, day: str, lane: str) -> dict:
    for row in full["by_day_lane"]:
        if row["day"] == day and row["lane"] == lane:
            return row["metrics"]["session_continuity"]
    raise AssertionError(f"no cell for {day}/{lane}")


DAY_SIX = _iso(6)[:10]
DAY_SEVEN = _iso(7)[:10]


def test_classifies_each_case():
    runs = [
        {
            "run_id": "r-a1",
            "project": "p",
            "plan": "pl",
            "node": "n1",
            "session_id": "s",
            "dispatched_at": _iso(1, 0),
        },
        {
            "run_id": "r-a2",
            "project": "p",
            "plan": "pl",
            "node": "n1",
            "session_id": "s",
            "dispatched_at": _iso(1, 10),
        },
        {
            "run_id": "r-b1",
            "project": "p",
            "plan": "pl",
            "node": "n2",
            "session_id": "s",
            "dispatched_at": _iso(1, 20),
        },
        {
            "run_id": "r-u",
            "project": "p",
            "plan": "pl",
            "node": "n3",
            "session_id": None,
            "dispatched_at": _iso(1, 30),
        },
    ]
    assert velocity.session_continuity(runs) == {
        "r-a1": "fresh",
        "r-a2": "same_task",
        "r-b1": "continued",
        "r-u": "unmeasured",
    }


def test_same_task_resume_is_not_counted(full: dict):
    # r-a1 opens the session and r-a2 resumes the same task on the same day:
    # neither continues another task's session, so the day-six codex share is
    # zero. Counting the same-task resume would move it to 0.5.
    cell = _cell(full, DAY_SIX, "codex")
    assert cell["denominator"] == 2
    assert cell["continued"] == 0
    assert cell["fresh"] == 1
    assert cell["same_task"] == 1
    assert cell["share"]["value"] == 0.0


def test_cross_task_continuation_is_counted(full: dict):
    cell = _cell(full, DAY_SEVEN, "codex")
    assert cell["denominator"] == 1
    assert cell["continued"] == 1
    assert cell["share"]["value"] == 1.0


def test_two_lanes_on_two_days_report_their_own_shares(full: dict):
    assert _cell(full, DAY_SIX, "claude")["share"]["value"] == 0.0
    assert _cell(full, DAY_SEVEN, "claude")["share"]["value"] == 0.0
    assert _cell(full, DAY_SEVEN, "codex")["share"]["value"] == 1.0


def test_missing_session_id_is_unmeasured_beside_the_share(full: dict):
    cell = _cell(full, DAY_SIX, "claude")
    # r-u records no session id: it counts in the denominator and is reported
    # as unmeasured, never as fresh and never as a continuation.
    assert cell["denominator"] == 1
    assert cell["unmeasured"] == 1
    assert cell["fresh"] == 0
    assert cell["continued"] == 0
    assert cell["share"]["value"] == 0.0


def test_compact_summary_reports_per_lane_day(full: dict):
    summary = velocity.compact_summary(full, [], artifacts=None)
    rows = summary["session_continuity"]
    codex_seven = next(
        r for r in rows if r["day"] == DAY_SEVEN and r["lane"] == "codex"
    )
    assert codex_seven["share"]["value"] == 1.0
    claude_six = next(r for r in rows if r["day"] == DAY_SIX and r["lane"] == "claude")
    assert claude_six["unmeasured"] == 1
