"""Every route that reaches a worker launch runs the backend it resolved.

A dispatch on a host whose PATH omits the directory an installed backend lives
in resolves the command through the backend's declared search path — the flight
view reports it found. A launch route that then runs the bare name dies at exec
with ``No such file or directory``, so the dispatch is refused for a command
the resolution had already located.

This drives the whole dispatch command end to end, with a stub backend that
exists only in a directory on the declared search path and a PATH that omits
that directory. It reads no refusal and a run that launched the resolved
command.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, flight
from reckon.crew import resumption

BACKEND = "declared-lane"
COMMAND = "codex"
PROJECT = "stub-project"
PLAN = "fixture"
SECTION = "guard"
PATH_WITHOUT_BACKEND = "/usr/bin:/bin"


def _write_flight_layer(home: Path, bindir: Path) -> Path:
    """A layer whose only route to the backend is its declared search path."""
    layer = home / "flight.yaml"
    layer.write_text(
        "version: 1\n"
        "default_backend: " + BACKEND + "\n"
        "backends:\n"
        f"  {BACKEND}:\n"
        "    launch: cli\n"
        f"    command: {COMMAND}\n"
        "    sandbox: worktree-full\n"
        '    time_budget: "20m"\n'
        "    budget_check: true\n"
        "    environment:\n"
        f"      PATH: {PATH_WITHOUT_BACKEND}\n"
        "    search_path:\n"
        f"      - {bindir}\n",
        encoding="utf-8",
    )
    return layer


def _write_stub(directory: Path) -> Path:
    binary = directory / COMMAND
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture()
def declared_repo(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """A throwaway repository and a backend reachable only through its layer."""
    monkeypatch.setenv("PATH", PATH_WITHOUT_BACKEND)
    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        '<!doctype html><html><head><meta name="docs-project" content="stub-project">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        "</head><body>"
        '<h2 id="guard">Dispatch guard</h2>'
        '<section data-reckon="section" data-id="guard" data-effort-hours="0.5" '
        'data-capability-version="1.0" data-capability-class="general" '
        'data-capability-reasoning="standard" data-capability-verification="strict" '
        'data-capability-risk="low" data-status="implementable" data-links=""></section>'
        "<p>Run the command the backend resolved.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    (repo / "target.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills", "target.py"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    bindir = tmp_path / "backend-bin"
    bindir.mkdir()
    _write_stub(bindir)
    _write_flight_layer(home, bindir)
    return repo, bindir


def _arguments(repo: Path) -> list[str]:
    return [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        PLAN,
        "--section",
        SECTION,
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "resolved-command",
        "--goal",
        "run the backend command the dispatch resolved",
        "--done-when",
        "the run launches the resolved command",
        "--write-path",
        "target.py",
        "--session",
        "resolved-command-session",
        "--repo",
        str(repo),
        "--no-watch",
    ]


def test_a_dispatch_runs_the_backend_command_it_resolved(
    declared_repo: tuple[Path, Path],
) -> None:
    """The declared stub is found although the launching PATH omits it."""
    repo, bindir = declared_repo
    assert str(bindir) not in os.environ["PATH"].split(os.pathsep)

    result = CliRunner().invoke(cli.main, _arguments(repo))
    output = result.output
    assert "No such file or directory" not in output, output
    assert result.exit_code == 0, output

    print(output)
    run = json.loads(output.splitlines()[0])
    assert run["ok"] is True, run
    assert str(bindir / COMMAND) in run.get("argv", []), run
PROBE_REPORT = "probe-report.json"


def _write_marking_stub(directory: Path, marker: Path) -> Path:
    """A backend command that records its own execution."""
    binary = directory / COMMAND
    binary.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_a_lane_probe_runs_the_backend_command_it_resolved(
    declared_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    """The lane probe runs the resolved command, not the bare name.

    A probe is the first call a dispatch makes that runs the backend's own
    command: it is how a probe reports the lane is up. On a host whose PATH
    omits the directory the backend declares, a probe that hands the composed
    argv straight to ``subprocess.run`` execs the bare name, and the lane reads
    unavailable although the command was found.
    """
    repo, bindir = declared_repo
    marker = tmp_path / PROBE_REPORT
    _write_marking_stub(bindir, marker)
    backend = {
        "launch": "cli",
        "command": COMMAND,
        "search_path": [str(bindir)],
        "environment": {"PATH": PATH_WITHOUT_BACKEND},
    }

    observation = resumption._request_lane_availability(
        BACKEND, backend, root=repo
    )

    assert marker.exists(), observation
    assert "No such file or directory" not in str(observation.get("detail") or "")
