"""A resume withdraws the review of the head it replaced.

A review composed for the head a run delivered, whose dispatch record is
written after the resume that replaced that head, speaks for a revision the run
has moved past: the reflex records it withdrawn even though its record reads as
newer than the resume. Ordering by the record's time keeps it standing, because
the record is written at the end of the compose and a resume can land inside
that window.

A review whose worker already launched finishes and keeps its verdict. A review
composed for the head the resume itself settles stands untouched.

Every case is driven against a real git history and a temporary crew home, so
the head the withdrawal reads is a revision the run's own tree carries.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon._timestamps import parse_utc
from reckon.crew import recovery, runs
from reckon.crew.node import CrewError

dispatch_module = importlib.import_module("reckon.crew.recovery_review_dispatch")

PROJECT = "replaced-head-fixture"
SESSION = "coordinator-fixture"

pytestmark = pytest.mark.arms_watch_producer


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
    """A repository, an isolated crew home and the head a run delivered."""
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
    delivered = _commit(
        repo,
        {
            "pkg/mod.py": "VALUE = 1\n",
            "docs/plans/fixture-plan.html": (
                '<meta name="docs-project" content="' + PROJECT + '">'
                '<meta name="reckon-type" content="plan">'
                '<meta name="plan-slug" content="fixture-plan">'
                '<h2 id="s2">A resume withdraws the review it replaced</h2>'
            ),
        },
        "feat: the reviewed change",
    )
    (config_home / "mounts.json").write_text(
        '{"' + PROJECT + '": "' + str(repo / "docs") + '"}'
    )
    return {"config_home": config_home, "repo": repo, "delivered": delivered}


def _write_manifest(
    world: dict, *, run_id: str, head: str, written_at: datetime
) -> Path:
    """One delivered manifest, stamped at the instant the fixture names.

    The write time decides whether a manifest belongs to the attempt a resume
    started, so it is set rather than left at the test's own clock: a manifest
    written before the resume is the pre-resume delivery the run was resumed
    from.
    """
    manifest = world["config_home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    stamp = written_at.timestamp()
    os.utime(manifest, (stamp, stamp))
    return manifest


def _write_run(
    world: dict, *, run_id: str, head: str, extra: dict | None = None
) -> dict:
    repo = world["repo"]
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "write_paths": ["pkg/mod.py"],
        },
    }
    pointer.update(extra or {})
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _write_queued_review(review_run_id: str) -> None:
    runs._write_json(
        runs.pointer_path(review_run_id),
        {
            "run_id": review_run_id,
            "project": PROJECT,
            "session": SESSION,
            "classification": "queued",
            "role": "review",
            "node": {"id": review_run_id, "plan": "fixture-plan"},
        },
    )


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
                "launched_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )


def _resume_superseded(head: str, at: datetime) -> dict:
    return {"head": head, "at": at.isoformat()}


def _dispatch_record(head: str, *, review_run_id: str, at: datetime) -> dict:
    return {
        "status": "dispatched",
        "reason": "the review dispatched automatically",
        "run_id": review_run_id,
        "head": head,
        "at": at.isoformat(),
        "attempt": 1,
    }


def test_a_review_recorded_after_the_resume_is_withdrawn(world):
    """Sequence 2: the resume lands inside the compose, the record follows it."""
    run_id = "r-resumed-with-post-resume-review"
    review_run_id = "r-review-of-the-replaced-head"
    delivered = world["delivered"]
    resumed_at = datetime.now(tz=UTC)
    # The run was delivered before the resume; the resume replaces that head.
    _write_manifest(
        world,
        run_id=run_id,
        head=delivered,
        written_at=resumed_at - timedelta(minutes=30),
    )
    # The review's dispatch record is written after the resume — the ordering a
    # record-time comparison reads as "newer than the resume" and keeps.
    recorded_at = resumed_at + timedelta(minutes=1)
    _write_run(
        world,
        run_id=run_id,
        head=delivered,
        extra={
            dispatch_module.RESUME_SUPERSEDED_HEAD: _resume_superseded(
                delivered, resumed_at
            ),
            recovery.REVIEW_DISPATCH_FIELD: _dispatch_record(
                delivered, review_run_id=review_run_id, at=recorded_at
            ),
        },
    )
    _write_queued_review(review_run_id)
    record = runs.read_pointer(run_id)
    assert parse_utc(record[recovery.REVIEW_DISPATCH_FIELD]["at"]) > resumed_at

    report = recovery.withdraw_superseded_review(record)

    assert report is not None and report["withdrawn"] is True
    assert report["review_run_id"] == review_run_id
    fresh = runs.read_pointer(run_id)
    assert fresh[recovery.REVIEW_DISPATCH_FIELD]["status"] == "withdrawn"
    assert (
        "superseded by that resume" in fresh[recovery.REVIEW_DISPATCH_FIELD]["reason"]
    )
    # The queued attempt's own pointer is discarded.
    with pytest.raises(CrewError):
        runs.read_pointer(review_run_id)


def test_a_review_of_the_resumed_head_is_not_withdrawn(world):
    """The resume settles a new head, and the review composed for it stands."""
    run_id = "r-resumed-then-redelivered"
    review_run_id = "r-review-of-the-resumed-head"
    delivered = world["delivered"]
    resumed_at = datetime.now(tz=UTC)
    new_head = _commit(
        world["repo"], {"pkg/mod.py": "VALUE = 2\n"}, "fix: carry the resume on"
    )
    _write_manifest(
        world,
        run_id=run_id,
        head=new_head,
        written_at=resumed_at + timedelta(minutes=2),
    )
    _write_run(
        world,
        run_id=run_id,
        head=new_head,
        extra={
            dispatch_module.RESUME_SUPERSEDED_HEAD: _resume_superseded(
                delivered, resumed_at
            ),
            recovery.REVIEW_DISPATCH_FIELD: _dispatch_record(
                new_head,
                review_run_id=review_run_id,
                at=resumed_at + timedelta(minutes=3),
            ),
        },
    )
    _write_queued_review(review_run_id)

    assert recovery.withdraw_superseded_review(runs.read_pointer(run_id)) is None

    fresh = runs.read_pointer(run_id)
    assert fresh[recovery.REVIEW_DISPATCH_FIELD]["status"] == "dispatched"


def test_a_launched_review_is_not_withdrawn_by_a_later_resume(world):
    """A review whose worker already spawned is running, and it finishes."""
    run_id = "r-resumed-over-a-launched-review"
    review_run_id = "r-review-launched-before-resume"
    delivered = world["delivered"]
    resumed_at = datetime.now(tz=UTC)
    _write_manifest(
        world,
        run_id=run_id,
        head=delivered,
        written_at=resumed_at - timedelta(minutes=30),
    )
    _write_run(
        world,
        run_id=run_id,
        head=delivered,
        extra={
            dispatch_module.RESUME_SUPERSEDED_HEAD: _resume_superseded(
                delivered, resumed_at
            ),
            recovery.REVIEW_DISPATCH_FIELD: _dispatch_record(
                delivered,
                review_run_id=review_run_id,
                at=resumed_at + timedelta(minutes=1),
            ),
        },
    )
    _write_queued_review(review_run_id)
    _write_review_worker_record(review_run_id)

    assert recovery.withdraw_superseded_review(runs.read_pointer(run_id)) is None

    fresh = runs.read_pointer(run_id)
    assert fresh[recovery.REVIEW_DISPATCH_FIELD]["status"] == "dispatched"
