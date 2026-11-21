"""A directory row names the address a peer messages its session by.

The harness keeps one record per session, naming the session's process and the
name a peer message uses. That name rides the session's existing transport
entry, which is where a reader looks for the address; reachability is the
harness record's process, so a session whose harness record names a process
that has ended reads unreachable while still naming the session. A session the
harness keeps no record for reads null for both, which is distinct from a
session that has gone.

A session resumed under a new process leaves the finished record behind, so the
live record wins: the name a peer can reach is the running process's, not the
one that has ended.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon.crew.directory import directory

PROJECT = "power_convertor"
REPOSITORY = "/repos/power_convertor"
SESSION = "6f0a2b1c-1111-4a2b-9c3d-abcdef012345"
PEER_NAME = "power-convertor-df"
SOCKET = "/run/observed/session.sock"

#: A pid this host cannot hold, the suite's own convention for a process that
#: has ended: the kernel answers a zero signal with ProcessLookupError.
DEAD_PID = 2_147_483_647


def _harness_record(directory: Path, *, pid: int, name: str) -> None:
    """One harness record, as the harness keeps it for a session's process."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pid}.json").write_text(
        json.dumps({"sessionId": SESSION, "name": name, "pid": pid}),
        encoding="utf-8",
    )


def _sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sessions = tmp_path / "harness-sessions"
    monkeypatch.setenv("RECKON_HARNESS_SESSIONS_DIR", str(sessions))
    return sessions


def _pointer(home: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """One live pointer whose stream records the session's observed transport."""
    monkeypatch.setenv("RECKON_HOME", str(home))
    run_id = "run-live"
    log = home / "crew" / "runs" / run_id / "stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "messaging_socket_path": SOCKET,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "session": SESSION,
        "project": PROJECT,
        "repo": REPOSITORY,
        "worktree": f"{REPOSITORY}/.reckon-worktrees/{SESSION}/build-the-coil",
        "node": {
            "id": "build-the-coil",
            "plan": "coil-build",
            "section": "s1",
            "write_paths": [],
        },
        "phase": "working",
        "process_alive": True,
        "log_path": str(log),
    }


def _transport(record: dict) -> dict:
    view = directory(PROJECT, pointers=[record])
    (row,) = view["coordinators"]
    (transport,) = row["transports"]
    return transport


def test_a_live_session_returns_its_peer_name_and_reads_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _sessions_dir(tmp_path, monkeypatch)
    record = _pointer(tmp_path / "config", monkeypatch)
    _harness_record(sessions, pid=os.getpid(), name=PEER_NAME)

    transport = _transport(record)

    assert transport["address"] == SOCKET
    assert transport["name"] == PEER_NAME
    assert transport["reachable"] is True


def test_a_session_whose_process_has_ended_keeps_its_name_and_reads_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _sessions_dir(tmp_path, monkeypatch)
    record = _pointer(tmp_path / "config", monkeypatch)
    _harness_record(sessions, pid=DEAD_PID, name="power-convertor-0a")

    transport = _transport(record)

    assert transport["name"] == "power-convertor-0a"
    assert transport["reachable"] is False


def test_a_session_with_no_harness_record_reads_null_for_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sessions_dir(tmp_path, monkeypatch)
    record = _pointer(tmp_path / "config", monkeypatch)

    transport = _transport(record)

    assert transport["address"] == SOCKET
    assert transport["name"] is None
    assert transport["reachable"] is None


def test_a_live_record_wins_over_the_one_a_resume_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _sessions_dir(tmp_path, monkeypatch)
    record = _pointer(tmp_path / "config", monkeypatch)
    _harness_record(sessions, pid=DEAD_PID, name="power-convertor-0a")
    _harness_record(sessions, pid=os.getpid(), name=PEER_NAME)

    transport = _transport(record)

    assert transport["name"] == PEER_NAME
    assert transport["reachable"] is True
