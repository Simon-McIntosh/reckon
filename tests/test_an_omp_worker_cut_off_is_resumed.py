"""An unfinished omp process exit is recovered by the normal sweep."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import recovery, resumption, runs
from tests import test_a_killed_worker_resumes_to_completion as supervised

home = supervised.home
repository = supervised.repository


def _exited_worker(tmp_path, repository, monkeypatch, *, status="in-progress"):
    monkeypatch.setattr(supervised, "WORKER_BINARY", "omp")
    worktree = supervised._worktree_with_committed_work(repository, tmp_path)
    directory = runs.run_dir(supervised.RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    bin_directory = directory / "bin"
    bin_directory.mkdir()
    pointer = supervised._write_pointer(
        repository, worktree, manifest, bin_directory=bin_directory
    )
    pointer["command"] = str(bin_directory / "omp")
    runs._write_json(runs.pointer_path(supervised.RUN_ID), pointer)
    manifest.write_text(f"status: {status}\ncommits: none\n")
    body = """#!/bin/sh
sleep 0.3
echo '{"type":"session","id":"omp-surviving-session"}'
echo '{"type":"tool_execution_start","toolCallId":"shell-call","toolName":"bash","args":{"command":"sleep 1200"}}'
echo '{"type":"tool_execution_update","toolCallId":"shell-call","toolName":"bash"}'
exit 0
"""
    worker = supervised._stub_worker(bin_directory, "omp", body)
    plan = _backends.LaunchPlan(
        backend=supervised.BACKEND,
        dialect="omp",
        argv=[str(worker)],
        cwd=str(worktree),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )
    pid = supervised._dispatch_attempt(plan, repository, worktree)
    pointer.update(pid=pid, launcher_host=socket.gethostname())
    runs._write_json(runs.pointer_path(supervised.RUN_ID), pointer)
    supervised._wait_until_gone(pid)
    end = supervised._wait_for_exit_record(attempt=1)
    assert end["exit_code"] == 0
    assert end["last_record_type"] == "tool_execution_update"
    return bin_directory, manifest


def test_an_unfinished_omp_exit_is_cut_off_and_automatically_resumed(
    home: Path, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_directory, manifest = _exited_worker(tmp_path, repository, monkeypatch)
    pointer = runs.read_pointer(supervised.RUN_ID)
    row = recovery.classify_pointer(pointer)
    assert row["classification"] == "cut-off", row["detail"]
    assert "bash" in row["detail"]
    assert "sleep 1200" in row["detail"]
    assert row["recovery"] == "resume"
    supervised._stub_worker(
        bin_directory,
        "omp",
        '#!/bin/sh\nsleep 0.3\nprintf "status: complete\\ncommits: none\\n" > '
        + str(manifest)
        + '\necho \'{"type":"agent_end","messages":[]}\'\n',
    )
    monkeypatch.setattr(resumption, "dispatch_awaiting_reviews", lambda **kw: {})
    result = resumption.sweep(
        supervised.PROJECT,
        config=supervised._flight_config(bin_directory),
        pointers=[pointer],
    )
    assert len(result["resumed"]) == 1, result
    assert result["resumed"][0]["trigger"] == "cut-off"
    resumed = runs.read_pointer(supervised.RUN_ID)
    assert resumed["attempt"] == 2
    supervised._wait_until_gone(int(result["resumed"][0]["pid"]))
    assert supervised._wait_for_exit_record(attempt=2)["exit_code"] == 0
    assert "status: complete" in manifest.read_text()


def test_a_complete_manifest_at_omp_exit_stays_delivered(
    home: Path, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exited_worker(tmp_path, repository, monkeypatch, status="complete")
    row = recovery.classify_pointer(runs.read_pointer(supervised.RUN_ID))
    assert row["classification"] in {"scoring", "promotable", "completed_unpromoted"}
    assert row["manifest_status"] == "complete"
    assert row["classification"] != "cut-off"


def test_automatic_cut_off_recovery_stops_at_the_flight_bound(
    home: Path, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_directory, _ = _exited_worker(tmp_path, repository, monkeypatch)
    pointer = runs.read_pointer(supervised.RUN_ID)
    config = supervised._flight_config(bin_directory)
    config["fences"]["needs_help_after_failures"] = 1
    result = resumption.sweep(
        supervised.PROJECT, config=config, pointers=[pointer], dry_run=True
    )
    assert result["resumed"] == []
    assert any(item["reason"] == "cut-off-attempt-limit" for item in result["skipped"])


def test_a_live_omp_worker_is_never_cut_off(
    home: Path, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    _exited_worker(tmp_path, repository, monkeypatch)
    pointer = runs.read_pointer(supervised.RUN_ID)
    pointer.update(pid=os.getpid(), launcher_host=socket.gethostname())
    pointer.pop("pid_start_time", None)
    worker_path = runs.run_dir(supervised.RUN_ID) / "worker.json"
    import json

    worker = json.loads(worker_path.read_text())
    worker.update(
        pid=os.getpid(), pid_start_time=recovery._process_start_time(os.getpid())
    )
    runs._write_json(worker_path, worker)
    directory = runs.run_dir(supervised.RUN_ID)
    end = supervised.dispatch_module._supervisor_exit_record(
        run_id=worker["run_id"],
        attempt=worker["attempt"],
        worker_pid=worker["pid"],
        launched_at=worker["launched_at"],
        status=0,
        run_directory=directory,
    )
    runs._write_json(directory / recovery.EXIT_RECORD_NAME, end)
    assert end["worker_pid"] == os.getpid()
    row = recovery.classify_pointer(pointer)
    assert row["classification"] == "running"


def test_omp_launch_waits_for_background_results(tmp_path: Path) -> None:
    import yaml

    overlay = _backends.omp_overlay_path(str(tmp_path))
    assert yaml.safe_load(overlay.read_text())["async"]["printWake"] is True


def test_cut_off_reuses_the_shared_last_tool_reader(tmp_path, monkeypatch) -> None:
    import json

    from reckon.crew import recovery_cut_off, recovery_wait

    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps(
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "toolCallId": "shell-call",
                "args": {"command": "sleep 1200"},
            }
        )
        + "\n"
    )
    read_paths = []
    original = recovery_wait._last_tool_call

    def observed(path):
        read_paths.append(path)
        return original(path)

    monkeypatch.setattr(recovery_wait, "_last_tool_call", observed)
    cut_off = recovery_cut_off.omp_cut_off(
        {"command": "omp", "attempt": 1},
        manifest_status="in-progress",
        exit_record={"ended_during": "working"},
        stream=stream,
    )
    assert recovery_cut_off.cut_off_detail(cut_off) == "bash: sleep 1200"
    assert read_paths == [stream]
