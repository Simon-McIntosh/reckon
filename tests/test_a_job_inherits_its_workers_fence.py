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
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import _worker_fence as fence
from reckon import crew, ledger, nested_launch

dispatch_module = importlib.import_module("reckon.crew.dispatch")

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


PLAN_HTML = (
    "<!doctype html>\n<html><head>\n"
    f'<meta name="docs-project" content="{PROJECT}">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="plan-a">\n'
    '</head><body><h2 id="s9">\u00a79 \u2014 Job fence</h2></body></html>\n'
)

# The smallest dispatch a waived run needs: one cli backend carrying no launch
# that this test has to run, because the launcher is a stand-in.
DISPATCH_CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository, *arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _stub_launcher(plan, *, log_path, stderr_path, prompt_path):
    """Stand in for the launch: spawn nothing, return a pid."""
    return 4242


def _node(scratch):
    return crew.TaskNode(
        id="job-fence-node",
        goal="a waived worker's job is unfenced and recorded",
        plan="plan-a",
        section="\u00a79",
        done_when="tests/test_a_job_inherits_its_workers_fence.py reports 4 passed",
        write_paths=["allowed.txt"],
        time_budget="25m",
        manifest_path=str(scratch / "manifest.md"),
        spec_level="guided",
        role="implement",
    )


def _build_repository(config_home, root):
    """A synthetic repo, its mount registry and the fleet script it needs."""
    config_home.mkdir(parents=True, exist_ok=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "plan-a.html").write_text(PLAN_HTML, encoding="utf-8")
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "worktree_fleet.py").write_text(
        (
            Path(__file__).parents[1]
            / "skills"
            / "reckon-build"
            / "scripts"
            / "worktree_fleet.py"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "allowed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "allowed.txt", "docs/plans/plan-a.html", "skills"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture
def waived_run(tmp_path, monkeypatch):
    """Dispatch one run with the fence waived, as a coordinator would.

    The record the dispatch returns is the production account of that run, and
    the reason on it is what every later reader — the job, the ledger row — is
    checked against.
    """
    config_home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config_home / "mounts.json"))
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "store"))
    repository = _build_repository(config_home, tmp_path / "repo")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    record = dispatch_module.dispatch(
        node=_node(scratch),
        project=PROJECT,
        repo=repository,
        config=DISPATCH_CONFIG,
        session="job-fence-session",
        check_budget=False,
        launcher=_stub_launcher,
        no_fence_reason=WAIVER_REASON,
    )
    return record, repository, _run_dir(record)


def _run_dir(record):
    from reckon.crew.runs import run_dir

    return run_dir(record["run_id"])


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


def _submit(run, run_id, worker_environment, job, job_report):
    """Run one worker stand-in, which submits one job and reports both fences."""
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
        RECKON_RUN_ID=run_id,
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

    # The composition names the channel this reader reads: a composer that
    # spelled another name would leave the stand-in unfenced here rather than
    # fail further down as a mismatched root.
    assert nested_launch.WORKER_FENCE_ENV in worker_argv, worker_argv
    report, _led = _submit(run, RUN_ID, worker_environment, job, job_report)

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


def test_a_waived_workers_job_is_unfenced_and_its_row_carries_the_reason(
    waived_run,
):
    """Both the job and the row are read from what the dispatch path recorded.

    Nothing here appends a row or names the reason as a literal in the
    assertion: the reason is read from the pointer dispatch wrote, the row is
    written by promotion, and the run the row must belong to is the id the job
    itself reported.
    """
    record, repository, run_directory = waived_run
    job = _job_script(run_directory)
    job_report = run_directory / "job-fence.txt"

    report, led = _submit(run_directory, record["run_id"], {}, job, job_report)

    # A waived worker's launch carries no recipe, so its job holds no fence and
    # the shim forwards the submitted argv unchanged.
    assert report["worker_fence"] is None
    assert report["submitted"] == ["/bin/sh", str(job), str(job_report), str(led)]
    assert _job_report(job_report) is None
    assert led.read_text() == record["run_id"]

    # Promotion is the production writer of the ledger row, and it reads the
    # reason from the record dispatch wrote rather than from this test.
    worktree = Path(str(record["worktree"]))
    (worktree / "allowed.txt").write_text("seed\nwork\n", encoding="utf-8")
    _git(worktree, "add", "allowed.txt")
    _git(worktree, "commit", "-q", "-m", "test: worker edit")
    commit = _git(worktree, "rev-parse", "HEAD")
    crew.complete(record["run_id"], gate="passed", commits=[commit], root=repository)

    row = next(
        item
        for item in ledger.runs(PROJECT, root=repository)
        if item["run_id"] == led.read_text()
    )
    # The row carries the reason the launch recorded for the run whose worker
    # submitted this job, and it is the same job the row is read against.
    assert record["fence_waiver"]["reason"] == WAIVER_REASON
    assert row["fence_waiver"] == record["fence_waiver"]


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
