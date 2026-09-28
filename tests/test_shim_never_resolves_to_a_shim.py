"""A reckon shim resolves to the real tool, never to another checkout's shim.

Every checkout of reckon carries its own shim directories, and a crew worker
running a suite from a worktree or a base-revision copy has the main checkout's
shims on its inherited ``PATH`` as well as the copy's. A lookup that drops only
its own directory then takes the other checkout's shim for the real tool. For
the git shim that is an unbounded chain: it runs the tool it found as a child
to ask which repository an invocation resolves to, the child does the same, and
a new interpreter starts at the leaf several times a second until the host runs
out of memory. These cases put two shims on one ``PATH`` and require the real
tool to answer, and they pin the depth bound that ends any loop the lookup
cannot see.

The end-to-end case runs in its own process group with a time bound, and a
timeout kills the whole group, so a regression here fails rather than leaving a
chain growing on the host.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

import reckon
from reckon import nested_launch, worker_git_shim
from reckon.host import HostFacts
from reckon.shim_lookup import (
    MAX_SHIM_DEPTH,
    SHIM_DEPTH_ENV,
    holds_shim,
    is_shim,
    real_executable,
)

dispatch = import_module("reckon.crew.dispatch")

REPO_ROOT = Path(reckon.__file__).resolve().parents[1]
GIT_SHIM_DIR = worker_git_shim.worker_shim_directory()
HOST_SHIM_DIR = nested_launch.shim_directory()
REAL_GIT = real_executable("git", os.environ.get("PATH", ""))
# A correct run starts two interpreters; the bound only has to separate that
# from a chain that never returns.
CHAIN_BOUND_SECONDS = 60


def _outside() -> HostFacts:
    return HostFacts(
        in_allocation=False,
        job_id=None,
        step_id=None,
        node=None,
        tmp_filesystem="xfs",
        home_filesystem="gpfs",
        tmp_is_node_local=True,
        reason="no-slurm-job-id-in-the-environment",
        sources={},
    )


def _copied_shim_directory(root: Path) -> Path:
    """A second checkout's git shim directory, laid out as a copy carries it."""
    directory = root / "other-checkout" / "reckon" / "worker_shims"
    directory.mkdir(parents=True)
    shutil.copy2(GIT_SHIM_DIR / "git", directory / "git")
    return directory


def test_every_shipped_shim_carries_the_marker() -> None:
    shims = [*GIT_SHIM_DIR.iterdir(), *HOST_SHIM_DIR.iterdir()]
    assert {path.name for path in shims} >= {"git", "srun", "sbatch", "salloc"}
    for path in shims:
        assert is_shim(path), f"{path} lacks the shim marker"


def test_the_real_git_is_not_a_shim() -> None:
    assert REAL_GIT is not None
    assert not is_shim(REAL_GIT)


def test_another_checkouts_shim_ahead_on_path_is_skipped(tmp_path: Path) -> None:
    assert REAL_GIT is not None
    copied = _copied_shim_directory(tmp_path)
    path = os.pathsep.join([str(copied), str(GIT_SHIM_DIR), os.path.dirname(REAL_GIT)])
    assert worker_git_shim.real_git(GIT_SHIM_DIR, path) == os.path.join(
        os.path.dirname(REAL_GIT), "git"
    )
    # Dropping only the copy's own directory still leaves the main one.
    assert worker_git_shim.real_git(copied, path) == os.path.join(
        os.path.dirname(REAL_GIT), "git"
    )


def test_a_scheduler_shim_never_resolves_to_another_checkouts(tmp_path: Path) -> None:
    other = tmp_path / "other-checkout" / "reckon" / "host_shims"
    other.mkdir(parents=True)
    shutil.copy2(HOST_SHIM_DIR / "srun", other / "srun")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "srun").write_text("#!/bin/sh\nexit 0\n")
    (fake / "srun").chmod(0o755)
    path = os.pathsep.join([str(other), str(HOST_SHIM_DIR), str(fake)])
    assert nested_launch.real_binary("srun", HOST_SHIM_DIR, path) == str(fake / "srun")


def test_two_checkouts_shims_on_path_reach_the_real_git(tmp_path: Path) -> None:
    """The shape that filled a node: a worker's run id and two shims on PATH."""
    assert REAL_GIT is not None
    copied = _copied_shim_directory(tmp_path)
    repo = tmp_path / "repo"
    subprocess.run([REAL_GIT, "init", "-q", str(repo)], check=True)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.pathsep.join(
            [
                str(copied),
                str(GIT_SHIM_DIR),
                os.path.dirname(REAL_GIT),
                "/usr/bin",
                "/bin",
            ]
        ),
        "HOME": str(home),
        "RECKON_HOME": str(home),
        "RECKON_RUN_ID": "r-shim-never-resolves-to-a-shim",
        "RECKON_SHIM_PYTHON": sys.executable,
        "PYTHONPATH": str(REPO_ROOT),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }
    process = subprocess.Popen(
        [str(copied / "git"), "status", "--short"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = process.communicate(timeout=CHAIN_BOUND_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        pytest.fail(
            f"git through two shims did not return within {CHAIN_BOUND_SECONDS}s: "
            "a shim resolved to the other shim instead of the real git"
        )
    assert process.returncode == 0, stderr


def test_the_forwarded_tool_carries_the_depth_one_higher(tmp_path: Path) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "git").write_text(f'#!/bin/sh\nprintf "%s" "${SHIM_DEPTH_ENV}"\n')
    (fake / "git").chmod(0o755)
    env = {
        "PATH": os.pathsep.join([str(GIT_SHIM_DIR), str(fake), "/usr/bin", "/bin"]),
        "HOME": str(tmp_path),
        "RECKON_SHIM_PYTHON": sys.executable,
        "PYTHONPATH": str(REPO_ROOT),
        SHIM_DEPTH_ENV: "3",
    }
    result = subprocess.run(
        [str(GIT_SHIM_DIR / "git"), "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=CHAIN_BOUND_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "4"


def test_a_git_shim_at_the_depth_bound_refuses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = worker_git_shim.main(
        ["status"],
        environ={
            "PATH": os.environ.get("PATH", ""),
            SHIM_DEPTH_ENV: str(MAX_SHIM_DEPTH),
        },
    )
    assert status == worker_git_shim.REFUSAL_STATUS
    assert f"{SHIM_DEPTH_ENV}={MAX_SHIM_DEPTH}" in capsys.readouterr().err


def test_a_scheduler_shim_at_the_depth_bound_refuses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = nested_launch.main(
        "srun",
        ["true"],
        environ={
            "PATH": os.environ.get("PATH", ""),
            SHIM_DEPTH_ENV: str(MAX_SHIM_DEPTH),
        },
    )
    assert status == nested_launch.REFUSAL_STATUS
    assert "srun shim: refusing" in capsys.readouterr().err


def test_a_launch_path_drops_another_checkouts_shims(tmp_path: Path) -> None:
    copied = _copied_shim_directory(tmp_path)
    assert holds_shim(copied, ["git"])
    searched = dispatch.launch_search_path(
        {"PATH": os.pathsep.join([str(copied), "/usr/bin", "/bin"])},
        facts=_outside(),
    )
    assert searched.split(os.pathsep) == [str(GIT_SHIM_DIR), "/usr/bin", "/bin"]
