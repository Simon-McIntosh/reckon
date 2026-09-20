"""A worker signal reaches the worker, and never the whole account.

``os.killpg`` takes a process GROUP id, so ``killpg(1, ...)`` is ``kill(-1,
...)``: every process the caller may signal, across every control group and
session. A pid read from a scan or a run record can report a group it does not
lead, and at the call site that argument looks like one process. These tests
pin the distinction so the reach cannot widen again unnoticed.
"""

from __future__ import annotations

import os
import signal

from reckon.crew import routing


def _record(monkeypatch, group_of):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(routing.os, "getpgid", group_of)
    monkeypatch.setattr(routing.os, "killpg", lambda g, s: calls.append(("killpg", g)))
    monkeypatch.setattr(routing.os, "kill", lambda p, s: calls.append(("kill", p)))
    return calls


def test_a_process_that_does_not_lead_its_group_is_signalled_alone(monkeypatch):
    """The group belongs to unrelated work, so only the process is signalled."""
    calls = _record(monkeypatch, lambda pid: 4242 if pid else 777)
    assert routing.signal_worker(9999, signal.SIGTERM) is True
    assert calls == [("kill", 9999)]


def test_the_broadcast_group_is_never_signalled(monkeypatch):
    """Group 1 is the account-wide broadcast and must never be passed to killpg."""
    calls = _record(monkeypatch, lambda pid: 1 if pid else 777)
    assert routing.signal_worker(9999, signal.SIGTERM) is True
    assert calls == [("kill", 9999)]
    assert all(kind != "killpg" for kind, _ in calls)


def test_the_callers_own_group_is_never_signalled(monkeypatch):
    """Signalling the caller's own group would end the caller mid-release."""
    calls = _record(monkeypatch, lambda pid: 555)
    assert routing.signal_worker(555, signal.SIGTERM) is True
    assert calls == [("kill", 555)]


def test_a_detached_worker_leading_its_group_still_takes_its_children(monkeypatch):
    """The group signal is kept for the case it exists for: a real session leader."""
    calls = _record(monkeypatch, lambda pid: 9999 if pid else 777)
    assert routing.signal_worker(9999, signal.SIGTERM) is True
    assert calls == [("killpg", 9999)]


def test_a_vanished_process_is_not_an_error(monkeypatch):
    def gone(pid):
        raise ProcessLookupError

    monkeypatch.setattr(routing.os, "getpgid", gone)
    assert routing.signal_worker(9999, signal.SIGTERM) is False
