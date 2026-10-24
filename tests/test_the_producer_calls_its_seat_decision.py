"""The producer asks its seat owner whether it may keep the seat.

The start-up decision lives in the seat owner, but nothing called it: a
producer on a host that is not the fleet record's node took the seat anyway,
published an unknown liveness reading for every run launched on the fleet
node, and held the seat that would let a producer there start. The producer now
asks at start-up and again on every lease renewal, so a producer reloaded onto
another host after it took the seat gives that seat up too.

The unit-disable command is composed by the decision and run only through a
runner this test records, so no service manager is asked to stop anything here.
A temporary fleet record and a temporary configuration home stand in for the
account's own, so neither the real record nor the real seat is touched.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
from pathlib import Path

import pytest

from reckon.crew import fleet_supervisor, recovery_watch, runs, watch_unit
from reckon.crew.node import DEFAULT_WATCH_STALL_WINDOW

PROJECT = "the-producer-calls-its-seat-decision"
FLEET_NODE = "98dci4-clu-2068"
OTHER_NODE = "98dci4-srv-1006"
UNIT = f"reckon-watch-{PROJECT}.service"
RUN_ID = "r-20261023T220036362628-the-producer-calls-its-seat-decision"


class _ProducerExitedError(Exception):
    """The producer ended itself, as a release-and-exit must."""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A temporary configuration home and a temporary fleet record path."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config / "mounts.json"))
    monkeypatch.delenv("RECKON_MOUNTS", raising=False)
    fleet_record = tmp_path / "state" / "fleet" / "record.json"
    fleet_record.parent.mkdir(parents=True)
    monkeypatch.setenv("RECKON_FLEET_RECORD", str(fleet_record))
    monkeypatch.setenv(watch_unit.WATCH_UNIT_ENV, UNIT)
    # The decisions this test takes are the process's own; a project left in the
    # announcement memory by another test would hide the one this test asserts.
    monkeypatch.setattr(watch_unit, "_ANNOUNCED_RELEASES", set())
    return {"config": config, "fleet_record": fleet_record}


@pytest.fixture
def runners(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list]:
    """Record the unit-disable command and the exit instead of performing them."""
    calls: dict[str, list] = {"commands": [], "exits": []}

    def runner(command: list[str]) -> None:
        calls["commands"].append(list(command))

    def exit_process(code: int) -> None:
        calls["exits"].append(code)
        raise _ProducerExitedError(code)

    monkeypatch.setattr(watch_unit, "_UNIT_DISABLE_RUNNER", runner)
    monkeypatch.setattr(watch_unit, "_EXIT_PROCESS", exit_process)
    return calls


def _write_fleet_record(path: Path, node: str) -> None:
    path.write_text(
        json.dumps(
            {
                "job_id": "1291586",
                "node": node,
                "runtime_dir": str(path.parent / "run"),
                "started_at": "2026-10-23T22:00:36Z",
            }
        ),
        encoding="utf-8",
    )


def _write_run(project: str, run_id: str, launcher_host: str) -> None:
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "launcher_host": launcher_host,
            "phase": "working",
            "pid": 4242,
            "session": "s",
        },
    )


def _seat_path(project: str) -> Path:
    return runs.watch_lock_path(project)


def _seat_is_free(project: str) -> bool:
    """Whether no producer holds the seat, proved by taking the lock."""
    path = _seat_path(project)
    if not path.exists():
        return True
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return True


def _enter_registration(project: str):
    return recovery_watch._watch_registration(project, DEFAULT_WATCH_STALL_WINDOW)


def test_the_producer_gives_up_its_seat_at_start_up_off_the_fleet_node(
    home: dict[str, Path],
    runners: dict[str, list],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A producer whose host is not the fleet node's releases and exits."""
    _write_fleet_record(home["fleet_record"], OTHER_NODE)
    _write_run(PROJECT, RUN_ID, OTHER_NODE)

    with pytest.raises(_ProducerExitedError), _enter_registration(PROJECT):
        pytest.fail("a misplaced producer took the seat instead of exiting")

    # The unit that would restart it is disabled first, through the runner.
    assert runners["commands"] == [["systemctl", "--user", "disable", "--now", UNIT]]
    assert runners["exits"] == [0]
    assert _seat_is_free(PROJECT)
    announcement = capsys.readouterr().out
    assert announcement.count("releasing the seat") == 1
    assert OTHER_NODE in announcement


def test_the_producer_gives_up_the_seat_it_holds_on_a_lease_renewal(
    home: dict[str, Path],
    runners: dict[str, list],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A producer the fleet leaves behind releases the seat it already holds.

    The producer starts on the fleet node's own host, which holds. The fleet
    record then names another node and a run was launched there, so this
    producer can no longer probe that run: the next renewal releases the seat,
    announces once, and ends the process.
    """
    _write_fleet_record(home["fleet_record"], fleet_supervisor._short_hostname())

    with _enter_registration(PROJECT) as (acquired, watcher):
        assert acquired is True
        assert watcher["project"] == PROJECT
        # At start-up on the fleet node the producer keeps running.
        assert runners["exits"] == []
        assert _seat_is_free(PROJECT) is False

        _write_fleet_record(home["fleet_record"], OTHER_NODE)
        _write_run(PROJECT, RUN_ID, OTHER_NODE)

        with pytest.raises(_ProducerExitedError):
            runs.renew_watch_host_lease(PROJECT)

    assert runners["commands"] == [["systemctl", "--user", "disable", "--now", UNIT]]
    assert runners["exits"] == [0]
    assert _seat_is_free(PROJECT)
    announcement = capsys.readouterr().out
    assert announcement.count("releasing the seat") == 1
    assert OTHER_NODE in announcement


def test_the_producer_on_the_fleet_node_keeps_running_through_renewals(
    home: dict[str, Path],
    runners: dict[str, list],
) -> None:
    """The same host holds, at start-up and on every renewal."""
    _write_fleet_record(home["fleet_record"], fleet_supervisor._short_hostname())
    _write_run(PROJECT, RUN_ID, fleet_supervisor._short_hostname())

    with _enter_registration(PROJECT) as (acquired, _watcher):
        assert acquired is True
        for _ in range(3):
            assert runs.renew_watch_host_lease(PROJECT) is True

    assert runners == {"commands": [], "exits": []}


def test_ten_renewals_off_the_fleet_node_publish_no_watch_stream_rows(
    home: dict[str, Path],
    runners: dict[str, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renewal question reads pointers; it does not publish or sweep.

    A producer off the fleet node asks this on every renewal, so answering it
    by listing the live fleet — which publishes the watcher stream and can
    start an obligations sweep — would scan and publish every five seconds for
    a producer that does nothing else. The project here has a run, launched on
    this host rather than the fleet node, so the foreign-host arm reaches the
    pointer read and holds.
    """
    _write_fleet_record(home["fleet_record"], OTHER_NODE)
    _write_run(PROJECT, RUN_ID, fleet_supervisor._short_hostname())
    published: list[tuple] = []
    monkeypatch.setattr(
        runs, "_publish_watch_stream", lambda *args, **kwargs: published.append(args)
    )

    for _ in range(10):
        runs.renew_watch_host_lease(PROJECT)

    assert published == []
    assert runners["exits"] == []


def test_the_host_comparison_shortens_both_names_through_the_fleet_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_hosts_match`` asks the fleet record's own shortener for both names.

    A fleet record publishes a short hostname while a seat records the whole
    one, so one spelling of the split belongs in the module that publishes it.
    The recorder here proves the match is not re-spelling it.
    """
    asked: list[str] = []

    def recording_short_hostname(name: str | None = None) -> str:
        asked.append("<this-host>" if name is None else name)
        return str(
            fleet_supervisor.socket.gethostname() if name is None else name
        ).split(".")[0]

    monkeypatch.setattr(fleet_supervisor, "_short_hostname", recording_short_hostname)

    assert watch_unit._hosts_match("98dci4-clu-2068.example.org", "98dci4-clu-2068")
    assert asked == ["98dci4-clu-2068.example.org", "98dci4-clu-2068"]
    # An empty name still matches nothing and is still put to the shortener.
    assert not watch_unit._hosts_match("", "98dci4-clu-2068")


def test_the_real_fleet_record_and_seat_are_not_written(
    home: dict[str, Path], runners: dict[str, list]
) -> None:
    """Neither the account's fleet record nor its seat is opened for writing."""
    real_fleet = Path.home() / ".local" / "state" / "fleet" / "record.json"
    real_watch = runs._config_home({}) / "crew" / "watch"
    before = real_fleet.read_bytes() if real_fleet.exists() else None
    seats_before = sorted(p.name for p in real_watch.glob(f"{PROJECT}-*.lock"))

    _write_fleet_record(home["fleet_record"], OTHER_NODE)
    _write_run(PROJECT, RUN_ID, OTHER_NODE)
    with contextlib.suppress(_ProducerExitedError), _enter_registration(PROJECT):
        pass

    assert (real_fleet.read_bytes() if real_fleet.exists() else None) == before
    assert sorted(p.name for p in real_watch.glob(f"{PROJECT}-*.lock")) == seats_before
