"""A reservation holds only while its holder is working on what it guards.

A reservation is a claim on an attempt, taken so two doors cannot start one
attempt between them. The claim is only as good as the evidence that its holder
is still there, and each kind of holder leaves different evidence:

* this host can probe a pid, and a dead pid is a holder that has gone;
* a pid issued on another host cannot be probed here at all, so that host's own
  heartbeat says whether it is still running: a lapsed beat is a host that has
  gone, a fresh one keeps what it holds however dead its pid number looks here;
* a door that takes a reservation and then declines to launch holds an attempt
  no worker stands behind, and lets it go rather than refusing the resume its
  own hold was waiting for.

A resume mark follows the same rule: it says a door began a resume, and it stops
saying anything about what is running once its attempt has recorded an answer.

Every case runs against a temporary crew home and asserts afterwards that the
operator's own crew home is untouched.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch, host_heartbeat, process_liveness
from reckon.crew import runs as runs_module
from reckon.crew.node import CrewError
from reckon.crew.recovery_repair_dispatch import (
    supersede_a_held_repair_round,
    withdraw_unlaunched_repair_reservations,
)

RUN_ID = "r-reservation-holder"
NODE_ID = "reservation-holder"


@pytest.fixture(autouse=True)
def real_home_untouched() -> None:
    real = runs_module.crew_home()
    before = _listing(real)
    yield
    assert _listing(real) == before, "the case wrote into the real crew home"


def _listing(directory: Path) -> list[str] | None:
    try:
        return sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return None


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding one run that has launched before."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: None)
    directory = runs_module.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs_module.live_dir().mkdir(parents=True, exist_ok=True)
    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer())
    _retired_exit(directory, 1)
    return directory


def _pointer(**extra: object) -> dict:
    return {
        "run_id": RUN_ID,
        "project": "sample",
        "phase": "stopped",
        "backend": "probe",
        "launch": "cli",
        "launcher_host": socket.gethostname(),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "pid": None,
        "session": "session-1",
        "worktree": str(runs_module.run_dir(RUN_ID)),
        "manifest_path": str(runs_module.run_dir(RUN_ID) / "manifest.md"),
        "log_path": str(runs_module.run_dir(RUN_ID) / "stream.jsonl"),
        "node": {"id": NODE_ID, "plan": "fixture", "section": "holder"},
        **extra,
    }


def _retired_exit(directory: Path, attempt: int) -> None:
    runs_module._write_json(
        directory / f"attempt-{attempt}-exit.json",
        {"run_id": RUN_ID, "attempt": attempt, "exit_code": 0, "detail": "ended"},
    )


def _reservation(
    directory: Path,
    attempt: int,
    *,
    pid: int,
    host: str | None = None,
    supervisor_pid: int | None = None,
) -> dict:
    payload = {
        "run_id": RUN_ID,
        "attempt": attempt,
        "kind": "resume",
        "pid": pid,
        "pid_start_time": runs_module._process_start_time(pid),
        "at": "2026-11-15T00:00:00Z",
    }
    if host is not None:
        payload["launcher_host"] = host
    if supervisor_pid is not None:
        payload["supervisor_pid"] = supervisor_pid
        payload["supervisor_pid_start_time"] = runs_module._process_start_time(
            supervisor_pid
        )
    path = directory / f"attempt-{attempt}.reservation"
    runs_module._write_json(path, payload)
    return payload


def _live_child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _dead_pid() -> int:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    return gone.pid


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)


def _held(directory: Path) -> dict | None:
    return dispatch_launch.held_launch_reservation(directory)


# ── a holder that is working still holds ────────────────────────────────────


def test_a_reservation_naming_a_live_door_refuses_the_next_launch(home: Path) -> None:
    """A door that is still running is a launch under way, and still refuses."""
    door = _live_child()
    try:
        _reservation(home, 2, pid=door.pid)
        with pytest.raises(CrewError) as refused:
            dispatch_launch.reserve_launch_attempt(home, RUN_ID, kind="resume")
        assert "in flight" in str(refused.value)
    finally:
        _stop(door)


def test_a_reservation_naming_a_dead_pid_is_released_on_read(home: Path) -> None:
    _reservation(home, 2, pid=_dead_pid())
    assert _held(home) is None


# ── a holder on another host is judged by that host's beat ──────────────────


def test_a_reservation_naming_a_host_whose_beat_lapsed_is_released(
    home: Path,
) -> None:
    payload = _reservation(home, 2, pid=os.getpid(), host="another-host")
    assert (
        dispatch_launch.reservation_in_flight(
            payload, run_directory=home, attempt=2
        )
        is False
    )
    assert _held(home) is None


def test_a_lapsed_beat_whose_holder_is_running_still_holds(home: Path) -> None:
    """A renewal that stalled is a slow host, not one that has gone."""
    host_heartbeat.publish_host_heartbeat(
        runs_module.crew_home(), "another-host", now=time.time() - 600.0
    )
    payload = _reservation(home, 2, pid=os.getpid(), host="another-host")
    assert host_heartbeat.host_is_live(runs_module.crew_home(), "another-host")
    assert (
        dispatch_launch.reservation_in_flight(
            payload, run_directory=home, attempt=2
        )
        is True
    )
    assert _held(home) is not None


def test_a_lapsed_beat_whose_holder_has_gone_releases_the_reservation(
    home: Path,
) -> None:
    path = host_heartbeat.publish_host_heartbeat(
        runs_module.crew_home(), "another-host", now=time.time() - 600.0
    )
    path.write_text(
        json.dumps(
            {
                "host": "another-host",
                "pid": _dead_pid(),
                "pid_start": "",
                "pid_namespace": "",
                "at": time.time() - 600.0,
            }
        ),
        encoding="utf-8",
    )
    stamp = time.time() - 600.0
    os.utime(path, (stamp, stamp))
    assert host_heartbeat.host_is_live(runs_module.crew_home(), "another-host") is False
    assert _held(home) is None


def test_a_reservation_naming_a_host_with_a_fresh_beat_is_kept(home: Path) -> None:
    host_heartbeat.publish_host_heartbeat(runs_module.crew_home(), "another-host")
    payload = _reservation(home, 2, pid=_dead_pid(), host="another-host")
    assert (
        dispatch_launch.reservation_in_flight(
            payload, run_directory=home, attempt=2
        )
        is True
    )
    assert _held(home) is not None


# ── a resume mark says only what its attempt has not yet answered ───────────


def test_a_resume_mark_whose_attempt_delivered_holds_no_review(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon.crew import obligations, recovery_review_dispatch, resumption

    monkeypatch.setattr(
        obligations, "_acknowledgements_in_force", lambda project, now=None: set()
    )
    stream = home / "stream.jsonl"
    stream.write_text(
        '{"type":"turn.completed"}\n{"type":"result"}\n', encoding="utf-8"
    )
    holder = _live_child()
    try:
        record = _pointer(
            attempt=2,
            resume_launch_in_flight={
                "pid": holder.pid,
                "pid_start_time": runs_module._process_start_time(holder.pid),
                "at": "2026-11-15T00:00:00Z",
            },
        )
        assert resumption.launch_mark_holds(record) is False
        assert (
            recovery_review_dispatch._settled_delivery_refusal(record, dry_run=True)
            is None
        )
    finally:
        _stop(holder)


def test_a_resume_mark_whose_attempt_is_undelivered_still_holds(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon.crew import obligations, recovery_review_dispatch, resumption

    monkeypatch.setattr(
        obligations, "_acknowledgements_in_force", lambda project, now=None: set()
    )
    stream = home / "stream.jsonl"
    stream.write_text('{"type":"turn.completed"}\n', encoding="utf-8")
    holder = _live_child()
    try:
        record = _pointer(
            attempt=2,
            resume_launch_in_flight={
                "pid": holder.pid,
                "pid_start_time": runs_module._process_start_time(holder.pid),
                "at": "2026-11-15T00:00:00Z",
            },
        )
        assert resumption.launch_mark_holds(record) is True
        refused = recovery_review_dispatch._settled_delivery_refusal(
            record, dry_run=True
        )
        assert refused is not None
        assert "resume launching" in refused["reason"]
    finally:
        _stop(holder)


# ── a door that holds without launching lets go ────────────────────────────


def test_an_advice_hold_withdraws_the_reservation_it_took(home: Path) -> None:
    _reservation(home, 2, pid=os.getpid(), host=socket.gethostname())
    assert _held(home) is not None
    assert withdraw_unlaunched_repair_reservations(
        RUN_ID, reason="held for coordinator advice"
    ) == 1
    assert _held(home) is None
    assert (home / "attempt-2-exit.json").exists()
    admitted = dispatch_launch.reserve_launch_attempt(home, RUN_ID, kind="resume")
    assert admitted.attempt == 3


def test_a_coordinator_resume_supersedes_a_held_round(home: Path) -> None:
    door = _live_child()
    try:
        _reservation(home, 2, pid=door.pid, host=socket.gethostname())
        pointer = runs_module.read_pointer(RUN_ID)
        pointer["repair_dispatch"] = {
            "status": "advice-hold",
            "round_id": f"{RUN_ID}@a1b2c3",
            "node_id": NODE_ID,
        }
        runs_module._write_json(runs_module.pointer_path(RUN_ID), pointer)
        assert _held(home) is not None
        assert supersede_a_held_repair_round(RUN_ID) == 2
        assert _held(home) is None
        recorded = runs_module.read_pointer(RUN_ID)["repair_dispatch"]
        assert recorded["status"] == "superseded"
    finally:
        _stop(door)


_BEAT_CHILD = """
import sys
import time
from pathlib import Path

from reckon.crew import host_heartbeat

home = Path(sys.argv[1])
host = sys.argv[2]
hold = float(sys.argv[3])
host_heartbeat.HEARTBEAT_RENEW_SECONDS = 0.1
host_heartbeat.publish_host_heartbeat(home, host)
time.sleep(hold)
"""


def _beat_process(home: Path, host: str, hold: float) -> subprocess.Popen:
    root = str(Path(__file__).resolve().parents[1])
    environ = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(
            [root, os.environ.get("PYTHONPATH", "")]
        ).strip(os.pathsep),
    )
    return subprocess.Popen(
        [sys.executable, "-c", _BEAT_CHILD, str(home), host, str(hold)],
        env=environ,
    )


def _await(predicate, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_a_beat_outlives_the_launcher_that_first_published_it(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launcher that exits must not take the holder's beat with it.

    A dispatch records its reservation and returns, while the supervisor it
    started goes on working. A beat only the launcher renewed would lapse while
    that holder was still there, so the freshness is measured across the
    launcher's own exit: it publishes and ends, a second process keeps the beat
    current, and the beat is read well past the stale window.
    """
    monkeypatch.setattr(host_heartbeat, "HEARTBEAT_STALE_SECONDS", 1.0)
    crew_home = runs_module.crew_home()
    launcher = _beat_process(crew_home, "renewing-host", 0.0)
    launcher.wait(timeout=60)
    assert launcher.poll() == 0, "the launcher did not exit cleanly"
    holder = _beat_process(crew_home, "renewing-host", 120.0)
    try:
        assert _await(
            lambda: host_heartbeat.host_heartbeat_path(
                crew_home, "renewing-host"
            ).exists()
        )
        time.sleep(2.5)
        assert host_heartbeat.host_is_live(crew_home, "renewing-host") is True
    finally:
        _stop(holder)
    assert _await(
        lambda: host_heartbeat.host_is_live(crew_home, "renewing-host") is False,
        seconds=6.0,
    ), "the beat stayed live after its holder ended"


def test_a_renewer_whose_beat_vanished_lets_the_next_publish_renew(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renewal that ended must not leave a key that suppresses renewal."""
    monkeypatch.setattr(host_heartbeat, "HEARTBEAT_RENEW_SECONDS", 0.05)
    crew_home = runs_module.crew_home()
    key = (str(crew_home), "vanishing-host")
    path = host_heartbeat.publish_host_heartbeat(crew_home, "vanishing-host")
    path.unlink()
    assert _await(
        lambda: key not in host_heartbeat._RENEWERS,
        seconds=10.0)
    path = host_heartbeat.publish_host_heartbeat(crew_home, "vanishing-host")
    stale = time.time() - 600.0
    os.utime(path, (stale, stale))
    assert _await(lambda: path.stat().st_mtime > stale + 1.0, seconds=10.0), (
        "the second publication started no renewal"
    )
    assert host_heartbeat.stop_host_heartbeat(crew_home, "vanishing-host")


def test_the_supervisor_publishes_the_beat_it_keeps_fresh(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process that outlives a launch is the one that publishes its beat."""
    from reckon.crew.dispatch_peer import _supervisor_command

    crew_home = runs_module.crew_home()
    missing = home / "no-such-spec.json"
    assert _supervisor_command(["--spec", str(missing)]) == 0
    assert host_heartbeat.host_is_live(crew_home, host_heartbeat.heartbeat_host())
