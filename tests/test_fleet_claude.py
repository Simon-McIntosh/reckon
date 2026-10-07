"""The fleet pane launcher caps compute-library thread pools.

``bin/fleet-claude`` is run as a subprocess with a stub ``claude`` on PATH, so
each case reads the environment the stub actually received rather than the text
the script holds. The launcher is started with a clean environment so an ambient ``OMP_NUM_THREADS`` on the machine running the suite cannot decide the result.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from reckon.crew.fleet_node import DEFAULT_THREAD_CAP, THREAD_CAP_VARIABLES

FLEET_CLAUDE = Path(__file__).resolve().parents[1] / "bin" / "fleet-claude"


def _stub_run(tmp_path: Path, preset: dict[str, str]) -> dict[str, str]:
    """Run fleet-claude with a stub ``claude``; return the env the stub saw.

    The stub records its own environment to a file, so the assertion is about
    what the launcher exported rather than about the text it holds. The
    environment is built from scratch, not copied, so a thread cap the machine
    running the suite happens to set cannot leak in.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text('#!/bin/bash\nenv > "$STUB_ENV_OUT"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    env_out = tmp_path / "stub-env.txt"
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(home),
        "SLURM_JOB_ID": "1275000",
        "STUB_ENV_OUT": str(env_out),
    }
    env.update(preset)

    done = subprocess.run(
        [str(FLEET_CLAUDE)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    # The stub wrote this name into its own environment, so reading it here
    # proves the run reached the stub rather than exiting earlier.
    assert env_out.is_file(), done.stderr
    received = dict(
        line.split("=", 1) for line in env_out.read_text().splitlines() if "=" in line
    )
    assert received.get("STUB_ENV_OUT") == str(env_out)
    return received


def test_fleet_claude_caps_every_compute_library_thread_pool(tmp_path: Path) -> None:
    received = _stub_run(tmp_path, {})

    for name in THREAD_CAP_VARIABLES:
        assert received.get(name) == str(DEFAULT_THREAD_CAP), name


def test_fleet_claude_keeps_a_thread_cap_already_set(tmp_path: Path) -> None:
    preset = {THREAD_CAP_VARIABLES[0]: "3"}
    received = _stub_run(tmp_path, preset)

    assert received.get(THREAD_CAP_VARIABLES[0]) == "3"
    for name in THREAD_CAP_VARIABLES[1:]:
        assert received.get(name) == str(DEFAULT_THREAD_CAP), name
