"""A placed run signals its Slurm task and lets the srun client exit itself."""

import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon._backends import LaunchPlan

process_signal = importlib.import_module("reckon.crew.process_signal")
dispatch_launch = importlib.import_module("reckon.crew.dispatch_launch")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
@pytest.mark.parametrize(
    "cgroup",
    [
        "0::/system.slice/slurmstepd.scope/job_1281970/step_17/user/task_0\n",
        "11:cpuset:/slurm/uid_39486/job_1281970/step_17/task_0\n",
    ],
)
def test_step_stop_signals_only_inner_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sig: int, cgroup: str
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "worker.json").write_text(
        json.dumps({"pid": 101, "argv": ["/usr/bin/srun", "--jobid=1281970"]})
    )
    (run / "step-task.txt").write_text("1281970 17 303 77\n")
    direct: list[int] = []
    direct_group_allowed: list[bool] = []
    inner_signals: list[tuple[int, int]] = []

    monkeypatch.setattr(
        process_signal,
        "_run_process_pids",
        lambda *args, **kwargs: [99, 101, 102, 303, 304, 305, 306],
    )
    monkeypatch.setattr(
        process_signal,
        "_running_descendants",
        lambda pid: [101, 102] if pid == 101 else [303, 304, 305, 306],
    )
    monkeypatch.setattr(
        process_signal, "_process_start_time", lambda pid: "77" if pid == 303 else "78"
    )
    monkeypatch.setattr(
        process_signal,
        "_process_comm",
        lambda pid: (
            "srun" if pid in (101, 102) else "bwrap" if pid in (303, 304) else "python"
        ),
    )
    read_text = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            cgroup
            if path in {Path(f"/proc/{pid}/cgroup") for pid in (303, 305, 306)}
            else ("bwrap\n" if path == Path("/proc/304/comm") else "python\n")
            if path in {Path(f"/proc/{pid}/comm") for pid in (304, 305, 306)}
            else read_text(path, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(
        process_signal.os, "kill", lambda pid, sent: inner_signals.append((pid, sent))
    )
    monkeypatch.setattr(
        process_signal,
        "_signal_one",
        lambda pid, *args, **kwargs: (
            direct.append(pid),
            direct_group_allowed.append(kwargs["allow_group"]),
            True,
        )[-1],
    )

    delivered = process_signal.signal_worker(
        99, sig, reason="run-stop", run_dir=run, run_id=run.name, reach_run=True
    )

    assert direct == [99]
    assert direct_group_allowed == [False]
    assert inner_signals == [(305, sig), (306, sig)]
    assert delivered == [99, 305, 306]
    records = [
        json.loads(line) for line in (run / "senders.jsonl").read_text().splitlines()
    ]
    assert [record["target_pid"] for record in records] == [305, 305, 306, 306]
    assert records[-1]["outcome"] == "delivered"


def test_step_without_task_identity_never_signals_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "worker.json").write_text(
        json.dumps({"pid": 101, "argv": ["/usr/bin/srun", "--jobid=1281970"]})
    )
    direct: list[int] = []
    monkeypatch.setattr(
        process_signal, "_run_process_pids", lambda *args, **kwargs: [99, 101, 102]
    )
    monkeypatch.setattr(process_signal, "_running_descendants", lambda pid: [101, 102])
    monkeypatch.setattr(
        process_signal,
        "_process_comm",
        lambda pid: "srun" if pid in (101, 102) else "python",
    )
    monkeypatch.setattr(
        process_signal,
        "_signal_one",
        lambda pid, *args, **kwargs: direct.append(pid) or True,
    )
    assert process_signal.signal_worker(
        99, reason="run-stop", run_dir=run, run_id=run.name, reach_run=True
    ) == [99]
    assert direct == [99]


def test_step_stop_before_worker_record_signals_only_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "supervisor.json").write_text(
        json.dumps({"plan": {"argv": ["/usr/bin/srun", "--jobid=1281970"]}})
    )
    monkeypatch.setattr(
        process_signal, "_run_process_pids", lambda *args, **kwargs: [99, 101, 102]
    )
    monkeypatch.setattr(
        process_signal, "_process_comm", lambda pid: "python" if pid == 99 else "srun"
    )
    sent: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        process_signal,
        "_signal_one",
        lambda pid, *args, **kwargs: sent.append((pid, kwargs["allow_group"])) or True,
    )
    assert process_signal.signal_worker(
        99, reason="run-stop", run_dir=tmp_path, reach_run=True
    ) == [99]
    assert sent == [(99, False)]


def test_step_supervisor_signal_never_uses_its_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_signal.os, "getpgid", lambda pid: 99 if pid == 99 else 7
    )
    sent: list[int] = []
    monkeypatch.setattr(process_signal.os, "kill", lambda pid, sig: sent.append(pid))
    monkeypatch.setattr(
        process_signal.os,
        "killpg",
        lambda *_args: pytest.fail("the supervisor's group contains srun"),
    )
    assert process_signal._signal_one(99, signal.SIGTERM, allow_group=False)
    assert sent == [99]


@pytest.mark.parametrize(
    ("cgroup", "actual_start"),
    [
        ("0::/system.slice/slurmstepd.scope/job_1281970/step_18/user/task_0\n", "77"),
        ("0::/system.slice/slurmstepd.scope/job_1281970/step_17/user/task_0\n", "78"),
    ],
)
def test_step_identity_mismatch_refuses_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cgroup: str, actual_start: str
) -> None:
    read_text = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            cgroup
            if path == Path("/proc/303/cgroup")
            else read_text(path, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(process_signal, "_process_start_time", lambda pid: actual_start)
    monkeypatch.setattr(
        process_signal.os,
        "kill",
        lambda *_args: pytest.fail("mismatched task identity was signalled"),
    )
    assert (
        process_signal._signal_step_task(
            tmp_path, "1281970.17", 303, "77", signal.SIGTERM, "run-stop", None
        )
        == []
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "senders.jsonl").read_text().splitlines()
    ]
    assert len(records) == 1
    assert records[0]["target_pid"] == 303
    assert records[0]["outcome"] == "refused"


@pytest.mark.parametrize("terminal_manifest", [False, True])
@pytest.mark.parametrize(
    ("record_at", "scheduler_step"),
    [(None, None), (None, "1281970.17"), (36, "1281970.17"), (15, None)],
)
def test_pending_step_signal_requires_scheduler_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_manifest: bool,
    record_at: int | None,
    scheduler_step: str | None,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "worker.json").write_text(
        json.dumps(
            {
                "pid": 101,
                "pid_start_time": "77",
                "argv": ["/usr/bin/srun", "--jobid=1281970", f"--job-name={run.name}"],
            }
        )
    )
    manifest = run / "manifest.md"
    if terminal_manifest:
        manifest.write_text("status: complete\n")
        monkeypatch.setattr(
            dispatch_launch, "_worker_manifest_done_status", lambda _: "complete"
        )
        monkeypatch.setattr(
            dispatch_launch, "_worker_manifest_is_whole", lambda _: True
        )
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(dispatch_launch.time, "monotonic", lambda: clock.now)

    def advance(seconds: float) -> None:
        clock.now += seconds
        if (
            record_at is not None
            and clock.now >= record_at
            and not (run / "step-task.txt").exists()
        ):
            (run / "step-task.txt").write_text("1281970 17 303 88\n")

    monkeypatch.setattr(
        dispatch_launch.time,
        "sleep",
        advance,
    )
    monkeypatch.setattr(
        dispatch_launch.os,
        "waitpid",
        lambda pid, _: (pid, 0) if clock.now >= 45 else (0, 0),
    )
    monkeypatch.setattr(process_signal, "_process_comm", lambda pid: "srun")
    monkeypatch.setattr(process_signal, "_process_start_time", lambda pid: "77")
    monkeypatch.setattr(process_signal.os, "getpgid", lambda pid: 101)
    queries: list[list[str]] = []

    def list_steps(command: list[str], **_: object) -> SimpleNamespace:
        queries.append(command)
        named = f"{scheduler_step} {run.name}\n" if scheduler_step else ""
        return SimpleNamespace(returncode=0, stdout="1281970.batch batch\n" + named)

    monkeypatch.setattr(process_signal, "subprocess", subprocess, raising=False)
    monkeypatch.setattr(process_signal.subprocess, "run", list_steps)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_signal.os, "kill", lambda pid, sig: sent.append((pid, sig))
    )
    inner: list[tuple[int, str]] = []
    attempts: list[float] = []

    def signal_step(pid: int, sig: int, **kwargs: object) -> list[int]:
        assert pid == 101
        assert sig == signal.SIGTERM or record_at is not None
        attempts.append(clock.now)
        if (run / "step-task.txt").exists():
            inner.append((sig, str(kwargs["reason"])))
            return [303]
        return []

    monkeypatch.setattr(dispatch_launch, "signal_worker", signal_step)
    stop = threading.Event()
    if not terminal_manifest:
        stop.set()
    ended: list[int] = []
    assert (
        dispatch_launch._reap_worker_on_its_terminal_manifest(
            101,
            run_directory=run,
            manifest_path=manifest,
            grace_seconds=0,
            baseline_ns=0,
            stop_requested=stop,
            stop_grace_seconds=55,
            ended_pids=ended,
        )
        == 0
    )
    assert len(queries) == (0 if record_at == 15 else 1)
    assert all(
        query[1:] == ["--steps", "--jobs", "1281970", "--noheader", "--format=%i %j"]
        for query in queries
    )
    if scheduler_step is None and record_at is None:
        assert sent == [(101, signal.SIGTERM)]
        assert ended == [101]
    else:
        assert sent == []
    assert all(
        attempt <= attempts[0] + 30 or (record_at is not None and attempt >= record_at)
        for attempt in attempts
    )
    if record_at is not None:
        assert inner
        assert ended == [303]
    elif scheduler_step is not None:
        assert inner == []
        assert ended == []
    if record_at != 15:
        receipts = [
            json.loads(line)
            for line in (run / "senders.jsonl").read_text().splitlines()
        ]
        if scheduler_step is None:
            assert [
                (entry["reason"], entry.get("outcome"), entry["target_pid"])
                for entry in receipts
            ] == [
                ("stop-before-step-started", None, 101),
                ("stop-before-step-started", "delivered", 101),
            ]
        else:
            assert [
                (entry["reason"], entry.get("outcome"), entry["target_pid"])
                for entry in receipts
            ] == [
                ("stop-before-step-unverified", "refused", 101),
            ]
            assert scheduler_step in receipts[0]["outcome_detail"]
    else:
        assert not (run / "senders.jsonl").exists()


def test_step_wrapper_does_not_exec_after_record_write_failure(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    command = f"from pathlib import Path; Path({str(marker)!r}).touch()"
    environment = os.environ | {
        "SLURM_JOB_ID": "1281970",
        "SLURM_STEP_ID": "17",
        "RECKON_STEP_RECORD": str(tmp_path / "missing" / "step-task.txt"),
    }
    answer = subprocess.run(
        [
            "/bin/sh",
            "-c",
            dispatch_launch._STEP_TASK_WRAPPER,
            "sh",
            sys.executable,
            "-c",
            command,
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert answer.returncode != 0
    assert not marker.exists()


def test_placed_step_is_named_for_its_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from reckon import flight
    from reckon.crew import placement

    backend = {"placement": {"scheduler": "srun", "options": []}}
    monkeypatch.setattr(flight, "placement_for", lambda _: backend["placement"])
    monkeypatch.setattr(
        placement, "ensure_reservation", lambda **_: {"job_id": "1281970"}
    )
    monkeypatch.setattr(placement, "read_reservation", lambda _: {"job_id": "1281970"})
    monkeypatch.setattr(placement, "reservation_alive", lambda _: True)
    monkeypatch.setattr(
        placement, "step_prefix", lambda job, _: ["--overlap", f"--jobid={job}"]
    )
    monkeypatch.setattr(
        dispatch_launch.shutil, "which", lambda *_, **__: "/usr/bin/srun"
    )
    plan = LaunchPlan(
        backend="probe",
        dialect="codex",
        argv=["/usr/bin/python3", "-c", "pass"],
        cwd="/work/tree",
        stdin_text="",
        environment={"PATH": "/usr/bin"},
        final_message_path=None,
        resumed_session=None,
    )
    placed = dispatch_launch.resolve_backend_placement(
        plan, backend, payload={"run_id": "r-probe-name"}
    )
    assert "--job-name=r-probe-name" in placed.argv


@pytest.mark.parametrize(("exit_code", "output"), [(1, ""), (0, "")])
def test_unreadable_step_listing_never_signals_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, output: str
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "worker.json").write_text(
        json.dumps(
            {
                "pid": 101,
                "pid_start_time": "77",
                "argv": ["/usr/bin/srun", "--jobid=1281970", f"--job-name={run.name}"],
            }
        )
    )
    monkeypatch.setattr(
        process_signal.subprocess,
        "run",
        lambda *_, **__: SimpleNamespace(returncode=exit_code, stdout=output),
    )
    monkeypatch.setattr(
        process_signal.os,
        "kill",
        lambda *_: pytest.fail("scheduler absence was not verified"),
    )
    assert process_signal.signal_pending_step_client(101, run) is None
    assert not (run / "senders.jsonl").exists()


def test_pending_client_start_mismatch_refuses_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "worker.json").write_text(
        json.dumps(
            {
                "pid": 101,
                "pid_start_time": "77",
                "argv": ["/usr/bin/srun", "--jobid=1281970", f"--job-name={run.name}"],
            }
        )
    )
    monkeypatch.setattr(
        process_signal.subprocess,
        "run",
        lambda *_, **__: SimpleNamespace(returncode=0, stdout="1281970.batch batch\n"),
    )
    monkeypatch.setattr(process_signal, "_process_comm", lambda _: "srun")
    monkeypatch.setattr(process_signal, "_process_start_time", lambda _: "78")
    monkeypatch.setattr(
        process_signal.os,
        "kill",
        lambda *_: pytest.fail("recycled client pid was signalled"),
    )
    assert process_signal.signal_pending_step_client(101, run) == "refused"
    records = [
        json.loads(row) for row in (run / "senders.jsonl").read_text().splitlines()
    ]
    assert records[0]["outcome"] == "refused"
    assert "identity" in records[0]["outcome_detail"]


@pytest.mark.skipif(
    os.environ.get("RECKON_FLEET_STEP_PROBE") != "1",
    reason="requires the held fleet allocation",
)
def test_fleet_step_finishes_after_inner_stop(tmp_path: Path) -> None:
    """The client waits while the sandbox reaps workers killed after grace."""
    run = tmp_path / "probe"
    run.mkdir()
    ready = run / "ready.txt"
    step_record = run / "step-task.txt"
    child = (
        "import os,signal,time,pathlib; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(120)"
    )
    parent = (
        "import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "time.sleep(120); p.wait()"
    )
    argv = [
        "/usr/bin/srun",
        "--overlap",
        f"--jobid={os.environ['SLURM_JOB_ID']}",
        "--ntasks=1",
        "--cpus-per-task=1",
        "--mem=1G",
        "--cpu-bind=none",
        f"--job-name={run.name}",
        "/bin/sh",
        "-c",
        dispatch_launch._STEP_TASK_WRAPPER,
        "sh",
        "/usr/bin/bwrap",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        "--bind",
        str(tmp_path),
        str(tmp_path),
        "--proc",
        "/proc",
        "--dev-bind",
        "/dev",
        "/dev",
        "--",
        "/usr/bin/python3",
        "-c",
        parent,
    ]
    environment = os.environ | {
        "RECKON_STEP_RECORD": str(step_record),
        "RECKON_ALLOW_NESTED_LAUNCH": "1",
    }
    node = os.environ["SLURMD_NODENAME"]
    state_command = ["sinfo", "-N", "-n", node, "-h", "-o", "%N %T %E"]
    state_before = subprocess.run(
        state_command, capture_output=True, text=True, check=True
    ).stdout
    with (run / "srun.stderr.log").open("w") as stderr:
        client = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=stderr, env=environment
        )
        try:
            deadline = time.monotonic() + 30
            while not (step_record.exists() and ready.exists()):
                assert client.poll() is None, (
                    "step exited before its child became ready"
                )
                assert time.monotonic() < deadline, "step did not become ready"
                time.sleep(0.1)
            job, step, task_pid, start_tick = step_record.read_text().split()
            step_id = f"{job}.{step}"
            listed = subprocess.run(
                ["squeue", "--steps", "--jobs", job, "--noheader", "--format=%i %j"],
                capture_output=True,
                text=True,
                check=False,
            )
            child_pids = [
                candidate
                for candidate in process_signal._running_descendants(int(task_pid))
                if any(
                    line.startswith("NSpid:") and line.split()[-1] == ready.read_text()
                    for line in Path(f"/proc/{candidate}/status")
                    .read_text()
                    .splitlines()
                )
            ]
            assert len(child_pids) == 1, (
                "child must be visible outside the PID namespace"
            )
            child_host_pid = child_pids[0]
            task_tree = process_signal._running_descendants(int(task_pid))
            bwrap_pids = {
                candidate
                for candidate in task_tree
                if Path(f"/proc/{candidate}/comm").read_text().strip() == "bwrap"
            }
            inner_pids = set(task_tree) - bwrap_pids
            assert int(task_pid) in bwrap_pids
            assert child_host_pid in inner_pids
            assert len(inner_pids) == 2, "probe needs one worker and one child"
            client_tree = set(process_signal._running_descendants(client.pid))
            print(
                f"step={step_id} task_pid={task_pid} task_start={start_tick} "
                f"bwrap_pids={sorted(bwrap_pids)} inner_pids={sorted(inner_pids)} "
                f"client_tree={sorted(client_tree)} child_namespace_pid={ready.read_text()}",
                flush=True,
            )
            (run / "worker.json").write_text(
                json.dumps(
                    {
                        "pid": client.pid,
                        "pid_start_time": process_signal._process_start_time(
                            client.pid
                        ),
                        "argv": argv,
                    }
                )
            )
            stop = threading.Event()
            stop.set()
            ended: list[int] = []
            started = time.monotonic()
            status = dispatch_launch._reap_worker_on_its_terminal_manifest(
                client.pid,
                run_directory=run,
                manifest_path=run / "manifest.md",
                grace_seconds=300,
                baseline_ns=0,
                stop_requested=stop,
                stop_grace_seconds=55,
                ended_pids=ended,
            )
            elapsed = time.monotonic() - started
            record = dispatch_launch._supervisor_exit_record(
                run_id=run.name,
                attempt=1,
                worker_pid=client.pid,
                launched_at="probe",
                status=status,
                run_directory=run,
                ended_pids=ended,
            )
            (run / "exit.json").write_text(json.dumps(record))
            print(
                f"supervisor_record={json.dumps(record, sort_keys=True)} elapsed={elapsed:.2f}s",
                flush=True,
            )
            rows: list[str] = []
            for _ in range(30):
                receipt = subprocess.run(
                    [
                        "sacct",
                        "-n",
                        "-P",
                        "-j",
                        step_id,
                        "-o",
                        "JobIDRaw,JobName,State,ExitCode",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                rows = [
                    line
                    for line in receipt.stdout.splitlines()
                    if line.split("|")[0] == step_id
                ]
                if rows and not rows[0].split("|")[2].startswith(
                    ("RUNNING", "PENDING")
                ):
                    break
                time.sleep(1)
            state_after = subprocess.run(
                state_command, capture_output=True, text=True, check=True
            ).stdout
            print(
                f"sacct={rows} node_before={state_before.strip()!r} node_after={state_after.strip()!r}",
                flush=True,
            )
            assert rows
            _, step_name, state, exit_code = rows[0].split("|")
            assert state in {"COMPLETED", "FAILED"}
            assert exit_code.split(":")[1] == "0"
            assert state_after == state_before
            assert 55 <= elapsed < 90, "step did not end after inner SIGKILL grace"
            assert set(ended) == inner_pids
            receipts = [
                json.loads(line)
                for line in (run / "senders.jsonl").read_text().splitlines()
            ]
            delivered = [row for row in receipts if row.get("outcome") == "delivered"]
            assert {row["target_pid"] for row in delivered} == inner_pids
            assert {row["signal"] for row in delivered} == {"SIGTERM", "SIGKILL"}
            assert all(
                row["target_pid"] not in bwrap_pids | client_tree for row in receipts
            )
            assert step_name == run.name
            if listed.returncode == 0:
                assert f"{step_id} {run.name}" in listed.stdout.splitlines()
            else:
                print(f"squeue_unreadable={listed.stderr.strip()!r}", flush=True)
        finally:
            if client.poll() is None:
                if step_record.exists():
                    job, step, task_pid, start_tick = step_record.read_text().split()
                    process_signal._signal_step_task(
                        run,
                        f"{job}.{step}",
                        int(task_pid),
                        start_tick,
                        signal.SIGKILL,
                        "probe-cleanup",
                        None,
                    )
                client.wait(timeout=65)
            artifact_home = os.environ.get("RECKON_STEP_PROBE_ARTIFACT_DIR")
            if artifact_home:
                destination = Path(artifact_home)
                destination.mkdir(parents=True, exist_ok=True)
                for name in (
                    "step-task.txt",
                    "senders.jsonl",
                    "exit.json",
                    "srun.stderr.log",
                ):
                    source = run / name
                    if source.exists():
                        shutil.copy2(source, destination / name)
