"""A probe that ran out of time is retried, not read as a broken image.

The import probe bounds how long it will wait for a fresh interpreter to load
every module under ``reckon/``. Under load it can exceed that bound on a
checkout that loads perfectly well, and a timeout must not then freeze the
follower onto the old image until the next merge: it is the instrument running
out of budget, not a property of the code. Re-probing at the follower's own
cadence would only make a slow node slower, so the retry waits out a gap that
grows on each further timeout. A probe that ran and genuinely failed is a
property of the code, and that one still holds until the code moves.
"""

from __future__ import annotations

import subprocess
from importlib import import_module

import pytest

from reckon import crew_follow_commands as follow
from reckon.crew import runs

PROJECT = "reload-timeout-fixture"
MOVED_STAMP = "moved"
STALE_STAMP = "stale"


@pytest.fixture()
def reloader(monkeypatch):
    """A reloader holding a stale image at a moved code stamp, plus its execs.

    The tree is reported clean, the code stamp is held steady, and the clock
    ``poll`` reads is a value the test advances, so the only variables left are
    what the import probe answers and how much time has passed.
    """
    monkeypatch.setattr(runs, "follower_code_stamp", lambda: MOVED_STAMP)
    instance = follow._FollowerReloader(PROJECT, None)
    instance.code_stamp = STALE_STAMP
    monkeypatch.setattr(
        follow.checkout_trust,
        "tracked_reckon_tree_is_dirty",
        lambda root: (False, ""),
    )
    dispatch = import_module("reckon.crew.dispatch")
    monkeypatch.setattr(dispatch, "_export_launched_workers_for_reexec", lambda: None)
    clock = [1000.0]
    monkeypatch.setattr(follow.time, "monotonic", lambda: clock[0])
    execs: list = []
    monkeypatch.setattr(follow.os, "execve", lambda *args, **kwargs: execs.append(args))
    return instance, execs, clock


def _advance(clock, seconds):
    """Move the clock ``poll`` reads, so a wait is measured and not slept."""
    clock[0] += seconds


def _probe(monkeypatch, calls, answer):
    """Point the import probe's subprocess at ``answer``, counting every call."""

    def run(*args, **kwargs):
        calls.append(args)
        return answer()

    monkeypatch.setattr(follow.checkout_trust.subprocess, "run", run)


def _timed_out():
    raise subprocess.TimeoutExpired("probe", follow.checkout_trust.IMPORT_PROBE_TIMEOUT)


def _loads():
    return subprocess.CompletedProcess([], 0, "", "")


def _fails_to_import():
    return subprocess.CompletedProcess(
        [], 1, "", "ModuleNotFoundError: No module named 'reckon.a_module_that_is_gone'"
    )


def test_a_timed_out_probe_is_named_a_timeout_and_retried(
    reloader, monkeypatch, capsys
):
    instance, execs, clock = reloader
    calls: list = []
    _probe(monkeypatch, calls, _timed_out)

    instance.poll({})
    line = capsys.readouterr().out
    assert "timed out" in line, line
    assert "does not import" not in line, line
    assert line.count("timed out") == 1, f"the reason was nested in itself: {line!r}"
    assert len(calls) == 1
    # A timeout says nothing about the code, so nothing is held back by it.
    assert instance.deferred_stamp is None
    assert execs == []

    # A poll inside the gap the timeout earned does not probe again.
    _advance(clock, 2.0)
    instance.poll({})
    assert capsys.readouterr().out == ""
    assert len(calls) == 1

    # A second consecutive timeout probes, and is not said a second time.
    _advance(clock, follow.FOLLOWER_RELOAD_PROBE_RETRY_INITIAL_SECONDS)
    instance.poll({})
    assert capsys.readouterr().out == "", (
        "a second consecutive timeout was echoed again"
    )
    assert len(calls) == 2
    assert instance.deferred_stamp is None

    # The probe answers on the first check past the doubled gap and the code has
    # not moved again, so the reload proceeds.
    _probe(monkeypatch, calls, _loads)
    _advance(clock, follow.FOLLOWER_RELOAD_PROBE_RETRY_INITIAL_SECONDS * 2)
    instance.poll({})

    assert len(calls) == 3, "the retry did not probe again"
    assert len(execs) == 1, "the reload was not performed after the probe answered"


def test_a_moved_stamp_starts_the_gap_afresh(reloader, monkeypatch, capsys):
    instance, execs, clock = reloader
    calls: list = []
    _probe(monkeypatch, calls, _timed_out)

    instance.poll({})
    capsys.readouterr()
    assert len(calls) == 1

    # The code moves again: the checkout now on disk is probed at once rather
    # than waiting out the gap the previous one earned.
    monkeypatch.setattr(runs, "follower_code_stamp", lambda: "moved-again")
    _advance(clock, 2.0)
    instance.poll({})

    line = capsys.readouterr().out
    assert "timed out" in line, line
    assert len(calls) == 2, "a moved stamp waited out the previous checkout's gap"
    assert execs == []


def test_a_failed_import_defers_and_names_the_module(reloader, monkeypatch, capsys):
    instance, execs, clock = reloader
    calls: list = []
    _probe(monkeypatch, calls, _fails_to_import)

    instance.poll({})
    line = capsys.readouterr().out
    assert "does not import" in line, line
    assert "reckon.a_module_that_is_gone" in line, line
    assert len(calls) == 1
    assert instance.deferred_stamp == MOVED_STAMP

    _advance(clock, 2.0)
    instance.poll({})

    assert len(calls) == 1, (
        "an import failure was re-probed though the code had not moved"
    )
    assert execs == []
