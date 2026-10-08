"""A watch producer records why it ended and its follower reports that reason."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew
from reckon.crew import recovery_watch
from reckon.crew import recovery, runs


def test_expired_lease_writes_stop_reason(tmp_path) -> None:
    log = tmp_path / "watch.log"
    environment = {
        **os.environ,
        "RECKON_HOME": str(tmp_path),
        "RECKON_PRODUCER_LEASE_SECONDS": "1",
        "PYTHONPATH": str(Path(cli.__file__).resolve().parent.parent),
    }
    with log.open("w") as output:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from reckon.cli import main; main()",
                "crew",
                "watch",
                "--project",
                "sample",
                "--no-color",
            ],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )

    assert completed.returncode == 0, log.read_text()
    assert "reckon crew watch stopped: producer lease expired" in log.read_text()


def test_renewed_producer_keeps_publishing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", "3")
    run_log = tmp_path / "run.log"
    run_log.write_text('{"type":"turn.started"}\n')
    pointer = {
        "run_id": "r-live",
        "project": "sample",
        "node": {"id": "live", "plan": "sample", "time_budget": "20m"},
        "phase": "starting",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "log_path": str(run_log),
        "process_alive": None,
    }
    crew._write_json(crew.pointer_path("r-live"), pointer)
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(cli.__file__).resolve().parent.parent),
    }
    producer_log = tmp_path / "watch.log"
    with producer_log.open("w") as output:
        producer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from reckon.cli import main; main()",
                "crew",
                "watch",
                "--project",
                "sample",
                "--no-color",
            ],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 10
            while not runs.producer_live("sample") and time.monotonic() < deadline:
                assert producer.poll() is None, producer_log.read_text()
                time.sleep(0.1)
            assert runs.producer_live("sample"), producer_log.read_text()
            deadline = time.monotonic() + 6.5
            while time.monotonic() < deadline:
                runs.renew_producer_lease("sample")
                time.sleep(0.4)
            assert producer.poll() is None, producer_log.read_text()
            stream = runs.watch_stream_path("sample")
            before = stream.stat().st_size
            pointer["phase"] = "working"
            crew._write_json(crew.pointer_path("r-live"), pointer)
            deadline = time.monotonic() + 5
            while stream.stat().st_size == before and time.monotonic() < deadline:
                runs.renew_producer_lease("sample")
                time.sleep(0.4)
            assert stream.stat().st_size > before, producer_log.read_text()
        finally:
            producer.terminate()
            producer.wait(timeout=10)


@pytest.mark.parametrize(
    "line",
    [
        "reckon crew watch stopped: producer lease expired",
        "[2026-10-02T20:05:30Z] reckon crew watch stopped: producer lease expired",
    ],
)
def test_follower_reports_logged_stop_reason(tmp_path, monkeypatch, line) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    log = runs.watch_log_path("sample")
    log.parent.mkdir(parents=True)
    log.write_text(f"{line}\n")
    monkeypatch.setattr(runs, "producer_live", lambda project: False)
    stop = threading.Event()
    events = cli._follow_watch_lines(
        "sample", stop=stop, sweep=None, sleeper=lambda interval: stop.set()
    )

    event = next(events)
    stop.set()
    events.close()

    assert event["event"] == cli.FOLLOWER_PRODUCER_STOPPED_EVENT
    assert "producer lease expired" in event["line"]


def _stop_events(tmp_path, monkeypatch, *, phase: str | None, identity=None):
    """Arm the follower against a stopped producer and collect its events."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    log = runs.watch_log_path("sample")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("reckon crew watch stopped: producer lease expired\n")
    if phase is not None:
        runs._write_json(
            runs.pointer_path("r-live"),
            {"run_id": "r-live", "project": "sample", "phase": phase, "session": ""},
        )
    monkeypatch.setattr(runs, "producer_live", lambda project: False)
    if identity is not None:
        monkeypatch.setattr(runs, "watch_producer_identity", lambda project: identity)
    stop = threading.Event()
    events = cli._follow_watch_lines(
        "sample", stop=stop, sweep=None, sleeper=lambda interval: stop.set()
    )
    return events, stop


def test_stop_without_live_work_stays_quiet(tmp_path, monkeypatch) -> None:
    """An idle follower's producer exit keeps the pane silent but the event."""
    events, stop = _stop_events(tmp_path, monkeypatch, phase=None)
    event = next(events)
    stop.set()
    events.close()

    assert event["event"] == cli.FOLLOWER_PRODUCER_STOPPED_EVENT
    assert "producer lease expired" in event["line"]
    assert event["pane_line"] is False


def test_stop_events_with_live_work_print(tmp_path, monkeypatch) -> None:
    """A producer that stops while a run is live reaches the pane line."""
    events, stop = _stop_events(tmp_path, monkeypatch, phase="working")
    event = next(events)
    stop.set()
    events.close()

    assert event["event"] == cli.FOLLOWER_PRODUCER_STOPPED_EVENT
    assert event["pane_line"] is True


def test_reload_failed_follows_the_same_predicate(tmp_path, monkeypatch) -> None:
    """The reload-failed line is quiet for the same empty population."""
    events, stop = _stop_events(
        tmp_path,
        monkeypatch,
        phase=None,
        identity={"reload_started_at": "2026-10-02T12:58:12Z", "log_path": "x"},
    )
    first = next(events)
    second = next(events)
    stop.set()
    events.close()

    assert first["event"] == cli.FOLLOWER_PRODUCER_STOPPED_EVENT
    assert second["event"] == cli.FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT
    assert second["pane_line"] is False


def test_signal_writes_stop_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.setattr(cli._StampPoll, "start", lambda self: None)
    monkeypatch.setattr(cli._StampPoll, "stop", lambda self: None)

    def interrupted(*args, **kwargs):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        yield

    (monkeypatch.setattr(recovery_watch, "watch_follow", interrupted), monkeypatch.setattr(recovery, "watch_follow", interrupted))
    result = CliRunner().invoke(cli.main, ["crew", "watch", "--project", "sample"])

    assert result.exit_code == 128 + signal.SIGTERM
    assert "reckon crew watch stopped: received SIGTERM" in result.output


def test_exception_writes_stop_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.setattr(cli._StampPoll, "start", lambda self: None)
    monkeypatch.setattr(cli._StampPoll, "stop", lambda self: None)

    def failed(*args, **kwargs):
        raise RuntimeError("tick failed")
        yield

    (monkeypatch.setattr(recovery_watch, "watch_follow", failed), monkeypatch.setattr(recovery, "watch_follow", failed))
    result = CliRunner().invoke(cli.main, ["crew", "watch", "--project", "sample"])

    assert result.exit_code != 0
    assert "reckon crew watch stopped: RuntimeError: tick failed" in result.output


def _follow_text(tmp_path, monkeypatch, *, phase: str | None) -> str:
    """Arm the real ``crew follow`` command against a stopped producer.

    The command is invoked end to end so the pane line is decided by its own
    ``pane_line`` handling rather than by a value the test injected. ``phase``
    is the live pointer's state: ``None`` leaves the fleet with nothing to
    watch, while a non-terminal phase gives the stop a population to name.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    log = runs.watch_log_path("sample")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("reckon crew watch stopped: producer lease expired\n")
    if phase is not None:
        runs._write_json(
            runs.pointer_path("r-live"),
            {"run_id": "r-live", "project": "sample", "phase": phase, "session": ""},
        )
    monkeypatch.setattr(runs, "producer_live", lambda project: False)
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "follow",
            "--project",
            "sample",
            "--session",
            "s1",
            "--lifetime",
            "1s",
            "--no-color",
            "--width",
            "200",
        ],
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_the_pane_echoes_a_producer_stop_only_when_work_is_at_stake(
    tmp_path, monkeypatch
) -> None:
    """A stop with nothing to watch is JSON-only; a live fleet gets the pane line.

    A producer's idle exit is not news when the follower's own population holds
    nothing left to watch, so the pane stays quiet and only the JSON stream
    carries the event. The same stop while a run is live is the silent fleet a
    reader has to see, and the pane line appears exactly then.
    """
    quiet = _follow_text(tmp_path, monkeypatch, phase=None)
    assert "producer sample is gone" not in quiet, (
        "a stop with no live work echoed a pane line"
    )

    loud = _follow_text(tmp_path, monkeypatch, phase="working")
    assert "producer sample is gone" in loud, (
        "a stop with a live run never reached the pane line"
    )
