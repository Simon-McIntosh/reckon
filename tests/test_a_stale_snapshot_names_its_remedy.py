"""A stale snapshot's remedy names what can actually refresh it.

A session's snapshot is republished only while that session has a live
follower, so a coordinator that stops its follower keeps reading a snapshot
that ages out while the project's producer is healthy and publishing every
other session. The not-current line used to answer that reading with the seat
command, which reports that the seat is held and cannot refresh anything.
These cases pin the line to the reading: a stale snapshot beside a live
producer lease names this session's own follower arming, and a stale snapshot
whose lease has expired or that has no lease at all keeps the seat command.

Every case builds a temporary configuration home: a mount for the fixture
repository, a released follower registration for the session, a stale snapshot
whose recorded producer is this live process, and the lease registration the
watch seat writes -- fresh, aged, or absent. The drives run as subprocesses
against that home, so no case reads or writes the operator's own state.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew.obligations import obligations as obligations_view
from reckon.hooks.coordinator_obligations import (
    PRODUCER_LEASE_SECONDS,
    producer_lease_path,
    producer_lease_seconds,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "stale-remedy-fixture"
SESSION = "stale-remedy-session"
FOLLOW_REMEDY = f"reckon crew follow --project {PROJECT} --session {SESSION}"
ENSURE_REMEDY = f"reckon crew watch --ensure --project {PROJECT}"
# The publication is old enough to be stale under any plausible window, and the
# expired lease is a full hour behind so it reads lapsed whatever the interval.
STALE_AGE_SECONDS = 900
EXPIRED_LEASE_AGE_SECONDS = 3_600

# A pid no process can hold, so the drive's session resolves through the
# registration's own recorded name rather than an ambient harness identity.
_ABSENT_HARNESS_PID = 2_147_483_647


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def repository(tmp_path: Path, config_home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed stale remedy fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _stop_the_follower() -> None:
    """Register the session, then release it, as a session with no follower.

    The release leaves the registration file in place on purpose, so the hook
    still resolves the session by the name it registered under; only the
    delivery is gone, which is the state a session that stopped its follower
    keeps while its snapshot ages out.
    """
    with runs.follower_registration(PROJECT, SESSION):
        pass


def _publish_stale_snapshot() -> None:
    """Publish this session's stale snapshot, its producer being this process.

    The recorded pid, start time and code stamp are this live process's, so the
    snapshot's own reading is the one this section is about: a live producer
    whose publication has aged past the freshness window, not an absence. The
    drive reads the same source tree this module was imported from.
    """
    document = obligation_snapshot.document_for(
        obligations_view(PROJECT, SESSION),
        computed_at=datetime.now(tz=UTC) - timedelta(seconds=STALE_AGE_SECONDS),
        stream_offset=0,
        producer={
            "pid": os.getpid(),
            "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
            "started_at": datetime.now(tz=UTC).isoformat(),
            "code_stamp": obligation_snapshot.source_code_stamp(),
        },
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _plant_lease(*, age_seconds: float) -> None:
    """Write the seat's lease registration renewed the given age.

    Written through the producer's own registration writer, so the record and
    its location are the ones a real seat leaves, not a second reading of the
    naming rule.
    """
    runs.update_watch_registration(
        PROJECT,
        pid=os.getpid(),
        pid_start_time=obligation_snapshot.process_start_time(os.getpid()),
        lease_renewed_at=time.time() - age_seconds,
    )


def _drive(repository: Path) -> subprocess.CompletedProcess[str]:
    """Run the prompt hook as the harness does, against the temporary home."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    environment["RECKON_PRODUCER_LEASE_SECONDS"] = str(PRODUCER_LEASE_SECONDS)
    payload = {
        "session_id": SESSION,
        "cwd": str(repository),
        "hook_event_name": "UserPromptSubmit",
    }
    return subprocess.run(
        [sys.executable, str(HOOK), "--hook", "prompt"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _line(completed: subprocess.CompletedProcess[str]) -> str:
    """The single not-current line one drive emitted."""
    assert completed.returncode == 0
    assert completed.stderr == "", completed.stderr
    text = json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]
    assert text.strip(), "a not-fresh drive must speak its line"
    assert "\n" not in text, text
    return text


def test_a_stale_snapshot_beside_a_live_lease_names_the_follower(
    config_home: Path, repository: Path
) -> None:
    """A healthy seat with a stale list earns the follower arming, not the seat.

    The producer stands behind the seat -- its lease was renewed this instant --
    and the snapshot is stale only because the session stopped its follower, so
    the line must name the command that refreshes this session's list. The seat
    command is the wrong answer here: it reports that the seat is held and
    fixes nothing. The reading is asserted positively as well, so the case can
    never pass by falling into the no-producer branch.
    """
    _stop_the_follower()
    _publish_stale_snapshot()
    _plant_lease(age_seconds=0.0)

    line = _line(_drive(repository))

    assert re.search(r"the snapshot is \d+s old", line), line
    assert "no producer" not in line, line
    assert FOLLOW_REMEDY in line, line
    assert "watch --ensure" not in line, line


def test_a_stale_snapshot_beside_an_expired_lease_keeps_the_seat_remedy(
    config_home: Path, repository: Path
) -> None:
    """A lapsed lease is no live producer, so the seat command stays.

    The lease renewal is a full interval behind, which is what the producer
    itself reads as its own expiry, so the seat is not healthy and arming a
    follower against it would be the wrong instruction. The snapshot's age is
    asserted beside the remedy so the case is about the lease and not about a
    snapshot that read as something else.
    """
    _stop_the_follower()
    _publish_stale_snapshot()
    _plant_lease(age_seconds=EXPIRED_LEASE_AGE_SECONDS)

    line = _line(_drive(repository))

    assert re.search(r"the snapshot is \d+s old", line), line
    assert ENSURE_REMEDY in line, line
    assert "crew follow" not in line, line


def test_a_stale_snapshot_with_no_lease_keeps_the_seat_remedy(
    config_home: Path, repository: Path
) -> None:
    """No lease record at all names the seat command, as it always did."""
    _stop_the_follower()
    _publish_stale_snapshot()

    line = _line(_drive(repository))

    assert re.search(r"the snapshot is \d+s old", line), line
    assert ENSURE_REMEDY in line, line
    assert "crew follow" not in line, line


def test_the_lease_reader_looks_where_the_seat_writes(
    config_home: Path,
) -> None:
    """The hook's lease path is pinned against the writer's own path.

    The reader derives the registration's name itself, so this is the check
    that keeps its derivation and the producer's from drifting apart.
    """
    assert producer_lease_path(PROJECT) == runs.watch_registration_path(PROJECT)


def test_the_lease_interval_mirrors_the_producers(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook ages a lease against the same interval the producer does.

    The two live in modules that do not import each other, so the hook's copy
    is asserted against the producer's own figures -- default and override --
    rather than trusted.
    """
    monkeypatch.delenv("RECKON_PRODUCER_LEASE_SECONDS", raising=False)
    assert PRODUCER_LEASE_SECONDS == runs.DEFAULT_PRODUCER_LEASE_SECONDS
    assert producer_lease_seconds() == runs.producer_lease_seconds()
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", "17")
    assert producer_lease_seconds() == runs.producer_lease_seconds() == 17.0
