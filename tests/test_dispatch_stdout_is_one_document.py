"""`crew dispatch` writes exactly one JSON document to stdout.

Callers decode the dispatch payload; a second document, or any notice text,
on stdout makes `json.loads` raise "Extra data" and the caller reads nothing.
Notices belong on stderr, and only the final emission belongs on stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_dispatch_commands

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "20m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

BRIEF_TEXT = "Measure the paid lane's queue shape and record the bound.\n"

DONE_WHEN = "the gate command pytest exits 0 and prints the measured bound"


@pytest.fixture()
def brief_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )

    brief = tmp_path / "brief.md"
    brief.write_text(BRIEF_TEXT, encoding="utf-8")
    return config_home, root, brief


def _cli_arguments(
    repo: Path,
    brief: Path,
    *,
    node_id: str,
    done_when: str | None = DONE_WHEN,
    spec_level: str = "exact",
    dry_run: bool = True,
    extra: list[str] | None = None,
) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--brief",
        str(brief),
        "--role",
        "implement",
        "--node",
        node_id,
        "--goal",
        "measure the paid lane queue shape",
        "--write-path",
        "result.json",
        "--time-budget",
        "20m",
        "--session",
        f"session-{node_id}",
        "--repo",
        str(repo),
    ]
    if spec_level:
        arguments += ["--spec-level", spec_level]
    if done_when is not None:
        arguments += ["--done-when", done_when]
    arguments.append("--dry-run" if dry_run else "--no-watch")
    arguments += list(extra or [])
    return arguments


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    *,
    beneath=None,
):
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG)
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    if beneath is not None:
        # A seam on the path dispatch walks, standing in for a notice printed
        # anywhere beneath the command — including code this module cannot
        # edit. The notice must reach stderr, never the payload channel.
        monkeypatch.setattr(crew_dispatch_commands, "_model_availability_refusal", beneath)
    if "--no-watch" in arguments:
        import importlib

        dispatch_module = importlib.import_module("reckon.crew.dispatch")
        monkeypatch.setattr(
            dispatch_module,
            "_start_supervisor",
            lambda *_a, **_k: os.getpid(),
        )
    return CliRunner().invoke(cli_module.main, arguments)


def _one_document(result) -> dict:
    """Return the document stdout carries, requiring stdout to carry only it."""
    return json.loads(result.stdout)


def _beneath_notice(*_args, **_kwargs):
    """Stands in for a helper that prints a notice while dispatch runs."""
    print("notice from beneath the command")


@pytest.mark.parametrize(
    "case",
    [
        "dry-run",
        "launch",
        "laneless-refusal",
        "contract-refusal",
        "not-dispatchable",
        "request-error",
        "unknown-backend",
        "presence-check",
    ],
)
def test_every_outcome_puts_one_document_on_stdout(
    brief_repo: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Each outcome answers with one decodable document, and only that."""
    _config_home, repo, brief = brief_repo
    if case == "dry-run":
        args = _cli_arguments(repo, brief, node_id=f"case-{case}")
    elif case == "launch":
        args = _cli_arguments(repo, brief, node_id=f"case-{case}", dry_run=False)
    elif case == "laneless-refusal":
        args = _cli_arguments(
            repo, brief, node_id=f"case-{case}", extra=["--set", "nope=1"]
        )
    elif case == "contract-refusal":
        args = _cli_arguments(repo, brief, node_id=f"case-{case}")
    elif case == "not-dispatchable":
        args = _cli_arguments(repo, brief, node_id=f"case-{case}", done_when=None)
    elif case == "request-error":
        args = _cli_arguments(
            repo, brief, node_id=f"case-{case}", extra=["--set", "no.such=1"]
        )
    elif case == "unknown-backend":
        args = _cli_arguments(
            repo,
            brief,
            node_id=f"case-{case}",
            extra=["--backend", "nope"],
        )
    else:
        args = _cli_arguments(repo, brief, node_id=f"case-{case}", dry_run=False)
    beneath = _beneath_notice if case == "presence-check" else None

    result = _invoke(repo, monkeypatch, args, beneath=beneath)

    _one_document(result)
    if beneath is not None:
        # The positive control for the guard: the notice was actually printed,
        # and it landed on stderr rather than on the caller's channel.
        assert "notice from beneath the command" in result.stderr
        assert "notice from beneath the command" not in result.stdout
