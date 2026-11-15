"""A landing commit retries a lock that was released before it looked again.

A peer committing in the same checkout holds ``.git/index.lock`` for
milliseconds, which is long enough to refuse a landing's ``git add`` and short
enough to be gone by the time the wait helper asks whether the file is still
there. Reading only the file then returns the failure after one attempt, and
the landing loses a race it had already been told to wait out.

These cases drive ``_run_waiting_out_a_held_index_lock`` in a throwaway
repository, with a second process holding the lock so the failure is git's own:

* a lock released just after the failed call lets the call succeed on its retry,
  and the retry is recorded;
* a lock held for the whole call is retried exactly to the bound and refuses,
  with the attempt count returned;
* a call that succeeded is run once, even with a lock held at the check, so a
  second merge or commit can never be attempted over a success;
* a failure with any other cause, and a failure naming another checkout's lock,
  return after one attempt.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from reckon.crew.merge import _run_waiting_out_a_held_index_lock

# A second process that takes the checkout's index lock and holds it until the
# test signals, by creating the release file, that the first call has failed.
_HOLD_THE_LOCK = """
import os
import sys
import time

lock, release = sys.argv[1], sys.argv[2]
os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
deadline = time.monotonic() + 60
while not os.path.exists(release):
    if time.monotonic() > deadline:
        break
    time.sleep(0.001)
try:
    os.unlink(lock)
except OSError:
    pass
"""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _repo(root: Path, name: str = "checkout") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    (repo / "landed.txt").write_text("a landed change\n", encoding="utf-8")
    return repo


def _lock(repo: Path) -> Path:
    return repo / ".git" / "index.lock"


def _hold(lock: Path, release: Path) -> subprocess.Popen[bytes]:
    """Start the second process that creates ``lock`` and removes it when released."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_THE_LOCK, str(lock), str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10.0
    while not lock.exists():
        assert holder.poll() is None, (
            "the holder exited without taking the lock: "
            + holder.stderr.read().decode("utf-8", "replace")
        )
        assert time.monotonic() < deadline, "the holder never created the lock"
        time.sleep(0.001)
    return holder


def _release(release: Path) -> None:
    release.write_text("", encoding="utf-8")


def test_a_lock_released_after_the_failed_call_still_waits(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    lock = _lock(repo)
    release = tmp_path / "release"
    attempts: list[int] = []
    retries: list[subprocess.CompletedProcess[str]] = []
    released_after: list[float] = []
    holder = _hold(lock, release)

    def step(attempt: int) -> subprocess.CompletedProcess[str]:
        attempts.append(attempt)
        result = _git(repo, "add", "--", "landed.txt")
        if attempt == 1:
            failed_at = time.monotonic()
            _release(release)
            deadline = time.monotonic() + 0.05
            while lock.exists():
                assert time.monotonic() < deadline, (
                    "the holder still held the lock 50 ms after the failed call"
                )
                time.sleep(0.001)
            released_after.append(time.monotonic() - failed_at)
        return result

    try:
        result, retry_count = _run_waiting_out_a_held_index_lock(
            repo,
            step,
            attempts=4,
            retry_seconds=0.01,
            on_retry=lambda number, failed: retries.append(failed),
        )
    finally:
        holder.wait(timeout=10)

    assert attempts == [1, 2], "the released lock is retried once"
    assert len(retries) == 1, "the retry is recorded through on_retry"
    assert retry_count == 1
    assert "index.lock" in retries[0].stderr, (
        "the first call failed on this checkout's lock: " + retries[0].stderr
    )
    assert released_after[0] < 0.05
    assert result.returncode == 0, result.stderr
    assert _git(repo, "diff", "--cached", "--name-only").stdout.split() == [
        "landed.txt"
    ], "the retried call is the one that staged the landing write"


def test_a_lock_held_for_the_whole_call_is_retried_to_the_bound(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    lock = _lock(repo)
    release = tmp_path / "release"
    attempts: list[int] = []
    retries: list[int] = []
    holder = _hold(lock, release)

    def step(attempt: int) -> subprocess.CompletedProcess[str]:
        attempts.append(attempt)
        return _git(repo, "add", "--", "landed.txt")

    try:
        result, retry_count = _run_waiting_out_a_held_index_lock(
            repo,
            step,
            attempts=5,
            retry_seconds=0.01,
            on_retry=lambda number, _failed: retries.append(number),
        )
    finally:
        _release(release)
        holder.wait(timeout=10)

    assert attempts == [1, 2, 3, 4, 5], "a held lock is tried exactly to the bound"
    assert retries == [1, 2, 3, 4], "every retry is recorded, and none beyond the bound"
    assert retry_count == 4
    assert result.returncode != 0, "a lock held past the bound refuses"
    assert "index.lock" in result.stderr


def test_a_successful_call_is_not_retried_while_a_lock_is_held(
    tmp_path: Path,
) -> None:
    """A call that succeeded must not run twice because a peer's lock is present.

    Re-running a successful step lands a second merge commit or reports a
    landed commit as uncommittable, so the wait is asked only about a failure.
    """
    repo = _repo(tmp_path)
    lock = _lock(repo)
    release = tmp_path / "release"
    attempts: list[int] = []
    retries: list[int] = []
    held_at_step: list[bool] = []
    holder = _hold(lock, release)

    def step(attempt: int) -> subprocess.CompletedProcess[str]:
        attempts.append(attempt)
        held_at_step.append(lock.exists())
        return subprocess.CompletedProcess(
            args=["git", "merge", "--ff-only"],
            returncode=0,
            stdout="Fast-forward\n",
            stderr="",
        )

    try:
        result, retry_count = _run_waiting_out_a_held_index_lock(
            repo,
            step,
            attempts=4,
            retry_seconds=0.01,
            on_retry=lambda number, _failed: retries.append(number),
        )
    finally:
        _release(release)
        holder.wait(timeout=10)

    assert held_at_step == [True], "the instrument held the lock the step ran under"
    assert attempts == [1], "a successful call is run once, whatever lock exists"
    assert retries == []
    assert retry_count == 0
    assert result.returncode == 0


def test_a_failure_with_another_cause_is_returned_after_one_attempt(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    attempts: list[int] = []
    retries: list[int] = []

    def step(attempt: int) -> subprocess.CompletedProcess[str]:
        attempts.append(attempt)
        return _git(repo, "rev-parse", "--verify", "no-such-revision")

    result, retry_count = _run_waiting_out_a_held_index_lock(
        repo,
        step,
        attempts=4,
        retry_seconds=0.01,
        on_retry=lambda number, _failed: retries.append(number),
    )

    assert attempts == [1], "a failure with no lock behind it is not retried"
    assert retries == []
    assert retry_count == 0
    assert result.returncode != 0
    assert "index.lock" not in result.stderr
    assert not _lock(repo).exists(), "the instrument held no lock of this checkout"


def test_a_failure_naming_another_checkouts_lock_is_not_retried(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    other = _repo(tmp_path, "another-checkout")
    other_lock = _lock(other)
    release = tmp_path / "release"
    attempts: list[int] = []
    holder = _hold(other_lock, release)

    def step(attempt: int) -> subprocess.CompletedProcess[str]:
        attempts.append(attempt)
        return subprocess.CompletedProcess(
            args=["git", "add"],
            returncode=128,
            stdout="",
            stderr=(f"fatal: Unable to create '{other_lock}': File exists.\n"),
        )

    try:
        result, retry_count = _run_waiting_out_a_held_index_lock(
            repo, step, attempts=4, retry_seconds=0.01
        )
    finally:
        _release(release)
        holder.wait(timeout=10)

    assert str(other_lock) != str(_lock(repo))
    assert attempts == [1], "another checkout's lock is not this one's to wait for"
    assert retry_count == 0
    assert result.returncode != 0
    assert str(other_lock) in result.stderr
