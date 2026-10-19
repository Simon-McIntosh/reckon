"""A composed launch names the interpreter of the process that composed it.

A worker's shims on the composed ``PATH`` start an interpreter that can import
the package. A launch composed by a process running out of a reload image, or
any tree without a ``.venv`` of its own, has none to fall back on: its shims
then pick a bare ``python3.12`` off ``PATH`` that cannot import the package's
dependencies and the launch fails at once with ``ModuleNotFoundError``. The
dispatcher now names its own interpreter in the environment it persists for a
launch, beside the ``PATH`` its shims lead, so a shim resolves that interpreter
first however the dispatcher was started or reloaded. A caller that already
named one keeps it.

The negative control removes that setting from the composed environment, and
the shim case below then reaches the ``python3.12`` stub on ``PATH`` and fails.
It is logged under the run directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from reckon.crew import dispatch_launch
from reckon.host import HostFacts

# The mutation this file declares. It removes the interpreter the composer names
# for a launch, so the persisted environment carries no RECKON_SHIM_PYTHON and
# the shim falls through to the system ``python3.12`` on PATH.
DECLARED_MUTATION = (
    "remove the RECKON_SHIM_PYTHON setting from the composed environment; "
    "the shim test then reaches the stub"
)

# The variable the composer writes and the shims read, named literally so the
# expectation is not satisfied by the mutation renaming the module constant.
SHIM_PYTHON_ENV = "RECKON_SHIM_PYTHON"

# Set in the environment to apply the declared mutation: the composer then
# writes the setting under a name no shim reads, which is the setting removed.
NEGATIVE_CONTROL = os.environ.get("RECKON_SHIM_PYTHON_NEGATIVE_CONTROL", "").strip()
_CONTROL_VARIABLE = "RECKON_SHIM_PYTHON_DISABLED_BY_THE_NEGATIVE_CONTROL"

# The marker a ``python3.12`` stub prints and its exit status; the shim must
# never run it once the composer has named the interpreter.
STUB_MARKER = "STUB-PYTHON3.12-RAN"
STUB_STATUS = 42

# A fake real scheduler tool that confirms the shim reached its main and
# forwarded: it prints the tool name it was invoked as.
_FAKE_TOOL = """#!/bin/sh
printf 'TOOL=%s\\n' "$(basename "$0")"
"""


def _facts(*, inside: bool) -> HostFacts:
    """A fixture host so a case never reads the process's real cgroup."""
    return HostFacts(
        in_allocation=inside,
        job_id="1277272" if inside else None,
        step_id="batch" if inside else None,
        node="98dci4-clu-2058" if inside else None,
        tmp_filesystem="xfs",
        home_filesystem="gpfs",
        tmp_is_node_local=True,
        reason="" if inside else "no-slurm-job-id-in-the-environment",
        sources={},
    )


def _apply_negative_control(monkeypatch) -> None:
    """Apply the declared mutation: name no interpreter a shim will read."""
    if NEGATIVE_CONTROL:
        monkeypatch.setattr(dispatch_launch, "_SHIM_PYTHON_ENV", _CONTROL_VARIABLE)


def _stub_directory(tmp_path: Path) -> Path:
    """A directory whose ``python3.12`` prints a marker and exits non-zero."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / "python3.12"
    stub.write_text(
        f'#!/bin/sh\necho "{STUB_MARKER}" >&2\nexit {STUB_STATUS}\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub_dir


def _fake_tools_directory(tmp_path: Path) -> Path:
    """A directory holding a fake real ``srun`` that prints the tool it ran as."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "srun"
    fake.write_text(_FAKE_TOOL, encoding="utf-8")
    fake.chmod(0o755)
    return fake_bin


def _package_without_an_environment(tmp_path: Path) -> Path:
    """A checkout of the package carrying no ``.venv`` of its own.

    A reload image and a dispatcher running from a bare tree both have this
    shape: the package is importable from ``PYTHONPATH`` alone, so the shims
    find no ``$repo_root/.venv/bin/python`` to fall back on.
    """
    package = Path(__file__).resolve().parents[1] / "reckon"
    source = tmp_path / "source"
    shutil.copytree(
        package,
        source / "reckon",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    assert not (source / ".venv").exists()
    return source


def test_a_composed_launch_environment_names_the_composing_interpreter(
    monkeypatch,
) -> None:
    _apply_negative_control(monkeypatch)
    facts = _facts(inside=True)

    composed = dispatch_launch._persisted_worker_environment({}, facts=facts)

    assert composed[SHIM_PYTHON_ENV] == sys.executable

    # A caller that already named an interpreter keeps it: the composer does not
    # overwrite a choice already made for the launch.
    chosen = "/opt/an-interpreter/the-caller-named/bin/python"
    kept = dispatch_launch._persisted_worker_environment(
        {SHIM_PYTHON_ENV: chosen}, facts=facts
    )
    assert kept[SHIM_PYTHON_ENV] == chosen


def test_a_composed_launch_shim_reaches_the_composing_interpreter(
    monkeypatch, tmp_path: Path
) -> None:
    _apply_negative_control(monkeypatch)
    facts = _facts(inside=True)

    source = _package_without_an_environment(tmp_path)
    shim = source / "reckon" / "host_shims" / "srun"
    assert shim.is_file()

    stub_dir = _stub_directory(tmp_path)
    fake_bin = _fake_tools_directory(tmp_path)

    # The environment the composer persists for the launch, with the stub's
    # directory ahead of the real tools so a shim that misses the setting runs
    # the stub instead.
    environment = dict(dispatch_launch._persisted_worker_environment({}, facts=facts))
    environment["PATH"] = os.pathsep.join(
        [str(stub_dir), str(fake_bin), environment.get("PATH", os.defpath)]
    )
    environment.setdefault("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
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
