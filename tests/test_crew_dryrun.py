"""Hermetic coverage for dry-run visibility into live write claims."""

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
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "docs" / "plans" / "dispatch-safety.html").write_text(
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="dispatch-safety">'
        '<h2 id="dispatch">Dispatch safety</h2>',
        encoding="utf-8",
    )
    (root / "package" / "target.py").write_text("value = 1\n", encoding="utf-8")
    (root / "package" / "schema.yaml").write_text("value: 1\n", encoding="utf-8")
    (root / "package" / "generated.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "package", "skills"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture(autouse=True)
def routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)


def _node(path: str) -> crew.TaskNode:
    return crew.TaskNode(
        id="candidate",
        goal="record one dispatch readiness result",
        plan="dispatch-safety",
        section="dispatch",
        done_when="pytest reports the conflict projection with zero failures",
        write_paths=[path],
        time_budget="20m",
    )


def _arguments(repo: Path, path: str, *, dry_run: bool = True) -> list[str]:
    node = _node(path)
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        node.plan,
        "--section",
        node.section,
        "--node",
        node.id,
        "--goal",
        node.goal,
        "--done-when",
        node.done_when,
        "--write-path",
        path,
        "--session",
        "dry-run-session",
        "--repo",
        str(repo),
    ]
    if dry_run:
        arguments.append("--dry-run")
    return arguments


def _claim(repo: Path, path: str, *, run_id: str = "r-live-owner") -> dict:
    record = {
        "run_id": run_id,
        "project": "proj",
        "repo": str(repo.resolve()),
        "phase": "working",
        "node": {
            "id": "owner-node",
            "plan": "dispatch-safety",
            "write_paths": [path],
        },
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _worktrees(repo: Path) -> str:
    return subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_dry_run_reports_a_live_conflict_and_keeps_validation_successful(
    home: Path, repo: Path
) -> None:
    owner = _claim(repo, "package")
    worktrees_before = _worktrees(repo)

    result = CliRunner().invoke(cli_module.main, _arguments(repo, "package/target.py"))

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["validation"]["ok"] is True
    assert payload["live_conflicts"] == [
        {
            "candidate": "candidate",
            "run_id": owner["run_id"],
            "node": "owner-node",
            "claimed_path": "package",
            "paths": [{"left_path": "package/target.py", "right_path": "package"}],
        }
    ]
    assert crew.list_live(project="proj") == [owner]
    assert _worktrees(repo) == worktrees_before
    assert crew.live_dir().is_relative_to(home)


def test_dry_run_reports_no_conflicts_for_a_disjoint_live_claim(
    home: Path, repo: Path
) -> None:
    owner = _claim(repo, "package/generated.py")

    result = CliRunner().invoke(cli_module.main, _arguments(repo, "package/target.py"))

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["validation"]["ok"] is True
    assert payload["live_conflicts"] == []
    assert crew.list_live(project="proj") == [owner]
    assert crew.live_dir().is_relative_to(home)


def test_dry_run_reports_a_conflict_from_a_declared_derivation(
    home: Path, repo: Path
) -> None:
    project_dir = repo / "docs" / "state" / "proj"
    project_dir.mkdir(parents=True)
    (project_dir / "index.json").write_text(
        json.dumps(
            {
                "project": "proj",
                "doc": "index",
                "data": {
                    "_version": 0,
                    "projects": [
                        {
                            "name": "proj",
                            "derivations": {
                                "package/schema.yaml": ["package/generated.py"]
                            },
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    owner = _claim(repo, "package/generated.py")

    result = CliRunner().invoke(
        cli_module.main, _arguments(repo, "package/schema.yaml")
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["validation"]["ok"] is True
    assert payload["live_conflicts"] == [
        {
            "candidate": "candidate",
            "run_id": owner["run_id"],
            "node": "owner-node",
            "claimed_path": "package/generated.py",
            "paths": [
                {
                    "left_path": "package/generated.py",
                    "right_path": "package/generated.py",
                }
            ],
        }
    ]
    assert crew.live_dir().is_relative_to(home)


def test_real_dispatch_still_refuses_before_creating_a_worktree(
    home: Path, repo: Path
) -> None:
    owner = _claim(repo, "package")
    worktrees_before = _worktrees(repo)

    with pytest.raises(crew.ScopeConflict) as excinfo:
        crew.dispatch(
            node=_node("package/target.py"),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="dispatch-session",
            launcher=lambda *args, **kwargs: pytest.fail("dispatch must be refused"),
        )

    assert excinfo.value.run_id == owner["run_id"]
    assert excinfo.value.candidate_path == "package/target.py"
    assert excinfo.value.claimed_path == "package"
    assert crew.list_live(project="proj") == [owner]
    assert _worktrees(repo) == worktrees_before
    assert crew.live_dir().is_relative_to(home)
