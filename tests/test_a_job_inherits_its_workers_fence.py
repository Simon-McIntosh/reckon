"""A submitted job holds its worker's fence, or none and its reason.

The fence a dispatched worker runs under is composed once, on the host that
launches it. A scheduler task starts outside that mount namespace, so the
worker's mounts travel to the job through one channel: the recipe the fence
writes into ``RECKON_WORKER_FENCE``. Nothing recomposes a fence inside the job.
That is what keeps the run's record true — a fenced worker's job holds exactly
the roots its worker holds, and a worker launched with the fence waived (or
in-harness, or with ``--no-fence REASON``) hands no fence down, so its job runs
unfenced and the reason sits on the run's ledger row.

Two stand-ins drive that channel as a process would. Each is handed the
environment its launch produces, submits one job through the shim's own
``fenced_scheduler_task`` decision, and the job reports what it inherited.

Declared negative control: a job fence built from defaults differs from a
worker holding an extra writable grant. Under that mutation the shim replaces
the worker's inherited recipe with a bare default fence, the fenced arm's
equality assertion fails on the roots, and the waived arm still passes — which
is why the packs of roots are compared rather than "a recipe exists".
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import _worker_fence as fence
from reckon import ledger, nested_launch

PROJECT = "sample"
RUN_ID = "r-20261117T120000000000-a-job-inherits-its-workers-fence"
WAIVER_REASON = "kernel refuses unprivileged user namespaces on this host"

WORKER_STAND_IN = """
import json, os, subprocess, sys

from reckon import nested_launch

job, job_report, led = sys.argv[1:4]
mine = nested_launch.job_fence(os.environ)
submitted = nested_launch.fenced_scheduler_task(
    "srun", ["/bin/sh", job, job_report, led], os.environ
)
result = subprocess.run(submitted, capture_output=True, text=True, check=False)
print(json.dumps({
    "worker_fence": None if mine is None else mine.as_dict(),
    "submitted": submitted,
    "returncode": result.returncode,
    "stderr": result.stderr,
    "stdout": result.stdout,
}))
"""

JOB_SCRIPT = """#!/bin/sh
printf '%s' "${RECKON_WORKER_FENCE:-}" > "$1"
printf '%s' "${RECKON_RUN_ID:-}" > "$2"
"""

requires_bwrap = pytest.mark.skipif(
    shutil.which(fence.FENCE_BINARY) is None,
    reason=(
        "a user namespace is required to compose and enter the fence: "
        "bubblewrap is not installed, so a job's roots cannot be observed"
    ),
)


@pytest.fixture
def decoys(tmp_path, monkeypatch):
    """A home, a worktree, a run directory and a scratch root, sealed apart."""
    home = tmp_path / "home"
    home.mkdir()
    registry = home / ".config/reckon/mounts.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RECKON_HOME", str(registry.parent))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(registry))
    monkeypatch.delenv("UV_PYTHON_INSTALL_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "store"))
    node_tmp = tmp_path / "node-tmp"
    node_tmp.mkdir()
    monkeypatch.setattr(fence, "node_tmp_seal_root", lambda: node_tmp)
    monkeypatch.setattr(fence, "create_node_tmp_runtime_paths", list)
    worktree = home / "Code/.reckon-worktrees/own"
    run = home / ".config/reckon/crew/runs/own"
    scratch = tmp_path / "scratch/own"
    # Beneath a sealed path, because a fence grants only a root that
    # re-opens one it sealed: this is a grant beyond the defaults.
    extra = home / ".config/reckon/extra-grant"
    for path in (worktree, run, scratch, extra):
        path.mkdir(parents=True)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch.parent))
    return home, worktree, run, scratch, extra


def _composition(decoys, *, writable):
    """The argv a worker is launched with, and the environment it carries.

    The environment is read back from the composition's own ``--setenv``, which
    is the channel the fence actually uses to hand its mounts to a worker and
    from there to a job. Entering a second user namespace to reach the stand-in
    would prove nothing extra about that channel, and cannot be relied on where
    namespaces do not nest.
    """
    home, worktree, run, _scratch, _extra = decoys
    argv = fence.fence_argv(
        ["/bin/sh", "-c", "true"],
        home=home,
        worktree=worktree,
        manifest_path=run / "manifest.md",
        writable_directories=writable,
    )
    environment = {}
    if nested_launch.WORKER_FENCE_ENV in argv:
        index = argv.index(nested_launch.WORKER_FENCE_ENV)
        environment[nested_launch.WORKER_FENCE_ENV] = argv[index + 1]
    return argv, environment


def _job_script(run):
    job = run / "gate.sh"
    job.write_text(JOB_SCRIPT)
    job.chmod(0o755)
    return job


def _submit(decoys, worker_environment, job, job_report):
    """Run one worker stand-in, which submits one job and reports both fences."""
    _home, _worktree, run, _scratch, _extra = decoys
    stand_in = run / "worker-stand-in.py"
    stand_in.write_text(WORKER_STAND_IN)
    led = run / "job-ledger-row.json"
    # The worker's environment is what its own launch produced, so nothing
    # this test process happens to carry rides in: a fenced test runner
    # carries a recipe of its own, and inheriting it would make an unfenced
    # stand-in look fenced.
    environ = {
        name: value
        for name, value in os.environ.items()
        if name != nested_launch.WORKER_FENCE_ENV
    }
    environ.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        RECKON_RUN_ID=RUN_ID,
        **worker_environment,
    )
    result = subprocess.run(
        [sys.executable, str(stand_in), str(job), str(job_report), str(led)],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=environ,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    return report, led


def _job_report(path):
    """The fence the job reported, read from what it wrote beside its own run."""
    raw = path.read_text() if path.exists() else ""
    return nested_launch.job_fence({nested_launch.WORKER_FENCE_ENV: raw})


@requires_bwrap
def test_a_fenced_job_holds_exactly_its_workers_roots(decoys):
    _home, _worktree, run, scratch, extra = decoys
    worker_argv, worker_environment = _composition(decoys, writable=[scratch, extra])
    job = _job_script(run)
    job_report = run / "job-fence.txt"

    report, _led = _submit(decoys, worker_environment, job, job_report)

    worker_roots = nested_launch.fence_roots(worker_argv)
    assert report["worker_fence"] == worker_roots.as_dict(), report
    assert job_report.exists(), report
    job_roots = _job_report(job_report)
    assert job_roots is not None, "the job inherited no fence"
    # The job's roots are the worker's, mount for mount.
    assert job_roots.as_dict() == worker_roots.as_dict()
    # A root this worker holds beyond the defaults, so an empty or truncated
    # report cannot pass: the extra grant is sealed into its fence and reaches
    # the job unchanged.
    assert str(extra.resolve()) in worker_roots.writable
    assert str(extra.resolve()) in job_roots.writable


@requires_bwrap
def test_a_waived_workers_job_is_unfenced_and_its_row_carries_the_reason(
    decoys, tmp_path
):
    _home, _worktree, run, _scratch, _extra = decoys
    _argv, waived_environment = _unfenced_composition()
    assert waived_environment == {}, "a waived worker must carry no fence"
    job = _job_script(run)
    job_report = run / "job-fence.txt"

    report, led = _submit(decoys, waived_environment, job, job_report)

    # The shim forwarded the worker's argv unchanged: nothing was rebuilt.
    assert report["submitted"] == ["/bin/sh", str(job), str(job_report), str(led)]
    assert report["worker_fence"] is None
    # The job reports none, and it is the run's own job, not a stray process.
    assert _job_report(job_report) is None
    assert led.read_text() == RUN_ID

    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "sample.html").write_text("<!doctype html>\n")
    registry = Path(os.environ["RECKON_MOUNTS_PATH"])
    registry.write_text(json.dumps({PROJECT: str(root / "docs")}))
    ledger.append_run(
        PROJECT,
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "fenced": False,
            "fence_waiver": {"reason": WAIVER_REASON},
        },
        root=root,
        allow_create=True,
    )
    row = next(
        item for item in ledger.runs(PROJECT, root=root) if item["run_id"] == RUN_ID
    )
    assert row["fence_waiver"] == {"reason": WAIVER_REASON}
    assert row["run_id"] == led.read_text()


def _unfenced_composition():
    """The argv and environment of a worker the fence was waived for."""
    return [sys.executable, "-c", "true"], {}


def test_a_fence_built_from_defaults_differs_from_one_holding_an_extra_grant(decoys):
    """The equality above is decidable: two compositions that differ, differ."""
    _home, _worktree, _run, scratch, extra = decoys
    defaults, _ = _composition(decoys, writable=[scratch])
    with_extra, _ = _composition(decoys, writable=[scratch, extra])

    default_roots = nested_launch.fence_roots(defaults)
    extra_roots = nested_launch.fence_roots(with_extra)
    assert default_roots.writable != extra_roots.writable
    assert str(extra.resolve()) not in default_roots.writable
    assert str(extra.resolve()) in extra_roots.writable


def test_a_recipe_that_cannot_be_read_is_an_error_not_an_unfenced_job():
    """A fence that is present but unreadable must not report "no fence"."""
    encoded = base64.urlsafe_b64encode(b"{}").decode()
    with pytest.raises(ValueError, match="no readable fence recipe"):
        nested_launch.job_fence({nested_launch.WORKER_FENCE_ENV: encoded})
    with pytest.raises(ValueError, match="carries no readable fence recipe"):
        nested_launch.job_fence({nested_launch.WORKER_FENCE_ENV: "not base64!"})
