"""A process reloaded into an image launches its shims through its interpreter.

A reload image holds the package but no ``.venv`` of its own, so the host shims
a reloaded follower or producer invokes -- ``srun``, ``sbatch`` and ``salloc``,
and the worker ``git`` -- have no repository interpreter to fall back on. They
then pick a bare ``python3.12`` (or ``python3``) off ``PATH`` that cannot import
the package's dependencies, and the launch fails at once with
``ModuleNotFoundError``. The reloader now names the interpreter it runs under in
the environment it hands the replacement image, so every shim the reloaded
process launches resolves that interpreter first and never reaches the system
one. A process outside a reload image is unchanged.

The negative control drops that export, and the shim case below then reaches the
``python3.12`` stub on ``PATH`` and fails. It is logged under the run directory.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

from reckon import crew_follow_commands as follow
from reckon.crew import runs

# The mutation this file declares. It removes the interpreter the reloader
# exports, so the environment handed to the replacement image names no shim
# interpreter and the shim falls through to the system ``python3.12``.
DECLARED_MUTATION = (
    "drop the RECKON_SHIM_PYTHON export, and the shim test reaches the stub"
)

# The marker a ``python3.12`` stub prints and its exit status; the shim must
# never run it once the reloader has named the interpreter.
STUB_MARKER = "STUB-PYTHON3.12-RAN"
STUB_STATUS = 42

# A fake real scheduler tool that confirms the shim reached its main and
# forwarded: it prints the tool name it was invoked as.
_FAKE_TOOL = """#!/bin/sh
printf 'TOOL=%s\\n' "$(basename "$0")"
"""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(root: Path, value: str) -> str:
    (root / "reckon" / "example.py").write_text(f"VALUE = {value!r}\n")
    _git(root, "add", "reckon/example.py")
    _git(root, "commit", "-qm", f"Set example to {value}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def source(tmp_path: Path, monkeypatch):
    """A git checkout of the package, its reloader ready to re-exec."""
    monkeypatch.setattr(
        follow.checkout_trust,
        "_committed_tree_cache",
        lambda: tmp_path / "home" / "crew" / "reload-trees",
    )
    root = tmp_path / "source"
    package = Path(__file__).resolve().parents[1] / "reckon"
    shutil.copytree(
        package,
        root / "reckon",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _git(root, "init")
    _git(root, "config", "user.name", "Reload Test")
    _git(root, "config", "user.email", "reload@example.invalid")
    _git(root, "add", "reckon")
    _git(root, "commit", "-qm", "Seed source")
    first = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(follow, "_import_root", lambda module: root)
    monkeypatch.setattr(
        runs, "follower_code_stamp", lambda: _git(root, "rev-parse", "HEAD")
    )
    monkeypatch.setattr(runs, "FOLLOWER_FRESHNESS_SECONDS", 0)
    monkeypatch.setattr(
        import_module("reckon.crew.dispatch"),
        "_export_launched_workers_for_reexec",
        lambda: None,
    )
    # A carried image's pin is opened at construction; none applies here.
    for variable in (
        follow._FOLLOWER_SOURCE_ROOT_ENV,
        follow._FOLLOWER_IMAGE_COMMIT_ENV,
        follow._FOLLOWER_IMAGE_PIN_ENV,
        follow._SHIM_PYTHON_ENV,
    ):
        monkeypatch.delenv(variable, raising=False)
    # Record the re-exec rather than performing it, and report the image loads
    # so the reload reaches the exec rather than the import proof's slow walk.
    execs: list[tuple] = []
    monkeypatch.setattr(follow.os, "execve", lambda *args: execs.append(args))
    return root, first, execs


def _executed_root(execs) -> Path:
    launcher = execs[0][1][2]
    literal = launcher.split("sys.path.insert(0, ", 1)[1].split(");", 1)[0]
    return Path(ast.literal_eval(literal))


def _reload_into_image(reloader, monkeypatch) -> None:
    monkeypatch.setattr(reloader, "_replacement_imports", lambda: (True, ""))


def test_reexec_exports_the_running_interpreter(source, monkeypatch, capsys) -> None:
    root, first, execs = source
    reloader = follow._FollowerReloader("test-project", None)
    assert reloader.code_stamp == first
    _commit(root, "second")
    _reload_into_image(reloader, monkeypatch)

    reloader.poll({})

    assert len(execs) == 1, capsys.readouterr().out
    environment = execs[0][2]
    assert environment[follow._SHIM_PYTHON_ENV] == sys.executable

    # A caller that already named an interpreter keeps it: the reloader does not
    # overwrite a choice already made for the image it is about to launch.
    chosen = "/opt/an-interpreter/the-caller-named/bin/python"
    monkeypatch.setenv(follow._SHIM_PYTHON_ENV, chosen)
    _commit(root, "third")
    reloader.code_stamp = _git(root, "rev-parse", "HEAD~1")
    reloader.poll({})

    assert len(execs) == 2, capsys.readouterr().out
    assert execs[1][2][follow._SHIM_PYTHON_ENV] == chosen


def test_a_reload_image_launches_through_the_exported_interpreter(
    source, monkeypatch, tmp_path, capsys
) -> None:
    root, _first, execs = source
    reloader = follow._FollowerReloader("test-project", None)
    _commit(root, "second")
    _reload_into_image(reloader, monkeypatch)

    reloader.poll({})

    assert len(execs) == 1, capsys.readouterr().out
    image = _executed_root(execs)
    shim = image / "reckon" / "host_shims" / "srun"
    assert shim.is_file()
    # A reload image carries no ``.venv``; the bug is that its shim then falls
    # through to a system interpreter, so the image is where this must be run.
    assert not (image / ".venv").exists()

    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / "python3.12"
    stub.write_text(
        f'#!/bin/sh\necho "{STUB_MARKER}" >&2\nexit {STUB_STATUS}\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "srun"
    fake.write_text(_FAKE_TOOL, encoding="utf-8")
    fake.chmod(0o755)

    # The environment the reloader handed the image, with the stub's directory
    # ahead of the real tools so a shim that misses the export runs the stub.
    environment = dict(execs[0][2])
    environment["PATH"] = os.pathsep.join([str(stub_dir), str(fake_bin), os.defpath])
    # Forward rather than refuse, so the shim reaches its main and the run shows
    # which interpreter it launched through.
    environment["RECKON_ALLOW_NESTED_LAUNCH"] = "1"

    result = subprocess.run(
        [str(shim), "--version"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert STUB_MARKER not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert "TOOL=srun" in result.stdout, result.stdout
