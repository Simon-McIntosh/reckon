"""Local disk samples retain mount identity and hold launches below reserve."""

import json
import os
import socket
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
    assert supervisor.node_disk_obligation("fixture") is None


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
    assert service.node_disk_admission_hold() is None


@pytest.mark.parametrize("named", [False, True])
def test_disk_pressure_prevents_both_named_and_service_admission(
    disk_state, monkeypatch, named
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
        "kind": "node-disk-reserve",
        "reason": "free bytes under reserve",
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
