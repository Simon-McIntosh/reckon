"""A landing's commit and rollback survive a held index lock.

The staging ``git add`` already waits out a briefly held ``index.lock``; the
``git commit`` that follows did not, so a peer promotion holding the lock for a
moment turned an already-staged landing into a refused one. Worse, the rollback
that refusal ran could not unstage the forward writes while the lock was held,
leaving a half-landed plan staged in the shared index for any path-less commit
to sweep up. These cases hold a real ``.git/index.lock`` at each boundary and
drive the landing commit itself, so the waits and the refusal are the ones a
coordinator reads.

Each case names its own negative control: removing the commit's wait fails the
first case, and removing the rollback's wait fails the second.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import promotion, promotion_scope
from reckon.crew.runs import run_dir

RUN = "landing-commit-lock"
SEEDED = "committed plan\n"
LANDING = "landing narrative the promotion writes\n"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=check
    )


def _staged_names(root: Path) -> list[str]:
    return sorted(_git(root, "diff", "--cached", "--name-only").stdout.split())


@pytest.fixture
def repository(tmp_path, monkeypatch):
    """A temporary repository whose crew home resolves inside the test tree."""
    config_home = tmp_path / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    plan = root / "plan.html"
    plan.write_text(SEEDED, encoding="utf-8")
    _git(root, "add", "--", str(plan))
    _git(root, "commit", "-qm", "test: seed", "-m", "Seed the tracked plan.")
    assert run_dir(RUN).is_relative_to(tmp_path)
    return root, plan


def _landing_entries(run_id: str) -> list[dict]:
    path = run_dir(run_id) / promotion_scope.LANDING_RECORD_NAME
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _steps(entries: list[dict], step: str) -> list[dict]:
    return [entry for entry in entries if entry.get("step") == step]


def _release_lock_after_first_retry(
    run_id: str, lock: Path, step: str
) -> tuple[threading.Thread, dict[str, bool]]:
    """Release the held lock once the landing records its first retry.

    A fixed delay would race the landing's own setup. Waiting for the retry
    entry instead makes the hold end exactly when the retry it is meant to
    serve has happened, so the case does not depend on timing. The thread gives
    up after a minute so a landing that never retries fails its assertion
    rather than hanging, and it reports whether it saw the retry while the lock
    was still held, which is what proves a wait happened.
    """
    observed = {"retried_under_the_lock": False}

    def release() -> None:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if _steps(_landing_entries(run_id), step):
                observed["retried_under_the_lock"] = lock.exists()
                lock.unlink(missing_ok=True)
                return
            time.sleep(0.01)

    thread = threading.Thread(target=release, daemon=True)
    thread.start()
    return thread, observed


def _stage_then_hold(root: Path, monkeypatch) -> Path:
    """Take the index lock after staging and before the commit, through a stub.

    A lock taken before staging measures the staging retry; this stub puts it
    at the boundary the commit case is about, so the staging succeeds and the
    commit is what meets the lock.
    """
    real_stage = promotion_scope._stage_landing_writes
    lock = root / ".git" / "index.lock"

    def stage_then_hold(run_id, checkout, targets):
        staged = real_stage(run_id, checkout, targets)
        assert staged.returncode == 0, "the staging must not meet the lock"
        lock.write_text("", encoding="utf-8")
        return staged

    monkeypatch.setattr(promotion_scope, "_stage_landing_writes", stage_then_hold)
    return lock


def test_a_commit_waits_out_a_lock_taken_after_staging(repository, monkeypatch):
    """A lock released while the commit retries, and the landing still commits.

    The lock is taken after the staging ``git add`` has succeeded and released
    once the commit's first retry is recorded, so what is measured is the
    commit's wait rather than the staging's. The landing commits, the plan file
    carries its new content, and the retry is recorded as a commit retry, with
    no staging retry — which is what shows the lock was met by the commit.
    """
    root, plan = repository
    monkeypatch.setattr(promotion_scope, "_INDEX_LOCK_RETRY_ATTEMPTS", 600)
    monkeypatch.setattr(promotion_scope, "_INDEX_LOCK_RETRY_INTERVAL_SECONDS", 0.02)
    plan.write_text(LANDING, encoding="utf-8")
    lock = _stage_then_hold(root, monkeypatch)
    releaser, observed = _release_lock_after_first_retry(RUN, lock, "commit_retry")
    try:
        result = promotion._commit_landing_writes(
            run_id=RUN,
            verdict="passed",
            checkout=root,
            paths=[plan],
        )
    finally:
        releaser.join(timeout=60.0)
        lock.unlink(missing_ok=True)

    assert result["committed"] is True, (
        "a lock released while the commit retries must not refuse the landing"
    )
    assert _git(root, "log", "-1", "--format=%s").stdout.strip() == (
        f"promote({RUN}): passed"
    )
    assert plan.read_text(encoding="utf-8") == LANDING
    assert _staged_names(root) == [], (
        "a landed commit must leave nothing staged at the paths it wrote"
    )
    entries = _landing_entries(RUN)
    assert _steps(entries, "commit_retry"), "the commit's retry must be recorded"
    assert not _steps(entries, "stage_retry"), (
        "the lock was taken after staging, so staging must not have retried"
    )
    assert observed["retried_under_the_lock"], (
        "the retry must be observed while the lock was still held"
    )


def test_a_rollback_waits_out_a_lock_that_outlasted_the_commit(repository, monkeypatch):
    """A lock that outlasts the commit's bound is unstaged by the rollback's wait.

    The lock is held for every commit attempt and released one second after the
    commit step gives up. The landing refuses, but the rollback's own ``git
    restore`` runs through the same bound and waits the remaining second out, so
    the forward write is unstaged and the plan file returns to its HEAD content.
    The rollback's retry is recorded, and observed while the lock was held.
    """
    root, plan = repository
    attempts = promotion_scope._INDEX_LOCK_RETRY_ATTEMPTS
    plan.write_text(LANDING, encoding="utf-8")
    lock = _stage_then_hold(root, monkeypatch)
    real_git = promotion_scope._git
    commits = {"made": 0}

    def counting_git(checkout, *args, check=True):
        result = real_git(checkout, *args, check=check)
        if args and args[0] == "commit":
            commits["made"] += 1
            if commits["made"] == attempts:
                threading.Timer(1.0, lambda: lock.unlink(missing_ok=True)).start()
        return result

    monkeypatch.setattr(promotion_scope, "_git", counting_git)
    releaser, observed = _release_lock_after_first_retry(RUN, lock, "restore_retry")
    try:
        with pytest.raises(promotion.CrewError) as caught:
            promotion._commit_landing_writes(
                run_id=RUN,
                verdict="passed",
                checkout=root,
                paths=[plan],
            )
    finally:
        releaser.join(timeout=60.0)
        lock.unlink(missing_ok=True)

    assert commits["made"] == attempts, (
        "the commit must use its whole bound before the commit step gives up"
    )
    assert "could not be restored" not in str(caught.value), (
        "a rollback that waited out the lock must name no unrestored path"
    )
    assert _staged_names(root) == [], (
        "the rollback's wait must unstage the forward write once the lock frees"
    )
    assert plan.read_text(encoding="utf-8") == SEEDED, (
        "the rollback's wait must restore the plan file to its HEAD content"
    )
    entries = _landing_entries(RUN)
    assert _steps(entries, "commit_retry"), "the commit's retries must be recorded"
    assert _steps(entries, "restore_retry"), (
        "the rollback must be observed retrying rather than falling straight back"
    )
    assert observed["retried_under_the_lock"], (
        "the rollback's retry must be observed while the lock was still held"
    )
    assert _steps(entries, "commit"), "the refusal is recorded at step commit"


def test_a_lock_held_past_both_bounds_refuses_naming_every_path(
    repository, monkeypatch
):
    """A lock held past both bounds refuses and names every write left staged.

    The lock is never released, so the commit and then the rollback use up their
    bounds. The landing refuses, the refusal names each landing write that still
    carries its landing content, and those paths are left staged — the state the
    refusal exists to warn a coordinator about.
    """
    root, plan = repository
    monkeypatch.setattr(promotion_scope, "_INDEX_LOCK_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(promotion_scope, "_INDEX_LOCK_RETRY_INTERVAL_SECONDS", 0.02)
    plan.write_text(LANDING, encoding="utf-8")
    created = root / "docs" / "state" / "run.json"
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text('{"run_id": "landing-commit-lock"}\n', encoding="utf-8")
    lock = _stage_then_hold(root, monkeypatch)
    try:
        with pytest.raises(promotion.CrewError) as caught:
            promotion._commit_landing_writes(
                run_id=RUN,
                verdict="passed",
                checkout=root,
                paths=[plan, created],
            )
    finally:
        lock.unlink(missing_ok=True)

    message = str(caught.value)
    for path in (plan, created):
        assert str(path) in message, (
            "the refusal must name every landing write it could not restore"
        )
    assert "could not be restored" in message, (
        "the refusal must say the writes still carry their landing content"
    )
    assert _staged_names(root) == ["docs/state/run.json", "plan.html"], (
        "the refusal warns about exactly the writes the index still stages"
    )
    entries = _landing_entries(RUN)
    assert _steps(entries, "commit"), "the refusal is recorded at step commit"
