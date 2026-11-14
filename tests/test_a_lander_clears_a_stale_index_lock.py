"""A lander clears an index lock whose holder is proven gone.

A git process killed while holding the checkout's ``.git/index.lock`` leaves the
file behind, and every later ``crew merge`` then waits out its retries and
refuses a lock no live process will ever release. Measured on 14 November: an
empty lock dated 11:54:40 sat in the shared checkout for more than sixteen
minutes with no git process on the host holding it, and no landing reached main
until the file went.

The lander now clears such a lock, but only on evidence: it must be at least ten
minutes old, its size and mtime must not have moved across the whole retry
window, and no process on this host may hold it open. Every case here builds a
real repository with a bare origin and a reviewed head on a side commit, runs
the landing, and reads the checkout, the lock and the returned report back.

The declared mutation removes the staleness check, so a lock is cleared whether
or not it is held; the second case then fails, because the lock a live child
process holds is deleted instead of refused.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Self

import pytest

from reckon.crew import merge
from tests.helpers.git import git as _git
from tests.test_a_reviewed_head_lands_without_touching_the_index import (
    _init_checkout,
    _reviewed_head,
)

DECLARED_MUTATION = (
    "with the staleness check removed, a lock a live child process holds is "
    "cleared instead of refused, and the second case fails"
)

# Ten minutes is the bound the module applies; the ages here sit either side of
# it by a wide margin so a slow test host cannot move a case across the bound.
STALE_AGES_SECONDS = (11 * 60, 5 * 60, 30)

_HOLDER = """
import os, sys, time

path, seconds = sys.argv[1], float(sys.argv[2])
handle = os.open(path, os.O_WRONLY)
print("held", flush=True)
time.sleep(seconds)
os.close(handle)
try:
    os.unlink(path)
except FileNotFoundError:
    pass
"""


def _place_lock(work: Path, age_seconds: float) -> Path:
    """Write a dry index lock whose mtime is ``age_seconds`` in the past."""
    lock = work / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(lock, (old, old))
    return lock


class _LockHolder:
    """A second process that holds the checkout's index lock open for a while.

    This is deliberately not the holder in
    ``test_a_landing_write_outlasts_a_briefly_held_index_lock``: that one takes
    the lock and closes the descriptor, so the host's ``/proc`` scan cannot see
    it. The case here needs a holder the scan must find, so the descriptor stays
    open for the whole hold.
    """

    def __init__(self, lock: Path, seconds: float):
        self.lock = lock
        self.seconds = seconds
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> Self:
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-c", _HOLDER, str(self.lock), str(self.seconds)],
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
        return False


def _land(work: Path, head: str, **kwargs):
    return merge.land_merge(
        work,
        head=head,
        message="Land the reviewed feature",
        body="the feature is reviewed and its suite passes",
        remote="origin",
        **kwargs,
    )


def test_a_lock_older_than_the_bound_with_no_holder_is_cleared(tmp_path: Path):
    """An eleven-minute-old lock no process holds is removed and the merge lands.

    The returned record names the removal and the age the lock had reached, and
    the reviewed commit is on the branch.
    """
    _origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    lock = _place_lock(work, STALE_AGES_SECONDS[0])

    report = _land(work, head)

    assert not lock.exists(), "the stale lock must be removed"
    assert _git(work, "rev-parse", "HEAD") == report["commit"], (
        "the merge must have landed after the stale lock was cleared"
    )
    cleared = report["index_lock_cleared"]
    assert len(cleared) == 1, f"the record must name one removal, saw {cleared}"
    detail = cleared[0]["detail"]
    assert "index.lock" in detail and "removed" in detail, (
        f"the record must name the lock and the removal: {detail}"
    )
    assert cleared[0]["age_seconds"] >= merge.INDEX_LOCK_STALE_SECONDS, (
        "the record must name the age the lock had reached"
    )


def test_a_lock_a_live_process_holds_refuses_and_survives(tmp_path: Path):
    """A lock a live child keeps open is refused, and the file survives.

    The lock is as old as the stale case, so only the open-holder scan can tell
    it apart: a holder this host can see is never cleared.
    """
    _origin, work = _init_checkout(tmp_path)
    base = _git(work, "rev-parse", "HEAD")
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    lock = _place_lock(work, STALE_AGES_SECONDS[0])

    with _LockHolder(lock, 15.0):
        assert lock.exists()
        with pytest.raises(merge.MergeError) as caught:
            _land(work, head)
        assert "index.lock stayed held" in str(caught.value)
        assert "aged" in str(caught.value), (
            f"the refusal must name the lock's age: {caught.value}"
        )
        assert lock.exists(), "a lock a process holds must survive the landing"

    assert _git(work, "rev-parse", "HEAD") == base, (
        "a refused landing must not move the branch"
    )


def test_a_lock_whose_mtime_moves_refuses_and_survives(tmp_path: Path):
    """A lock touched during the retries is refused, and the file survives.

    The touch keeps the lock eleven minutes old, so the age bound still passes;
    only the still-lock condition refuses it.
    """
    _origin, work = _init_checkout(tmp_path)
    base = _git(work, "rev-parse", "HEAD")
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    lock = _place_lock(work, STALE_AGES_SECONDS[0])
    touched: list[float] = []

    def _touch_once_then_sleep(seconds: float) -> None:
        if not touched:
            stat = lock.stat()
            os.utime(lock, (stat.st_atime, stat.st_mtime + 1))
            touched.append(stat.st_mtime + 1)
        time.sleep(seconds)

    with pytest.raises(merge.MergeError) as caught:
        _land(work, head, sleep=_touch_once_then_sleep)

    assert touched, "the wait must have been entered"
    assert "index.lock stayed held" in str(caught.value)
    assert lock.exists(), "a lock that moved must survive the landing"
    assert _git(work, "rev-parse", "HEAD") == base


@pytest.mark.parametrize("age_seconds", [STALE_AGES_SECONDS[1], STALE_AGES_SECONDS[2]])
def test_a_lock_younger_than_the_bound_refuses_and_survives(
    tmp_path: Path, age_seconds: float
):
    """A lock younger than ten minutes is refused, and the file survives."""
    _origin, work = _init_checkout(tmp_path)
    base = _git(work, "rev-parse", "HEAD")
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    lock = _place_lock(work, age_seconds)

    with pytest.raises(merge.MergeError) as caught:
        _land(work, head)

    assert "index.lock stayed held" in str(caught.value)
    assert lock.exists(), "a lock younger than the bound must survive the landing"
    assert _git(work, "rev-parse", "HEAD") == base


def test_a_three_minute_lock_is_cleared_under_a_shorter_bound(tmp_path: Path):
    """A three-minute lock is cleared when the bound names less than its age.

    The bound is a parameter of the landing, so a caller may set it shorter than
    the ten-minute default; the same three-minute lock refuses at the default in
    the case above.
    """
    _origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work, "reviewed-feature", "feature.txt", "feature\n")
    lock = _place_lock(work, 3 * 60)

    report = _land(work, head, stale_lock_seconds=120)

    assert not lock.exists(), "the lock is stale against a two-minute bound"
    assert _git(work, "rev-parse", "HEAD") == report["commit"]
    assert report["index_lock_cleared"], "the record must name the removal"
