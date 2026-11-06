"""A repair round reads each finding's stated severity and what it is worth.

A finding states its severity in a field or by opening its claim with the
severity and a colon, and both spellings reach the one reader the repair reflex
and the landing gate share. A finding that states neither is repaired as
blocking but is reported apart, and a head the integration branch has already
taken in commissions no round at all.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew import recovery, repair, resumption, review, runs
from reckon.crew.recovery_review_acceptance import _finding_blocks

PROJECT = "repair-severity-fixture"
RUN_ID = "r-20260101T000000000000-reviewed-run"
REVIEW_RUN_ID = "r-20260101T010000000000-review-run"
POST_AUDIT = "2026-11-01T00:00:00Z"
CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "fixture",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _seed(repo: Path) -> str:
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="repair-severity-fixture">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">Repair findings</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", "docs/plans/fixture.html"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(repo, *args)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def round_fixture(tmp_path: Path, isolated_reckon_home: Path) -> dict[str, Any]:
    assert isolated_reckon_home.is_relative_to(tmp_path.parent)
    repo = tmp_path / "repo"
    head = _seed(repo)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    manifest = isolated_reckon_home / "manifest.md"
    manifest.write_text("node: reviewed\nstatus: complete\n", encoding="utf-8")
    pointer = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(repo),
        "role": "implement",
        "node": {"id": "reviewed", "plan": "fixture", "section": "s2"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(RUN_ID), pointer)
    return {
        "home": isolated_reckon_home,
        "repo": repo,
        "head": head,
        "pointer": pointer,
    }


def _store(
    fixture: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    head: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    reviewed_head = head or fixture["head"]
    record: dict[str, Any] = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "review_run_id": REVIEW_RUN_ID,
        "reviewed_base_sha": reviewed_head,
        "reviewed_head_sha": reviewed_head,
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 15),
        "findings": findings,
    }
    if timestamp:
        record["timestamp"] = timestamp
    stored_path = review.store_review(record)
    assert stored_path.is_relative_to(fixture["home"])
    return record


def _round(fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    resumed: list[str] = []
    dispatched: list[dict[str, Any]] = []
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        resumed.append(advice)
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return {"run_id": "r-repair-stub"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        result = recovery.dispatch_repair_for_run(
            fixture["pointer"], config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )
    return result, resumed


NON_BLOCKING_PREFIXED = [
    {
        "file": "reckon/crew/first.py",
        "line": "10",
        "text": "non-blocking: a naming preference",
    },
    {
        "file": "reckon/crew/second.py",
        "line": "20",
        "text": "non-blocking: a follow-on elsewhere",
    },
    {
        "file": "reckon/crew/third.py",
        "line": "30",
        "text": "non-blocking: a note for the coordinator",
    },
]


@pytest.mark.arms_watch_producer
def test_prefixed_non_blocking_findings_start_no_round(round_fixture, monkeypatch):
    """A severity stated in the claim is read by the reflex and the gate alike."""
    _store(round_fixture, NON_BLOCKING_PREFIXED)

    result, resumed = _round(round_fixture, monkeypatch)

    assert result["dispatched"] is False
    assert result.get("resumed") is not True
    assert resumed == []
    assert "no blocking finding" in result["reason"]
    for finding in NON_BLOCKING_PREFIXED:
        assert _finding_blocks(finding) is False


@pytest.mark.arms_watch_producer
def test_an_undisposed_blocking_finding_starts_the_round(round_fixture, monkeypatch):
    _store(
        round_fixture,
        [
            {
                "file": "reckon/crew/first.py",
                "line": "10",
                "text": "a defect",
                "severity": "blocking",
            }
        ],
    )

    result, resumed = _round(round_fixture, monkeypatch)

    assert result["resumed"] is True
    assert len(resumed) == 1
    assert "found 1 blocking finding(s)" in resumed[0]


@pytest.mark.arms_watch_producer
def test_a_reviewed_head_the_branch_moved_past_starts_no_round(
    round_fixture, monkeypatch
):
    """A head main has already taken in is repaired by nobody."""
    repo = round_fixture["repo"]
    reviewed_head = round_fixture["head"]
    tree = repo.parent / "reviewed-tree"
    _git(repo, "worktree", "add", "--detach", str(tree), reviewed_head)
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-q", "-m", "chore: integrate the reviewed head")
    round_fixture["pointer"]["worktree"] = str(tree)
    crew._write_json(crew.pointer_path(RUN_ID), round_fixture["pointer"])
    _store(
        round_fixture,
        [
            {
                "file": "reckon/crew/first.py",
                "line": "10",
                "text": "a defect",
                "severity": "blocking",
            }
        ],
        head=reviewed_head,
    )

    result, resumed = _round(round_fixture, monkeypatch)

    assert result["dispatched"] is False
    assert result.get("integrated") is True
    assert resumed == []
    assert reviewed_head[:12] in result["reason"]


@pytest.mark.arms_watch_producer
def test_an_unclassified_finding_is_counted_apart_and_named(round_fixture, monkeypatch):
    blocking = {
        "file": "reckon/crew/first.py",
        "line": "10",
        "text": "a defect",
        "severity": "blocking",
    }
    unclassified = {
        "file": "reckon/crew/second.py",
        "line": "20",
        "text": "a note carrying neither a severity nor a prefix",
    }
    _store(round_fixture, [blocking, unclassified], timestamp=POST_AUDIT)

    result, resumed = _round(round_fixture, monkeypatch)

    assert result["resumed"] is True
    assert len(resumed) == 1
    advice = resumed[0]
    assert "found 1 blocking finding(s)" in advice
    assert "found 2 blocking finding(s)" not in advice
    assert "unclassified" in advice
    assert repair.finding_id(unclassified) in advice.split("unclassified", 1)[1]
