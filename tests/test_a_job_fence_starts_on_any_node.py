"""Job-local directory grants survive a change of node without host writes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
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

        def compose(config=None):
            return fence.fence_argv(
                ["true"],
                home=home,
                worktree=worktree,
                manifest_path=run / "manifest.md",
                writable_directories=[scratch, grant],
                read_only_binds=[(sealed, sealed)],
                config=config,
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
            "compose": compose,
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


# The status a task exits with when the fence program its recipe carries cannot
# run where the task landed. Written out rather than imported so a job that
# never started is told apart by what the job did, not by an attribute the
# product happens to export.
NOT_STARTED = 127

# A search path with no fence program on it: the program's name cannot be found
# by name there, and nothing the job runs may fall back to looking for it.
NO_SEARCH_PATH = {"PATH": ""}


def test_prepare_runs_the_carried_program_with_an_empty_search_path(rehearsal):
    case = rehearsal
    recipe = case["recipe"]
    report = case["run"] / "report.json"
    # The outer fence hides the worker's local directory, so the task's own
    # prepare step has to build the grant. That step runs with an empty search
    # path, so only a location carried in the recipe can start the fence
    # program it runs.
    outer = shutil.which(fence.FENCE_BINARY)
    assert outer, "the fence program is missing from this node"
    command = [
        outer,
        "--dev-bind",
        "/",
        "/",
        "--tmpfs",
        str(case["local"]),
        "--",
        *nested_launch._task_command(recipe, _task(case, report)),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=NO_SEARCH_PATH,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(report.read_text())
    assert receipt["empty"] and receipt["redirect_refused"], receipt
    # The product built this recipe, and the location it carries is the one the
    # composition resolved: absolute, and the program itself.
    program = recipe.get("program")
    assert program and Path(program).is_absolute(), program
    assert Path(program).name == fence.FENCE_BINARY


def test_absent_carried_program_refuses_the_task_and_names_where_it_looked(rehearsal):
    case = rehearsal
    recipe = case["recipe"]
    absent = case["run"] / "absent-bwrap"
    recipe["program"] = str(absent)
    marker = case["run"] / "command-ran"
    result = subprocess.run(
        nested_launch._task_command(recipe, ["/usr/bin/touch", str(marker)]),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=NO_SEARCH_PATH,
    )
    assert result.returncode == NOT_STARTED, result
    assert socket.gethostname() in result.stderr
    assert str(absent) in result.stderr and fence.FENCE_BINARY in result.stderr
    assert not marker.exists()


def test_recipe_without_a_carried_program_refuses_rather_than_searching(rehearsal):
    case = rehearsal
    recipe = case["recipe"]
    recipe.pop("program", None)
    marker = case["run"] / "command-ran"
    # The program is exactly where a job node would find it by name, so a
    # refusal here is the difference between searching and not searching.
    result = subprocess.run(
        nested_launch._task_command(recipe, ["/usr/bin/touch", str(marker)]),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == NOT_STARTED, result
    assert "was not searched" in result.stderr
    assert not marker.exists()


def test_program_that_exists_only_at_the_carried_location_starts_the_task(rehearsal):
    case = rehearsal
    marker = case["run"] / "command-ran"
    result = subprocess.run(
        nested_launch._task_command(case["recipe"], ["/usr/bin/touch", str(marker)]),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=NO_SEARCH_PATH,
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists()


def test_program_that_exists_but_cannot_run_refuses_before_the_task(rehearsal):
    case = rehearsal
    recipe = case["recipe"]
    inert = case["run"] / "inert-bwrap"
    inert.write_text("#!/bin/sh\nexit 0\n")
    inert.chmod(0o644)
    recipe["program"] = str(inert)
    marker = case["run"] / "command-ran"
    result = subprocess.run(
        nested_launch._task_command(recipe, ["/usr/bin/touch", str(marker)]),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=NO_SEARCH_PATH,
    )
    assert result.returncode == NOT_STARTED, result
    assert "not executable" in result.stderr and str(inert) in result.stderr
    assert not marker.exists()


def test_program_is_read_from_its_field_and_never_from_the_recipe_head(rehearsal):
    case = rehearsal
    recipe = case["recipe"]
    # The head of the carried argv is not a program name. A task that reads the
    # head as one runs an option as a program; a task that reads the field runs
    # the fence the dispatching node resolved.
    recipe["argv"][0] = "--dev-bind"
    marker = case["run"] / "command-ran"
    result = subprocess.run(
        nested_launch._task_command(recipe, ["/usr/bin/touch", str(marker)]),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=NO_SEARCH_PATH,
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists()


def _carried_program(case):
    """Copy this node's fence program into the run directory as the carried one."""
    directory = case["run"] / "carried"
    directory.mkdir()
    program = directory / "bwrap"
    found = shutil.which(fence.FENCE_BINARY)
    assert found, "the fence program is missing from this node"
    shutil.copy2(found, program)
    program.chmod(0o755)
    return program


def _carried_recipe(case, program, *, checksum=None, program_value=None):
    config = {
        "fence_program": {
            "path": str(program),
            "checksum": checksum or hashlib.sha256(program.read_bytes()).hexdigest(),
            "source": "rpm:bubblewrap-0.4.1-6.el9",
            "version": "0.4.1",
        }
    }
    argv = case["compose"](config)
    recipe = json.loads(
        base64.urlsafe_b64decode(argv[argv.index(nested_launch.WORKER_FENCE_ENV) + 1])
    )
    if program_value is not None:
        recipe["program"] = program_value
    return recipe


def _mount_index(argv, kind, path):
    pair = [kind, str(path), str(path)]
    for index in range(len(argv) - 2):
        if argv[index : index + 3] == pair:
            return index
    raise AssertionError(f"no {kind} of {path} in {argv}")


def test_recipe_carries_the_configured_location_checksum_and_provenance(rehearsal):
    case = rehearsal
    program = _carried_program(case)
    digest = hashlib.sha256(program.read_bytes()).hexdigest()
    recipe = _carried_recipe(case, program)
    assert recipe["carried_program"] == {
        "path": str(program),
        "checksum": digest,
        "source": "rpm:bubblewrap-0.4.1-6.el9",
        "version": "0.4.1",
    }
    # The carried directory is sealed as the last word on its path: the run
    # directory's writable grant must come first, or the program is writable.
    argv = recipe["argv"]
    assert _mount_index(argv, "--ro-bind", program.parent) > _mount_index(
        argv, "--bind", case["run"]
    )


def test_carried_program_without_a_checksum_refuses_composition(rehearsal):
    case = rehearsal
    program = _carried_program(case)
    with pytest.raises(fence.BackendError) as refusal:
        case["compose"]({"fence_program": {"path": str(program)}})
    assert "checksum" in str(refusal.value)


def test_matching_carried_program_starts_the_task_with_an_empty_search_path(rehearsal):
    case = rehearsal
    program = _carried_program(case)
    # The dispatching node's own location does not exist here, so the only
    # program the task can start is the carried one.
    recipe = _carried_recipe(
        case, program, program_value=str(case["run"] / "absent-bwrap")
    )
    marker = case["run"] / "command-ran"
    result = subprocess.run(
        nested_launch._task_command(recipe, ["/usr/bin/touch", str(marker)]),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=NO_SEARCH_PATH,
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists()


def test_a_carried_checksum_mismatch_refuses_before_the_task(rehearsal):
    case = rehearsal
    program = _carried_program(case)
    recorded = "0" * 64
    recipe = _carried_recipe(
        case, program, checksum=recorded, program_value=str(case["run"] / "absent-bwrap")
    )
    marker = case["run"] / "command-ran"
    result = subprocess.run(
        nested_launch._task_command(recipe, ["/usr/bin/touch", str(marker)]),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=NO_SEARCH_PATH,
    )
    assert result.returncode == NOT_STARTED, result
    assert recorded in result.stderr
    assert hashlib.sha256(program.read_bytes()).hexdigest() in result.stderr
    assert socket.gethostname() in result.stderr
    assert not marker.exists()


def test_an_absent_carried_program_refuses_before_the_task(rehearsal):
    case = rehearsal
    absent = case["run"] / "carried" / "bwrap"
    recipe = _carried_recipe(
        case, absent, checksum="1" * 64, program_value=str(case["run"] / "absent-bwrap")
    )
    marker = case["run"] / "command-ran"
    result = subprocess.run(
        nested_launch._task_command(recipe, ["/usr/bin/touch", str(marker)]),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=NO_SEARCH_PATH,
    )
    assert result.returncode == NOT_STARTED, result
    assert str(absent) in result.stderr and socket.gethostname() in result.stderr
    assert not marker.exists()


def test_the_carried_program_and_its_directory_are_not_writable(rehearsal):
    case = rehearsal
    program = _carried_program(case)
    original = program.read_bytes()
    recipe = _carried_recipe(
        case, program, program_value=str(case["run"] / "absent-bwrap")
    )
    result = subprocess.run(
        nested_launch._task_command(
            recipe,
            [
                "/bin/sh",
                "-c",
                'printf replacement > "$1" && exit 9; '
                'mkdir "$2/new" && exit 8; exit 0',
                "write-carried",
                str(program),
                str(program.parent),
            ],
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=NO_SEARCH_PATH,
    )
    assert result.returncode == 0, result
    assert "Read-only file system" in result.stderr
    assert program.read_bytes() == original
    assert not (program.parent / "new").exists()
