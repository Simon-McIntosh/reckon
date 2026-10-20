"""A landing write waits out a briefly held index lock.

The landing staging makes one ``git add`` and refuses on any failure, so a
transient ``index.lock`` a peer's git command holds for a moment turns an
already-committed promotion into a failed verb. These cases hold a real
``.git/index.lock`` and drive the landing commit itself, so the wait and the
refusal are the ones a coordinator reads. The short-hold case releases the lock
once the first retry is recorded, so the retry a real peer would need is
exercised; the long-hold case keeps it for the whole call.
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

RUN = "landing-lock"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=check
    )


@pytest.fixture
def repository(tmp_path, monkeypatch):
    """A temporary repository whose crew home resolves inside the test tree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    plan = root / "plan.html"
    plan.write_text("committed plan\n", encoding="utf-8")
    _git(root, "add", "--", str(plan))
    _git(root, "commit", "-qm", "test: seed", "-m", "Seed the tracked plan.")
    assert run_dir(RUN).is_relative_to(tmp_path)
    return root, plan


def _hold_index_lock(root: Path) -> Path:
    lock = root / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    return lock


def _landing_entries(run_id: str) -> list[dict]:
    path = run_dir(run_id) / promotion_scope.LANDING_RECORD_NAME
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _release_lock_after_first_retry(run_id: str, lock: Path) -> threading.Thread:
    """Release the held lock once the landing records its first retry.

    A fixed delay would race the landing's own setup, which spends time before
    its first ``git add``. Waiting for the retry entry instead makes the hold
    end exactly when the retry it is meant to serve has happened, so the next
    attempt stages and the case does not depend on timing. The thread gives up
    after a minute so a landing that never retries fails its assertion rather
    than hanging.
    """

    def release() -> None:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if any(
                entry.get("step") == "stage_retry" for entry in _landing_entries(run_id)
            ):
                lock.unlink(missing_ok=True)
                return
            time.sleep(0.01)

    thread = threading.Thread(target=release, daemon=True)
    thread.start()
    return thread


def test_a_landing_write_waits_out_a_briefly_held_lock(repository, monkeypatch):
    """A lock released while the retries last is waited out and the write commits.

    The lock refuses the first staging attempt. It is released once that retry
    has been recorded, so the next attempt stages and the landing commits. Each
    retry is recorded on the run's landing record, and the commit itself is
    recorded as committed.
    """
    root, plan = repository
    # The attempt budget is widened so a slow git call under load cannot use it
    # up before the lock is released; what bounds this case is the release.
    monkeypatch.setattr(promotion_scope, "_INDEX_LOCK_RETRY_ATTEMPTS", 600)
    monkeypatch.setattr(promotion_scope, "_INDEX_LOCK_RETRY_INTERVAL_SECONDS", 0.02)
    plan.write_text("landing narrative the promotion writes\n", encoding="utf-8")
    lock = _hold_index_lock(root)
    releaser = _release_lock_after_first_retry(RUN, lock)
    try:
        result = promotion._commit_landing_writes(
            run_id=RUN,
            verdict="passed",
            checkout=root,
            paths=[plan],
        )
    finally:
        releaser.join(timeout=60.0)
        if lock.exists():
            lock.unlink()

    assert result["committed"] is True, (
        "a lock released while the retries last must not refuse the landing"
    )
    assert (
        _git(root, "log", "-1", "--format=%s").stdout.strip()
        == f"promote({RUN}): passed"
    )
    entries = _landing_entries(RUN)
    retries = [entry for entry in entries if entry.get("step") == "stage_retry"]
    assert retries, "each retry must be recorded on the run's landing record"
    assert any(
        entry.get("step") == "commit" and entry.get("committed") for entry in entries
    ), "the landing record must carry the commit this wait produced"


def test_a_lock_held_past_the_attempts_refuses_naming_the_lock(repository, monkeypatch):
    """A lock held for every attempt refuses, and the refusal names it.

    The lock is never released, so the staging uses up its attempts and returns
    the failing result. The landing refuses; the refusal names the lock, the
    retries are recorded, and the final refusal is recorded at step ``stage``.
    The attempt count is shrunk so the case spends seconds at most.
    """
    root, plan = repository
    monkeypatch.setattr(promotion_scope, "_INDEX_LOCK_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(promotion_scope, "_INDEX_LOCK_RETRY_INTERVAL_SECONDS", 0.02)
    plan.write_text("landing narrative the promotion writes\n", encoding="utf-8")
    lock = _hold_index_lock(root)
    try:
        with pytest.raises(promotion.CrewError) as caught:
            promotion._commit_landing_writes(
                run_id=RUN,
                verdict="passed",
                checkout=root,
                paths=[plan],
            )
    finally:
        lock.unlink()

    assert "index.lock" in str(caught.value), (
        "the refusal must name the lock the landing waited on"
    )
    entries = _landing_entries(RUN)
    retries = [entry for entry in entries if entry.get("step") == "stage_retry"]
    assert retries, "a lock held past the attempts still records its retries"
    assert [entry["attempt"] for entry in retries] == list(range(1, len(retries) + 1))
    assert entries[-1]["step"] == "stage", "the final refusal is recorded at step stage"


def test_a_staging_failure_that_leaves_no_lock_refuses_on_its_first_attempt(
    repository,
):
    """A staging failure that leaves no lock behind is not retried.

    An unreadable file makes ``git add`` fail with no ``index.lock`` left in
    the checkout. The landing refuses at once: no retry is recorded, and the
    refusal is recorded at step ``stage``.
    """
    root, plan = repository
    plan.write_text("landing narrative the promotion writes\n", encoding="utf-8")
    plan.chmod(0o000)
    try:
        with pytest.raises(promotion.CrewError):
            promotion._commit_landing_writes(
                run_id=RUN,
                verdict="passed",
                checkout=root,
                paths=[plan],
            )
    finally:
        plan.chmod(0o644)

    entries = _landing_entries(RUN)
    assert not [entry for entry in entries if entry.get("step") == "stage_retry"], (
        "a failure that leaves no lock behind must not be retried"
    )
    assert any(entry.get("step") == "stage" for entry in entries)
