"""Local disk samples retain mount identity and hold launches below reserve."""

import json
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import fleet_supervisor as supervisor
from reckon.crew.admission import selection, service
from tests.test_fleet_health import BASE, run_pass, sample_rows, sandbox


def test_sampler_appends_all_disk_fields(tmp_path):
    box = sandbox(tmp_path, extra_env=BASE)
    reader = box.root / "stubbin" / "disk-python"
    reader.write_text("#!/bin/bash\necho '100 60 40 200'\n")
    reader.chmod(0o755)
    box.env["FLEET_DISK_PYTHON"] = str(reader)
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr
    row = sample_rows(box)[0]
    assert {
        key: row[key]
        for key in (
            "node_tmp_total_bytes",
            "node_tmp_used_bytes",
            "node_tmp_free_bytes",
            "node_tmp_free_inodes",
        )
    } == {
        "node_tmp_total_bytes": "100",
        "node_tmp_used_bytes": "60",
        "node_tmp_free_bytes": "40",
        "node_tmp_free_inodes": "200",
    }


@pytest.fixture
def mounts(tmp_path, monkeypatch):
    small, large = tmp_path / "small", tmp_path / "large"
    small.mkdir()
    large.mkdir()
    readings = {
        small: os.statvfs_result((4096, 4096, 100, 30, 20, 50, 12, 10, 0, 255)),
        large: os.statvfs_result((4096, 4096, 800, 500, 450, 900, 450, 440, 0, 255)),
    }
    original = os.statvfs
    monkeypatch.setattr(
        supervisor.os,
        "statvfs",
        lambda path: readings.get(Path(path)) or original(path),
    )
    return readings


def test_each_mount_records_its_own_independent_reading(mounts):
    rows = []
    for mount, independent in mounts.items():
        row = supervisor.node_disk_snapshot(mount, reserve_bytes=0)
        rows.append(row)
        block = independent.f_frsize
        assert row["mount"] == str(mount)
        assert abs(row["total_bytes"] - independent.f_blocks * block) <= block
        assert (
            abs(
                row["used_bytes"] - (independent.f_blocks - independent.f_bfree) * block
            )
            <= block
        )
        assert abs(row["free_bytes"] - independent.f_bavail * block) <= block
        assert row["free_inodes"] == independent.f_favail
    assert rows[0]["total_bytes"] != rows[1]["total_bytes"]


def test_pressure_names_largest_entries_and_owners_and_clears(mounts):
    small, large = mounts
    (small / "fuller").mkdir()
    (small / "fuller" / "data").write_bytes(b"x" * 32768)
    (small / "tiny").write_bytes(b"x")
    low = supervisor.node_disk_snapshot(small, reserve_bytes=100 * 4096)
    assert low["entries_complete"]
    assert low["largest_entries"][0]["path"] == str(small / "fuller")
    assert low["largest_entries"][0]["bytes"] >= 32768
    assert low["largest_entries"][0]["uid"] == os.getuid()
    pressure = supervisor.node_disk_pressure(low)
    assert pressure["kind"] == "node-disk-reserve"
    assert "fuller" in pressure["reason"]
    assert low["largest_entries"][0]["owner"] in pressure["reason"]
    assert (
        supervisor.node_disk_pressure(
            supervisor.node_disk_snapshot(large, reserve_bytes=100 * 4096)
        )
        is None
    )
    assert (
        supervisor.node_disk_pressure(
            supervisor.node_disk_snapshot(small, reserve_bytes=20 * 4096)
        )
        is None
    )


def test_a_failed_owner_census_is_never_reported_as_empty_usage(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("census refused")

    monkeypatch.setattr(supervisor.subprocess, "run", refuse)
    row = supervisor.node_disk_snapshot(tmp_path, reserve_bytes=10**30)
    assert row["entries_complete"] is False
    assert "census incomplete" in supervisor.node_disk_pressure(row)["reason"]
    assert "unavailable" in supervisor.node_disk_pressure(row)["reason"]


def test_sampler_failure_keeps_unknown_disk_fields(tmp_path):
    box = sandbox(tmp_path, extra_env=BASE)
    reader = box.root / "stubbin" / "disk-python"
    reader.write_text("#!/bin/bash\nexit 1\n")
    reader.chmod(0o755)
    box.env["FLEET_DISK_PYTHON"] = str(reader)
    assert run_pass(box.env).returncode == 0
    row = sample_rows(box)[0]
    assert all(row[name] == "unknown" for name in row if name.startswith("node_tmp_"))


@pytest.fixture
def disk_state(tmp_path, monkeypatch):
    state = tmp_path / "fleet"
    state.mkdir()
    monkeypatch.setenv("FLEET_STATE_DIR", str(state))
    monkeypatch.setenv("FLEET_NODE_TMP_ROOT", str(tmp_path))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "flight.yaml"))
    monkeypatch.setenv("SLURM_JOB_ID", "fixture-job")
    (state / "record.json").write_text(
        json.dumps({"job_id": "fixture-job", "node": socket.gethostname()})
    )
    return state


def test_sample_publishes_the_same_four_fields_and_obligation(
    disk_state, mounts, monkeypatch, capsys
):
    small = next(iter(mounts))
    monkeypatch.setenv("FLEET_NODE_TMP_ROOT", str(small))
    monkeypatch.setattr(supervisor, "node_disk_reserve_bytes", lambda: 100 * 4096)
    assert supervisor.sample_node_disk() == 0
    published = json.loads((disk_state / supervisor.NODE_DISK_RECORD).read_text())
    assert list(map(int, capsys.readouterr().out.split())) == [
        published[key]
        for key in (
            "total_bytes",
            "used_bytes",
            "free_bytes",
            "free_inodes",
        )
    ]
    assert supervisor.node_disk_obligation("fixture")["disk"] == published
    published["job_id"] = "departed-job"
    (disk_state / supervisor.NODE_DISK_RECORD).write_text(json.dumps(published))
    assert supervisor.node_disk_obligation("fixture")["kind"] == "node-disk-unreadable"


def test_admission_reads_the_local_disk_and_holds_only_below_reserve(
    disk_state, mounts, monkeypatch
):
    small, large = mounts
    monkeypatch.setattr(supervisor, "node_disk_reserve_bytes", lambda: 100 * 4096)
    monkeypatch.setenv("FLEET_NODE_TMP_ROOT", str(small))
    assert service.node_disk_admission_hold()["kind"] == "node-disk-reserve"
    monkeypatch.setenv("FLEET_NODE_TMP_ROOT", str(large))
    assert service.node_disk_admission_hold() is None
    monkeypatch.setenv("FLEET_NODE_TMP_ROOT", str(large / "missing"))
    assert service.node_disk_admission_hold()["kind"] == "node-disk-unreadable"
    (disk_state / "record.json").write_text(json.dumps({"node": "another-host"}))
    assert service.node_disk_admission_hold()["kind"] == "node-disk-unreadable"


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize(
    "reading_kind", ["node-disk-reserve", "node-disk-unreadable", "node-disk-stale"]
)
def test_disk_pressure_prevents_both_named_and_service_admission(
    disk_state, monkeypatch, named, reading_kind
):
    from contextlib import nullcontext

    from reckon.crew.admission import (
        holds,
        interrupted,
        launching,
        release,
        reviews,
        verdicts,
    )

    pointer = {
        "run_id": "fixture-run",
        "phase": "queued",
        "session": "fixture-session",
        "backend": "fixture-lane",
    }
    pressure = {
        "kind": reading_kind,
        "reason": f"disk condition: {reading_kind}",
        "run_id": "",
        "age_seconds": 0,
    }
    monkeypatch.setattr(service, "node_disk_admission_hold", lambda: pressure)
    monkeypatch.setattr(service, "_admit_lock", nullcontext)
    monkeypatch.setattr(selection, "_dispatch_fleet_gate", lambda: {"state": "open"})
    monkeypatch.setattr(
        selection.runs, "list_live", lambda **kwargs: [pointer] if not kwargs else []
    )
    monkeypatch.setattr(selection.runs, "read_pointer", lambda *args: pointer)
    monkeypatch.setattr(interrupted, "recover", dict)
    monkeypatch.setattr(
        launching, "_requeue_admitted_failures", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        launching, "_release_stranded_launches", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        release,
        "advance_waiting_requests",
        lambda *args: {"promoted": [], "blocked": [], "obligations": []},
    )
    monkeypatch.setattr(
        verdicts, "_record_readiness_verdicts", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        reviews, "_release_plan_review_holds", lambda *args, **kwargs: []
    )
    recorded = []
    monkeypatch.setattr(
        holds,
        "_record_pass_hold",
        lambda pointer, record, **kwargs: recorded.append(record),
    )

    def forbidden_launch(*args, **kwargs):
        pytest.fail("a launch reached the launcher below the disk reserve")

    result = selection.admit_once(
        run_id=pointer["run_id"] if named else "", launcher=forbidden_launch
    )
    assert result["launched"] == []
    assert result["held"] == [pointer["run_id"]]
    assert result["obligations"] == [pressure]
    assert recorded == [{"verdict": "held-by-gate", "reason": pressure["reason"]}]


def test_flight_reserve_default_and_override(disk_state):
    from reckon import flight
    from reckon._flight_schema import WorktreeConfig

    assert supervisor.node_disk_reserve_bytes(flight.resolve().config) == 8 * 1024**3
    assert (
        supervisor.node_disk_reserve_bytes(
            {"worktree": {"node_tmp_reserve_bytes": 123}}
        )
        == 123
    )
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        WorktreeConfig(node_tmp_reserve_bytes=-1)


def test_direct_and_published_obligations_include_the_same_disk_pressure(
    disk_state, mounts, monkeypatch
):
    from reckon.crew import obligation_snapshot, obligations

    small = next(iter(mounts))
    monkeypatch.setenv("FLEET_NODE_TMP_ROOT", str(small))
    monkeypatch.setattr(supervisor, "node_disk_reserve_bytes", lambda: 100 * 4096)
    assert supervisor.sample_node_disk() == 0
    direct = obligations.obligations("fixture", "fixture-session")
    published = obligation_snapshot.payload_for(
        obligation_snapshot.fleet_state("fixture"), "fixture-session"
    )
    direct_disk = [
        row for row in direct["obligations"] if row["kind"] == "node-disk-reserve"
    ]
    published_disk = [
        row for row in published["obligations"] if row["kind"] == "node-disk-reserve"
    ]
    assert len(direct_disk) == 1
    assert direct_disk == published_disk


def test_remote_admission_uses_the_published_fleet_disk(
    disk_state, mounts, monkeypatch
):
    small = next(iter(mounts))
    monkeypatch.setenv("FLEET_NODE_TMP_ROOT", str(small))
    monkeypatch.setattr(supervisor, "node_disk_reserve_bytes", lambda: 100 * 4096)
    assert supervisor.sample_node_disk() == 0
    expected = supervisor.node_disk_obligation("fixture")
    monkeypatch.setattr(socket, "gethostname", lambda: "remote-reader")

    def forbidden_local_sample(*args, **kwargs):
        pytest.fail("remote admission read the reader's local disk")

    monkeypatch.setattr(supervisor, "node_disk_snapshot", forbidden_local_sample)
    assert service.node_disk_admission_hold() == expected
    published = json.loads((disk_state / supervisor.NODE_DISK_RECORD).read_text())
    published["under_reserve"] = False
    published["free_bytes"] = published["reserve_bytes"]
    (disk_state / supervisor.NODE_DISK_RECORD).write_text(json.dumps(published))
    assert service.node_disk_admission_hold() is None


@pytest.mark.parametrize(
    "condition", ["missing", "unreadable", "expired-free", "expired-low"]
)
def test_remote_admission_refuses_unavailable_samples(
    disk_state, mounts, monkeypatch, condition
):
    small, large = mounts
    path = disk_state / supervisor.NODE_DISK_RECORD
    if condition == "unreadable":
        path.write_text("{broken")
    elif condition.startswith("expired"):
        sample = supervisor.node_disk_snapshot(
            small if condition == "expired-low" else large, reserve_bytes=100 * 4096
        )
        sample["utc"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        path.write_text(json.dumps(sample))
    monkeypatch.setattr(socket, "gethostname", lambda: "remote-reader")
    hold = service.node_disk_admission_hold()
    assert hold is not None, "a known remote fleet needs a current disk reading"
    expected = (
        "node-disk-stale" if condition.startswith("expired") else "node-disk-unreadable"
    )
    assert hold["kind"] == expected
    assert hold["kind"] != "node-disk-reserve"
    assert supervisor.node_disk_obligation("fixture")["kind"] == expected


@pytest.mark.parametrize("max_age", [17, 180])
def test_published_disk_reading_expires_at_the_configured_bound(
    disk_state, mounts, monkeypatch, max_age
):
    _, large = mounts
    sample = supervisor.node_disk_snapshot(large, reserve_bytes=100 * 4096)
    sampled_at = datetime.fromisoformat(sample["utc"])
    (disk_state / supervisor.NODE_DISK_RECORD).write_text(json.dumps(sample))
    monkeypatch.setattr(supervisor, "node_disk_sample_max_age_seconds", lambda: max_age)
    assert (
        supervisor.published_node_disk_sample(
            now=sampled_at + timedelta(seconds=max_age)
        )
        == sample
    )
    with pytest.raises(
        supervisor.NodeDiskSampleError, match="freshness limit"
    ) as error:
        supervisor.published_node_disk_sample(
            now=sampled_at + timedelta(seconds=max_age + 1)
        )
    assert error.value.kind == "node-disk-stale"


def test_fresh_remote_disk_reading_admits(disk_state, mounts, monkeypatch):
    _, large = mounts
    sample = supervisor.node_disk_snapshot(large, reserve_bytes=100 * 4096)
    (disk_state / supervisor.NODE_DISK_RECORD).write_text(json.dumps(sample))
    monkeypatch.setattr(socket, "gethostname", lambda: "remote-reader")
    assert service.node_disk_admission_hold() is None
    assert supervisor.node_disk_obligation("fixture") is None


@pytest.mark.parametrize(
    "damage",
    [
        "no-time",
        "bad-time",
        "future-time",
        "wrong-job",
        "wrong-node",
        "bad-metrics",
        "contradictory-reserve",
    ],
)
def test_remote_disk_reading_rejects_invalid_data(
    disk_state, mounts, monkeypatch, damage
):
    _, large = mounts
    sample = supervisor.node_disk_snapshot(large, reserve_bytes=100 * 4096)
    if damage == "no-time":
        sample.pop("utc")
    elif damage == "bad-time":
        sample["utc"] = "not a timestamp"
    elif damage == "future-time":
        sample["utc"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    elif damage == "wrong-job":
        sample["job_id"] = "departed-job"
    elif damage == "wrong-node":
        sample["node"] = "departed-host"
    elif damage == "bad-metrics":
        sample["free_bytes"] = "unknown"
    elif damage == "contradictory-reserve":
        sample["under_reserve"] = True
    (disk_state / supervisor.NODE_DISK_RECORD).write_text(json.dumps(sample))
    monkeypatch.setattr(socket, "gethostname", lambda: "remote-reader")
    assert service.node_disk_admission_hold()["kind"] == "node-disk-unreadable"


@pytest.mark.parametrize("state", ["missing", "expired"])
def test_both_obligation_readers_record_unavailable_disk_readings(
    disk_state, mounts, state
):
    from reckon.crew import obligation_snapshot, obligations

    if state == "expired":
        _, large = mounts
        sample = supervisor.node_disk_snapshot(large, reserve_bytes=100 * 4096)
        sample["utc"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        (disk_state / supervisor.NODE_DISK_RECORD).write_text(json.dumps(sample))
    kind = "node-disk-stale" if state == "expired" else "node-disk-unreadable"
    direct = obligations.obligations("fixture", "fixture-session")
    published = obligation_snapshot.payload_for(
        obligation_snapshot.fleet_state("fixture"), "fixture-session"
    )
    assert [
        row for row in direct["obligations"] if row["kind"].startswith("node-disk-")
    ] == [
        row for row in published["obligations"] if row["kind"].startswith("node-disk-")
    ]
    assert [
        row["kind"]
        for row in direct["obligations"]
        if row["kind"].startswith("node-disk-")
    ] == [kind]


def test_flight_disk_reading_age_default_and_override(disk_state):
    from reckon import flight
    from reckon._flight_schema import WorktreeConfig

    assert supervisor.node_disk_sample_max_age_seconds(flight.resolve().config) == 180
    assert (
        supervisor.node_disk_sample_max_age_seconds(
            {"worktree": {"node_tmp_sample_max_age_seconds": 17}}
        )
        == 17
    )
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        WorktreeConfig(node_tmp_sample_max_age_seconds=0)


def test_remote_queued_launch_waits_until_a_fresh_sample_arrives(
    disk_state, mounts, tmp_path, monkeypatch
):
    from reckon.crew import queue_admission, runs
    from tests import test_each_admission_pass_records_its_verdict as fixtures

    queue_root = tmp_path / "queue-fixture"
    queue_root.mkdir()
    repository = fixtures.admit_repo.__wrapped__(queue_root, monkeypatch)
    lane = queue_root / "lane.json"
    fixtures._write_lane(lane, fixtures._session_lane(1))
    _, resolve = fixtures._config_with_lane(lane)
    pointer = fixtures._queue(repository)
    runs.queue_dispatch(pointer)
    monkeypatch.setattr(socket, "gethostname", lambda: "remote-reader")
    launched = []

    def launch(queued, **kwargs):
        launched.append(queued["run_id"])
        return runs._mutate_pointer(
            queued["run_id"], lambda row: {**row, "phase": "working"}
        )

    monkeypatch.setattr(queue_admission, "_launch_queued", launch)
    missing = queue_admission.admit_once(run_id=pointer["run_id"], config_for=resolve)
    assert missing["held"] == [pointer["run_id"]]
    assert missing["obligations"][0]["kind"] == "node-disk-unreadable"
    assert launched == []
    assert runs.read_pointer(pointer["run_id"])["phase"] == "queued"

    _, large = mounts
    sample = supervisor.node_disk_snapshot(large, reserve_bytes=100 * 4096)
    sample["node"] = json.loads((disk_state / "record.json").read_text())["node"]
    (disk_state / supervisor.NODE_DISK_RECORD).write_text(json.dumps(sample))
    admitted = queue_admission.admit_once(run_id=pointer["run_id"], config_for=resolve)
    assert admitted["launched"] == [pointer["run_id"]]
    assert admitted["obligations"] == []
    assert launched == [pointer["run_id"]]


def test_remote_admission_holds_if_the_fleet_record_disappears_during_the_read(
    disk_state, monkeypatch
):
    monkeypatch.setattr(socket, "gethostname", lambda: "remote-reader")
    monkeypatch.setattr(supervisor, "published_node_disk_sample", lambda: None)
    assert service.node_disk_admission_hold()["kind"] == "node-disk-unreadable"


def test_disk_and_scheduler_samples_share_the_locked_series(tmp_path):
    from reckon.crew import fleet_node

    box = sandbox(tmp_path, extra_env=BASE)
    scratch_reader = box.root / "stubbin" / "permission-check"
    scratch_reader.write_text(
        '#!/bin/bash\ncase "${!#}" in sample-session-scratch) echo 73 ;; *) echo 0 ;; esac\n'
    )
    reader = box.root / "stubbin" / "disk-python"
    reader.write_text("#!/bin/bash\necho '100 60 40 200'\n")
    reader.chmod(0o755)
    box.env["FLEET_DISK_PYTHON"] = str(reader)
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr
    node_row = sample_rows(box)[0]
    assert node_row["source"] == "node"
    assert node_row["session_scratch_bytes"] == "73"
    assert node_row["node_tmp_free_bytes"] == "40"
    disk_columns = [
        "node_tmp_total_bytes",
        "node_tmp_used_bytes",
        "node_tmp_free_bytes",
        "node_tmp_free_inodes",
    ]
    columns = list(node_row)
    assert [column for column in columns if column in disk_columns] == disk_columns
    assert columns.index("source") < columns.index(disk_columns[0])
    assert fleet_node.append_scheduler_reading(
        box.samples()[0].parent,
        {
            "utc": "2000-01-01T00:00:00Z",
            "job_id": node_row["job_id"],
            "node": node_row["node"],
            "source": "scheduler",
            "scheduler_state": "CANCELLED",
            "scheduler_reason": "None",
            "scheduler_running": "false",
            "scheduler_event": "end",
        },
    )
    result = sample_rows(box)
    assert len(result) == 2
    assert result[0] == node_row
    assert result[1]["scheduler_event"] == "end"
    assert result[1]["node_tmp_free_bytes"] == "-"
    assert all(None not in row for row in result)


@pytest.fixture
def existing_scheduler_series(tmp_path):
    columns = [
        "utc",
        "load1",
        "load5",
        "procs_running",
        "procs_blocked",
        "dstate",
        "mem_avail_mb",
        "swap_free_mb",
        "job_mem_mb",
        "job_mem_max_mb",
        "home_stat_ms",
        "slurmd_ms",
        "ctld_ping",
        "tasks",
        "python_procs",
        "top_parent",
        "writable_interpreters",
        "job_oom_kill",
        "job_id",
        "node",
        "scheduler_state",
        "scheduler_reason",
        "scheduler_running",
        "scheduler_event",
        "source",
    ]
    row = {
        "utc": "2000-01-01T00:00:00Z",
        "job_id": "84521",
        "node": "recorded-node",
        "source": "node",
        "job_mem_mb": "37",
    }
    path = tmp_path / "84521-recorded-node.tsv"
    return path, columns, row


@pytest.mark.parametrize("extra_columns", [[], ["further_metric", "another_metric"]])
def test_outside_reader_finishes_existing_series_with_any_suffix(
    existing_scheduler_series, extra_columns, monkeypatch
):
    import csv
    import subprocess

    from reckon.crew import fleet_node

    monkeypatch.setattr(
        fleet_node,
        "_HEALTH_COLUMNS",
        [
            *fleet_node._HEALTH_COLUMNS,
            "additional_memory_bytes",
            "additional_task_count",
        ],
    )
    path, columns, node_row = existing_scheduler_series
    columns = columns + extra_columns
    with path.open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        writer.writerow(node_row)
    prefix = path.read_text()
    calls = []

    def scheduler(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "squeue"
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(
            argv, 0, "84521|recorded-node|CANCELLED|None\n", ""
        )

    fleet_node.watch_scheduler(
        "84521", "recorded-node", directory=path.parent, runner=scheduler
    )
    assert len(calls) == 1
    assert path.read_text().startswith(prefix)
    with path.open() as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        rows = list(reader)
        assert reader.fieldnames == columns
    assert len(rows) == 2
    assert rows[0]["source"] == "node"
    assert rows[0]["job_mem_mb"] == "37"
    assert rows[1]["source"] == "scheduler"
    assert rows[1]["scheduler_state"] == "CANCELLED"
    assert rows[1]["scheduler_event"] == "end"
    assert all(rows[1][column] == "-" for column in extra_columns)
    assert all(None not in row for row in rows)


@pytest.mark.parametrize(
    "missing_column",
    [
        "job_id",
        "node",
        "scheduler_state",
        "scheduler_reason",
        "scheduler_running",
        "scheduler_event",
        "source",
    ],
)
def test_scheduler_refuses_a_series_missing_a_required_column(
    existing_scheduler_series, missing_column
):
    import csv

    from reckon.crew import fleet_node

    path, columns, node_row = existing_scheduler_series
    columns.remove(missing_column)
    with path.open("w") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=columns, delimiter="\t", extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerow(node_row)
    before = path.read_bytes()
    terminal = dict(
        node_row,
        source="scheduler",
        scheduler_state="CANCELLED",
        scheduler_reason="None",
        scheduler_running="false",
        scheduler_event="end",
    )
    with pytest.raises(fleet_node.FleetNodeError, match="lacks scheduler columns"):
        fleet_node.append_scheduler_reading(path.parent, terminal)
    assert path.read_bytes() == before
