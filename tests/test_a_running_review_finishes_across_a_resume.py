"""A resume withdraws only a review that has not started.

When a resume moves a run's head, the review queued against the superseded
revision no longer speaks for it and is recorded withdrawn — but only if its
worker never launched. A review whose worker already launched is running, and
it finishes: its verdict reaches the run's new head through the carry-forward
rule when the move changes no runtime source, and it earns a light re-review of
the new commits alone when the move does change runtime source. Withdrawing a
launched review is what loses a verdict.

Every case is driven end to end against a real git history, with the launched
review's own launch record written where the supervisor writes it.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "running-review-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

LOCAL_BACKEND = "clive"
OTHER_BACKEND = "delta"

pytestmark = pytest.mark.arms_watch_producer


def _backend(command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }


CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {
        LOCAL_BACKEND: _backend("codex"),
        OTHER_BACKEND: _backend("claude"),
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, paths: dict[str, str], message: str) -> str:
    for name, body in paths.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "add", *paths], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A repository, an isolated crew home and a reviewed implement run."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    reviewed = _commit(
        repo,
        {
            "pkg/mod.py": "VALUE = 1\n",
            "docs/plans/fixture-plan.html": (
                '<meta name="docs-project" content="' + PROJECT + '">'
                '<meta name="reckon-type" content="plan">'
                '<meta name="plan-slug" content="fixture-plan">'
                '<h2 id="s2">A resume withdraws only what never started</h2>'
            ),
        },
        "feat: the reviewed change",
    )
    (config_home / "mounts.json").write_text(
        '{"' + PROJECT + '": "' + str(repo / "docs") + '"}'
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    monkeypatch.setattr(
        review_module,
        "review_store_root",
        lambda base_dir=None: config_home / "reviews",
    )
    return {"config_home": config_home, "repo": repo, "reviewed": reviewed}


def _write_run(
    world: dict, *, run_id: str, head: str, extra: dict | None = None
) -> dict:
    """One complete, unpromoted implement run whose worktree is the repo."""
    repo = world["repo"]
    manifest = world["config_home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "write_paths": ["pkg/mod.py"],
        },
    }
    pointer.update(extra or {})
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _write_review_worker_record(review_run_id: str) -> None:
    """Write the launch record a supervised review worker leaves at spawn."""
    directory = runs.run_dir(review_run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.WORKER_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": review_run_id,
                "attempt": 1,
                "pid": os.getpid(),
                "launched_at": OBSERVED_AT.isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )


def _store_review(run_id: str, *, base: str, head: str) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    stored = review_module.parse_review(emitted)
    stored.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(stored)


def _resume_stamp(minutes_ago: int) -> dict:
    return {
        "trigger": "the run was resumed",
        "at": (OBSERVED_AT - timedelta(minutes=minutes_ago)).isoformat(),
    }


def _dispatch_record(head: str, *, review_run_id: str, at_minutes_ago: int) -> dict:
    return {
        "status": "dispatched",
        "reason": "the review dispatched automatically",
        "run_id": review_run_id,
        "backend": LOCAL_BACKEND,
        "head": head,
        "at": (OBSERVED_AT - timedelta(minutes=at_minutes_ago)).isoformat(),
        "attempt": 1,
    }


def test_a_queued_not_launched_review_is_withdrawn_when_the_resume_moves_the_head(
    world,
):
    repo = world["repo"]
    reviewed = world["reviewed"]
    moved = _commit(repo, {"later.txt": "more\n"}, "feat: the run moved on")
    run_id = "r-resumed-queued-review"
    review_run_id = "r-review-queued-not-launched"
    _write_run(
        world,
        run_id=run_id,
        head=moved,
        extra={
            recovery.REVIEW_DISPATCH_FIELD: _dispatch_record(
                reviewed, review_run_id=review_run_id, at_minutes_ago=30
            ),
            "auto_resume": _resume_stamp(2),
        },
    )
    record = runs.read_pointer(run_id)

    report = recovery.withdraw_superseded_review(record)

    assert report is not None and report["withdrawn"] is True
    assert report["review_run_id"] == review_run_id
    fresh = runs.read_pointer(run_id)
    assert fresh[recovery.REVIEW_DISPATCH_FIELD]["status"] == "withdrawn"


def test_a_launched_review_is_not_withdrawn_and_its_verdict_carries_forward(world):
    repo = world["repo"]
    reviewed = world["reviewed"]
    run_id = "r-resumed-launched-data-move"
    review_run_id = "r-review-launched-running"
    _write_run(
        world,
        run_id=run_id,
        head=reviewed,
        extra={
            recovery.REVIEW_DISPATCH_FIELD: _dispatch_record(
                reviewed, review_run_id=review_run_id, at_minutes_ago=30
            ),
            "auto_resume": _resume_stamp(2),
        },
    )
    _write_review_worker_record(review_run_id)
    _store_review(run_id, base=reviewed, head=reviewed)
    new_head = _commit(
        repo, {"data/record.txt": "payload\n"}, "chore(data): land a record"
    )
    record = runs.read_pointer(run_id)

    # The review's worker launched, so the resume withdraws nothing: the running
    # review finishes and its verdict stands for the head it read.
    assert recovery.withdraw_superseded_review(record) is None
    fresh = runs.read_pointer(run_id)
    assert fresh[recovery.REVIEW_DISPATCH_FIELD]["status"] == "dispatched"

    # The data-only move carries that verdict to the new head, so the run raises
    # no review requirement at the revision it now carries.
    report = recovery.carry_review_forward(fresh)
    assert report is not None and report["carried"] is True
    assert report["scope"] == ["data/record.txt"]
    carried = review_module.read_review(PROJECT, run_id, reviewed_head_sha=new_head)
    assert carried is not None
    assert recovery.same_revision(carried["reviewed_head_sha"], new_head)


def test_a_launched_review_over_a_source_move_earns_a_light_re_review(
    world, monkeypatch: pytest.MonkeyPatch
):
    repo = world["repo"]
    reviewed = world["reviewed"]
    run_id = "r-resumed-launched-source-move"
    review_run_id = "r-review-launched-running-source"
    _write_run(
        world,
        run_id=run_id,
        head=reviewed,
        extra={
            recovery.REVIEW_DISPATCH_FIELD: _dispatch_record(
                reviewed, review_run_id=review_run_id, at_minutes_ago=30
            ),
            "auto_resume": _resume_stamp(2),
        },
    )
    _write_review_worker_record(review_run_id)
    _store_review(run_id, base=reviewed, head=reviewed)
    _commit(repo, {"pkg/mod.py": "VERSION = 2\n"}, "fix: a later source commit")
    record = runs.read_pointer(run_id)

    # The launched review is not withdrawn; because the move changes runtime
    # source it cannot be carried, so the new commits alone earn a light review.
    assert recovery.withdraw_superseded_review(record) is None

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    worktree = world["config_home"].parent / "worktrees" / run_id
    worktree.mkdir(parents=True, exist_ok=True)

    def prepare(_repo, _session, _node, base):
        return {
            "path": str(worktree),
            "base": base,
            "base_sha": _git(repo, "rev-parse", "HEAD"),
        }

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare)

    from reckon import crew

    try:
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            report = recovery.dispatch_review_for_run(
                record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
            )
    finally:
        if crew.watch_state(PROJECT)["watcher_live"]:
            recovery.unwatch(PROJECT)

    assert report["dispatched"] is True, report.get("reason")
    assert report["review_tier"] == "light"
    assert report["scope"] == ["pkg/mod.py"]