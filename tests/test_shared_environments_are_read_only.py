"""Environment symlink targets stay sealed while a run keeps its write roots."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

import pytest

from reckon import _worker_fence as fence
from reckon import nested_launch
from reckon.crew import fleet_supervisor


@pytest.fixture
def decoys(tmp_path, monkeypatch):
    if os.environ.get("RECKON_TEST_SCHEDULER_ROOT"):
        root = Path(os.environ["RECKON_TEST_SCHEDULER_ROOT"])
        root.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(tempfile.mkdtemp(prefix="decoys-", dir=root))
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
    if os.environ.get("RECKON_TEST_UNSEALED_INSTALL_TREE") == "1":
        original = fence._named_protected_paths
        install = home / ".local/share/uv/python"
        monkeypatch.setattr(
            fence,
            "_named_protected_paths",
            lambda home=None: [path for path in original(home) if path != install],
        )
    if os.environ.get("RECKON_TEST_NO_JOB_REENTRY") == "1":
        compose = fence.fence_argv

        def without_reentry(*args, **kwargs):
            argv = compose(*args, **kwargs)
            if "RECKON_WORKER_FENCE" in argv:
                index = argv.index("RECKON_WORKER_FENCE") - 1
                del argv[index : index + 3]
            return argv

        monkeypatch.setattr(fence, "fence_argv", without_reentry)
    return home, registry, worktree, run, scratch


def _chain(decoys, *, registered):
    home, registry, worktree, _, _ = decoys
    repo = home / "elsewhere/checkout"
    environment = home / "environments/shared"
    install = home / ".local/share/uv/python"
    target = (
        home / "external/bin" if registered else install / "runtime/bin"
    ) / "python"
    repo.mkdir(parents=True)
    (repo / "docs").mkdir()
    (environment / "bin").mkdir(parents=True)
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\nprintf 'decoy-executes\\n'\n")
    target.chmod(0o755)
    (repo / ".venv").symlink_to(environment)
    (worktree / ".venv").symlink_to(environment)
    (environment / "bin/python").symlink_to("python3")
    (environment / "bin/python3").symlink_to(target)
    (environment / "package.py").write_text("sentinel\n")
    if registered:
        registry.write_text(json.dumps({"fixture": str(repo / "docs")}))
    return repo, environment, target


def _identity(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def _replay(decoys, destination):
    home, _, worktree, run, scratch = decoys
    wrapper = run / "gate.sh"
    wrapper.write_text(
        '#!/bin/sh\nlog="$3"\nprintf "replacement gate log\\n" > "$log"\n'
    )
    argv = fence.fence_argv(
        ["/bin/sh", str(wrapper), "gate", "command", str(destination)],
        worktree=worktree,
        manifest_path=run / "manifest.md",
        writable_directories=[scratch],
        home=home,
    )
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=20)


@pytest.mark.parametrize(
    "registered", [False, True], ids=["uv-install", "registered-external"]
)
def test_redirect_through_three_links_refuses_and_preserves_decoy(decoys, registered):
    _, _, worktree, run, scratch = decoys
    _, environment, target = _chain(decoys, registered=registered)
    before = _identity(target)
    result = _replay(decoys, worktree / ".venv/bin/python")
    print(
        f"redirect refusal ({registered=}): {result.returncode}: {result.stderr.strip()}"
    )
    assert result.returncode != 0, "redirect replaced the decoy"
    assert "Read-only file system" in result.stderr
    assert _identity(target) == before
    print(f"preserved size, mtime_ns, sha256: {before}")
    execution = subprocess.run(
        [str(target)], capture_output=True, text=True, check=True
    )
    assert execution.stdout == "decoy-executes\n"
    if registered:
        package = environment / "package.py"
        package_before = _identity(package)
        assert _replay(decoys, package).returncode != 0
        assert _identity(package) == package_before
    for destination in (
        run / "fresh.log",
        worktree / "result.log",
        scratch / "result.log",
    ):
        written = _replay(decoys, destination)
        assert written.returncode == 0, written.stderr
        assert destination.read_text() == "replacement gate log\n"


def test_install_tree_removed_from_sealed_roots_replaces_decoy(decoys, monkeypatch):
    home, _, worktree, _, _ = decoys
    _, _, target = _chain(decoys, registered=False)
    before = _identity(target)
    original = fence._named_protected_paths
    install = home / ".local/share/uv/python"
    monkeypatch.setattr(
        fence,
        "_named_protected_paths",
        lambda home=None: [path for path in original(home) if path != install],
    )
    result = _replay(decoys, worktree / ".venv/bin/python")
    assert result.returncode == 0, result.stderr
    assert _identity(target) != before
    assert target.read_text() == "replacement gate log\n"
    print(f"unsealed decoy changed: {before} -> {_identity(target)}")


def test_environment_roots_follow_the_shared_resolver(decoys, monkeypatch):
    _, _, _, _, _ = decoys
    repo, environment, target = _chain(decoys, registered=True)
    alternate = environment.parent / "alternate"
    (alternate / "bin").mkdir(parents=True)
    (alternate / "bin/python").symlink_to(target)
    calls = []

    def resolve(repository):
        calls.append(repository)
        return alternate

    monkeypatch.setattr(fleet_supervisor, "resolved_environment", resolve)
    protected = fence.declared_protected_paths(decoys[0])
    assert alternate in protected
    assert target in protected
    assert calls and set(calls) == {repo}


@pytest.mark.parametrize("setting", ["UV_PYTHON_INSTALL_DIR", "XDG_DATA_HOME"])
def test_configured_install_tree_is_sealed(decoys, monkeypatch, setting):
    home, _, _, _, _ = decoys
    configured = home / "configured-install"
    monkeypatch.setenv(setting, str(configured))
    install = (
        configured if setting == "UV_PYTHON_INSTALL_DIR" else configured / "uv/python"
    )
    target = install / "bin/python"
    target.parent.mkdir(parents=True)
    target.write_text("decoy\n")
    before = _identity(target)
    result = _replay(decoys, target)
    assert result.returncode != 0, result.stderr
    assert "Read-only file system" in result.stderr
    assert _identity(target) == before


def test_registered_environment_retargeting_is_seen_on_next_composition(decoys):
    repo, environment, _ = _chain(decoys, registered=True)
    first = fence.declared_protected_paths(decoys[0])
    replacement = environment.parent / "replacement"
    replacement.mkdir()
    (repo / ".venv").unlink()
    (repo / ".venv").symlink_to(replacement)
    second = fence.declared_protected_paths(decoys[0])
    assert environment in first
    assert replacement in second
    assert environment not in second


def test_unreadable_registry_refuses_instead_of_omitting_environments(decoys):
    from reckon.flight import FlightConfigError

    _, registry, _, _, _ = decoys
    registry.write_text("{")
    with pytest.raises(FlightConfigError, match="readable JSON"):
        fence.fence_argv(["true"], home=decoys[0])


@pytest.mark.skipif(
    not os.environ.get("RECKON_TEST_SCHEDULER_ROOT"),
    reason="requires dedicated all_debug allocations and durable receipt directory",
)
@pytest.mark.parametrize("tool", ["sbatch", "sbatch-script", "sbatch-stdin", "srun"])
def test_scheduler_task_reenters_fence_and_preserves_decoy(decoys, tool):
    _scheduler_replay(decoys, tool, absolute=False)


@pytest.mark.skipif(
    not os.environ.get("RECKON_TEST_SCHEDULER_ROOT"),
    reason="requires dedicated all_debug allocation and durable receipt directory",
)
def test_absolute_scheduler_route_measures_the_uncovered_limit(decoys):
    _scheduler_replay(decoys, "sbatch", absolute=True)


def _scheduler_replay(decoys, tool, *, absolute):
    home, _, worktree, run, scratch = decoys
    tool, _, form = tool.partition("-")
    _, _, target = _chain(decoys, registered=False)
    before = _identity(target)
    wrapper = run / "gate.sh"
    wrapper.write_text(
        '#!/bin/sh\n#SBATCH --job-name=fence-decoy\nprintf "TASK_JOB=%s TASK_STEP=%s\\n" '
        '"$SLURM_JOB_ID" "${SLURM_STEP_ID:-batch}"\n'
        'log="$3"\nprintf "replacement gate log\\n" > "$log"\n'
    )
    task = [
        "/bin/sh",
        str(wrapper),
        "gate",
        "command",
        str(worktree / ".venv/bin/python"),
    ]
    shim = Path(__file__).resolve().parents[1] / "reckon/host_shims" / tool
    executable = str(Path("/usr/bin") / tool) if absolute else str(shim)
    output = run / "scheduler-output.log"
    if tool == "sbatch":
        command = [
            executable,
            "--partition=all_debug",
            "--time=00:03:00",
            "--cpus-per-task=1",
            "--mem=1G",
            "--wait",
            "--parsable",
            f"--output={output}",
            "--wrap=" + shlex.join(task),
        ]
        if form:
            command.pop()
            command += [
                "--export=NONE",
                "-" if form == "stdin" else str(wrapper),
                *task[2:],
            ]
    else:
        command = [
            executable,
            "--ntasks=1",
            "--cpus-per-task=1",
            "--cpu-bind=none",
            f"--output={output}",
            *task,
        ]
    # A batch submission starts a separate allocation; the step deliberately
    # stays in this test's own all_debug allocation, never the fleet job.
    shell = "unset SLURM_JOB_ID SLURM_JOBID SLURM_STEP_ID; " if tool == "sbatch" else ""
    shell += "exec " + shlex.join(command)
    argv = fence.fence_argv(
        ["/bin/sh", "-c", shell],
        home=home,
        worktree=worktree,
        manifest_path=run / "manifest.md",
        writable_directories=[scratch],
    )
    env = dict(os.environ)
    env["PATH"] = f"{shim.parent}:/usr/bin:/bin"
    env["RECKON_SHIM_PYTHON"] = "/home/ITER/mcintos/Code/reckon/.venv/bin/python"
    env["RECKON_ALLOW_NESTED_LAUNCH"] = "1"
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
        env=env,
        input=wrapper.read_text() if form == "stdin" else None,
    )
    receipt = {
        "tool": tool,
        "form": form,
        "absolute": absolute,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "output_path": str(output),
        "output": output.read_text() if output.exists() else "",
        "before": before,
        "after": _identity(target),
        "target_bytes": target.read_text(),
    }
    (run / "receipt.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, sort_keys=True))
    assert output.exists(), receipt
    assert "TASK_JOB=" in receipt["output"], receipt
    if absolute:
        assert result.returncode == 0, receipt
        assert _identity(target) != before
        assert target.read_text() == "replacement gate log\n"
    else:
        assert result.returncode != 0, "scheduler task replaced the decoy"
        assert "Read-only file system" in receipt["output"], receipt
        assert _identity(target) == before


def _recipe(decoys):
    home, _, worktree, run, scratch = decoys
    argv = fence.fence_argv(
        ["true"],
        home=home,
        worktree=worktree,
        manifest_path=run / "manifest.md",
        writable_directories=[scratch],
    )
    return json.loads(
        base64.urlsafe_b64decode(argv[argv.index("RECKON_WORKER_FENCE") + 1])
    )


def test_task_recipe_keeps_seals_grants_stdin_and_caller_python_environment(decoys):
    _, _, worktree, run, scratch = decoys
    _, _, target = _chain(decoys, registered=False)
    before = _identity(target)
    recipe = _recipe(decoys)
    script = run / "task.sh"
    script.write_text(
        '#!/bin/sh\nread value\nprintf "%s|%s" "$value" "$PYTHONHOME" > "$1"\n'
        'printf "replacement gate log\\n" > "$2"\n'
    )
    argv = nested_launch._task_command(
        recipe,
        [
            "/bin/sh",
            str(script),
            str(run / "fresh.log"),
            str(worktree / ".venv/bin/python"),
        ],
    )
    environment = dict(os.environ, PYTHONHOME="/nonexistent-decoy-python-home")
    environment.pop("RECKON_WORKER_FENCE", None)
    result = subprocess.run(
        argv,
        input="stdin-marker\n",
        text=True,
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert "Read-only file system" in result.stderr
    assert _identity(target) == before
    assert (
        run / "fresh.log"
    ).read_text() == "stdin-marker|/nonexistent-decoy-python-home"
    assert str(scratch) in recipe["directories"]


@pytest.mark.parametrize(
    "tool,options",
    [
        ("srun", ["--output", "--partition=decoy", "-n1", "--cpu-bind=none"]),
        ("srun", ["--wait=30", "--pty=0", "--compress", "-K1", "-I", "-E"]),
        ("sbatch", ["--export=NONE", "-vvpall_debug", "--wrap", "printf payload"]),
    ],
)
def test_task_boundary_keeps_option_values_and_resources(decoys, tool, options):
    recipe = _recipe(decoys)
    environment = {
        "RECKON_WORKER_FENCE": base64.urlsafe_b64encode(
            json.dumps(recipe).encode()
        ).decode()
    }
    original = [*options, "printf", "payload"] if tool == "srun" else options
    wrapped = nested_launch.fenced_scheduler_task(tool, original, environment)
    if tool == "srun":
        assert wrapped[: len(options)] == options
        task = wrapped[len(options) :]
    else:
        assert wrapped[:2] == options[:2]
        task = shlex.split(wrapped[-1].removeprefix("--wrap="))
    spec = json.loads(base64.urlsafe_b64decode(task[-1]))
    assert spec["fence"] == recipe
    assert spec["command"] == (
        ["printf", "payload"] if tool == "srun" else ["/bin/sh", "-c", "printf payload"]
    )


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("srun", ["--unknown-boundary", "printf", "payload"]),
        ("srun", ["--multi-prog", "commands.conf"]),
        ("srun", ["--task-prolog=/bin/true", "printf", "payload"]),
        ("sbatch", ["--wrap=printf payload", "script.sh"]),
        ("sbatch", ["--output"]),
    ],
)
def test_unknown_or_unfenced_task_forms_refuse_before_forwarding(
    decoys, tool, arguments
):
    recipe = _recipe(decoys)
    environment = {
        "RECKON_WORKER_FENCE": base64.urlsafe_b64encode(
            json.dumps(recipe).encode()
        ).decode()
    }
    with pytest.raises(ValueError, match=r"unknown|needs a value|cannot"):
        nested_launch.fenced_scheduler_task(tool, arguments, environment)
