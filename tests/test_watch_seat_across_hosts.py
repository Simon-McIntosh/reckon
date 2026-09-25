"""A watcher seat is judged on the host that issued its pid, and nowhere else.

The seat lives on the shared home every fleet node mounts, and its record
carried a pid with no host. A reader on another node therefore probed its own
process table for a pid it could not have issued, found nothing, and confirmed a
running producer dead — erasing the seat. Delivery and admission both follow
that record, so the fleet went unwatched while the stream kept growing.

Measured 2026-09-25 on imas-ambix: a producer, pid 1438527 under
``reckon-watch-imas-ambix.service`` on compute node 98dci4-clu-2058, held the
seat and kept writing transitions while its record read ``{}`` from 15:04Z. The
coordinator's attached follower delivered nothing for about three hours.

These tests pin both directions. A record naming another host, or naming none at
all, is judged by its transition stream — the one piece of evidence that crosses
the shared home — and is never erased from here. A record naming *this* host is
still judged by pid, so the fix does not turn a genuine local death into an
unhauntable seat. Every case is isolated in a throwaway configuration home.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from reckon import service
from reckon.crew import runs
from tests.test_crew_watch_ensure import FakeWatchService, _backend_bin, _config

# A pid this host cannot have issued and cannot be running: large enough that no
# live process holds it, so the local table answers for it the way it answers for
# a pid on another host — with nothing.
ABSENT_PID = 1 << 29

OTHER_HOST = "98dci4-clu-2058"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move the seat, the stream and the unit into a throwaway config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _seat_record(project: str, *, host: str) -> dict:
    """A seat record as a producer on ``host`` writes one."""
    return {
        "project": project,
        "pid": ABSENT_PID,
        "pid_start_time": "4242",
        "host": host,
        "stall_window": "15m",
        "started_at": runs._utc_now(),
        "stream_path": str(runs.watch_stream_path(project)),
    }


def _write_seat(project: str, record: dict) -> None:
    path = runs.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def _read_seat(project: str) -> dict:
    path = runs.watch_lock_path(project)
    return json.loads(path.read_text() or "{}")


def _write_stream(project: str, *, age_seconds: float = 0.0) -> Path:
    """Write a transition stream and date it, as a producer's writes would."""
    path = runs.watch_stream_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"event": "transition", "run_id": "r-one"}) + "\n")
    if age_seconds:
        past = time.time() - age_seconds
        os.utime(path, (past, past))
    return path


def test_a_record_naming_another_host_with_a_fresh_stream_reads_live_and_is_not_erased(
    home,
) -> None:
    """The stream is the evidence a pid cannot be: a fresh one reads live.

    The pid is absent from this host's table, and at base that alone confirmed
    the producer dead and erased the record before returning. Here the record
    must survive the read, and the read must answer live.
    """
    _write_seat("proj", _seat_record("proj", host=OTHER_HOST))
    _write_stream("proj")

    assert runs.producer_live("proj") is True
    kept = _read_seat("proj")
    assert kept.get("host") == OTHER_HOST, "the record was erased from here"
    assert kept.get("pid") == ABSENT_PID
    assert runs.project_watch_visibility("proj")["watcher_live"] is True


def test_a_record_naming_another_host_with_a_stale_stream_is_still_not_erased(
    home,
) -> None:
    """A quiet stream reads not-live, but this host may not erase the record.

    A reader here cannot tell a stalled producer from one on a host it cannot
    see, so the judgement stops at reporting: the record is left for the host
    that issued its pid to clear.
    """
    _write_seat("proj", _seat_record("proj", host=OTHER_HOST))
    _write_stream("proj", age_seconds=2 * 60 * 60)

    assert runs.producer_live("proj") is False
    assert _read_seat("proj").get("host") == OTHER_HOST, (
        "the record was erased from here"
    )
    assert runs.watch_producer_identity("proj") != {}, "the record was cleared"


def test_a_seat_naming_no_host_is_judged_by_its_stream_and_never_erased_by_pid(
    home,
) -> None:
    """A record predating the host field, or an erased one, names no host.

    Read against the local process table it would be confirmed dead the moment
    its pid was unfamiliar, which is the same defect with a wider blast radius:
    every seat written before the field landed reads that way.
    """
    record = _seat_record("proj", host=OTHER_HOST)
    del record["host"]
    _write_seat("proj", record)
    _write_stream("proj")

    assert runs.producer_live("proj") is True
    # The record is asserted by what it carries, not by the absence of a field it
    # never had: a hostless record and an erased one both read ``host`` as none,
    # so only the pid tells them apart.
    kept = _read_seat("proj")
    assert kept.get("pid") == ABSENT_PID, "a hostless record was erased by pid"
    assert "host" not in kept


def test_a_same_host_dead_pid_is_still_erased(home) -> None:
    """The local direction is unchanged while the foreign one is repaired.

    Both populations are read here on purpose, because the defect has one
    direction and the fix could trade it for its opposite. A local pid this host
    issued and can therefore judge is gone, and its seat is cleared even though
    its stream is fresh; a foreign seat in the same call, with the same fresh
    stream and the same absent pid, is left alone. A fix that clears a live
    foreign seat to keep the local direction happy fails the second half.
    """
    _write_seat("local-proj", _seat_record("local-proj", host=socket.gethostname()))
    _write_stream("local-proj")
    _write_seat("foreign-proj", _seat_record("foreign-proj", host=OTHER_HOST))
    _write_stream("foreign-proj")

    assert runs.producer_live("local-proj") is False
    assert _read_seat("local-proj") == {}, "a local dead pid was left as a stale record"

    assert runs.producer_live("foreign-proj") is True, "a foreign seat was cleared"
    assert _read_seat("foreign-proj").get("host") == OTHER_HOST


@pytest.fixture()
def loginctl_refuses(monkeypatch, tmp_path):
    """A ``loginctl`` on PATH that fails the way a fleet compute node does.

    The refusal is not simulated at the Python boundary: ``enable-linger`` is
    run for real and answers with the login manager's own words, so the raise
    under test is the one ``reckon/service.py`` writes rather than one this file
    builds.
    """
    directory = tmp_path / "loginctl-bin"
    directory.mkdir()
    binary = directory / "loginctl"
    binary.write_text(
        "#!/bin/sh\n"
        "echo 'Could not enable linger: No such device or address' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    path = os.environ.get("PATH") or ""
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{path}")
    return binary


class LingerRefusingWatchService(FakeWatchService):
    """A reachable manager whose login manager refuses to keep units past logout.

    The unit is written and the manager answers every bus call, so the only
    thing that fails is real: this manager delegates ``enable_linger`` to the
    module function, which shells out to the shadowed ``loginctl``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._linger = False

    def enable_linger(self) -> None:
        service.enable_linger()


class RecordingProducer:
    def __init__(self, *, live: bool = True) -> None:
        self.calls: list[str] = []
        self.live = live

    def __call__(self, project: str) -> dict:
        self.calls.append(project)
        return {"project": project, "watcher_live": self.live}


def test_a_linger_failure_falls_back_rather_than_raising(
    home, tmp_path: Path, loginctl_refuses: Path
) -> None:
    """A unit that dies at logout is placed in the session that owns it instead of raising.

    At base the failure escaped ``ensure_watcher_service``, so the command whose
    whole job is to leave a watcher behind ended in a traceback with no watcher
    armed. The fallback is the session-owned process producer, and it names the
    linger refusal as its cause rather than the bus failure it never had.

    The refusal arrives through a real ``loginctl enable-linger``, so reverting
    the raise in ``reckon/service.py`` to a plain ``ServiceError`` lets the error
    escape here and reddens this test.
    """
    assert loginctl_refuses.is_file()
    backend_bin = _backend_bin(tmp_path)
    manager = LingerRefusingWatchService()
    producer = RecordingProducer(live=True)

    result = runs.ensure_watcher_service(
        "sample",
        manager=manager,
        config=_config(backend_bin),
        producer=producer,
    )

    assert result["path"] == "fallback"
    assert producer.calls == ["sample"], "the session-owned producer was not armed"
    assert "linger" in result["fallback_reason"].lower()
    assert "linger" in result["detail"].lower()
    assert result["watcher_live"] is True
    # The unit is never started: a service that will stop at logout must not hold
    # the seat the process fallback is about to take.
    assert manager.starts == []
