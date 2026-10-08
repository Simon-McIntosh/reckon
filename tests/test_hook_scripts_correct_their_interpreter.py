"""Bare hook commands recover from an old Python without losing their input."""

from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "reckon" / "hooks"
OLD_PYTHON = Path("/usr/bin/python3")


def _run(python: Path, script: Path, payload: dict, env: dict, *args: str):
    return subprocess.run(
        [str(python), str(script), *args],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=15,
    )


def _old_python_on_path(tmp_path: Path) -> tuple[Path, dict]:
    assert OLD_PYTHON.exists()
    version = subprocess.check_output(
        [str(OLD_PYTHON), "-c", "import sys; print(*sys.version_info[:2])"], text=True
    )
    assert tuple(map(int, version.split())) < (3, 12)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(OLD_PYTHON)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["RECKON_HOME"] = str(tmp_path / "config")
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        "import os, sys\n"
        "with open(os.environ['RECKON_INTERPRETER_TRACE'], 'a') as log: "
        "log.write(f'{sys.version_info.major}.{sys.version_info.minor}\\n')\n"
    )
    env["PYTHONPATH"] = f"{site}{os.pathsep}{HOOKS.parents[1]}"
    env["RECKON_INTERPRETER_TRACE"] = str(tmp_path / "interpreter-starts.log")
    env.pop("RECKON_MANIFEST", None)
    env.pop("RECKON_RUN_ID", None)
    env.pop("RECKON_HOOK_REEXEC_ATTEMPT", None)
    return bin_dir / "python3", env


def _reckon_importing_hooks() -> list[Path]:
    scripts = []
    for script in HOOKS.glob("*.py"):
        if not script.read_text().startswith("#!/usr/bin/env python3"):
            continue
        syntax = ast.parse(script.read_text())
        imports_reckon = any(
            (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("reckon")
            )
            or (
                isinstance(node, ast.Import)
                and any(alias.name.startswith("reckon") for alias in node.names)
            )
            for node in ast.walk(syntax)
        )
        if imports_reckon:
            scripts.append(script)
    return sorted(scripts)


def test_every_reckon_importing_hook_uses_the_shared_bootstrap():
    scripts = _reckon_importing_hooks()
    assert {script.name for script in scripts} == {
        "coordinator_obligations.py",
        "worker_git_guard.py",
        "worker_stop.py",
    }
    for script in scripts:
        source = script.read_text()
        assert 'with_name("interpreter_bootstrap.py")' in source
        assert "_bootstrap.ensure_interpreter(__file__)" in source


def test_importing_hooks_reexecute_under_old_python(tmp_path: Path):
    scripts = _reckon_importing_hooks()
    old_python, env = _old_python_on_path(tmp_path)
    manifest = tmp_path / "missing-manifest.md"
    env["RECKON_MANIFEST"] = str(manifest)
    live_dir = Path(env["RECKON_HOME"]) / "crew" / "live"
    live_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (live_dir / "synthetic-run.json").write_text(
        json.dumps({"run_id": "synthetic-run", "worktree": str(worktree)})
    )
    cases = {
        "coordinator_obligations.py": ({"cwd": str(tmp_path)}, ("--hook", "prompt")),
        "worker_stop.py": ({"cwd": str(tmp_path)}, ()),
        "worker_git_guard.py": (
            {
                "tool_name": "Bash",
                "cwd": str(worktree),
                "tool_input": {"command": f"git -C {tmp_path / 'other'} reset --hard"},
            },
            (),
        ),
    }
    for script in scripts:
        payload, args = cases[script.name]
        case_env = env.copy()
        if script.name == "worker_git_guard.py":
            case_env["RECKON_RUN_ID"] = "synthetic-run"
        expected = _run(Path(sys.executable), script, payload, case_env, *args)
        if script.name == "worker_stop.py":
            (manifest.parent / ".worker_stop_blocks").unlink(missing_ok=True)
        Path(env["RECKON_INTERPRETER_TRACE"]).unlink()
        actual = _run(old_python, script, payload, case_env, *args)
        starts = Path(env["RECKON_INTERPRETER_TRACE"]).read_text().splitlines()
        assert tuple(map(int, starts[0].split("."))) < (3, 12)
        assert starts[1] == f"{sys.version_info.major}.{sys.version_info.minor}"
        assert (actual.returncode, actual.stdout, actual.stderr) == (
            expected.returncode,
            expected.stdout,
            expected.stderr,
        ), script.name
        if script.name == "worker_stop.py":
            assert json.loads(actual.stdout)["decision"] == "block"
        if script.name == "worker_git_guard.py":
            assert (
                json.loads(actual.stdout)["hookSpecificOutput"]["permissionDecision"]
                == "deny"
            )


def test_stdlib_guards_keep_their_decisions_under_old_python(tmp_path: Path):
    old_python, env = _old_python_on_path(tmp_path)
    env["RECKON_RUN_ID"] = "synthetic-run"
    live_dir = Path(env["RECKON_HOME"]) / "crew" / "live"
    live_dir.mkdir(parents=True)
    (live_dir / "recipient-run.json").write_text(
        json.dumps({"run_id": "recipient-run", "launcher_host": socket.gethostname()})
    )
    cases = {
        "native_agent_guard.py": {
            "tool_name": "Agent",
            "tool_input": {"prompt": "hello"},
        },
        "worker_message_guard.py": {
            "tool_name": "SendMessage",
            "tool_input": {"to": "recipient-run"},
        },
    }
    for name, payload in cases.items():
        script = HOOKS / name
        expected = _run(Path(sys.executable), script, payload, env)
        actual = _run(old_python, script, payload, env)
        assert (actual.returncode, actual.stdout, actual.stderr) == (
            expected.returncode,
            expected.stdout,
            expected.stderr,
        )
        assert (
            json.loads(actual.stdout)["hookSpecificOutput"]["permissionDecision"]
            == "deny"
        )


@pytest.mark.parametrize(
    "name", ["worker_stop.py", "coordinator_obligations.py", "worker_git_guard.py"]
)
def test_missing_checkout_interpreter_reports_without_looping(
    tmp_path: Path, name: str
):
    old_python, env = _old_python_on_path(tmp_path)
    checkout = tmp_path / "checkout"
    hook_dir = checkout / "reckon" / "hooks"
    hook_dir.mkdir(parents=True)
    for filename in (name, "interpreter_bootstrap.py"):
        (hook_dir / filename).write_bytes((HOOKS / filename).read_bytes())
    payload = {"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}}
    result = _run(old_python, hook_dir / name, payload, env)
    assert "checkout interpreter is missing" in (result.stdout + result.stderr)
    if name == "worker_git_guard.py":
        assert result.returncode == 0
        assert (
            json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
            == "deny"
        )
    else:
        assert result.returncode != 0
