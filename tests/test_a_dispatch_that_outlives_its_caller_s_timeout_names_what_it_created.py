"""A caller interrupted during launch retains the run's durable identity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

DRIVER = r"""
import importlib
import json
import os
import sys
import time
from pathlib import Path
from reckon import cli, crew_dispatch_commands as commands

root, brief, config, marker, release = map(Path, sys.argv[1:])
commands._resolved_flight = lambda *a, **k: json.loads(config.read_text())
commands._model_availability_refusal = lambda *a, **k: None
dispatch = importlib.import_module('reckon.crew.dispatch')

def slow_launch(*args, **kwargs):
    marker.write_text('launch reached')
    while not release.exists():
        time.sleep(0.01)
    return os.getpid()

dispatch._start_supervisor = slow_launch
cli.main(args=[
    'crew', 'dispatch', '--project', 'sample', '--brief', str(brief),
    '--role', 'implement', '--node', 'interrupted-launch',
    '--goal', 'record an interrupted launch identity',
    '--done-when', 'pytest verifies the recorded launch identity',
    '--write-path', 'result.json', '--time-budget', '20m',
    '--spec-level', 'exact', '--session', 'caller', '--repo', str(root),
    '--backend', 'worker', '--no-watch',
    '--unplanned-reason', 'temporary dispatch identity probe',
])
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture()
def dispatch_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills/reckon-build/scripts/worktree_fleet.py"
    (scripts / "worktree_fleet.py").write_bytes(source.read_bytes())
    (root / "docs").mkdir()
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", "skills/reckon-build/scripts/worktree_fleet.py"),
        ("commit", "-q", "-m", "chore: seed checkout", "-m", "Seed dispatch inputs."),
    ):
        _git(root, *args)
    home = tmp_path / "home"
    home.mkdir()
    (home / "mounts.json").write_text(json.dumps({"sample": str(root / "docs")}))
    brief = tmp_path / "brief.md"
    brief.write_text("Record an interrupted launch identity.\n")
    config = tmp_path / "flight.json"
    config.write_text(json.dumps(CONFIG))
    return root, home, brief, config


def _run_dispatch(
    inputs: tuple[Path, Path, Path, Path], tmp_path: Path, *, interrupt: bool
) -> tuple[dict, str, str]:
    root, home, brief, config = inputs
    marker = tmp_path / "launch-entered"
    release = tmp_path / "launch-release"
    environment = dict(os.environ, RECKON_HOME=str(home))
    for key in (
        "RECKON_SESSION_HOST_SETTLE",
        "RECKON_FOLLOWER_OWNER",
        "RECKON_FOLLOWER_IMAGE_COMMIT",
        "RECKON_FOLLOWER_SOURCE_ROOT",
    ):
        environment.pop(key, None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            DRIVER,
            *map(str, (root, brief, config, marker, release)),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while (
            not marker.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not marker.exists():
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"dispatch never reached launch: {stdout=} {stderr=}")
        pointers = list((home / "crew/live").glob("*.json"))
        assert len(pointers) == 1, "launch must hold one durable pointer"
        pointer = json.loads(pointers[0].read_text())
        assert pointer["run_id"], "the pointer is the independent identity control"
        if interrupt:
            process.kill()
        else:
            release.write_text("continue")
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == (-9 if interrupt else 0), (stdout, stderr)
        return pointer, stdout, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_interrupted_dispatch_output_names_its_existing_pointer(
    dispatch_inputs: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    pointer, stdout, stderr = _run_dispatch(dispatch_inputs, tmp_path, interrupt=True)
    assert pointer["run_id"] in stdout + stderr, (
        f"pointer exists for {pointer['run_id']}, but killed caller captured "
        f"{stdout=} {stderr=}"
    )


def test_completed_dispatch_keeps_one_json_document_on_stdout(
    dispatch_inputs: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    pointer, stdout, _stderr = _run_dispatch(dispatch_inputs, tmp_path, interrupt=False)
    assert json.loads(stdout)["run_id"] == pointer["run_id"]
