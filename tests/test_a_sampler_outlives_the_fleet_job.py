"""Scheduler rows continue the node's series after the in-job sampler ends."""

from __future__ import annotations

import csv
import fcntl
import subprocess
from pathlib import Path

import pytest

from reckon.crew import fleet_node

JOB = "84521"
NODE = "recorded-node"


def scheduler(*answers):
    calls = []
    pending = iter(answers)

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 10
        answer = next(pending)
        if isinstance(answer, Exception):
            raise answer
        status, stdout, stderr = answer
        return subprocess.CompletedProcess(argv, status, stdout, stderr)

    return run, calls


def rows(directory):
    with (directory / f"{JOB}-{NODE}.tsv").open() as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def seed(directory):
    directory.mkdir(exist_ok=True)
    path = directory / f"{JOB}-{NODE}.tsv"
    with path.open("w") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=fleet_node._HEALTH_COLUMNS,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(
            {
                "utc": "2000-01-01T00:00:00Z",
                "job_id": JOB,
                "node": NODE,
                "source": "node",
                "job_mem_mb": "37",
            }
        )
    return path


def test_running_job_and_ended_job_continue_the_same_series(tmp_path):
    seed(tmp_path)
    runner, calls = scheduler(
        (0, f"{JOB}|{NODE}|RUNNING|None\n", ""),
        (0, "", ""),
        (0, f"{JOB}|{NODE}|CANCELLED by 1001|None\n", ""),
    )
    delays = []
    fleet_node.watch_scheduler(
        JOB, NODE, directory=tmp_path, runner=runner, sleep=delays.append
    )
    result = rows(tmp_path)
    assert len(result) == 3
    assert result[0]["job_mem_mb"] == "37"  # known-present positive control
    running, ended = result[1:]
    assert running["scheduler_running"] == "true"
    assert running["scheduler_event"] == "sample"
    assert ended["scheduler_event"] == "end"
    assert ended["scheduler_state"] == "CANCELLED by 1001"
    assert ended["scheduler_reason"] == "None"
    assert ended["job_id"] == JOB and ended["node"] == NODE
    assert ended["utc"] > result[0]["utc"]
    assert ended["job_mem_mb"] == "-"  # never invent a post-mortem memory sample
    assert len(delays) == 1 and 59 <= delays[0] <= 60
    assert calls[-1][0] == "sacct"
    # A restarted reader does not duplicate a terminal row.
    fleet_node.watch_scheduler(
        JOB,
        NODE,
        directory=tmp_path,
        runner=scheduler(
            (0, "", ""), (0, f"{JOB}|{NODE}|CANCELLED by 1001|None\n", "")
        )[0],
    )
    assert len(rows(tmp_path)) == 3


@pytest.mark.parametrize(
    "answers",
    [
        [(1, "", "controller unavailable"), (1, "", "accounting unavailable")],
        [subprocess.TimeoutExpired("squeue", 10)],
        [(0, "", ""), (0, "", "")],
        [(0, "malformed answer\n", "")],
        [(0, f"{JOB}|{NODE}|UNKNOWN|unknown scheduler state\n", "")],
        [(0, "", ""), (0, f"{JOB}.batch|{NODE}|COMPLETED|None\n", "")],
    ],
)
def test_unreadable_or_absent_scheduler_writes_unknown_never_end(tmp_path, answers):
    fleet_node.watch_scheduler(
        JOB,
        NODE,
        directory=tmp_path,
        passes=1,
        runner=scheduler(*answers)[0],
    )
    result = rows(tmp_path)
    assert len(result) == 1
    assert result[0]["scheduler_state"] == "UNKNOWN"
    assert result[0]["scheduler_event"] == "unknown"
    assert result[0]["scheduler_running"] == "unknown"


def test_scheduler_failure_does_not_stop_sampling(tmp_path):
    runner, _ = scheduler(
        (0, "", ""),
        (1, "", "temporarily unavailable"),
        (0, f"{JOB}|{NODE}|RUNNING|None\n", ""),
    )
    fleet_node.watch_scheduler(
        JOB,
        NODE,
        directory=tmp_path,
        passes=2,
        runner=runner,
        sleep=lambda _: None,
    )
    assert [row["scheduler_event"] for row in rows(tmp_path)] == ["unknown", "sample"]


def test_ended_allocation_keeps_node_and_scheduler_reason(tmp_path):
    runner, _ = scheduler(
        (1, "", "Invalid job id specified"),
        (0, f"{JOB}|{NODE}|NODE_FAIL|NodeFailure\n", ""),
    )
    fleet_node.watch_scheduler(JOB, NODE, directory=tmp_path, runner=runner)
    assert rows(tmp_path)[0]["scheduler_reason"] == "NodeFailure"
    assert rows(tmp_path)[0]["node"] == NODE


def test_reader_inside_watched_allocation_is_refused_before_any_write(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SLURM_JOB_ID", JOB)
    with pytest.raises(
        fleet_node.FleetNodeError, match="outside the watched allocation"
    ):
        fleet_node.watch_scheduler(JOB, NODE, directory=tmp_path / "absent")
    assert not (tmp_path / "absent").exists()


def test_a_second_reader_exits_while_the_first_holds_the_lock(tmp_path):
    with (tmp_path / f"{JOB}-scheduler.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fleet_node.watch_scheduler(JOB, NODE, directory=tmp_path)
    assert not list(tmp_path.glob("*.tsv"))


def test_hold_launches_reader_in_a_separate_job_on_another_node():
    script = fleet_node.generate_hold_script(
        fleet_node.fleet_size(),
        log_path="/decoy/fleet-%j.log",
    )
    assert "env -u SLURM_JOB_ID -u SLURM_JOBID sbatch" in script
    assert '--exclude="$(hostname -s)"' in script
    assert '--scheduler "$SLURM_JOB_ID" "$(hostname -s)"' in script
    assert "--partition=rigel" in script


def test_shell_and_reader_share_one_schema(tmp_path):
    script = Path(__file__).resolve().parents[1] / "bin" / "fleet-health"
    text = script.read_text()
    header = next(line for line in text.splitlines() if line.startswith("header=$'"))
    columns = header.removeprefix("header=$'").removesuffix("'").split(r"\t")
    assert columns == fleet_node._HEALTH_COLUMNS


def test_stopping_outside_reader_leaves_no_row_after_node_sampler(tmp_path):
    seed(tmp_path)
    # The stopped-reader mutation replaces watch_scheduler with a no-op.
    runner, _ = scheduler((0, f"{JOB}|{NODE}|CANCELLED|None\n", ""))
    fleet_node.watch_scheduler(JOB, NODE, directory=tmp_path, runner=runner)
    result = rows(tmp_path)
    assert len(result) == 2, (
        "outside reader must append after the in-job sampler's last row"
    )
    assert result[-1]["scheduler_event"] == "end"
