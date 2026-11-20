"""A scheduler call by absolute path from inside the fence runs fenced.

The fence guards a submitted task through the ``sbatch``/``srun``/``salloc``
shims it puts first on ``PATH``, and a path is not what a scheduler call always
names: on 19 November a worker ran ``type -a sbatch``, found the real binary,
and submitted every later job as ``/usr/bin/sbatch`` from inside a fenced
worker, so the shim was never consulted and the jobs ran unfenced on a node
(A worker took that route on 19 November: jobs 1332134, 1332191 and 1332272 of
that run completed unfenced). The fence is the one place that can change what
an absolute path resolves to, so this file drives both routes from inside a
composed fence and reads what the scheduler was handed.

The scheduler here is a stand-in: an executable named ``sbatch`` in a directory
on ``PATH`` *after* the shim directory, which is the real binary the shim
resolves and forwards to. It writes the argv it is handed, and nothing else, so
the two arms are compared on the bytes a real scheduler would have received.

Declared negative control: at the base revision (main after ``1df382b80c``)
the absolute-path call reaches the stand-in with the bare, unfenced task,
while the by-name arm still reaches it wrapped.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from reckon import _worker_fence as fence
from reckon import nested_launch

# The stand-in is the real scheduler as far as the shim is concerned: it carries
# no shim marker, so a lookup that skips shims resolves it, and it records its
# own argv rather than launching anything.
STAND_IN = """#!/bin/sh
# NUL-separated, because one argument of a wrapped task is a program with
# newlines in it and a line-per-argument record would read as several words.
printf '%s\\0' "$@" > "$RECKON_STANDIN_RECORD"
"""

# One submission, spelled the way a worker inside an allocation must spell it: a
# partition named explicitly is the route the shim admits, and the wrap is what
# a caller hands the scheduler to run.
SUBMISSION = ("--partition=all_debug", "--wrap=echo fenced-task")

requires_bwrap = pytest.mark.skipif(
    shutil.which(fence.FENCE_BINARY) is None,
    reason=(
        "a user namespace is required to enter the fence: bubblewrap is not "
        "installed, so what a scheduler call reaches inside it cannot be read"
    ),
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A home, a worktree, a run directory and a scratch, sealed apart.

    The node's own ``/tmp`` is replaced by a directory of this test's making, so
    a composition can be entered here and its seals never reach the temporary
    files the fence itself has to read.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".config/reckon").mkdir(parents=True)
    (home / ".config/reckon/mounts.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RECKON_HOME", str(home / ".config/reckon"))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / ".config/reckon/mounts.json"))
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "store"))
    monkeypatch.delenv("UV_PYTHON_INSTALL_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    node_tmp = tmp_path / "node-tmp"
    node_tmp.mkdir()
    monkeypatch.setattr(fence, "node_tmp_seal_root", lambda: node_tmp)
    monkeypatch.setattr(fence, "create_node_tmp_runtime_paths", list)
    worktree = home / "Code/.reckon-worktrees/own"
    run = home / ".config/reckon/crew/runs/own"
    scratch = tmp_path / "scratch/own"
    for path in (worktree, run, scratch):
        path.mkdir(parents=True)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch.parent))
    # The real scheduler, one directory after the shim on the path below.
    scheduler = tmp_path / "scheduler-bin"
    scheduler.mkdir()
    stand_in = scheduler / "sbatch"
    stand_in.write_text(STAND_IN, encoding="utf-8")
    stand_in.chmod(0o755)
    # A second copy in a directory the flight layer declares for finding a
    # backend's command: the launch appends it to the worker's path, and this
    # process's own path never holds it.
    declared = tmp_path / "declared-bin"
    declared.mkdir()
    declared_stand_in = declared / "sbatch"
    declared_stand_in.write_text(STAND_IN, encoding="utf-8")
    declared_stand_in.chmod(0o755)
    record = run / "stand-in-argv.txt"
    path = os.pathsep.join(
        [
            str(nested_launch.shim_directory()),
            str(scheduler),
            os.environ.get("PATH", os.defpath),
        ]
    )
    monkeypatch.setenv("PATH", path)
    return {
        "home": home,
        "worktree": worktree,
        "run": run,
        "scratch": scratch,
        "stand_in": stand_in,
        "declared": declared,
        "record": record,
        "path": path,
    }


def _composed(case, command, *, config=None):
    """The argv a fenced worker launch is started with, and its own recipe."""
    argv = fence.fence_argv(
        ["/bin/sh", "-c", command],
        home=case["home"],
        worktree=case["worktree"],
        manifest_path=case["run"] / "manifest.md",
        writable_directories=[case["scratch"]],
        config=config,
    )
    assert nested_launch.WORKER_FENCE_ENV in argv, argv
    index = argv.index(nested_launch.WORKER_FENCE_ENV)
    recipe = json.loads(base64.urlsafe_b64decode(argv[index + 1]))
    return argv, recipe


def _environment(case):
    """What the launch hands the sandboxed process, with nothing of this one."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != nested_launch.WORKER_FENCE_ENV
    }
    environment.update(
        PATH=case["path"],
        HOME=str(case["home"]),
        RECKON_HOME=str(case["home"] / ".config/reckon"),
        RECKON_MOUNTS_PATH=str(case["home"] / ".config/reckon/mounts.json"),
        RECKON_STANDIN_RECORD=str(case["record"]),
        RECKON_SHIM_PYTHON=sys.executable,
        TMPDIR=str(case["scratch"]),
    )
    return environment


def _scheduler_received(case, command, *, config=None):
    """Enter the fence, run one submission, and read what the stand-in got."""
    case["record"].unlink(missing_ok=True)
    argv, recipe = _composed(case, command, config=config)
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
        env=_environment(case),
    )
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert case["record"].exists(), (
        f"the scheduler was never reached from inside the fence: {command!r}\n"
        f"{result.stderr}"
    )
    return _recorded(case["record"]), recipe


def _recorded(path):
    """The argv the stand-in was handed, as the stand-in separated it."""
    return [word.decode() for word in path.read_bytes().split(b"\0") if word]


def _carried_fence(arguments):
    """The fence a wrapped scheduler task carries, or None for a bare one.

    The wrap is an interpreter running the task bootstrap with the encoded spec
    as its last word, so a task submitted through the shim carries one and a
    task submitted past the shim carries none.
    """
    for argument in arguments:
        if not argument.startswith("--wrap="):
            continue
        words = shlex.split(argument[len("--wrap=") :])
        if not words:
            continue
        try:
            spec = json.loads(base64.urlsafe_b64decode(words[-1]))
        except ValueError:
            continue
        if isinstance(spec, dict) and "fence" in spec:
            return spec["fence"]
    return None


@requires_bwrap
def test_an_absolute_scheduler_path_reaches_the_shim(sandbox):
    absolute, worker_recipe = _scheduler_received(
        sandbox, shlex.join([str(sandbox["stand_in"]), *SUBMISSION])
    )
    carried = _carried_fence(absolute)
    assert carried is not None, (
        "the absolute path reached the real scheduler with a bare task, so the "
        f"job would run outside the worker's fence: {absolute}"
    )
    assert carried == worker_recipe, (
        "the task was wrapped, but not in the stand-in worker's own fence"
    )


@requires_bwrap
def test_a_scheduler_name_reaches_the_shim_with_the_same_wrapped_task(sandbox):
    by_name, _recipe = _scheduler_received(sandbox, shlex.join(["sbatch", *SUBMISSION]))
    absolute, _recipe = _scheduler_received(
        sandbox, shlex.join([str(sandbox["stand_in"]), *SUBMISSION])
    )
    assert _carried_fence(by_name) is not None, by_name
    # The same wrapped task either way: the route a caller picks moves the
    # file, never the fence on the task.
    assert by_name == absolute


@requires_bwrap
def test_a_declared_backend_directory_is_shielded(sandbox, monkeypatch):
    declared = sandbox["declared"]
    config = {"backends": {"alpha": {"search_path": [str(declared)]}}}
    command = shlex.join([str(declared / "sbatch"), *SUBMISSION])
    # The composing process's own path holds no copy of the tool at all: the
    # only one this launch can reach is in the directory the flight layer
    # declares, which the launch appends to the worker's path. A fence that
    # read only its own path would compose no mask here, and the absolute call
    # below would reach the stand-in bare.
    with monkeypatch.context() as composer:
        composer.setenv("PATH", str(nested_launch.shim_directory()))
        received, worker_recipe = _scheduler_received(sandbox, command, config=config)
    carried = _carried_fence(received)
    assert carried is not None, (
        f"the declared directory's scheduler was reached with a bare task: {received}"
    )
    assert carried == worker_recipe
