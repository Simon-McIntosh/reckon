"""A landing write outlasts a briefly held index lock.

Another session's commit holds the shared checkout's ``.git/index.lock`` for
seconds at a time while a promotion stages its landing writes. A retry bound of
about six seconds ran out before that peer finished, and the promotion rolled
back after minutes of reads. These cases hold the lock from a second process, so
the lock and its release are not artefacts of this process's own git calls: the
first holds it for twenty seconds and requires the landing to commit anyway, and
the second holds it past the bound and requires the refusal to name how long the
lock had been held and the bound it outlasted.

The negative control is the base revision, where the twenty-second hold outlasts
the staging retries and the landing rolls back.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Self

import pytest

from reckon.crew import promotion, promotion_scope
from reckon.crew.runs import run_dir

RUN = "landing-write-outlasts-lock"
SEEDED = "committed plan\n"
LANDING = "landing narrative the promotion writes\n"
HOLD_SECONDS = 20.0
# A retry bound is a spacing span, so the wall clock a landing spends on it is
# longer than the span by the cost of each git call it makes. A lock held past a
# span plus this margin cannot be reached by the last attempt.
HOLD_PAST_BOUND_SECONDS = 30.0

_HOLDER = """
import os, sys, time

path, seconds = sys.argv[1], float(sys.argv[2])
handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
os.close(handle)
print("held", flush=True)
time.sleep(seconds)
try:
    os.unlink(path)
except FileNotFoundError:
    pass
"""


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


class _IndexLockHolder:
    """A second process that holds the checkout's index lock for a while.

    The lock is taken by another process, not by a thread of this one, so the
    wait is against the same kind of writer a shared checkout has: a peer's git
    command that creates the lock and removes it when it finishes.
    """

    def __init__(self, checkout: Path, seconds: float):
        self.path = checkout / ".git" / "index.lock"
        self.seconds = seconds
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> Self:
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-c", _HOLDER, str(self.path), str(self.seconds)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if self.process.stdout.readline().strip() != "held":
            self.process.kill()
            raise AssertionError(
                "the second process could not take the index lock: "
                + self.process.stderr.read()
            )
        return self

    def __exit__(self, *_exc) -> bool:
        if self.process is not None:
            self.process.wait(timeout=self.seconds + 60.0)
        self.path.unlink(missing_ok=True)
        return False


def test_a_landing_write_outlasts_a_lock_held_for_twenty_seconds(repository):
    """A lock a second process holds for twenty seconds is waited out.

    The promotion reaches its staging step while the lock is held, retries for
    as long as the holder keeps it, and commits once it is released. The run's
    landing record lists each retry, and the commit is recorded, so a landing
    that waited says how often it did.
    """
    root, plan = repository
    plan.write_text(LANDING, encoding="utf-8")
    with _IndexLockHolder(root, HOLD_SECONDS):
        result = promotion._commit_landing_writes(
            run_id=RUN,
            verdict="passed",
            checkout=root,
            paths=[plan],
        )

    assert result["committed"] is True, (
        "a lock another process holds for twenty seconds must not roll the landing back"
    )
    assert _git(root, "log", "-1", "--format=%s").stdout.strip() == (
        f"promote({RUN}): passed"
    )
    assert plan.read_text(encoding="utf-8") == LANDING
    assert _staged_names(root) == [], (
        "a landing that waited out the lock must leave nothing staged"
    )
    entries = _landing_entries(RUN)
    retries = [entry for entry in entries if entry.get("step") == "stage_retry"]
    expected = HOLD_SECONDS / promotion_scope._INDEX_LOCK_RETRY_INTERVAL_SECONDS * 0.5
    assert len(retries) >= expected, (
        "the landing record must list the retries a twenty-second wait made; "
        f"saw {len(retries)}, expected at least {expected:.0f}"
    )
    assert [entry["attempt"] for entry in retries] == list(
        range(1, len(retries) + 1)
    ), "the recorded retries must be numbered from one without a gap"
    assert entries[-1].get("step") == "commit" and entries[-1].get("committed") is True


def test_a_lock_held_past_the_bound_refuses_naming_its_age(repository):
    """A lock that outlasts the bound refuses, naming its age and that bound.

    The lock is held well past the staging bound, so the staging cannot reach
    it however much a git call costs, and released once the staging has given
    up, so the rollback's own wait settles the forward write. The refusal still
    names how long the lock had been held when the staging gave up and the bound
    it outlasted, and the plan file returns to its committed content.
    """
    root, plan = repository
    attempts = promotion_scope._INDEX_LOCK_RETRY_ATTEMPTS
    interval = promotion_scope._INDEX_LOCK_RETRY_INTERVAL_SECONDS
    bound = promotion_scope._index_lock_retry_bound_seconds(attempts, interval)
    plan.write_text(LANDING, encoding="utf-8")
    with (
        _IndexLockHolder(root, bound + HOLD_PAST_BOUND_SECONDS),
        pytest.raises(promotion.CrewError) as caught,
    ):
        promotion._commit_landing_writes(
            run_id=RUN,
            verdict="passed",
            checkout=root,
            paths=[plan],
        )

    message = str(caught.value)
    assert "index.lock" in message, (
        "the refusal must name the lock the landing waited on"
    )
    age = re.search(r"held the checkout's index lock for ([0-9.]+)s", message)
    assert age, f"the refusal must name the lock's age: {message}"
    named_bound = re.search(r"([0-9.]+)s retry bound", message)
    assert named_bound, f"the refusal must name the bound it outlasted: {message}"
    assert float(named_bound.group(1)) == pytest.approx(bound, abs=0.05), (
        "the refusal must name the bound the landing actually waited"
    )
    assert float(age.group(1)) >= bound * 0.9, (
        "the lock's age must be at least the bound the landing waited out"
    )
    assert _staged_names(root) == [], (
        "the rollback that followed must leave nothing staged"
    )
    assert plan.read_text(encoding="utf-8") == SEEDED, (
        "the rollback that followed must restore the plan file to HEAD's copy"
    )
    entries = _landing_entries(RUN)
    assert any(entry.get("step") == "stage" for entry in entries), (
        "the refusal is recorded at step stage"
    )
