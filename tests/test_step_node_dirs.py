"""A placed step creates the node-local bind sources on the node it runs on.

A launch is composed on the host that dispatched it and placed by srun onto the
allocation's node, which can be another node. The fence binds node-local
directories by source path, so a source made only on the composing host is
missing where the step lands and bubblewrap refuses the launch. The step's
wrapper therefore creates the node-local directory sources the composed fence
names before it execs the fence.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from reckon import _worker_fence
from reckon.crew import dispatch_launch


@pytest.fixture
def node_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    node_tmp = tmp_path / "node-tmp"
    shm = tmp_path / "shm"
    node_tmp.mkdir()
    shm.mkdir()
    monkeypatch.setattr(_worker_fence, "NODE_TMP_ROOT", node_tmp)
    monkeypatch.setattr(dispatch_launch, "NODE_LOCAL_SHM_ROOT", shm)
    return node_tmp, shm


def test_the_node_directories_are_the_fences_node_local_directory_sources(
    tmp_path: Path, node_roots: tuple[Path, Path]
) -> None:
    node_tmp, shm = node_roots
    scratch_root = node_tmp / "reckon-crew-scratch"
    own = scratch_root / "r-1"
    runtime = node_tmp / "uid-fleet"
    marker = node_tmp / ".codex"
    session_tmp = shm / "uid-fleet-job"
    for directory in (own, runtime, marker, session_tmp):
        directory.mkdir(parents=True)
    socket_like = node_tmp / "a-file"
    socket_like.write_text("")
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    after_separator = node_tmp / "not-a-bind"
    after_separator.mkdir()
    argv = [
        "/usr/bin/srun",
        "--overlap",
        "--jobid=1",
        "/bin/sh",
        "-c",
        dispatch_launch._STEP_TASK_WRAPPER,
        "sh",
        "/usr/bin/bwrap",
        "--dev-bind", "/", "/",
        "--unshare-pid", "--proc", "/proc",
        "--ro-bind", str(elsewhere), str(elsewhere),
        "--ro-bind", str(scratch_root), str(scratch_root),
        "--ro-bind", str(node_tmp), str(node_tmp),
        "--bind", str(own), str(own),
        "--bind", str(runtime), str(runtime),
        "--bind", str(marker), str(marker),
        "--bind", str(session_tmp), str(session_tmp),
        "--ro-bind", str(socket_like), str(socket_like),
        "--bind", str(own), str(own),
        "--",
        "/usr/bin/claude",
        "--bind", str(after_separator), str(after_separator),
    ]  # fmt: skip
    assert dispatch_launch.step_node_directories(argv) == [
        str(scratch_root),
        str(own),
        str(runtime),
        str(marker),
        str(session_tmp),
    ]


def test_an_argv_with_no_fence_has_no_node_directories() -> None:
    assert dispatch_launch.step_node_directories(["/usr/bin/claude", "-p"]) == []


def _run_wrapper(tmp_path: Path, environment: dict[str, str]) -> Path:
    record = tmp_path / "step-task.txt"
    result = subprocess.run(
        ["/bin/sh", "-c", dispatch_launch._STEP_TASK_WRAPPER, "sh", "true"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "SLURM_JOB_ID": "7",
            "SLURM_STEP_ID": "3",
            "RECKON_STEP_RECORD": str(record),
            **environment,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return record


def test_the_wrapper_creates_the_listed_directories_before_it_execs(
    tmp_path: Path,
) -> None:
    wanted = [tmp_path / "node" / "scratch" / "r-1", tmp_path / "node" / ".codex"]
    record = _run_wrapper(
        tmp_path,
        {dispatch_launch.STEP_NODE_DIRS_ENV: os.pathsep.join(map(str, wanted))},
    )
    assert all(directory.is_dir() for directory in wanted)
    job, step, pid, start = record.read_text().split()
    assert (job, step) == ("7", "3")
    assert int(pid) > 0 and int(start) > 0


def test_the_wrapper_creates_nothing_when_no_directories_are_listed(
    tmp_path: Path,
) -> None:
    wanted = tmp_path / "node" / "scratch" / "r-1"
    record = _run_wrapper(tmp_path, {})
    assert not wanted.exists()
    assert record.read_text().split()[:2] == ["7", "3"]
