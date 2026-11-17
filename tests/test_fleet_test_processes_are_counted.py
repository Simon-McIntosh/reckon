"""Fleet health counts attributed runner trees and excludes bounded light work."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import fleet_supervisor as fleet
from reckon.crew import obligation_snapshot, obligations, runs
from tests import test_obligations_snapshot_is_published as snapshots
from tests.test_fleet_health import BASE, run_pass, sample_rows, sandbox

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PROJECT = snapshots.PROJECT
SESSION = snapshots.SESSION
snapshot_fleet = snapshots.fleet


def process(pid, ppid, argv, *, run_id="", rss=1024, elapsed=10, cpu=1):
    return {
        "pid": pid,
        "ppid": ppid,
        "argv": argv,
        "run_id": run_id,
        "rss_bytes": rss,
        "elapsed_seconds": elapsed,
        "cpu_seconds": cpu,
    }


def population():
    return [
        process(10, 1, ["worker"], run_id="r-first"),
        process(11, 10, ["python", "-m", "pytest", "tests/"], rss=2048),
        process(12, 11, ["python", "-c", "execnet_worker()"], rss=3072),
        process(13, 12, ["pytest", "tests/"], rss=4096),
        process(20, 1, ["worker"], run_id="r-second"),
        process(21, 20, ["/venv/bin/pytest", "tests/"], rss=8192),
        process(22, 21, ["helper"], rss=1024),
        process(
            30,
            1,
            ["python", "-m", "pytest", "tests/test_unit.py", "-n", "4"],
            run_id="r-light",
        ),
        process(31, 30, ["helper"]),
        process(
            40, 1, ["service", "--description", "pytest", "tests/"], run_id="r-service"
        ),
    ]


def write_processes(proc, rows, *, job="100"):
    proc.mkdir(exist_ok=True)
    (proc / "uptime").write_text("1000 1000\n")
    ticks = os.sysconf("SC_CLK_TCK")
    for row in rows:
        directory = proc / str(row["pid"])
        directory.mkdir()
        (directory / "cgroup").write_text(
            f"0::/slurm/uid_1/job_{job}/step_batch/task_0\n"
        )
        fields = ["0"] * 22
        fields[0] = "S"
        fields[1] = str(row["ppid"])
        fields[11] = str(int(row["cpu_seconds"] * ticks))
        fields[19] = str(int((1000 - row["elapsed_seconds"]) * ticks))
        (directory / "stat").write_text(
            f"{row['pid']} (name with spaces) " + " ".join(fields)
        )
        (directory / "status").write_text(f"VmRSS:\t{row['rss_bytes'] // 1024} kB\n")
        (directory / "cmdline").write_bytes("\0".join(row["argv"]).encode() + b"\0")
        (directory / "environ").write_bytes(f"RECKON_RUN_ID={row['run_id']}\0".encode())


def seed_pointers():
    for run_id, session in (("r-first", SESSION), ("r-second", "other-coordinator")):
        path = runs.pointer_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"run_id": run_id, "project": PROJECT, "session": session})
        )


def test_sampler_counts_disjoint_trees_and_excludes_service_and_light_work(
    tmp_path, monkeypatch
):
    proc = tmp_path / "proc"
    write_processes(proc, population())
    write_processes(proc, [process(90, 1, ["pytest", "tests/"], rss=65536)], job="999")
    state = tmp_path / "fleet"
    monkeypatch.setenv("FLEET_STATE_DIR", str(state))
    monkeypatch.setenv("FLEET_PROC_DIR", str(proc))
    monkeypatch.setenv("SLURM_JOB_ID", "100")
    seed_pointers()
    sample = fleet.sample_fleet_tests()
    assert sample["tree_count"] == 2
    assert sample["process_count"] == 5
    assert sample["rss_bytes"] == 18432
    assert [(tree["run_id"], tree["rss_bytes"]) for tree in sample["trees"]] == [
        ("r-first", 9216),
        ("r-second", 9216),
    ]
    assert [tree["session"] for tree in sample["trees"]] == [
        SESSION,
        "other-coordinator",
    ]
    assert json.loads((state / fleet.TEST_PROCESS_SAMPLE).read_text()) == sample


@pytest.mark.parametrize(
    "argv",
    [
        ["service", "--runner", "pytest"],
        ["python", "service.py", "pytest"],
        ["python", "-c", "pytest.main()"],
        ["bash", "-c", "pytest tests/"],
    ],
)
def test_service_arguments_do_not_identify_an_executed_runner(argv):
    assert fleet.test_runner_arguments(argv) is None


@pytest.mark.parametrize(
    "argv,elapsed,cpu,light",
    [
        (["pytest", "tests/test_unit.py"], 60, 60, True),
        (
            [
                "python",
                "-u",
                "-m",
                "pytest",
                "tests/test_one.py::test_it",
                "tests/test_two.py",
                "-n4",
                "-q",
            ],
            1,
            1,
            True,
        ),
        (
            [
                "python3.12",
                "/venv/bin/pytest",
                "tests/test_unit.py",
                "-p",
                "no:cacheprovider",
            ],
            1,
            1,
            True,
        ),
        (["pytest", "tests/test_unit.py", "--numprocesses=5"], 1, 1, False),
        (["pytest", "tests/test_unit.py", "-n", "auto"], 1, 1, False),
        (["pytest", "tests/test_unit.py"], 61, 1, False),
        (["pytest", "tests/test_unit.py"], 1, 61, False),
        (
            ["pytest", "tests/test_one.py", "tests/test_two.py", "tests/test_three.py"],
            1,
            1,
            False,
        ),
        (["pytest", "tests/"], 1, 1, False),
        (["pytest"], 1, 1, False),
        (["ctest"], 1, 1, False),
        (["python", "-m", "unittest", "discover"], 1, 1, False),
        (["pytest", "--pyargs", "package"], 1, 1, False),
    ],
)
def test_light_exception_requires_file_worker_and_time_bounds(
    argv, elapsed, cpu, light
):
    sample = fleet.fleet_test_processes(
        [process(1, 0, argv, elapsed=elapsed, cpu=cpu)],
        observed_at=NOW.isoformat(),
        job_id="100",
    )
    assert sample["tree_count"] == int(not light)


def test_descendant_cpu_can_exceed_the_light_bound():
    sample = fleet.fleet_test_processes(
        [
            process(1, 0, ["pytest", "tests/test_unit.py"], cpu=31),
            process(2, 1, ["helper"], cpu=30),
        ],
        observed_at=NOW.isoformat(),
        job_id="100",
    )
    assert sample["tree_count"] == 1


def test_health_row_carries_counts_bytes_and_attribution(tmp_path):
    box = sandbox(tmp_path, extra_env=BASE)
    write_processes(Path(box.env["FLEET_PROC_DIR"]), population())
    shim = box.root / "stubbin" / "permission-check"
    shim.write_text(
        f'#!/bin/bash\nif [ "$3" = check-interpreters ]; then echo 0; else exec {sys.executable} "$@"; fi\n'
    )
    seed_pointers()
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr
    row = sample_rows(box)[0]
    assert row["test_trees"] == "2"
    assert row["test_rss_bytes"] == "18432"
    assert [tree["run_id"] for tree in json.loads(row["test_runs"])] == [
        "r-first",
        "r-second",
    ]
    assert "2 test trees (18432 resident bytes)" in box.alerts()
    assert len(box.snapshots()) == 1


def test_failed_process_read_keeps_the_sample_unknown(tmp_path):
    box = sandbox(tmp_path, extra_env=BASE)
    shim = box.root / "stubbin" / "permission-check"
    shim.write_text(
        '#!/bin/bash\nif [ "$3" = check-interpreters ]; then echo 0; else exit 1; fi\n'
    )
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr
    assert sample_rows(box)[0]["test_trees"] == "unknown"
    assert sample_rows(box)[0]["test_rss_bytes"] == "unknown"


def test_zero_sample_clears_the_live_duty_and_other_owners_get_none(
    tmp_path, monkeypatch
):
    state = tmp_path / "fleet"
    state.mkdir()
    monkeypatch.setenv("FLEET_STATE_DIR", str(state))
    (state / fleet.RECORD_NAME).write_text('{"job_id":"100"}')
    sample = fleet.fleet_test_processes(
        population(), observed_at=NOW.isoformat(), job_id="100"
    )
    for tree in sample["trees"]:
        tree.update(project=PROJECT, session=SESSION)
    (state / fleet.TEST_PROCESS_SAMPLE).write_text(json.dumps(sample))
    duties = fleet.fleet_test_obligations(PROJECT, SESSION, now=NOW)
    assert len(duties) == 2
    assert "pytest tests/" in duties[0]["reason"]
    assert "r-first" in duties[0]["next_command"]
    assert fleet.fleet_test_obligations(PROJECT, "foreign", now=NOW) == []
    assert fleet.fleet_test_obligations("foreign", SESSION, now=NOW) == []
    assert (
        fleet.fleet_test_obligations(PROJECT, SESSION, now=NOW + timedelta(seconds=181))
        == []
    )
    sample["job_id"] = "999"
    (state / fleet.TEST_PROCESS_SAMPLE).write_text(json.dumps(sample))
    assert fleet.fleet_test_obligations(PROJECT, SESSION, now=NOW) == []
    sample = fleet.fleet_test_processes([], observed_at=NOW.isoformat(), job_id="100")
    (state / fleet.TEST_PROCESS_SAMPLE).write_text(json.dumps(sample))
    assert fleet.fleet_test_obligations(PROJECT, SESSION, now=NOW) == []


def test_the_duty_reaches_both_obligation_compositions(
    snapshot_fleet, tmp_path, monkeypatch
):
    state = tmp_path / "fleet"
    state.mkdir()
    monkeypatch.setenv("FLEET_STATE_DIR", str(state))
    sample = fleet.fleet_test_processes(
        population(), observed_at=snapshot_fleet["frozen"].isoformat(), job_id="100"
    )
    for tree in sample["trees"]:
        tree.update(project=PROJECT, session=SESSION)
    (state / fleet.RECORD_NAME).write_text('{"job_id":"100"}')
    (state / fleet.TEST_PROCESS_SAMPLE).write_text(json.dumps(sample))
    direct = obligations.obligations(PROJECT, SESSION)
    sliced = obligation_snapshot.payload_for(
        obligation_snapshot.fleet_state(PROJECT, now=snapshot_fleet["frozen"]), SESSION
    )

    def select(payload):
        return [
            row
            for row in payload["obligations"]
            if row["kind"] == "fleet-test-processes"
        ]

    assert len(select(direct)) == 2
    assert select(direct) == select(sliced)
