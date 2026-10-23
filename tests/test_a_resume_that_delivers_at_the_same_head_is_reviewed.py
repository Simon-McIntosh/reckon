"""A resumed attempt that delivers at the head it carried settles it.

A resume carries a stopped run forward from the revision it delivered and
withholds the duties that speak for that revision until the run moves past it.
A worker resumed only to record an outcome can finish at the head it already
carried instead, and that delivery is the run's own last word on it: it must
not leave a finished run with no review and no duty, which is exactly the state
the wait would otherwise hold open indefinitely.

Every case runs against a real git history and a temporary crew home, so the
head a duty names is a revision the run's own tree carries and every
environment-resolved directory points at a temporary path. Each manifest's
write time is set explicitly against the resume instant the pointer recorded,
so the ordering the predicate reads is the ordering the case states rather than
whatever the filesystem's clock resolution happened to produce.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module

obligations_module = importlib.import_module("reckon.crew.obligations")
resumption_module = importlib.import_module("reckon.crew.resumption")
dispatch_module = importlib.import_module("reckon.crew.recovery_review_dispatch")

PROJECT = "same-head-delivery-fixture"
SESSION = "coordinator-fixture"

LOCAL_BACKEND = "clive"
OTHER_BACKEND = "delta"

# How far before the resume instant a manifest is placed when the case needs it
# to belong to an earlier attempt, and how far after when it is the resumed
# attempt's own delivery. Both are wide against any clock resolution.
BEFORE_SECONDS = 300
AFTER_SECONDS = 5


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
    (config_home / "mounts.json").write_text(
        '{"' + PROJECT + '": "' + str(repo / "docs") + '"}'
    )
    monkeypatch.setattr(
        review_module,
        "review_store_root",
        lambda base_dir=None: config_home / "reviews",
    )
    return {"tmp": tmp_path, "config_home": config_home, "repo": repo}


def _commit_plan(repository: Path) -> str:
    return _commit(
        repository,
        {
            "pkg/mod.py": "VALUE = 1\n",
            "docs/plans/fixture-plan.html": (
                '<meta name="docs-project" content="' + PROJECT + '">'
                '<meta name="reckon-type" content="plan">'
                '<meta name="plan-slug" content="fixture-plan">'
                '<h2 id="delivery">A resume that delivers at the same head</h2>'
            ),
        },
        "feat: the delivered change",
    )


def _manifest_of(world: dict, run_id: str) -> Path:
    return world["config_home"] / "manifests" / f"{run_id}.md"


def _write_run(world: dict, *, run_id: str, head: str) -> dict:
    """One implement run whose worktree is the repo, with its own manifest.

    The manifest is placed well before any resume the test goes on to record,
    because that is the state a resume begins from: a delivered manifest left by
    the attempt the resume ended.
    """
    repo = world["repo"]
    manifest = _manifest_of(world, run_id)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    _place_manifest(manifest, time.time() - BEFORE_SECONDS)
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
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _resume_stamp(run_id: str) -> float:
    """The instant the pointer recorded its resume at, as epoch seconds."""
    record = runs.read_pointer(run_id)
    stamped = record[dispatch_module.RESUME_SUPERSEDED_HEAD]
    return datetime.fromisoformat(str(stamped["at"])).timestamp()


def _place_manifest(manifest: Path, written_at: float) -> None:
    os.utime(manifest, (written_at, written_at))


def _deliver(world: dict, run_id: str, head: str, *, status: str = "complete") -> None:
    """Rewrite the manifest as the resumed attempt's own verdict."""
    manifest = _manifest_of(world, run_id)
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: [{head}]\n", encoding="utf-8"
    )
    _place_manifest(manifest, _resume_stamp(run_id) + AFTER_SECONDS)


def _kinds(run_id: str) -> list[str]:
    report = obligations_module.obligations(PROJECT, SESSION)
    return sorted(
        str(item["kind"])
        for item in report["obligations"]
        if str(item.get("run_id")) == run_id
    )


def test_a_delivery_at_the_superseded_head_settles_it(world):
    """The cause: the finished resumed attempt was withheld indefinitely."""
    head = _commit_plan(world["repo"])
    run_id = "r-delivered-at-the-same-head"
    _write_run(world, run_id=run_id, head=head)
    assert resumption_module._record_resume_supersedes_head(run_id)
    assert _kinds(run_id) == []

    _deliver(world, run_id, head)
    record = runs.read_pointer(run_id)

    assert dispatch_module.supersedes_head(record, head) is False
    assert _kinds(run_id) == ["review-missing"]
    assert recovery.same_revision(
        str(record[dispatch_module.RESUME_SUPERSEDED_HEAD]["head"]), head
    )


def test_the_reflex_composes_the_review_for_the_delivered_head(world):
    """The reflex composes a round for the head the finished run carries."""
    head = _commit_plan(world["repo"])
    run_id = "r-composed-for-the-same-head"
    _write_run(world, run_id=run_id, head=head)
    resumption_module._record_resume_supersedes_head(run_id)
    _deliver(world, run_id, head)
    record = runs.read_pointer(run_id)

    report = dispatch_module.dispatch_review_for_run(
        record, config=CONFIG, dry_run=True
    )

    assert report.get("superseded_head") is None, report.get("reason")


def test_a_resumed_attempt_still_working_is_withheld(world):
    """A run whose manifest is in flight after the resume is withheld."""
    head = _commit_plan(world["repo"])
    run_id = "r-still-working"
    _write_run(world, run_id=run_id, head=head)
    resumption_module._record_resume_supersedes_head(run_id)
    _deliver(world, run_id, head, status="in-progress")
    record = runs.read_pointer(run_id)

    assert dispatch_module.supersedes_head(record, head) is True
    assert _kinds(run_id) == []

    report = dispatch_module.dispatch_review_for_run(
        record, config=CONFIG, dry_run=True
    )

    assert report["refused"] is True
    assert "the head its resume superseded" in report["reason"]


def test_a_delivery_before_the_resume_instant_is_withheld(world):
    """A delivered manifest older than the resume belongs to the ended attempt."""
    head = _commit_plan(world["repo"])
    run_id = "r-delivered-before-the-resume"
    _write_run(world, run_id=run_id, head=head)
    resumption_module._record_resume_supersedes_head(run_id)
    # The same delivered text, but written before the resume: the file a resume
    # leaves on disk is not the resumed attempt's own verdict.
    manifest = _manifest_of(world, run_id)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    _place_manifest(manifest, _resume_stamp(run_id) - BEFORE_SECONDS)
    record = runs.read_pointer(run_id)

    assert dispatch_module.supersedes_head(record, head) is True
    assert _kinds(run_id) == []


def test_a_moved_head_is_reviewed_at_its_new_head(world):
    """A run that delivers a new head is reviewed there, as at base."""
    head = _commit_plan(world["repo"])
    run_id = "r-moved-head"
    _write_run(world, run_id=run_id, head=head)
    resumption_module._record_resume_supersedes_head(run_id)

    moved = _commit(world["repo"], {"pkg/mod.py": "VALUE = 2\n"}, "fix: carry on")
    _deliver(world, run_id, moved)
    record = runs.read_pointer(run_id)

    assert dispatch_module.supersedes_head(record, moved) is False
    report = obligations_module.obligations(PROJECT, SESSION)
    duties = [
        item for item in report["obligations"] if str(item.get("run_id")) == run_id
    ]
    assert [item["kind"] for item in duties] == ["review-missing"]
    assert recovery.same_revision(str(duties[0]["head"]), moved)
