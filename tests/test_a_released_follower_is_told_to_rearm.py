"""A session whose follower has stopped is told to arm it again.

A session's obligations snapshot is republished only while that session has a
delivering follower, so once the follower stops the snapshot keeps the older
code stamp it was written with and ages. That combination read as a stale
producer, and the remedy it carried named the producer -- a command the session
cannot usefully run, because the producer is healthy and publishing for every
session that still has a follower.

Three readings are measured against a temporary crew home:

* a session whose registration is released, with a snapshot older than the
  freshness window and a code stamp other than the checkout's, reads
  ``follower-released``, and its not-fresh line names the stopped follower and
  the snapshot's age and ends with the follower-arming command;
* the same snapshot beside a delivering registration keeps the stale-code
  reading and the producer's own remedy.
* a released session whose snapshot is inside the freshness window keeps that
  same stale-code reading, so the new one only speaks for an aged snapshot.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.hooks import coordinator_obligations as hook

PROJECT = "released-follower-fixture"
SESSION = "released-follower-session"
OLDER_CODE = "older-code-than-the-checkout"
FOLLOW = f"reckon crew follow --project {PROJECT} --session {SESSION}"


@pytest.fixture()
def config_home(tmp_path, monkeypatch):
    """A temporary crew home every path in this module resolves through."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


def _released_follower() -> None:
    """A registration that was armed and has since let go of its lock."""
    with runs.follower_registration(PROJECT, SESSION, delivery="stream"):
        pass


def _registration_live() -> bool:
    """The publisher's own answer about this session's follower."""
    return bool(runs.follower_state(PROJECT, SESSION)["live"])


def _snapshot(*, age_seconds: float) -> dict[str, Any]:
    """One snapshot as the last follower's producer would have written it."""
    pid = os.getpid()
    return {
        "computed_at": (
            datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
        ).isoformat(),
        "producer": {
            "pid": pid,
            "pid_start_time": obligation_snapshot.process_start_time(pid),
            "code_stamp": OLDER_CODE,
        },
        "obligations": [],
    }


def _reading(document: dict[str, Any]) -> tuple[str, str]:
    return obligation_snapshot.freshness_reading(
        document, project=PROJECT, session=SESSION
    )


def _line(state: str, document: dict[str, Any]) -> str:
    return hook.not_fresh_line(
        state, project=PROJECT, session=SESSION, document=document
    )


def test_a_released_follower_aged_past_the_window_is_told_to_rearm(
    config_home,
) -> None:
    """The reported state: no follower, an aged snapshot, older code.

    The registration's own liveness is asserted first, so a reading that came
    from a registration this test never released cannot pass for this one.
    """
    _released_follower()
    assert _registration_live() is False, "the fixture follower is still delivering"
    document = _snapshot(age_seconds=7_200)

    state, _host = _reading(document)

    assert state == obligation_snapshot.FOLLOWER_RELEASED, state
    line = _line(state, document)
    assert "this session's follower has stopped" in line, line
    age = re.search(r"last published (\d+)s ago", line)
    assert age is not None, line
    assert 7_100 <= int(age.group(1)) <= 7_300, line
    assert line.endswith("to arm this session's follower"), line
    assert FOLLOW in line, line
    assert "reloading" not in line, line
    assert "the seat is held on" not in line, line
    assert "older code" not in line, line


def test_a_live_follower_keeps_the_stale_code_reading(config_home) -> None:
    """The same snapshot beside a delivering follower says nothing new."""
    _released_follower()
    document = _snapshot(age_seconds=7_200)
    with runs.follower_registration(PROJECT, SESSION, delivery="stream"):
        assert _registration_live() is True, "the fixture follower is not delivering"

        state, _host = _reading(document)

    assert state == obligation_snapshot.STALE_CODE, state
    line = _line(state, document)
    assert "the producer is running older code than the checkout" in line, line
    assert "this session's follower has stopped" not in line, line


def test_a_released_follower_inside_the_window_is_unaffected(config_home) -> None:
    """A snapshot the producer could have written a moment ago says nothing."""
    _released_follower()
    assert _registration_live() is False, "the fixture follower is still delivering"
    document = _snapshot(age_seconds=30)

    state, _host = _reading(document)

    assert state == obligation_snapshot.STALE_CODE, state
    line = _line(state, document)
    assert "the producer is running older code than the checkout" in line, line
    assert "this session's follower has stopped" not in line, line
