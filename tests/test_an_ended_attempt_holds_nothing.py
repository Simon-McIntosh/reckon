"""An attempt that has ended holds nothing.

On 15 November a ``crew discard`` and a ``crew resume`` of one run were both
refused with "already has attempt 1 in flight (supervisor pid 884012)" while
884012 was dead. The run directory held reservations and exit receipts for
attempts 1, 3 and 4, and attempt 4 had been launched by the station's own pid as
a resume. Read afterwards against that directory, ``held_launch_reservation``
returned nothing, because a dead supervisor is not in flight — but
``_reserved_attempt_ended`` returned False for every attempt whose
``attempt-N-exit.json`` was present, so the receipt guard refused nothing.

Two facts make that possible, and this file holds one case for each:

* the receipt was located by resolving the run id out of the reading process's
  own crew home instead of the directory the caller holds, so a door holding a
  run directory from another crew home — the station's overlay home, or a
  reader inspecting a peer's run afterwards — saw every receipt absent;
* the receipt was asked about only by the hold scan, so a launch asking about
  one attempt on its own, and a discard asking whether anything still holds the
  run, refused on the strength of an attempt that had already recorded its end.

The third case is a promotion's cleanup: the run is resumed while the promotion
runs, so the pointer names an attempt the promotion never checked. That cleanup
may finish or refuse, but a refusal that does not name the attempt it kept
leaves a coordinator hunting the pointer for the launch that owns the run.

Negative control, applied verbatim as the declared mutation: restore the
reservation check that ignores exit receipts, and the first case is refused
naming attempt 1 again.

Every case runs against a temporary crew home and asserts afterwards that the
real one is untouched, because a monitor of the machine is not a test of this
code.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch, process_liveness, promotion, runs
from reckon.crew.node import CrewError

RUN_ID = "r-an-ended-attempt"

#: The attempt numbers the recorded run directory carried, and the attempts
#: each case writes a receipt for.
ATTEMPTS = (1, 3, 4)

#: The mutation this file's first case fails against, verbatim.
DECLARED_MUTATION = (
    "restore the reservation check that ignores exit receipts: the first case "
    "is then refused naming attempt 1"
)


def _real_crew_home() -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"


def _listing(directory: Path) -> list[str] | None:
    try:
        return sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return None


@pytest.fixture(autouse=True)
def real_home_untouched() -> None:
    """Prove the case wrote nothing into the operator's crew home."""
    real = _real_crew_home()
    before = _listing(real)
    yield
    assert _listing(real) == before, "the case wrote into the real crew home"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, with liveness read from this host's own table."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: None)
    assert runs.crew_home().is_relative_to(tmp_path)
    return config_home


def _pointer(tmp_path: Path, *, pid: int | None = None) -> dict:
    """A stopped run whose pointer records no live process of its own."""
    tree = tmp_path / "tree"
    tree.mkdir(exist_ok=True)
    manifest = tree / "manifest.md"
    manifest.write_text("node: ended-attempt\nstatus: complete\n", encoding="utf-8")
    return {
        "run_id": RUN_ID,
        "project": "sample",
        "phase": "stopped",
        "launch": "cli",
        "launcher_host": socket.gethostname(),
        "attempt": 4,
        "attempt_kind": "resume",
        "pid": pid,
        "pid_start_time": None,
        "session": "session-1",
        "worktree": str(tree),
        "manifest_path": str(manifest),
        "log_path": str(runs.run_dir(RUN_ID) / "stream.jsonl"),
        "node": {
            "id": "ended-attempt",
            "plan": "fixture",
            "section": "ended-attempt",
        },
    }


def _reservation(
    directory: Path,
    attempt: int,
    *,
    pid: int,
    supervisor_pid: int | None = None,
) -> None:
    """One attempt's reservation, shaped as the recorded ones were.

    The reserving pid is a process this host can probe — a long-lived door, the
    station — and the supervisor is only recorded when the launch reached one.
    """
    payload = {
        "run_id": RUN_ID,
        "attempt": attempt,
        "kind": "resume" if attempt > 1 else "dispatch",
        "pid": pid,
        "pid_start_time": runs._process_start_time(pid),
        "launcher_host": socket.gethostname(),
        "at": "2026-11-15T20:21:47Z",
        "supervisor_pid": supervisor_pid,
        "supervisor_pid_start_time": (
            runs._process_start_time(supervisor_pid)
            if supervisor_pid is not None
            else None
        ),
    }
    runs._write_json(directory / f"attempt-{attempt}.reservation", payload)


def _worker_receipt(directory: Path, attempt: int) -> None:
    runs._write_json(
        directory / f"attempt-{attempt}-worker.json",
        {"run_id": RUN_ID, "attempt": attempt, "pid": 4242, "pid_start_time": "1"},
    )


def _exit_receipt(directory: Path, attempt: int) -> None:
    runs._write_json(
        directory / f"attempt-{attempt}-exit.json",
        {
            "run_id": RUN_ID,
            "attempt": attempt,
            "exit_code": 0,
            "exited_at": "2026-11-15T20:21:17Z",
            "recorded_by": "supervisor",
        },
    )


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


def _held_directory(tmp_path: Path) -> Path:
    """A run directory reached by path, under a crew home that is not ours.

    A door is handed the directory it is working in. A process whose own crew
    home is elsewhere resolves a different path for the same run id, and the
    directory it was handed is the only one that carries this run's receipts.
    """
    directory = tmp_path / "station-home" / "crew" / "runs" / RUN_ID
    directory.mkdir(parents=True)
    return directory


# ── an ended attempt frees the run ──────────────────────────────────────────


def test_an_ended_attempt_holds_nothing(home: Path, tmp_path: Path) -> None:
    """Every attempt of the recorded directory has ended, and holds nothing.

    Attempt 1's launch never recorded its supervisor, so its reservation names
    the admitting door — a process that outlives every run it launches. Attempt
    3 recorded a supervisor that is not running, and attempt 4 is a resume the
    station reserved, with a supervisor that is not running either. All three
    wrote their own exit receipt, so all three are ended whatever process their
    reservation still names.
    """
    directory = _held_directory(tmp_path)
    runs._write_json(runs.pointer_path(RUN_ID), _pointer(tmp_path))
    door = _live_child()
    station = _live_child()
    try:
        _reservation(directory, 1, pid=door.pid)
        _reservation(directory, 3, pid=door.pid, supervisor_pid=_dead_pid())
        _reservation(directory, 4, pid=station.pid, supervisor_pid=_dead_pid())
        for attempt in ATTEMPTS:
            _worker_receipt(directory, attempt)
            _exit_receipt(directory, attempt)

        for attempt in ATTEMPTS:
            assert (
                dispatch_launch._reserved_attempt_ended(directory, attempt) is True
            ), f"attempt {attempt} recorded its end and still reads as unended"

        reservation = dispatch_launch.reserve_launch_attempt(
            directory, RUN_ID, kind="resume"
        )
        try:
            assert reservation.attempt == 5, reservation
        finally:
            dispatch_launch.release_launch_reservation(
                reservation, only_when_unspawned=True
            )

        result = promotion.discard(RUN_ID)
        assert result["pointer_removed"] is True, result
    finally:
        _stop(door)
        _stop(station)


# ── a live attempt still holds the run ──────────────────────────────────────


def test_a_live_attempt_refuses_both_doors_by_its_own_number(
    home: Path, tmp_path: Path
) -> None:
    """Attempt 4 never ended, so both doors refuse — and name attempt 4.

    Attempts 1 and 3 recorded their end; attempt 4 has no exit receipt and its
    supervisor is a live process this test owns. A refusal that names attempt 1
    would send a coordinator to check a process that is not holding the run.
    """
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs._write_json(runs.pointer_path(RUN_ID), _pointer(tmp_path))
    door = _live_child()
    supervisor = _live_child()
    try:
        _reservation(directory, 1, pid=door.pid)
        _reservation(directory, 3, pid=door.pid, supervisor_pid=_dead_pid())
        _reservation(directory, 4, pid=door.pid, supervisor_pid=supervisor.pid)
        for attempt in (1, 3):
            _worker_receipt(directory, attempt)
            _exit_receipt(directory, attempt)
        _worker_receipt(directory, 4)

        with pytest.raises(CrewError) as resumed:
            dispatch_launch.reserve_launch_attempt(directory, RUN_ID, kind="resume")
        assert "attempt 4" in str(resumed.value), str(resumed.value)
        assert "attempt 1" not in str(resumed.value), str(resumed.value)

        with pytest.raises(CrewError) as discarded:
            promotion.discard(RUN_ID)
        assert "attempt 4" in str(discarded.value), str(discarded.value)
        assert "attempt 1" not in str(discarded.value), str(discarded.value)
        assert runs.pointer_path(RUN_ID).exists(), "a held run's pointer was removed"
    finally:
        _stop(door)
        _stop(supervisor)


# ── a promotion keeps a newer attempt it never checked ──────────────────────


def test_a_promotion_names_the_attempt_it_kept(home: Path, tmp_path: Path) -> None:
    """A run resumed while its promotion ran is named by the cleanup's refusal.

    The case goes through the cleanup every promotion reaches, not the private
    pointer-removal step beneath it: a refusal built in the inner call and
    dropped by the outer one names nothing a coordinator can act on. The
    cleanup either removes the attempt the promotion checked or leaves the
    pointer to the newer one, and a refusal that leaves the pointer behind says
    which attempt now owns it.
    """
    record = _pointer(tmp_path)
    runs._write_json(runs.pointer_path(RUN_ID), record)
    resumed = dict(record)
    resumed["attempt"] = 5
    resumed["attempt_kind"] = "resume"
    resumed["phase"] = "working"
    runs._write_json(runs.pointer_path(RUN_ID), resumed)

    with pytest.raises(CrewError) as refused:
        promotion._remove_promoted_pointer_or_defer(RUN_ID, record)
    assert "attempt 5" in str(refused.value), str(refused.value)
    assert runs.pointer_path(RUN_ID).exists(), "the kept pointer was removed"
