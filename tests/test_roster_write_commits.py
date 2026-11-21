from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main
from tests.helpers.git import git as _git


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "commit", "--allow-empty", "-q", "-m", "chore: seed fixture")
    return repository


def test_member_add_registers_in_the_store_and_commits_nothing(tmp_path: Path) -> None:
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
    assert _git(repository, "rev-parse", "HEAD") == base
    assert _git(repository, "rev-list", "--count", f"{base}..HEAD") == "0"
    assert not (repository / "docs").exists()

    stored = ledger.member("reckon", "worker-a", root=repository)
    assert stored is not None
    assert stored["harness"] == "codex"


def test_commit_argument_writes_no_committed_path(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")

    first = ledger.register_member(
        "reckon",
        "internal-a",
        harness="codex",
        root=repository,
    )
    second = ledger.register_member(
        "reckon",
        "public-b",
        harness="codex",
        root=repository,
        commit=True,
    )

    assert first["id"] == "internal-a"
    assert second["id"] == "public-b"
    assert _git(repository, "rev-parse", "HEAD") == base
    assert _git(repository, "status", "--porcelain") == ""
    assert not (repository / "docs").exists()
    assert ledger.member("reckon", "internal-a", root=repository) == first
    assert ledger.member("reckon", "public-b", root=repository) == second


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
    assert _git(repository, "status", "--porcelain") == ""
