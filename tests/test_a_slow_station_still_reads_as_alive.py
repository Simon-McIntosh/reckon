"""A station whose tick is slow still reads as alive; only a stopped one is cycled.

The station's beat used to be written by the tick, after ``classify_tick``. The
rest of a tick then walks every mounted project across ledger reads and path
resolution, and under a loaded filesystem that took minutes, so for most of
each tick every reader judged the station stopped and told its session to
restart a process that was working. Liveness and progress are separate facts
and are measured separately here:

* a station whose tick is blocked for three times the stale window keeps a
  fresh beat, written from its own thread, and reads ``slow`` with the stage it
  is in and that stage's age named;
* a station whose process has ended reads ``stopped`` within one window;
* the restart advice is emitted for the stopped station and never for the slow
  one, driven through the prompt hook the session actually runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import (
    obligation_snapshot,
    runs,
    station,
    station_obligations,
    station_store,
)
from reckon.hooks.coordinator_obligations import slow_station_note
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "slow-station-fixture"
SESSION = "slow-station-session"

# The stale window the runs below judge a beat by, in seconds. It is small so a
# tick can outlive it three times over inside one test; every reading is taken
# through the same constant the reader uses, so nothing here restates the
# product's two minutes.
WINDOW_SECONDS = 1.2

# The window the hook runs under. The hook is a separate process, so its window
# is the product's own and the markers written for it are placed relative to
# the real clock rather than the shortened one.
PRODUCT_WINDOW_SECONDS = obligation_snapshot.STATION_STALE_SECONDS

_A_WHILE_AGO = timedelta(seconds=PRODUCT_WINDOW_SECONDS * 3 + 60)
_ABSENT_HARNESS_PID = 2_147_483_647


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The node-local root the hook, the station and this test all resolve."""
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    return root


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
        ("commit", "-q", "-m", "test: seed the slow station fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def follower(config_home: Path):
    """Register the session exactly as a real follower arm would."""
    with runs.follower_claim(PROJECT, SESSION) as claim:
        assert claim[0] is True, claim[1]
        yield


def _stored_document(*, moment: datetime) -> dict[str, object]:
    """One session's stored obligations row, carrying a single open duty.

    The row is the shape a station writes: the derived payload wrapped under
    ``document``, with each duty's age stored as the instant it is measured
    from, which is what the reader turns back into an age.
    """
    duty = {
        "kind": "review",
        "run_id": "run-slow-station",
        "node": "slow-station-node",
        "age_since": moment.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "next_command": "reckon crew resume --run run-slow-station",
    }
    return {
        "document": {
            "project": PROJECT,
            "session": SESSION,
            "obligations": [duty],
            "acknowledged": [],
            "summary": {"count": 1},
        }
    }


def _seed_store(root: Path, *, moment: datetime) -> station_store.StationStore:
    store = station_store.StationStore(root)
    store.write_obligation(SESSION, _stored_document(moment=moment), changed_at=moment)
    return store


def _answer(*, moment: datetime | None = None) -> dict[str, object]:
    answer = obligation_snapshot.read_station_answer(
        PROJECT,
        SESSION,
        now=moment,
        environ=dict(os.environ),
    )
    assert answer is not None, "the seeded station row was not readable"
    return answer


def _wait_for(path: Path, *, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"the station never reached {what}")


_DRIVE_STATION = """\
import sys
import time
from pathlib import Path

from reckon.crew import station, station_obligations

entered, release = Path(sys.argv[1]), Path(sys.argv[2])
project, docs = sys.argv[3], Path(sys.argv[4])


def _blocked(*_args, **_kwargs):
    entered.write_text("1")
    deadline = time.monotonic() + 60
    while not release.exists():
        if time.monotonic() > deadline:
            raise SystemExit("the test never released the tick")
        time.sleep(0.02)


station_obligations.publish = _blocked
station.STATION_BEAT_SECONDS = 0.05
station.flight.mounted_project_docs = lambda: {project: docs}
station.run_station(once=True, interval=0.05)
"""


def test_a_blocked_tick_keeps_a_fresh_beat_and_reads_slow(
    config_home: Path,
    runtime_root: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick stuck in one project's refresh is slow, not stopped.

    The station runs as its own process, which is the only way it runs at all:
    it installs signal handlers, and the reading is taken from the parent, the
    way a hook on this node takes it.
    """
    monkeypatch.setattr(obligation_snapshot, "STATION_STALE_SECONDS", WINDOW_SECONDS)
    moment = datetime.now(tz=UTC)
    _seed_store(runtime_root, moment=moment).close()

    entered = repository.parent / "entered"
    release = repository.parent / "release"
    driver = repository.parent / "drive_station.py"
    driver.write_text(_DRIVE_STATION, encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    running = subprocess.Popen(
        [
            sys.executable,
            str(driver),
            str(entered),
            str(release),
            PROJECT,
            str(repository / "docs"),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(entered, seconds=30, what="the per-project refresh")
        # The tick is now blocked inside the stage, exactly where the night's
        # ticks spent their minutes. Wait out three windows.
        time.sleep(WINDOW_SECONDS * 3 + 0.3)
        answer = _answer()
        assert answer["state"] == "slow", answer
        assert answer["stale"] is False, answer
        assert answer["tick_stage"] == "per-project-refresh", answer
        assert answer["tick_age_seconds"] >= WINDOW_SECONDS * 3, answer
        # The beat moved while the tick was blocked: only a thread of its own
        # can have written it, because the tick is still inside the refresh.
        assert answer["heartbeat_age_seconds"] <= WINDOW_SECONDS, answer
    finally:
        release.write_text("1", encoding="utf-8")
        _out, err = running.communicate(timeout=60)
    assert running.returncode == 0, err
    assert err == "", err


def test_a_station_whose_process_has_ended_reads_stopped_within_one_window(
    config_home: Path,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ended station's beat ages into stopped, and its stage names no work."""
    monkeypatch.setattr(obligation_snapshot, "STATION_STALE_SECONDS", WINDOW_SECONDS)
    monkeypatch.setattr(station, "STATION_BEAT_SECONDS", 0.05)
    monkeypatch.setattr(
        station.flight, "mounted_project_docs", lambda: {PROJECT: config_home}
    )
    monkeypatch.setattr(station_obligations, "publish", lambda *_args, **_kwargs: None)
    moment = datetime.now(tz=UTC)
    _seed_store(runtime_root, moment=moment).close()

    # One real tick, which is the whole of a station's life that this arm
    # measures: it records its stages and stamps its finish, and then no
    # process is left to move the beat.
    store = station_store.StationStore(runtime_root)
    station.Station(store, projects=lambda: {PROJECT: config_home}).tick()
    store.close()
    ended = datetime.now(tz=UTC)
    inside = _answer(moment=ended)
    assert inside["state"] == "live", inside
    assert inside["stale"] is False, inside

    past = ended + timedelta(seconds=WINDOW_SECONDS + 1.0)
    stopped = _answer(moment=past)
    assert stopped["state"] == "stopped", stopped
    assert stopped["stale"] is True, stopped
    # A finished tick leaves its stage behind but claims no running age: the
    # station is not slow, it is gone, and the two must not be confused.
    assert stopped["tick_age_seconds"] is None, stopped


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _prompt_payload(repository: Path) -> dict[str, object]:
    return {
        "session_id": SESSION,
        "cwd": str(repository),
        "hook_event_name": "UserPromptSubmit",
    }


def _drive(payload: dict[str, object]) -> str:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    completed = subprocess.run(
        [sys.executable, str(HOOK), "--hook", "prompt"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.stderr == "", completed.stderr
    return json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]


def test_the_restart_line_is_emitted_only_for_a_stopped_station(
    repository: Path,
    runtime_root: Path,
    follower: None,
) -> None:
    """The prompt hook speaks the restart advice for a stopped beat only."""
    moment = datetime.now(tz=UTC)
    store = _seed_store(runtime_root, moment=moment)
    # A live beat with a tick that has been in one stage for three windows: the
    # station is working, and restarting it would cost the fleet that work.
    store.set_station_value(
        obligation_snapshot.STATION_TICK_STARTED_AT_KEY,
        (moment - _A_WHILE_AGO).isoformat(),
    )
    store.set_station_value(
        obligation_snapshot.STATION_TICK_STAGE_KEY, "per-project-refresh"
    )
    store.set_station_value(
        obligation_snapshot.STATION_TICK_FINISHED_AT_KEY,
        (moment - _A_WHILE_AGO * 2).isoformat(),
    )
    store.heartbeat(now=moment)
    slow = _drive(_prompt_payload(repository))
    store.close()

    assert "alive but slow" in slow, slow
    assert "per-project-refresh" in slow, slow
    assert f"reckon crew watch --ensure --project {PROJECT}" not in slow, slow

    # The same station once its beat ages past the window: now the advice is
    # the right remedy, and it is the only case that earns it.
    store = _seed_store(runtime_root, moment=moment)
    store.heartbeat(now=moment - _A_WHILE_AGO)
    store.set_station_value(
        obligation_snapshot.STATION_TICK_FINISHED_AT_KEY, moment.isoformat()
    )
    stopped = _drive(_prompt_payload(repository))
    store.close()

    assert "not current" in stopped, stopped
    assert f"reckon crew watch --ensure --project {PROJECT}" in stopped, stopped
    assert "alive but slow" not in stopped, stopped

    # The note itself names the stage and the age and carries no remedy; the
    # hook renders it, so a slow station cannot quietly grow restart advice.
    note = slow_station_note(
        answer={"state": "slow", "tick_stage": "publish", "tick_age_seconds": 900}
    )
    assert "publish" in note and "15m0s" in note, note
    assert "watch --ensure" not in note, note
