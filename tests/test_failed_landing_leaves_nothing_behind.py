"""A refused landing commit leaves nothing staged or stored behind.

A landing writes the plan file carrying the run's comment, the run's own ledger
file, and the run's row in the rebuildable store before it commits them. The
plan file's write is left in the working tree on purpose when the commit is
refused — the retry commits that write — so what a refusal must not leave is
the residue a peer could act on without the run: a staged entry in the shared
index, or a store row the retry's insert collides with, leaving the store
carrying the refused attempt's record instead of the landed one.

The tests hold a real ``.git/index.lock``, so the refusals come from git state
rather than a stubbed exit code. The store is read through its membership
reader, which opens the database read-only and so cannot rebuild away the row
it is being asked about.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, run_store
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "failed-landing-fixture"
PLAN = "failed-landing-target"
RUN_ID = "r-20261003T030000000001-failed-landing"
NARRATIVE = "the landing narrative the refused attempt writes"


def _git(
    repository: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=check,
    )


def _write_plan(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Failed landing target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _write_plan(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        (
            "commit",
            "-q",
            "-m",
            "test: seed repository",
            "-m",
            "Seed the plan the landing writes to.",
        ),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-10-03T03:00:00Z",
            "node": {
                "id": "failed-landing",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )
    # The fixture must never reach the operator's own run store or plan.
    assert Path(run_store.store_path()).is_relative_to(tmp_path)
    return root


def _promote(repository: Path) -> None:
    promotion._complete_locked(
        RUN_ID,
        gate="not-run",
        root=repository,
        outcome=NARRATIVE,
    )


def _staged_paths(repository: Path) -> list[str]:
    listed = _git(
        repository,
        "--no-optional-locks",
        "diff",
        "--cached",
        "--name-only",
    )
    return [line for line in listed.stdout.splitlines() if line.strip()]


def test_a_refused_landing_commit_leaves_nothing_behind(repository: Path) -> None:
    """The lock refuses the landing, and the retry inherits no residue.

    The lock is held across the whole promotion, so the landing's staging step
    is refused by git itself and nothing the attempt wrote reaches the guard's
    stores. The plan file keeps the attempt's write on purpose — the retry
    commits it — so what is asserted here is the residue a peer could act on
    instead: nothing staged, no store row left for the retry to collide with,
    and, once the lock is gone, a promotion that succeeds and leaves the
    worktree clean.
    """
    lock = repository / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")

    try:
        with pytest.raises(promotion.CrewError) as caught:
            _promote(repository)
    finally:
        lock.unlink(missing_ok=True)

    assert "could not stage the landing writes" in str(caught.value)
    assert _staged_paths(repository) == [], "the landing's staging survives"
    assert RUN_ID not in (run_store.indexed_run_ids(PROJECT) or set()), (
        "the landing's store row survives"
    )

    _promote(repository)

    assert RUN_ID in (run_store.indexed_run_ids(PROJECT) or set()), (
        "the store row must be written by a landing that commits."
    )
    assert (
        _git(repository, "--no-optional-locks", "status", "--porcelain").stdout.strip()
        == ""
    ), "a promoted landing leaves no uncommitted state"


def test_a_staged_created_path_is_never_left_without_its_file(
    repository: Path,
) -> None:
    """A lock that refuses the unstage cannot be allowed to leave a gap.

    The one-shot restore is refused by the held lock, so the fallback's job is
    to drop the index entry and only then the file. A file unlinked while its
    entry is still staged commits content no reader can see in the working
    tree, so a rollback that cannot drop the entry must leave the file in
    place and report the entry as surviving.
    """
    created = repository / "docs" / "state" / PROJECT / "landing-write.txt"
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text("landing content\n", encoding="utf-8")
    _git(repository, "add", "--", str(created))
    lock = repository / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")

    try:
        report = promotion._restore_landing_writes(repository, [created])
    finally:
        lock.unlink(missing_ok=True)

    assert created.exists(), (
        "the entry is still staged, so the file it stages must still exist"
    )
    assert report[str(created)] is False, (
        "a staged entry the rollback could not drop is not a path it restored"
    )

    report = promotion._restore_landing_writes(repository, [created])

    assert not created.exists()
    assert report[str(created)] is True
    assert _staged_paths(repository) == []
