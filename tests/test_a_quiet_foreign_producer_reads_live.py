"""A quiet producer on another host is read through its renewed seat lease."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, runs, watch_unit
from reckon.crew.host_lease import HostLease
from tests.helpers.git import git

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "quiet-foreign-producer"
SESSION = "quiet-foreign-session"
FOREIGN_HOST = "producer-elsewhere.invalid"
OTHER_HOST = "different-holder.invalid"
FOLLOW = f"reckon crew follow --project {PROJECT} --session {SESSION}"
ENSURE = f"reckon crew watch --ensure --project {PROJECT}"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(path))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(path / "mounts.json"))
    return path


@pytest.fixture
def repository(tmp_path: Path, home: Path) -> Path:
    path = tmp_path / "repo"
    (path / "docs" / "state" / PROJECT).mkdir(parents=True)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed fixture"),
    ):
        git(path, *args)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(path / "docs")}), encoding="utf-8"
    )
    with runs.follower_registration(PROJECT, SESSION):
        pass
    return path


def _seat(*, stamp: str, lease_host: str = FOREIGN_HOST, lease_age: int = 0) -> None:
    assert socket.gethostname() not in (FOREIGN_HOST, OTHER_HOST)
    path = runs.watch_lock_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "host": FOREIGN_HOST,
                "pid": 2_147_483_647,
                "stall_window": "10m",
                "code_stamp": stamp,
            }
        ),
        encoding="utf-8",
    )
    stream = runs.watch_stream_path(PROJECT)
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text("", encoding="utf-8")
    quiet = time.time() - 3_600
    os.utime(stream, (quiet, quiet))
    lease = HostLease(path.parent, path.stem, lease_host, 2_147_483_647, "")
    assert lease.claim()
    if lease_age:
        old = time.time() - lease_age
        os.utime(lease.path, (old, old))


def _line(repository: Path) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["CLAUDE_PID"] = "2147483647"
    run = subprocess.run(
        [sys.executable, str(HOOK), "--hook", "prompt"],
        input=json.dumps(
            {
                "session_id": SESSION,
                "cwd": str(repository),
                "hook_event_name": "UserPromptSubmit",
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert not run.stderr, run.stderr
    return json.loads(run.stdout)["hookSpecificOutput"]["additionalContext"]


def test_fresh_host_lease_keeps_quiet_foreign_seat_live(
    repository: Path,
) -> None:
    _seat(stamp=obligation_snapshot.source_code_stamp())

    assert watch_unit.live_foreign_seat_host(PROJECT) == FOREIGN_HOST
    line = _line(repository)
    assert FOREIGN_HOST in line and FOLLOW in line, line
    assert "no producer" not in line and "watch --ensure" not in line, line


@pytest.mark.parametrize("holder,age", [(FOREIGN_HOST, 31), (OTHER_HOST, 0)])
def test_stale_or_mismatched_lease_does_not_revive_quiet_seat(
    repository: Path, holder: str, age: int
) -> None:
    _seat(
        stamp=obligation_snapshot.source_code_stamp(), lease_host=holder, lease_age=age
    )

    assert watch_unit.live_foreign_seat_host(PROJECT) == ""
    line = _line(repository)
    assert "no producer" in line and ENSURE in line, line


@pytest.mark.parametrize("seat_is_current", [True, False])
def test_snapshot_from_predecessor_uses_seated_producers_stamp(
    repository: Path, seat_is_current: bool
) -> None:
    current = obligation_snapshot.source_code_stamp()
    old = "older-code-stamp"
    _seat(stamp=current if seat_is_current else old)
    computed = datetime.now(tz=UTC) - timedelta(hours=20)
    document = {
        "computed_at": computed.isoformat(),
        "producer": {
            "pid": 2_147_483_647,
            "pid_start_time": "0",
            "code_stamp": old,
        },
        "obligations": {},
    }
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)
    runs.update_watch_registration(
        PROJECT, pid=2_147_483_647, pid_start_time="0", lease_renewed_at=time.time()
    )

    expected = (
        obligation_snapshot.STALE_SNAPSHOT
        if seat_is_current
        else obligation_snapshot.STALE_CODE
    )
    assert obligation_snapshot.freshness(document, project=PROJECT) == expected
    line = _line(repository)
    if seat_is_current:
        age = re.search(r"the snapshot is (\d+)s old", line)
        assert age is not None and 71_900 <= int(age.group(1)) <= 72_100, line
        assert FOLLOW in line, line
        assert all(
            word not in line
            for word in ("no producer", "producer reloading", "--ensure")
        ), line
    else:
        assert "producer reloading" in line, line
