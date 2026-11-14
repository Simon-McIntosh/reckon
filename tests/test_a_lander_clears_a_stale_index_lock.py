"""A lander clears an index lock whose holder is proven gone.

A checkout is written from several hosts at once, so a lock whose writer died
mid-write outlives the process and blocks every landing on that checkout. The
landing may clear it only when nothing can still be writing: the lock is old,
its size and mtime never moved across the whole retry window, and no process on
this host holds it open. Each case builds a real repository with a bare origin
and drives ``crew merge`` itself, so the wait, the refusal and the removal are
the ones a coordinator reads.

Two arms declare the mutations that must fail: removing the holder check clears
a lock a live process still holds, and removing the unchanged check clears one a
writer is still touching. Both are applied under pytest through the environment
variables named inside the tests that exercise them.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import merge

# A lock is judged stale only well past the age bound. A case that touches the
# lock puts its mtime back to the same old age, so the movement is what the case
# exercises and not the age.
ELEVEN_MINUTES = 660.0
FIVE_MINUTES = 300.0


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _init_checkout(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    _git(work, "config", "user.email", "author@example.invalid")
    _git(work, "config", "user.name", "Author")
    _git(work, "config", "commit.gpgsign", "false")
    (work / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "branch", "-M", "main")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return origin, work


def _reviewed_head(work: Path) -> str:
    """Commit one file on a side branch, leave main checked out, return the head."""
    _git(work, "checkout", "-q", "-b", "reviewed-feature")
    (work / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work, "add", "feature.txt")
    _git(work, "commit", "-q", "-m", "add feature")
    _git(work, "checkout", "-q", "main")
    return _git(work, "rev-parse", "reviewed-feature")


def _aged_lock(work: Path, age_seconds: float) -> Path:
    """Write an empty lock and set its mtime into the past."""
    lock = work / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(lock, (stamp, stamp))
    return lock


def _land(work: Path, head: str, **kwargs):
    return merge.land_merge(
        work,
        head=head,
        message="Land the reviewed feature",
        body="the feature is reviewed and its suite passes",
        **kwargs,
    )


class _LiveHolder:
    """A child process that holds the lock open until it is stopped."""

    def __init__(self, lock: Path) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys, time\n"
                    "handle = open(sys.argv[1])\n"
                    "print('held', flush=True)\n"
                    "time.sleep(120)\n"
                ),
                str(lock),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = self.process.stdout.readline().strip() if self.process.stdout else ""
        assert line == "held", "the holder child must report that the lock is open"

    def stop(self) -> None:
        self.process.kill()
        self.process.wait(timeout=30)


def test_an_old_unchanged_lock_no_process_holds_is_removed_and_the_landing_lands(
    tmp_path: Path,
) -> None:
    """A lock eleven minutes old, still, and held by nobody is cleared and recorded.

    Every retry fails on the lock, so the landing reaches its stale judgement:
    the age, the unchanged size and mtime, and the absent holder all hold. The
    lock is removed, one line naming its age is recorded on the landing's own
    report, and the retried fast-forward lands and pushes.
    """
    origin, work = _init_checkout(tmp_path)
    base = _git(work, "rev-parse", "HEAD")
    head = _reviewed_head(work)
    lock = _aged_lock(work, ELEVEN_MINUTES)

    report = _land(work, head, attempts=3, retry_seconds=0.02)

    landed = _git(work, "rev-parse", "HEAD")
    assert report["base"] == base
    assert report["commit"] == landed, "the cleared lock must let the merge land"
    assert report["remote_head"] == landed
    assert not lock.exists(), "a provably stale lock is removed"
    removals = report["stale_index_lock_removed"]
    assert len(removals) == 1, "the landing's record names the one removal"
    line = removals[0]
    assert "removed" in line and "index.lock" in line, line
    assert re.search(r"after 6[0-9]\ds", line), f"the record names the age: {line}"
    assert _git(origin, "rev-parse", "main") == landed


def test_a_lock_a_live_process_holds_refuses_and_survives(
    tmp_path: Path, monkeypatch
) -> None:
    """A lock a live child holds open is not cleared, whatever its age.

    Host-local absence of a holder is the only local evidence that can clear a
    lock, so a live holder vetoes the removal even when the file is eleven
    minutes old and never moved. The landing refuses and the file survives.
    """
    _, work = _init_checkout(tmp_path)
    if os.environ.get("RECKON_INDEX_LOCK_HOLDER_CHECK") == "removed":
        monkeypatch.setattr(merge, "_processes_holding", lambda _path: [])
    base = _git(work, "rev-parse", "HEAD")
    head = _reviewed_head(work)
    lock = _aged_lock(work, ELEVEN_MINUTES)
    holder = _LiveHolder(lock)
    try:
        with pytest.raises(merge.MergeError) as caught:
            _land(work, head, attempts=2, retry_seconds=0.01)
    finally:
        holder.stop()

    assert "index.lock" in str(caught.value)
    assert lock.exists(), "a lock a live process holds must survive the refusal"
    assert _git(work, "rev-parse", "HEAD") == base


def test_a_lock_touched_during_the_retries_refuses_and_survives(
    tmp_path: Path, monkeypatch
) -> None:
    """A lock whose size and mtime move between attempts is a live writer.

    The wait between attempts grows the lock and puts its mtime back to the same
    old age, so only the movement can stop the removal: the age is still eleven
    minutes and no process holds it. The landing refuses and the file survives.
    """
    _, work = _init_checkout(tmp_path)
    if os.environ.get("RECKON_INDEX_LOCK_MTIME_CHECK") == "removed":
        monkeypatch.setattr(merge, "_index_lock_signature", lambda _path: (0, 0))
    head = _reviewed_head(work)
    lock = _aged_lock(work, ELEVEN_MINUTES)

    def touch_between_attempts(_seconds: float) -> None:
        with lock.open("ab") as handle:
            handle.write(b"x")
        stamp = time.time() - ELEVEN_MINUTES
        os.utime(lock, (stamp, stamp))
        time.sleep(0.01)

    with pytest.raises(merge.MergeError) as caught:
        _land(
            work,
            head,
            attempts=3,
            retry_seconds=0.01,
            sleep=touch_between_attempts,
        )

    assert "index.lock" in str(caught.value)
    assert lock.exists(), "a lock that moved under the landing must survive"


def test_a_lock_younger_than_the_age_bound_refuses_and_survives(
    tmp_path: Path,
) -> None:
    """A lock only five minutes old could still belong to a live writer.

    The age bound separates a crashed writer from a slow one, so a young, still,
    unheld lock is left alone and the refusal names its age.
    """
    _, work = _init_checkout(tmp_path)
    head = _reviewed_head(work)
    lock = _aged_lock(work, FIVE_MINUTES)

    with pytest.raises(merge.MergeError) as caught:
        _land(work, head, attempts=2, retry_seconds=0.01)

    message = str(caught.value)
    assert "index.lock" in message
    assert re.search(r"aged 3[0-9]\ds", message), (
        f"the refusal names the age: {message}"
    )
    assert lock.exists(), "a lock younger than the bound must survive"
