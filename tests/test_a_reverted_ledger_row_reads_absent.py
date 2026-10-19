"""A failed landing checks its own row even when another run document is damaged."""

import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion, promotion_scope
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"
RUN_ID = "reverted-row"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "pick-cache"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "tracked.txt").write_text("seed\n")
    data, version = ledger.load(PROJECT, root=root)
    ledger.write(PROJECT, data, version, root=root)
    _git(root, "add", "--", "tracked.txt", str(ledger.ledger_path(PROJECT, root)))
    _git(root, "commit", "-qm", "test: seed", "-m", "Seed the tracked ledger.")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "launch": "in-harness",
            "role": "investigate",
            "node": {"id": "receipt", "write_paths": []},
        },
    )
    return root


def test_rollback_ignores_an_unrelated_unreadable_document(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage refusal removes the new file; damage elsewhere cannot revive it."""
    run_file = ledger.run_path(PROJECT, RUN_ID, repository)
    other = ledger.run_path(PROJECT, "unrelated", repository)
    lock = repository / ".git" / "index.lock"
    lock.touch()
    real_git = promotion_scope._git
    saw_append = False

    def damage_before_stage(checkout, *args, **kwargs):
        nonlocal saw_append
        if args and args[0] == "add" and not saw_append:
            assert run_file.is_file(), "the append must precede staging"
            other.parent.mkdir(parents=True, exist_ok=True)
            other.write_text("{broken json\n")
            saw_append = True
        return real_git(checkout, *args, **kwargs)

    monkeypatch.setattr(promotion_scope, "_git", damage_before_stage)
    with pytest.raises(promotion.CrewError) as caught:
        promotion._complete_locked(RUN_ID, gate="not-run", root=repository)
    assert saw_append
    assert not run_file.exists(), "the rollback removed the row's own file"
    assert other.exists(), "the damaged document remains for the read-back"
    assert RUN_ID not in [
        row["run_id"] for row in ledger.load_aggregate(PROJECT, repository)[0]["runs"]
    ]
    with pytest.raises(ledger.LedgerError, match="unrelated"):
        ledger.load(PROJECT, root=repository, use_index=False)
    assert "re-promote once the landing failure is resolved" in str(caught.value)
    assert "do not re-promote" not in str(caught.value)

    lock.unlink()
    other.unlink()
    monkeypatch.setattr(promotion_scope, "_git", real_git)
    promotion._complete_locked(RUN_ID, gate="not-run", root=repository)
    rows = ledger.load(PROJECT, root=repository, use_index=False)[0]["runs"]
    assert [row["run_id"] for row in rows].count(RUN_ID) == 1


def test_a_committed_row_keeps_the_refusal_wording(repository: Path) -> None:
    """A later refusal must still warn when the row survives in the ledger."""
    promotion._complete_locked(RUN_ID, gate="not-run", root=repository)
    row_file = ledger.run_path(PROJECT, RUN_ID, repository)
    assert row_file.is_file()
    error = promotion_scope._landing_refusal(
        "later landing refused", {str(row_file): True}
    )
    with (
        pytest.raises(promotion.CrewError) as caught,
        promotion_scope._report_written_ledger_row(
            RUN_ID,
            ledger_path=row_file,
            row_present=lambda: promotion_scope._ledger_holds_row(
                PROJECT, repository, RUN_ID
            ),
        ),
    ):
        raise error
    assert "is already written" in str(caught.value)
    assert "do not re-promote" in str(caught.value)
