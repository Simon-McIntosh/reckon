"""A census acts only on local processes whose recorded ownership has ended."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import census as census_module
from reckon.crew.process_liveness import _process_start_time, process_alive
from reckon.crew.run_paths import crew_home


@pytest.fixture
def processes(monkeypatch):
    children = []
    read_processes = census_module._processes

    def spawn(stamps=None, *, ignore_term=False, argv=()):
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "RECKON_OWNER_PID",
                "RECKON_OWNER_START_TIME",
                "RECKON_OWNER_HOST",
                "RECKON_FOLLOWER_OWNER",
                "RECKON_RUN_ID",
                "RECKON_SESSION_ID",
                "RECKON_CREW_SESSION",
            }
        }
        environment.update(stamps or {})
        program = (
            "import signal,time; "
            + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "")
            + "print('ready',flush=True); time.sleep(300)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", program, *argv],
            env=environment,
            stdout=subprocess.PIPE,
            text=True,
        )
        children.append(child)
        assert select.select([child.stdout], [], [], 5)[0], "stub did not start"
        assert child.stdout.readline().strip() == "ready"
        return child

    def local_processes():
        observed = read_processes()
        selected = {child.pid for child in children} | {os.getpid(), os.getppid()}
        return {pid: process for pid, process in observed.items() if pid in selected}

    monkeypatch.setattr(census_module, "_processes", local_processes)
    yield spawn
    for child in children:
        if child.poll() is None:
            print(f"Stopping fixture pid {child.pid}")
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                print(f"Killing fixture pid {child.pid}")
                child.kill()
                child.wait(timeout=2)
        child.stdout.close()


def _dead_owner(spawn):
    owner = spawn()
    owner.terminate()
    owner.wait(timeout=2)
    assert process_alive(owner.pid) is False
    return owner.pid


def _write(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


def _registered(home, child, owner_pid, kind, *, live):
    record = {
        "pid": child.pid,
        "pid_start_time": _process_start_time(child.pid),
        "parent_pid": owner_pid,
        "parent_start_time": _process_start_time(owner_pid),
        "host": socket.gethostname(),
        "project": "fixture-project",
        "session": "fixture-session",
    }
    if kind == "follower":
        path = home / "watch" / "fixture.followers" / f"{child.pid}.lock"
    elif kind == "producer":
        record["lease_renewed_at"] = time.time() if live else 0.0
        path = home / "watch" / f"fixture-{child.pid}.lock.registration"
    else:
        record.update(
            pid=owner_pid,
            pid_start_time=_process_start_time(owner_pid),
            launcher_host=record.pop("host"),
        )
        path = home / "live" / ("live-run.json" if live else "orphan-run.json")
    _write(path, record)
    return path


@pytest.mark.parametrize("source", ["environment", "follower", "producer", "run"])
def test_only_the_dead_owned_stub_is_listed(processes, monkeypatch, source):
    home = crew_home()
    owner_pid = _dead_owner(processes)
    live_owner = processes()
    if source == "environment":
        dead_stamps = {"RECKON_OWNER_PID": str(owner_pid)}
        live_stamps = {"RECKON_OWNER_PID": str(live_owner.pid)}
    elif source == "run":
        dead_stamps = {"RECKON_RUN_ID": "orphan-run"}
        live_stamps = {"RECKON_RUN_ID": "live-run"}
    else:
        dead_stamps = live_stamps = {}
    orphan = processes(dead_stamps)
    owned = processes(live_stamps)
    decoy = processes(argv=("fixture-session", "reckon", "crew", "follow"))
    if source != "environment":
        _registered(home, orphan, owner_pid, source, live=False)
        _registered(home, owned, live_owner.pid, source, live=True)

    read_processes = census_module._processes

    def ancestry_with_dead_stamp():
        observed = read_processes()
        for pid in (os.getpid(), os.getppid()):
            if pid in observed:
                observed[pid]["environment"] = {"RECKON_OWNER_PID": str(owner_pid)}
        return observed

    monkeypatch.setattr(census_module, "_processes", ancestry_with_dead_stamp)
    report = census_module.census()
    assert [row["pid"] for row in report["orphaned"]] == [orphan.pid]
    assert owned.pid in {row["pid"] for row in report["spared"]}
    assert decoy.pid not in {
        row["pid"] for row in report["orphaned"] + report["spared"]
    }
    assert {os.getpid(), os.getppid()} <= set(report["excluded_pids"])
    assert report["unrecorded_count"] >= 2
    row = report["orphaned"][0]
    assert row["age_seconds"] >= 0
    assert row["state"] in {"S", "R"}
    assert row["wait_channel"] is not None
    assert row["cwd"] == str(Path.cwd())
    if source == "follower":
        assert report["duplicate_followers"] == [
            {
                "session": "fixture-session",
                "count": 2,
                "pids": sorted([orphan.pid, owned.pid]),
            }
        ]


def test_apply_prints_targets_then_stops_only_those_pids(processes):
    dead_pid = _dead_owner(processes)
    orphan = processes({"RECKON_OWNER_PID": str(dead_pid)})
    owned = processes({"RECKON_OWNER_PID": str(os.getpid())})
    decoy = processes(argv=("fixture-session",))
    result = CliRunner().invoke(main, ["crew", "census", "--apply"])
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.output.splitlines()]
    assert [row["pid"] for row in records[0]["orphaned"]] == [orphan.pid]
    assert records[1]["pid"] == orphan.pid
    assert records[1]["action"] == "stopped"
    assert len(records) == 2
    orphan.wait(timeout=2)
    assert orphan.returncode == -signal.SIGTERM
    assert owned.poll() is None
    assert decoy.poll() is None


def test_dry_run_sends_no_signal(processes):
    orphan = processes({"RECKON_OWNER_PID": str(_dead_owner(processes))})
    result = CliRunner().invoke(main, ["crew", "census"])
    assert result.exit_code == 0, result.output
    assert [row["pid"] for row in json.loads(result.output)["orphaned"]] == [orphan.pid]
    assert orphan.poll() is None


def test_renewed_producer_lease_is_spared_at_apply(processes):
    child = processes()
    path = _registered(crew_home(), child, os.getpid(), "producer", live=False)
    report = census_module.census()
    assert [row["pid"] for row in report["orphaned"]] == [child.pid]
    record = json.loads(path.read_text())
    record["lease_renewed_at"] = time.time()
    _write(path, record)
    stopped = census_module.apply_census(report)
    assert stopped[0]["action"] == "spared"
    assert child.poll() is None


@pytest.mark.parametrize(
    "change", ["foreign-host", "reused-pid", "missing-start", "missing-lease"]
)
def test_unverified_registration_is_spared(processes, change):
    child = processes()
    path = _registered(
        crew_home(), child, _dead_owner(processes), "producer", live=False
    )
    record = json.loads(path.read_text())
    if change == "foreign-host":
        record["host"] = "a-different-host"
    elif change == "reused-pid":
        record["pid_start_time"] = "not-the-process-start"
    elif change == "missing-start":
        record.pop("pid_start_time")
    else:
        record.pop("lease_renewed_at")
    _write(path, record)
    report = census_module.census()
    assert report["orphaned"] == []
    if change == "missing-lease":
        assert [row["pid"] for row in report["spared"]] == [child.pid]
    else:
        assert child.pid not in {row["pid"] for row in report["spared"]}
        assert report["unrecorded_count"] >= 1
    assert child.poll() is None


def test_follower_environment_carries_its_original_owner(processes):
    owner = _dead_owner(processes)
    child = processes(
        {
            "RECKON_FOLLOWER_OWNER": json.dumps(
                {"pid": owner, "start_time": "dead-owner"}
            )
        }
    )
    report = census_module.census()
    assert [row["pid"] for row in report["orphaned"]] == [child.pid]


def test_changed_target_identity_refuses_a_signal(processes):
    child = processes({"RECKON_OWNER_PID": str(_dead_owner(processes))})
    report = census_module.census()
    report["orphaned"][0]["start_time"] = "another-process"
    assert census_module.apply_census(report)[0]["action"] == "spared"
    assert child.poll() is None


def test_stopping_a_run_child_preserves_its_pointer(processes):
    owner = _dead_owner(processes)
    child = processes({"RECKON_RUN_ID": "orphan-run"})
    path = _registered(crew_home(), child, owner, "run", live=False)
    before = path.read_bytes()
    report = census_module.census()
    assert [row["pid"] for row in report["orphaned"]] == [child.pid]
    assert census_module.apply_census(report)[0]["action"] == "stopped"
    child.wait(timeout=2)
    assert path.read_bytes() == before


def test_apply_escalates_only_its_listed_stubborn_process(processes):
    owner = _dead_owner(processes)
    child = processes({"RECKON_OWNER_PID": str(owner)}, ignore_term=True)
    report = census_module.census()
    later_orphan = processes({"RECKON_OWNER_PID": str(owner)})
    results = census_module.apply_census(report)
    assert results == [
        {"pid": child.pid, "action": "stopped", "signals": ["SIGTERM", "SIGKILL"]}
    ]
    child.wait(timeout=2)
    assert child.returncode == -signal.SIGKILL
    assert later_orphan.poll() is None


def test_live_duplicate_followers_are_reported_and_spared(processes):
    children = [processes(), processes()]
    for child in children:
        _registered(crew_home(), child, os.getpid(), "follower", live=True)
    report = census_module.census()
    assert report["orphaned"] == []
    assert report["duplicate_followers"] == [
        {
            "session": "fixture-session",
            "count": 2,
            "pids": sorted(child.pid for child in children),
        }
    ]
    assert census_module.apply_census(report) == []
    assert all(child.poll() is None for child in children)


@pytest.mark.parametrize(
    "stamps",
    [
        {"RECKON_OWNER_PID": "unreadable"},
        {"RECKON_FOLLOWER_OWNER": "not-json"},
        {"RECKON_OWNER_PID": "0", "RECKON_OWNER_HOST": "another-host"},
    ],
)
def test_unknown_owner_is_spared(processes, stamps):
    child = processes(stamps)
    report = census_module.census()
    assert report["orphaned"] == []
    assert [row["pid"] for row in report["spared"]] == [child.pid]


def test_signal_refusal_is_printed_and_exits_nonzero(processes, monkeypatch):
    child = processes({"RECKON_OWNER_PID": str(_dead_owner(processes))})

    def refuse(_pid):
        raise PermissionError("fixture signal refusal")

    monkeypatch.setattr(census_module, "_pidfd_open", refuse)
    result = CliRunner().invoke(main, ["crew", "census", "--apply"])
    assert result.exit_code == 1
    records = [json.loads(line) for line in result.output.splitlines()]
    assert records[1] == {
        "pid": child.pid,
        "action": "refused",
        "reason": "fixture signal refusal",
    }
    assert child.poll() is None


@pytest.mark.parametrize("collision", ["foreign-host", "reused-pid"])
def test_colliding_records_do_not_mask_local_ownership(processes, collision):
    child = processes()
    _registered(crew_home(), child, _dead_owner(processes), "follower", live=False)
    path = _registered(crew_home(), child, os.getpid(), "producer", live=True)
    record = json.loads(path.read_text())
    if collision == "foreign-host":
        record["host"] = "a-different-host"
    else:
        record["pid_start_time"] = "a-reused-pid"
    _write(path, record)
    report = census_module.census()
    assert [row["pid"] for row in report["orphaned"]] == [child.pid]
    assert report["orphaned"][0]["owner"]["kind"] == "follower"


def test_conflicting_ownership_for_one_process_is_spared(processes):
    child = processes()
    owner = _dead_owner(processes)
    _registered(crew_home(), child, owner, "follower", live=False)
    _registered(crew_home(), child, owner, "producer", live=False)
    report = census_module.census()
    assert report["orphaned"] == []
    assert (
        report["spared"][0]["owner"]["unknown"]
        == "multiple registrations claim this process identity"
    )
    assert census_module.apply_census(report) == []
    assert child.poll() is None


@pytest.mark.parametrize("source", ["environment", "follower", "run"])
@pytest.mark.parametrize("collision", ["foreign-host", "reused-pid"])
def test_unrelated_registration_does_not_mask_recorded_owner(
    processes, source, collision
):
    owner = _dead_owner(processes)
    if source == "environment":
        stamps = {"RECKON_OWNER_PID": str(owner)}
    elif source == "follower":
        stamps = {"RECKON_FOLLOWER_OWNER": json.dumps({"pid": owner, "start_time": ""})}
    else:
        stamps = {"RECKON_RUN_ID": "orphan-run"}
    child = processes(stamps)
    if source == "run":
        _registered(crew_home(), child, owner, "run", live=False)
    path = _registered(crew_home(), child, os.getpid(), "producer", live=True)
    record = json.loads(path.read_text())
    if collision == "foreign-host":
        record["host"] = "a-different-host"
    else:
        record["pid_start_time"] = "a-reused-pid"
    _write(path, record)
    report = census_module.census()
    assert [row["pid"] for row in report["orphaned"]] == [child.pid]
    assert (
        report["orphaned"][0]["owner"]["kind"]
        == {
            "environment": "stamped",
            "follower": "follower",
            "run": "run",
        }[source]
    )


def test_process_snapshot_uses_the_owned_environment_reader(processes, monkeypatch):
    from reckon.crew import process_signal

    owner = _dead_owner(processes)
    child = processes()
    reader = process_signal._process_environment
    seen = []

    def read_environment(pid):
        seen.append(pid)
        if pid == child.pid:
            return {"RECKON_OWNER_PID": str(owner)}
        return reader(pid)

    monkeypatch.setattr(process_signal, "_process_environment", read_environment)
    report = census_module.census()
    assert child.pid in seen
    assert [row["pid"] for row in report["orphaned"]] == [child.pid]


def test_follower_stamp_uses_the_owned_decoder(processes, monkeypatch):
    from reckon.crew import follower_registration

    owner = _dead_owner(processes)
    child = processes({"RECKON_FOLLOWER_OWNER": "opaque-owner"})
    seen = []

    def decode(raw):
        seen.append(raw)
        return owner, ""

    monkeypatch.setattr(follower_registration, "_parse_follower_owner", decode)
    report = census_module.census()
    assert seen == ["opaque-owner"]
    assert [row["pid"] for row in report["orphaned"]] == [child.pid]


@pytest.mark.parametrize(
    ("renewed", "expected"),
    [
        (999.0, 9.0),
        (990.0, 0.0),
        (989.0, -1.0),
        (1001.0, 11.0),
        (None, None),
        (True, None),
        ("999", None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_producer_lease_snapshot_uses_record_and_clock(monkeypatch, renewed, expected):
    from reckon.crew import runs

    def unexpected_read(*_args):
        pytest.fail("a supplied registration and clock must need no live read")

    monkeypatch.setattr(runs, "read_watch_registration", unexpected_read)
    monkeypatch.setattr(runs, "_utc_epoch_seconds", unexpected_read)
    assert (
        runs.watch_lease_remaining_from_record(
            {"lease_renewed_at": renewed}, now=1000.0, lease_seconds=10.0
        )
        == expected
    )


@pytest.mark.parametrize("remaining", [1.0, 0.0, -1.0, None])
def test_census_uses_the_owned_producer_lease_reading(monkeypatch, remaining):
    from reckon.crew import runs

    owner = {"kind": "producer", "lease_renewed_at": 0.0}
    seen = []

    def lease_reading(record, *, now=None, lease_seconds=None):
        seen.append((record, now, lease_seconds))
        return remaining

    monkeypatch.setattr(runs, "watch_lease_remaining_from_record", lease_reading)
    alive, _reason = census_module._owner_alive(owner, now=1000.0)
    assert alive is (None if remaining is None else remaining > 0)
    assert seen == [(owner, 1000.0, None)]


def test_live_producer_lease_reader_uses_the_snapshot_primitive(monkeypatch):
    from reckon.crew import runs

    record = {"lease_renewed_at": 990.0}
    seen = []

    def lease_reading(snapshot, *, now=None, lease_seconds=None):
        seen.append((snapshot, now, lease_seconds))
        return 7.0

    monkeypatch.setattr(runs, "read_watch_registration", lambda _project: record)
    monkeypatch.setattr(runs, "watch_lease_remaining_from_record", lease_reading)
    assert runs.watch_lease_remaining("fixture-project", lease_seconds=20.0) == 7.0
    assert seen == [(record, None, 20.0)]
