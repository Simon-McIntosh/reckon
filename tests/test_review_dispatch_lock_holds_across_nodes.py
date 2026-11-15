"""A plan-review dispatch lock is held by a peer on another node.

The dispatch takes a per-plan lock around its in-flight check and launch. A
sweep on a second node reaches the same plan review, so the lock has to exclude a
holder there. ``flock`` is granted by the local kernel and excludes nothing
across nodes; the record lock is held by the file server. This case holds only
the record lock from a second process, proves the ``flock`` half is free, and
shows the sweep moving on instead of composing the review.
"""

from __future__ import annotations

import fcntl
import subprocess
import sys
import textwrap

import pytest

from reckon.crew import plan_review
from reckon.crew import recovery_review_dispatch as dispatch

RECORD = {
    "subject": "plan",
    "project": "lock-proj",
    "plan_slug": "lock-plan",
    "run_id": "r-20261114T000000000000-plan-review-of-lock-plan",
}

# A holder that takes only the POSIX record lock. It prints "held" once the lock
# is taken and waits for one line on stdin before exiting, so the parent knows
# the lock is held before it dispatches.
RECORD_LOCK_HOLDER = textwrap.dedent(
    """
    import fcntl
    import sys

    handle = open(sys.argv[1], "a+b")
    fcntl.lockf(handle.fileno(), fcntl.LOCK_EX)
    print("held", flush=True)
    sys.stdin.readline()
    """
)


@pytest.fixture
def composed(home, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Stand in for the composition so a case measures only the lock."""
    calls: list[dict] = []

    def compose(record, fields, **_kwargs):
        calls.append(dict(record))
        return {"dispatched": True, "run_id": record["run_id"]}

    monkeypatch.setattr(dispatch, "_standing_plan_review", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "_review_dispatch_fields", lambda *_a, **_k: {})
    monkeypatch.setattr(dispatch, "_dispatch_composed_review", compose)
    return calls


def _lock_path():
    directory = plan_review.review_report_directory(
        RECORD["project"], RECORD["plan_slug"], RECORD["run_id"]
    ).parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "dispatch.lock"


class _RecordLockHolder:
    """A second process holding only the record lock on ``path``."""

    def __init__(self, path):
        self._process = subprocess.Popen(
            [sys.executable, "-c", RECORD_LOCK_HOLDER, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def __enter__(self):
        assert self._process.stdout.readline().strip() == "held"
        return self

    def __exit__(self, *_exc):
        if self._process.poll() is None:
            self._process.stdin.write("\n")
            self._process.stdin.flush()
            self._process.stdin.close()
        self._process.wait(10)


def test_a_record_lock_holder_in_another_process_moves_the_sweep_on(composed):
    path = _lock_path()
    with _RecordLockHolder(path):
        # The flock half is free: this is a record lock only, so it can only be
        # seen by the record lock the dispatch takes.
        with path.open("a+b") as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        report = dispatch._dispatch_text_review_for_run(dict(RECORD))
    assert report["dispatched"] is False
    assert "composing this plan's review now" in report["reason"]
    assert composed == []


def test_a_free_lock_still_dispatches(composed):
    report = dispatch._dispatch_text_review_for_run(dict(RECORD))
    assert report["dispatched"] is True
    assert len(composed) == 1
