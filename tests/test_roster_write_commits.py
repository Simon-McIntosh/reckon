from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
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
    unrelated = repository / "notes.txt"
    unrelated.write_text("committed\n", encoding="utf-8")
    _git(repository, "add", "--", unrelated.name)
    _git(repository, "commit", "-q", "-m", "docs: add fixture note")
    base = _git(repository, "rev-parse", "HEAD")
    unrelated.write_text("dirty\n", encoding="utf-8")

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
    assert _git(repository, "status", "--porcelain") == "M notes.txt"
    assert _git(repository, "diff", "--cached", "--name-only") == ""
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


def test_commit_refuses_roster_with_uncommitted_registration(tmp_path: Path) -> None:
    checkout = Path(__file__).resolve().parents[1]
    checkout_status = _git(checkout, "status", "--porcelain")
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")

    ledger.register_member(
        "reckon",
        "internal-a",
        harness="codex",
        root=repository,
    )

    try:
        with pytest.raises(
            ledger.LedgerError,
            match=(
                r"cannot commit roster registration 'public-b'.*"
                r"uncommitted member registration.*internal-a"
            ),
        ):
            ledger.register_member(
                "reckon",
                "public-b",
                harness="codex",
                root=repository,
                commit=True,
            )
    finally:
        assert _git(checkout, "status", "--porcelain") == checkout_status

    assert _git(repository, "rev-parse", "HEAD") == base
    assert _git(repository, "status", "--porcelain") == "?? docs/"
    assert ledger.member("reckon", "internal-a", root=repository) is not None
    assert ledger.member("reckon", "public-b", root=repository) is None


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
