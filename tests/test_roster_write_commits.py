from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "commit", "--allow-empty", "-q", "-m", "chore: seed fixture")
    return repository


def test_member_add_commits_only_the_roster_write(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "member",
            "add",
            "--project",
            "reckon",
            "--member",
            "worker-a",
            "--harness",
            "codex",
            "--checkout-path",
            str(repository),
        ],
    )

    assert result.exit_code == 0, result.output
    assert _git(repository, "status", "--porcelain") == ""
    assert _git(repository, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert _git(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "HEAD",
    ).splitlines() == ["docs/state/reckon/crew.json"]
    assert _git(repository, "log", "-1", "--format=%s").startswith("chore(roster):")
    message = _git(repository, "log", "-1", "--format=%B")
    assert _git(repository, "log", "-1", "--format=%b")
    trailers = subprocess.run(
        ["git", "interpret-trailers", "--parse"],
        input=message,
        capture_output=True,
        text=True,
        check=True,
    )
    assert trailers.stdout == ""


def test_register_member_does_not_commit_by_default(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")

    stored = ledger.register_member(
        "reckon",
        "worker-a",
        harness="codex",
        root=repository,
    )

    assert stored["id"] == "worker-a"
    assert ledger.member("reckon", "worker-a", root=repository) == stored
    assert _git(repository, "rev-parse", "HEAD") == base
    assert _git(repository, "status", "--porcelain") == "?? docs/"
