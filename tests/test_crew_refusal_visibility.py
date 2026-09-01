"""A dispatch refusal must answer on the channel its caller reads.

Dispatch documents a JSON answer on stdout and a distinct exit code per
refusal. The refusals that had neither were not merely terse: an orchestrator
parsing stdout saw an empty document, and one dispatch chained behind another
in a single shell command had the successor's status to hide behind. These
cover the answer, not the sentence.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew


CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "worker",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository that never received a vendored copy of the fleet script."""
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    (root / "docs" / "plans" / "dispatch-safety.html").write_text(
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="dispatch-safety">'
        '<h2 id="dispatch">Dispatch safety</h2>',
        encoding="utf-8",
    )
    (root / "package" / "target.py").write_text("value = 1\n", encoding="utf-8")
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "crew@example.invalid"],
        ["git", "config", "user.name", "crew"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "base"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    mounts = home / "mounts.json"
    mounts.write_text(json.dumps({"proj": str(root / "docs")}), encoding="utf-8")
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *a, **k: CONFIG)
    return root


def _arguments(repo: Path, *extra: str, dry_run: bool = False) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "dispatch-safety",
        "--section",
        "dispatch",
        "--node",
        "candidate",
        "--goal",
        "Resolve the candidate node",
        "--done-when",
        "pytest tests/test_target.py runs to a log with exit 0",
        "--write-path",
        "package/target.py",
        "--session",
        "refusal-session",
        "--repo",
        str(repo),
        *extra,
    ]
    if dry_run:
        arguments.append("--dry-run")
    return arguments


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_refusal_with_no_dedicated_code_still_answers_on_stdout(
    home: Path, repo: Path, dry_run: bool
) -> None:
    # A malformed node id is refused inside the resolver both paths share, so
    # it reaches the generic tail whether the call validates or launches.
    result = CliRunner().invoke(
        cli_module.main,
        _arguments(repo, "--no-watch", "--node", "bad id", dry_run=dry_run),
    )

    payload = json.loads(result.stdout.splitlines()[0])
    assert result.exit_code == 1
    assert payload["ok"] is False
    assert payload["error"] == "dispatch-refused"
    assert "node id" in payload["detail"]
    assert crew.list_live() == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_missing_fleet_script_is_refused_identically_on_both_paths(
    home: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    """A dry run whose job is to validate a call must see this precondition.

    It used to live only on the launching path, so a call could validate
    clean and then fail for real on the very thing the dry run was asked to
    prove.
    """

    def missing() -> Path:
        raise crew.CrewError("the reckon installation is missing its fleet script")

    monkeypatch.setattr(crew, "fleet_script", missing)

    result = CliRunner().invoke(
        cli_module.main, _arguments(repo, "--no-watch", dry_run=dry_run)
    )

    payload = json.loads(result.stdout.splitlines()[0])
    assert result.exit_code == 1
    assert payload["error"] == "dispatch-refused"
    assert "fleet script" in payload["detail"]


def test_an_unknown_roster_member_is_refused_with_a_readable_answer(
    home: Path, repo: Path
) -> None:
    result = CliRunner().invoke(
        cli_module.main,
        _arguments(repo, "--no-watch", "--member", "ghost"),
    )

    payload = json.loads(result.stdout.splitlines()[0])
    assert result.exit_code == 1
    assert payload["error"] == "dispatch-refused"
    assert "ghost" in payload["detail"]
    assert crew.list_live() == []


def test_a_member_already_in_flight_is_named_with_its_owning_run(
    home: Path, repo: Path
) -> None:
    runner = CliRunner()
    registered = runner.invoke(
        cli_module.main,
        [
            "crew",
            "member",
            "add",
            "--project",
            "proj",
            "--member",
            "impl-one",
            "--harness",
            "worker",
            "--checkout-path",
            str(repo),
        ],
    )
    assert registered.exit_code == 0, registered.output
    crew._write_json(
        crew.pointer_path("r-live-owner"),
        {
            "run_id": "r-live-owner",
            "project": "proj",
            "repo": str(repo.resolve()),
            "phase": "working",
            "member": "impl-one",
            "node": {
                "id": "owner-node",
                "plan": "dispatch-safety",
                "write_paths": ["package/other.py"],
            },
        },
    )

    result = runner.invoke(
        cli_module.main,
        _arguments(repo, "--no-watch", "--member", "impl-one"),
    )

    payload = json.loads(result.stdout.splitlines()[0])
    assert result.exit_code == 9
    assert payload["error"] == "member-in-flight"
    assert payload["member"] == "impl-one"
    assert payload["run_id"] == "r-live-owner"
    assert [pointer["run_id"] for pointer in crew.list_live()] == ["r-live-owner"]
