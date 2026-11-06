"""A coordinator with nothing in flight is on the board.

The fleet's census of sessions lived in run pointers alone, so a session whose
runs had all ended and whose follower had settled named nowhere: it was still
attended, still holding its project, and no reader could see it. The session
host's census keeps a settled child with its session and project and a null
pid, and the directory lists each session a live host's census names.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from reckon import _store
from reckon.crew import session_host
from reckon.crew.directory import directory

PROJECT = "power_convertor"
REPOSITORY = "/repos/power_convertor"

#: A follower that reports it settled rather than failed.
SETTLING_FOLLOWER = [sys.executable, "-c", "import sys; sys.exit(66)"]

#: A follower that stays up, so its child remains live for the whole case.
LIVE_FOLLOWER = [sys.executable, "-c", "import time; time.sleep(60)"]

SETTLE_BOUND = 20.0


def _owner_for(process: subprocess.Popen) -> dict[str, Any]:
    """The owner identity the host records for a stub process."""
    return {
        "pid": process.pid,
        "start_time": session_host.process_start_time(process.pid) or "",
    }


def _host(
    home: Path,
    owner_process: subprocess.Popen,
    follower_argv: list[str],
) -> session_host.SessionHost:
    """A session host driven in this process, with its state under the home."""
    environ = dict(os.environ)
    environ["RECKON_HOME"] = str(home)
    environ["RECKON_SESSION_HOST_STATE_DIR"] = str(home / "crew" / "session-hosts")
    environ["RECKON_SESSION_HOST_LOG_DIR"] = str(home / "crew" / "session-logs")
    environ["RECKON_SESSION_HOST_POLL_SECONDS"] = "0.05"
    environ["XDG_RUNTIME_DIR"] = str(home / "run")
    return session_host.SessionHost(
        owner=_owner_for(owner_process),
        follower_argv=follower_argv,
        environ=environ,
    )


def _request(
    host: session_host.SessionHost, session: str, project: str = PROJECT
) -> None:
    host.handle(json.dumps({"project": project, "session": session}))


def _census(host: session_host.SessionHost) -> dict[str, Any]:
    try:
        return json.loads(host.record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"children": []}


def _tick_until_settled(host: session_host.SessionHost) -> None:
    """Run the host's supervision ticks until its follower has settled."""
    deadline = time.monotonic() + SETTLE_BOUND
    while time.monotonic() < deadline:
        host.tick()
        if any(
            child.get("settled_at") is not None for child in _census(host)["children"]
        ):
            return
        time.sleep(0.05)
    pytest.fail("timed out waiting for the follower to settle")


def _pointer(
    home: Path,
    *,
    run_id: str,
    session: str,
    node: str,
    plan: str,
    section: str,
) -> None:
    log = home / "crew" / "runs" / run_id / "stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"type": "system", "subtype": "init"}) + "\n")
    _store.write_json_atomically(
        home / "crew" / "live" / f"{run_id}.json",
        {
            "run_id": run_id,
            "session": session,
            "project": PROJECT,
            "repo": REPOSITORY,
            "worktree": f"{REPOSITORY}/.reckon-worktrees/{session}/{node}",
            "node": {
                "id": node,
                "plan": plan,
                "section": section,
                "write_paths": ["src/coil.py"],
            },
            "phase": "working",
            "process_alive": True,
            "log_path": str(log),
        },
    )


def _ledger(home: Path, rows: list[dict[str, Any]]) -> None:
    path = home / "state" / PROJECT / "crew.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"data": {"_version": 1, "members": [], "holds": [], "runs": rows}}),
        encoding="utf-8",
    )


def test_a_settled_child_stays_in_the_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A follower that settles keeps its session and project, with no pid."""
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    home = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv(
        "RECKON_SESSION_HOST_STATE_DIR", str(home / "crew" / "session-hosts")
    )
    host = _host(home, owner, SETTLING_FOLLOWER)
    try:
        _request(host, "idle-coordinator")
        _tick_until_settled(host)
        children = _census(host)["children"]
        assert len(children) == 1, children
        child = children[0]
        assert child["project"] == PROJECT, child
        assert child["session"] == "idle-coordinator", child
        assert child["pid"] is None, child
        assert child["settled_at"] is not None, child
        # The census a reader of the board sees names it too, and only while
        # the owner it belongs to still lives.
        assert [
            record["session"]
            for record in session_host.live_census()
            for record in record["children"]
        ] == ["idle-coordinator"]
    finally:
        owner.kill()
        owner.wait()


def test_the_board_lists_a_settled_coordinator_beside_a_live_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two live hosts for one project both appear, one with no runs in flight."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv(
        "RECKON_SESSION_HOST_STATE_DIR", str(home / "crew" / "session-hosts")
    )

    live_owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    idle_owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    gone_owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    gone_identity = _owner_for(gone_owner)
    gone_owner.kill()
    gone_owner.wait()

    live_host = _host(home, live_owner, LIVE_FOLLOWER)
    idle_host = _host(home, idle_owner, SETTLING_FOLLOWER)
    try:
        dispatched = "2026-11-05T12:00:00+00:00"
        _ledger(
            home,
            [
                {
                    "run_id": "run-live",
                    "session": "live-coordinator",
                    "project": PROJECT,
                    "plan": "in-flight",
                    "dispatched_at": dispatched,
                },
                {
                    "run_id": "run-earlier",
                    "session": "idle-coordinator",
                    "project": PROJECT,
                    "plan": "coil-build",
                    "dispatched_at": dispatched,
                },
            ],
        )
        _pointer(
            home,
            run_id="run-live",
            session="live-coordinator",
            node="build-the-coil",
            plan="in-flight",
            section="s1",
        )
        _request(live_host, "live-coordinator")
        _request(idle_host, "idle-coordinator")
        _tick_until_settled(idle_host)

        # A host whose owner has exited leaves a record behind; it names no
        # session, because the session it belonged to ended with that owner.
        (home / "crew" / "session-hosts").mkdir(parents=True, exist_ok=True)
        stale = home / "crew" / "session-hosts" / f"{gone_identity['pid']}-x.json"
        stale.write_text(
            json.dumps(
                {
                    "pid": gone_identity["pid"],
                    "owner": gone_identity,
                    "children": [
                        {
                            "project": PROJECT,
                            "session": "ended-coordinator",
                            "pid": None,
                            "settled_at": 1.0,
                        }
                    ],
                    "updated_at": 1.0,
                }
            ),
            encoding="utf-8",
        )

        view = directory(PROJECT)
        by_session = {row["session"]: row for row in view["coordinators"]}

        live = by_session["live-coordinator"]
        assert live["live_node_count"] == 1, live
        assert [run["run_id"] for run in live["runs"]] == ["run-live"], live

        idle = by_session["idle-coordinator"]
        assert idle["live_node_count"] == 0, idle
        assert idle["runs"] == [], idle
        assert idle["last_dispatch"] == dispatched, idle
        assert idle["settled_at"] is not None, idle

        assert "ended-coordinator" not in by_session, by_session
    finally:
        live_host.stop("test teardown")
        idle_host.stop("test teardown")
        for process in (live_owner, idle_owner):
            process.kill()
            process.wait()
