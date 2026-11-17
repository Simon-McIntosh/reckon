"""Job-local directory grants survive a change of node without host writes."""

from __future__ import annotations

import base64
import json
import os
import shlex
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from reckon import _worker_fence as fence
from reckon import nested_launch
from tests.test_shared_environments_are_read_only import _identity

TASK = r"""
import json, os, pathlib, socket, subprocess, sys
grant, sealed, sibling, decoy, report = map(pathlib.Path, sys.argv[1:])
assert grant.is_dir() and not list(grant.iterdir()), str(grant)
(grant / 'job-marker').write_text('private')
assert not sealed.exists(), str(sealed)
for path in (sealed, sibling):
    try:
        path.mkdir()
    except OSError:
        pass
    else:
        raise AssertionError('ungranted path became writable: ' + str(path))
result = subprocess.run(['/bin/sh', '-c', 'log="$3"; printf replacement > "$log"',
                         'gate', 'command', 'unused', str(decoy)],
                        capture_output=True, text=True)
assert result.returncode != 0, result
assert 'Read-only file system' in result.stderr, result.stderr
report.write_text(json.dumps({'node': socket.gethostname(),
    'job': os.environ.get('SLURM_JOB_ID'), 'grant': str(grant),
    'empty': True, 'redirect_refused': True, 'stderr': result.stderr}))
"""


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    print("measurement_module: " + nested_launch.__file__)
    print("measurement_cwd: " + str(Path.cwd().resolve()))
    durable = os.environ.get("RECKON_JOB_FENCE_REHEARSAL_ROOT")
    if durable:
        root = Path(durable)
        root.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(tempfile.mkdtemp(prefix="rehearsal-", dir=root))
    home = tmp_path / "home"
    worktree = home / "worktree"
    run = home / "run"
    scratch = home / "scratch/own"
    for path in (worktree, run, scratch):
        path.mkdir(parents=True)
    with tempfile.TemporaryDirectory(
        prefix="job-fence-", dir=os.environ["TMPDIR"]
    ) as raw:
        local = Path(raw)
        grant = local / "grants/own"
        sealed = local / "sealed"
        sibling = local / "grants/sibling"
        for path in (grant, sealed):
            path.mkdir(parents=True)
        (grant / "worker-marker").write_text("worker's node only")
        decoy = home / "install/python"
        decoy.parent.mkdir()
        decoy.write_text("#!/bin/sh\nprintf decoy\n")
        decoy.chmod(0o755)
        chain = worktree / ".venv/bin"
        chain.mkdir(parents=True)
        (chain / "python").symlink_to(decoy)
        monkeypatch.setattr(
            fence, "declared_protected_paths", lambda *args: [home, local, sealed]
        )
        monkeypatch.setattr(fence, "shared_store_seal_roots", list)
        monkeypatch.setattr(fence, "protected_checkouts", lambda *args: [])
        monkeypatch.setattr(fence, "node_tmp_seal_root", lambda: None)
        monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch.parent))
        argv = fence.fence_argv(
            ["true"],
            home=home,
            worktree=worktree,
            manifest_path=run / "manifest.md",
            writable_directories=[scratch, grant],
            read_only_binds=[(sealed, sealed)],
        )
        encoded = argv[argv.index(nested_launch.WORKER_FENCE_ENV) + 1]
        yield {
            "root": tmp_path,
            "local": local,
            "grant": grant,
            "sealed": sealed,
            "sibling": sibling,
            "decoy": decoy,
            "chain": chain / "python",
            "run": run,
            "argv": argv,
            "recipe": json.loads(base64.urlsafe_b64decode(encoded)),
        }


def _task(case, report):
    return [
        os.sys.executable,
        "-I",
        "-c",
        TASK,
        *map(
            str, (case["grant"], case["sealed"], case["sibling"], case["chain"], report)
        ),
    ]


def test_missing_directory_grant_is_private_and_missing_seal_stays_absent(rehearsal):
    case = rehearsal
    before = _identity(case["decoy"])
    report = case["run"] / "report.json"
    # A private empty overlay stands in for a node without the worker's local
    # directory, while the worker's marker is a positive presence control.
    command = [
        "bwrap",
        "--dev-bind",
        "/",
        "/",
        "--tmpfs",
        str(case["local"]),
        "--",
        *nested_launch._task_command(case["recipe"], _task(case, report)),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(report.read_text())["redirect_refused"]
    assert _identity(case["decoy"]) == before
    assert (case["grant"] / "worker-marker").read_text() == "worker's node only"
    assert not (case["grant"] / "job-marker").exists()


@pytest.mark.skipif(
    not os.environ.get("RECKON_JOB_FENCE_REHEARSAL_ROOT"),
    reason="dedicated scheduler rehearsal needs a shared durable receipt root",
)
@pytest.mark.parametrize("tool", ["sbatch", "srun"])
def test_scheduler_starts_with_absent_local_grant_and_refuses_redirect(rehearsal, tool):
    case = rehearsal
    before = _identity(case["decoy"])
    report = case["run"] / "report.json"
    output = case["run"] / "scheduler.log"
    shim = Path(nested_launch.__file__).parent / "host_shims" / tool
    options = [
        str(shim),
        "--partition=all_debug",
        "--time=00:03:00",
        "--cpus-per-task=1",
        "--mem=1G",
        "--ntasks=1",
        f"--exclude={socket.gethostname().split('.')[0]}",
        f"--chdir={case['run']}",
        f"--output={output}",
    ]
    if tool == "sbatch":
        options += ["--wait", "--parsable", "--wrap=" + shlex.join(_task(case, report))]
    else:
        options += ["--cpu-bind=none", *_task(case, report)]
    argv = [*case["argv"][: case["argv"].index("--")], "--", *options]
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("SLURM_")
    }
    environment["PATH"] = "/usr/bin:/bin"
    environment["RECKON_ALLOW_NESTED_LAUNCH"] = "1"
    environment["RECKON_SHIM_PYTHON"] = os.sys.executable
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=300, env=environment, check=False
    )
    receipt = {
        "tool": tool,
        "worker_node": socket.gethostname(),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "output": output.read_text() if output.exists() else "",
        "task": json.loads(report.read_text()) if report.exists() else None,
    }
    # Query the same foreign node after the task: its namespace has ended, and
    # neither the grant nor the scaffolding may remain on the host filesystem.
    if receipt["task"]:
        probe = subprocess.run(
            [
                "/usr/bin/sbatch",
                "--partition=all_debug",
                "--time=00:01:00",
                "--mem=1G",
                "--ntasks=1",
                "--wait",
                "--parsable",
                f"--nodelist={receipt['task']['node'].split('.')[0]}",
                f"--chdir={case['run']}",
                f"--output={case['run'] / 'cleanup.log'}",
                "--wrap=test ! -e " + shlex.quote(str(case["local"])),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=environment,
            check=False,
        )
        receipt["cleanup"] = {
            "returncode": probe.returncode,
            "stdout": probe.stdout,
            "stderr": probe.stderr,
        }
    (case["root"] / f"{tool}-receipt.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt))
    assert result.returncode == 0, receipt
    assert receipt["task"]["node"] != socket.gethostname(), receipt
    assert receipt["task"]["empty"] and receipt["task"]["redirect_refused"], receipt
    assert receipt["cleanup"]["returncode"] == 0, receipt
    assert _identity(case["decoy"]) == before


@pytest.mark.parametrize("option", ["--bind", "--ro-bind"])
def test_missing_file_source_refuses_before_command(rehearsal, option):
    case = rehearsal
    source = case["run"] / "absent-credential"
    destination = case["run"] / "credential"
    recipe = case["recipe"]
    recipe["argv"].extend([option, str(source), str(destination)])
    marker = case["run"] / "command-ran"
    result = subprocess.run(
        nested_launch._task_command(recipe, ["/usr/bin/touch", str(marker)]),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert str(source) in result.stderr
    assert not source.exists() and not destination.exists() and not marker.exists()


def test_missing_seal_alone_is_not_materialized(rehearsal):
    case = rehearsal
    case["sealed"].rmdir()
    command = [
        "/bin/sh",
        "-c",
        'test ! -e "$1" && ! mkdir "$1" && test -f "$2/worker-marker"',
        "check-seal",
        str(case["sealed"]),
        str(case["grant"]),
    ]
    result = subprocess.run(
        nested_launch._task_command(case["recipe"], command),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Read-only file system" in result.stderr
    assert not case["sealed"].exists()
