"""A job runs fenced whatever environment its submitter carries.

The shim reads the fence recipe from ``RECKON_WORKER_FENCE``, so a submitter
that clears the variables the shim reads makes its jobs run with no fence. What
such a shell clears is its own environment and what it hands its children; it
does not reach what the kernel recorded for the processes that started it, and
a fenced worker's tree always holds the fence there. The shim therefore fills
whatever its own environment is missing from the process tree, and every case
here drives a real submission and reads what the scheduler stand-in received.

The cases:

* a submission from inside a fence, unchanged, is wrapped in the worker's fence;
* the same submission from a shell with every ``RECKON_`` name dropped is
  wrapped in the same fence, byte for byte;
* the same submission with an empty environment is wrapped or refused;
* a submission from outside any fence is passed through unchanged;
* a fence that cannot be established refuses, and the refusal names the run.

Declared negative control: at the base revision the cleared case reaches the
stand-in scheduler with the bare task, because the shim has no recipe to wrap
with.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys

import pytest

from reckon import _worker_fence as fence
from reckon import nested_launch

RUN_ID = "r-20261121T090142936333-a-job-runs-fenced-whatever-its-submitter-carries"
SUBMISSION = ["--partition=all_debug", "--wrap=echo fenced-task"]

# The stand-in is the real scheduler as far as the shim is concerned: it
# carries no shim marker, writes the argv it was handed to the record file
# baked in here, and launches nothing. The record path is baked in rather than
# read from the environment because one arm hands it an empty environment.
STAND_IN = """#!/bin/sh
printf '%s\\0' "$@" > "{record}"
"""

# A worker stand-in: the fence starts it, and it hands one submission to a
# child whose environment the case names.
WORKER_STAND_IN = """\
import json, os, pathlib, subprocess, sys

mode, shim, record, report = sys.argv[1:5]
worker = dict(os.environ)
if mode == "unchanged":
    child = dict(worker)
elif mode == "cleared":
    child = {n: v for n, v in worker.items() if not n.startswith("RECKON_")}
elif mode == "empty":
    child = {}
else:
    raise SystemExit("unknown mode: " + mode)
record = pathlib.Path(record)
record.unlink(missing_ok=True)
done = subprocess.run(
    [shim, *json.loads(os.environ["SUBMISSION"])],
    env=child,
    capture_output=True,
    text=True,
    check=False,
)
recorded = record.read_bytes().split(b"\\0") if record.exists() else []
pathlib.Path(report).write_text(json.dumps({
    "mode": mode,
    "worker_names": sorted(n for n in worker if n.startswith("RECKON_")),
    "returncode": done.returncode,
    "stdout": done.stdout,
    "stderr": done.stderr,
    "recorded": [w.decode() for w in recorded if w],
}))
"""

requires_bwrap = pytest.mark.skipif(
    shutil.which(fence.FENCE_BINARY) is None,
    reason=(
        "a user namespace is required to enter the fence: bubblewrap is not "
        "installed, so what the scheduler was handed inside it cannot be read"
    ),
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A home, a worktree, a run directory and a scratch, sealed apart."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".config/reckon").mkdir(parents=True)
    (home / ".config/reckon/mounts.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RECKON_HOME", str(home / ".config/reckon"))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / ".config/reckon/mounts.json"))
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "store"))
    node_tmp = tmp_path / "node-tmp"
    node_tmp.mkdir()
    monkeypatch.setattr(fence, "node_tmp_seal_root", lambda: node_tmp)
    monkeypatch.setattr(fence, "create_node_tmp_runtime_paths", list)
    worktree = home / "Code/.reckon-worktrees/own"
    run = home / ".config/reckon/crew/runs" / RUN_ID
    scratch = tmp_path / "scratch" / RUN_ID
    for path in (worktree, run, scratch):
        path.mkdir(parents=True)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch.parent))
    scheduler = tmp_path / "scheduler-bin"
    scheduler.mkdir()
    record = run / "stand-in-argv.bin"
    stand_in = scheduler / "sbatch"
    stand_in.write_text(STAND_IN.format(record=record), encoding="utf-8")
    stand_in.chmod(0o755)
    worker = tmp_path / "worker_stand_in.py"
    worker.write_text(WORKER_STAND_IN, encoding="utf-8")
    # The composing process's own search path names the stand-in, so the fence
    # relays and masks that copy rather than the node's real scheduler.
    path = os.pathsep.join(
        [str(nested_launch.shim_directory()), str(scheduler), os.defpath]
    )
    monkeypatch.setenv("PATH", path)
    return {
        "path": path,
        "home": home,
        "worktree": worktree,
        "run": run,
        "scratch": scratch,
        "scheduler": scheduler,
        "stand_in": stand_in,
        "record": record,
        "worker": worker,
    }


def _path(case):
    return case["path"]


def _run_worker(case, *, mode):
    """Enter the fence and run one worker stand-in in the named mode."""
    report = case["run"] / f"report-{mode}.json"
    argv = fence.fence_argv(
        [
            sys.executable,
            str(case["worker"]),
            mode,
            str(nested_launch.shim_directory() / "sbatch"),
            str(case["record"]),
            str(report),
        ],
        home=case["home"],
        worktree=case["worktree"],
        manifest_path=case["run"] / "manifest.md",
        writable_directories=[case["scratch"]],
    )
    index = argv.index(nested_launch.WORKER_FENCE_ENV)
    recipe = json.loads(base64.urlsafe_b64decode(argv[index + 1]))
    case["record"].unlink(missing_ok=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("RECKON_")
    }
    environment.update(
        PATH=_path(case),
        HOME=str(case["home"]),
        RECKON_MOUNTS_PATH=str(case["home"] / ".config/reckon/mounts.json"),
        SUBMISSION=json.dumps(SUBMISSION),
        TMPDIR=str(case["scratch"]),
    )
    done = subprocess.run(
        argv, capture_output=True, text=True, check=False, timeout=240, env=environment
    )
    assert done.returncode == 0, (
        done.returncode,
        done.stderr,
    )
    assert report.exists(), f"the worker stand-in never reported: {done.stderr}"
    return json.loads(report.read_text(encoding="utf-8")), recipe


def _carried_fence(arguments):
    """The fence a wrapped scheduler task carries, or None for a bare one."""
    for argument in arguments:
        if not argument.startswith("--wrap="):
            continue
        for word in argument[len("--wrap=") :].split():
            try:
                spec = json.loads(base64.urlsafe_b64decode(word))
            except (ValueError, TypeError):
                continue
            if isinstance(spec, dict) and "fence" in spec:
                return spec["fence"]
    return None


@requires_bwrap
def test_a_a_fenced_worker_is_wrapped_in_its_own_fence(sandbox):
    report, recipe = _run_worker(sandbox, mode="unchanged")
    carried = _carried_fence(report["recorded"])
    assert carried is not None, (
        "the submission reached the scheduler with a bare task: "
        f"{report['recorded']} rc={report['returncode']} err={report['stderr']!r} "
        f"out={report['stdout']!r}"
    )
    assert carried == recipe, "the task was wrapped, but not in the worker's own fence"


@requires_bwrap
def test_b_a_shell_that_drops_every_reckon_name_still_submits_fenced(sandbox):
    unchanged, _recipe = _run_worker(sandbox, mode="unchanged")
    cleared, _recipe = _run_worker(sandbox, mode="cleared")
    assert any(n.startswith("RECKON_") for n in cleared["worker_names"]), (
        "the worker stand-in itself lost the fence, so the case measured nothing"
    )
    carried = _carried_fence(cleared["recorded"])
    assert carried is not None, (
        f"a shell that dropped every RECKON_ name submitted the bare task: "
        f"{cleared['recorded']}"
    )
    assert cleared["recorded"] == unchanged["recorded"], (
        "the cleared submission was wrapped differently from the unchanged one"
    )


@requires_bwrap
def test_c_an_empty_environment_is_wrapped_or_refused(sandbox):
    unchanged, _recipe = _run_worker(sandbox, mode="unchanged")
    empty, _recipe = _run_worker(sandbox, mode="empty")
    if empty["returncode"] == 0:
        wrapped = _carried_fence(empty["recorded"])
        assert wrapped is not None, (
            f"an empty-environment submission ran unfenced: {empty['recorded']}"
        )
        assert empty["recorded"] == unchanged["recorded"]
    else:
        assert empty["recorded"] == [], (
            f"a refused submission still reached the scheduler: {empty['recorded']}"
        )
        assert RUN_ID in (empty["stderr"] + empty["stdout"]), empty["stderr"]


@requires_bwrap
def test_d_a_submission_from_outside_any_fence_is_passed_through(sandbox):
    # A fresh pid namespace, whose own processes carry no fence: the shim's
    # ancestors are that namespace's, and none of them holds a recipe.
    sandbox["record"].unlink(missing_ok=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("RECKON_")
    }
    environment.update(PATH=_path(sandbox), TMPDIR=str(sandbox["scratch"]))
    command = [
        fence.FENCE_BINARY,
        "--dev-bind",
        "/",
        "/",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--",
        str(nested_launch.shim_directory() / "sbatch"),
        *SUBMISSION,
    ]
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
        env=environment,
    )
    assert done.returncode == 0, (done.returncode, done.stderr)
    recorded = [
        word.decode() for word in sandbox["record"].read_bytes().split(b"\0") if word
    ]
    assert _carried_fence(recorded) is None, (
        f"a submission from outside any fence was wrapped: {recorded}"
    )
    assert recorded[: len(SUBMISSION)] == SUBMISSION, recorded


@requires_bwrap
def test_e_a_fence_that_cannot_be_established_refuses_and_names_the_run(sandbox):
    shim = nested_launch.shim_directory() / "sbatch"
    sandbox["record"].unlink(missing_ok=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("RECKON_")
    }
    environment.update(
        PATH=_path(sandbox),
        TMPDIR=str(sandbox["scratch"]),
        RECKON_WORKER_FENCE="not-a-recipe",
        RECKON_RUN_ID=RUN_ID,
    )
    done = subprocess.run(
        [str(shim), *SUBMISSION],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=environment,
    )
    assert done.returncode == nested_launch.REFUSAL_STATUS, (
        done.returncode,
        done.stderr,
    )
    assert RUN_ID in done.stderr, done.stderr
    assert "could not be established" in done.stderr, done.stderr
    assert not sandbox["record"].exists(), (
        "a submission whose fence could not be established still reached the scheduler"
    )


def test_f_the_process_tree_fills_only_what_a_submitter_is_missing():
    recipe = base64.urlsafe_b64encode(
        json.dumps({"argv": ["/usr/bin/bwrap", "--", "/bin/true"]}).encode()
    ).decode()
    tree = {
        nested_launch.WORKER_FENCE_ENV: recipe,
        nested_launch.RUN_ID_ENV: RUN_ID,
        "PATH": "/inherited/bin",
    }
    filled = nested_launch.fence_environment({}, ancestry=lambda: dict(tree))
    assert filled[nested_launch.WORKER_FENCE_ENV] == recipe
    assert filled[nested_launch.RUN_ID_ENV] == RUN_ID
    own = nested_launch.fence_environment(
        {nested_launch.WORKER_FENCE_ENV: "own"}, ancestry=lambda: dict(tree)
    )
    assert own[nested_launch.WORKER_FENCE_ENV] == "own"
    assert own[nested_launch.RUN_ID_ENV] == RUN_ID
    assert nested_launch.fence_environment({}, ancestry=dict) == {}


def test_g_the_process_tree_is_read_by_the_repositorys_own_readers(monkeypatch):
    """The walk resolves to the existing owners rather than to a second parse."""
    from reckon.crew import process_signal
    from reckon.hooks import coordinator_obligations

    monkeypatch.setattr(
        process_signal, "_process_environment", lambda pid: {"probe": str(pid)}
    )
    monkeypatch.setattr(coordinator_obligations, "_parent_of", lambda pid: 4242)
    assert nested_launch._process_environment(7) == {"probe": "7"}
    assert nested_launch._parent_process(7) == 4242


def _walk(*, parents, environments, pid=10):
    """Run the ancestor walk over named parents and environments."""
    return nested_launch.ancestry_fence(
        pid=pid,
        parent_of=parents.get,
        environment_of=environments.get,
    )


def test_h_the_ancestor_walk_stops_at_the_nearest_fence():
    nearest = {nested_launch.WORKER_FENCE_ENV: "nearest"}
    farther = {nested_launch.WORKER_FENCE_ENV: "farther"}
    found = _walk(
        parents={10: 11, 11: 12, 12: 13},
        environments={11: nearest, 12: farther},
    )
    assert found[nested_launch.WORKER_FENCE_ENV] == "nearest"


def test_i_the_ancestor_walk_ends_on_a_root_a_cycle_or_the_bound():
    # A tree whose root reports pid 0: the walk ends there rather than reading
    # the kernel's pid 0 entry.
    assert (
        _walk(parents={10: 0}, environments={0: {nested_launch.WORKER_FENCE_ENV: "x"}})
        == {}
    )
    # A parent chain that loops back on itself terminates.
    assert _walk(parents={10: 11, 11: 10}, environments={11: {}}) == {}
    # An unreadable parent ends the walk.
    assert _walk(parents={}, environments={}) == {}
    # A fence beyond the bound is not reached: the walk is finite whatever the
    # chain looks like.
    depth = nested_launch.ANCESTRY_BOUND + 5
    parents = {pid: pid + 1 for pid in range(10, 10 + depth)}
    environments = {10 + depth: {nested_launch.WORKER_FENCE_ENV: "too far"}}
    assert _walk(parents=parents, environments=environments) == {}
