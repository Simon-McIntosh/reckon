"""A reload deferral is said once for its whole span, however many stamps pass.

The follower's own image proof contends with peers' landings for the import
proof lock, so while a peer holds it a probe can run out of its bound and the
reload is deferred. That line is owed once per contiguous run of deferrals: a
landing that moves the checkout stamp under a follower is not news about the
follower's own image, and reprinting the line for each one is the noise a pane
9h27m old filled with.

A stamp move still starts the retry gap afresh, and a probe that answers with
a genuine import failure, or answers and lets the reload proceed, ends the run
and clears the line. A later run is owed its own line.
"""

from __future__ import annotations

import fcntl
import subprocess
from importlib import import_module

import pytest

from reckon import checkout_trust
from reckon import crew_follow_commands as follow
from reckon.crew import runs

PROJECT = "reload-deferral-fixture"
STALE_STAMP = "stale"
DEFERRAL = "deferred its reload"


class _HeldLock:
    """A peer's hold on the image's import proof lock.

    The lock file lives outside the checkout: it stands for the peer whose
    landing keeps this follower's proof from answering, so where the bytes sit
    is immaterial, and a file in the source tree would not be.
    """

    def __init__(self, tmp_path) -> None:
        self.path = tmp_path / checkout_trust.IMPORT_PROBE_LOCK
        self.handle = None

    @property
    def held(self) -> bool:
        return self.handle is not None

    def hold(self) -> None:
        self.handle = self.path.open("a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        assert self.handle is not None
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


@pytest.fixture()
def contended(monkeypatch, tmp_path):
    """A reloader whose checkout stamp the test moves, and a peer's proof lock.

    The tree is reported clean, the clock is a value the test advances, and the
    proof is stood in so it cannot answer while the lock is held: the stand-in
    consults the lock exactly as a contended proof does. The only variables
    left are how often the checkout stamp moves and when the lock is released.
    """
    stamp = ["landing-0"]
    monkeypatch.setattr(runs, "follower_code_stamp", lambda: stamp[0])
    instance = follow._FollowerReloader(PROJECT, None)
    instance.code_stamp = STALE_STAMP
    monkeypatch.setattr(
        checkout_trust,
        "tracked_reckon_tree_is_dirty",
        lambda root: (False, ""),
    )
    dispatch = import_module("reckon.crew.dispatch")
    monkeypatch.setattr(dispatch, "_export_launched_workers_for_reexec", lambda: None)
    clock = [1000.0]
    monkeypatch.setattr(follow.time, "monotonic", lambda: clock[0])
    execs: list = []
    monkeypatch.setattr(follow.os, "execve", lambda *args, **kwargs: execs.append(args))
    lock = _HeldLock(tmp_path)
    lock.hold()
    return instance, execs, clock, stamp, lock


def _probe(monkeypatch, calls, lock):
    """Stand a proof in: it cannot answer while a peer holds the lock."""

    def run(*args, **kwargs):
        calls.append(args)
        if lock.held:
            raise subprocess.TimeoutExpired(
                "probe", checkout_trust.IMPORT_PROBE_TIMEOUT
            )
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(checkout_trust.subprocess, "run", run)


def _deferral_lines(capsys) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if DEFERRAL in line]


def _poll_when_gap_expires(instance, clock) -> None:
    """Poll at the instant the timeout's earned gap expires."""
    clock[0] = instance.probe_retry_at


def test_a_deferral_is_printed_once_while_the_stamp_moves(
    contended, monkeypatch, capsys
):
    instance, execs, clock, stamp, lock = contended
    calls: list = []
    _probe(monkeypatch, calls, lock)

    # The first check finds the peer's hold and defers: one line, owed for the
    # run of deferrals that starts here.
    instance.poll({})
    assert len(_deferral_lines(capsys)) == 1
    assert len(calls) == 1
    assert instance.timeout_deferral is True

    # Three peer landings during that deferral. Each moves the stamp and each
    # earns a fresh probe; none of them is news about this follower.
    reprinted: list[str] = []
    for landing in ("landing-1", "landing-2", "landing-3"):
        stamp[0] = landing
        _poll_when_gap_expires(instance, clock)
        instance.poll({})
        reprinted.extend(_deferral_lines(capsys))

    assert reprinted == [], (
        f"{len(reprinted)} deferral lines reprinted across three stamp moves: "
        f"{reprinted}"
    )

    assert len(calls) == 4, "a stamp move did not earn a fresh probe"
    assert execs == []

    # The peer releases the lock: the probe answers, the reload proceeds, and
    # nothing further is said about the deferral.
    lock.release()
    _poll_when_gap_expires(instance, clock)
    instance.poll({})

    assert _deferral_lines(capsys) == [], "the answer reprinted a deferral"
    assert len(calls) == 5, "the released lock did not earn a probe"
    assert len(execs) == 1, "the reload was not performed once the lock was free"


def test_a_second_deferral_is_owed_its_own_line(contended, monkeypatch, capsys):
    instance, execs, clock, stamp, lock = contended
    calls: list = []
    _probe(monkeypatch, calls, lock)

    instance.poll({})
    assert len(_deferral_lines(capsys)) == 1

    # The peer lets go and the reload proceeds, so the run is over.
    lock.release()
    _poll_when_gap_expires(instance, clock)
    instance.poll({})
    assert _deferral_lines(capsys) == []
    assert len(execs) == 1

    # A later peer hold, on a checkout that has moved again, is a new run of
    # deferrals, and a reader is owed one line for it.
    lock.hold()
    stamp[0] = "landing-4"
    clock[0] += 2.0
    instance.poll({})

    assert len(_deferral_lines(capsys)) == 1


def test_the_gap_still_starts_afresh_on_a_stamp_move(contended, monkeypatch, capsys):
    """Silencing the line must not cost the retry backoff its reset."""
    instance, _execs, clock, stamp, lock = contended
    calls: list = []
    _probe(monkeypatch, calls, lock)

    # Take timeouts until the gap reaches its cap, so a stale gap is visibly
    # different from a fresh one.
    for _ in range(6):
        instance.poll({})
        _poll_when_gap_expires(instance, clock)
    capsys.readouterr()
    assert instance.probe_retry_gap == follow.FOLLOWER_RELOAD_PROBE_RETRY_CAP_SECONDS

    # The code moves: the new checkout starts its gap rather than inheriting
    # the capped one.
    stamp[0] = "landing-5"
    instance.poll({})

    assert (
        instance.probe_retry_gap
        == follow.FOLLOWER_RELOAD_PROBE_RETRY_INITIAL_SECONDS * 2
    ), "a moved checkout inherited the previous checkout's gap"
