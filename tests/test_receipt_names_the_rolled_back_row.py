"""A failed landing's receipt says which of the two states the ledger row is in.

A landing that fails its commit rolls its own writes back, and the ledger is
one of them: a successful restore returns the file to HEAD and takes the
appended row with it. The receipt raised around that landing then has two
cases to tell apart — the row survived the rollback, or the rollback reverted
it — and it must name the recovery that fits the case it reports, because the
already-written wording advises against re-promoting and re-promoting is
exactly what recovers a reverted row.

Both cases are driven through the promotion path with a real failing landing
commit, produced by an executable pre-commit hook rather than a stubbed exit
code, so the rollback that follows runs against real git state.
"""

import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

RUN_ID = "rolled-back-row"


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _install_failing_hook(repository: Path, marker: Path) -> None:
    """Make `git commit` fail for real, and leave evidence that it ran."""
    hook = repository / ".git" / "hooks" / "pre-commit"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    hook.chmod(0o755)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    tracked = root / "tracked file.txt"
    tracked.write_text("committed content\n")
    data, version = ledger.load("sample", root=root)
    ledger.write("sample", data, version, root=root)
    _git(root, "add", "--", str(tracked), str(ledger.ledger_path("sample", root)))
    _git(root, "commit", "-qm", "test: seed", "-m", "Seed the tracked ledger.")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "sample",
            "repo": str(root),
            "worktree": str(root),
            "launch": "in-harness",
            "role": "investigate",
            "node": {"id": "receipt", "write_paths": []},
        },
    )
    return root


def _rows(repository) -> list[str]:
    return [
        str(r.get("run_id") or "")
        for r in ledger.load("sample", root=repository)[0]["runs"]
    ]


def test_rolled_back_row_receipt_names_re_promotion_as_the_recovery(
    repository, tmp_path
):
    """The rollback reverts the appended row, and the receipt says so.

    The hook makes the landing commit fail for real; the rollback that follows
    restores the tracked ledger to HEAD and the row leaves the working tree.
    The receipt must state that, and must not carry the advice that forbids the
    only action that recovers the row.
    """
    marker = tmp_path / "hook-ran"
    _install_failing_hook(repository, marker)
    ledger_path = ledger.ledger_path("sample", root=repository)
    assert ledger_path.is_file()

    with pytest.raises(promotion.CrewError) as caught:
        promotion._complete_locked(RUN_ID, gate="not-run", root=repository)

    assert marker.exists(), "the real commit path must reach the failing hook"
    message = str(caught.value)
    assert "was written and has been rolled back" in message
    assert f"rolled back with {ledger_path}" in message
    assert "re-promote once the landing failure is resolved" in message
    assert "do not re-promote" not in message
    # The read-back the branch rests on, asserted directly: the row is gone.
    assert RUN_ID not in _rows(repository)
    assert _git(repository, "status", "--porcelain").stdout.strip() == ""
    restored = _git(repository, "show", f"HEAD:{ledger_path.relative_to(repository)}")
    assert RUN_ID not in restored.stdout


def test_survived_row_receipt_keeps_the_do_not_re_promote_wording(
    repository, tmp_path, monkeypatch
):
    """A rollback git refuses preserves the row, and the receipt says so.

    Same real commit failure; here the restore is refused, so HEAD carries the
    ledger path and the rollback keeps it. The row is still in the ledger a
    reader will open, and the receipt keeps the wording that warns against
    re-promoting — the two cases must not collapse into one another.
    """
    marker = tmp_path / "hook-ran"
    _install_failing_hook(repository, marker)
    real_git = promotion._git

    def refuse_restore(checkout, *args, **kwargs):
        if args and args[0] == "restore":
            return subprocess.CompletedProcess(args, 128, "", "index is locked")
        return real_git(checkout, *args, **kwargs)

    monkeypatch.setattr(promotion, "_git", refuse_restore)

    with pytest.raises(promotion.CrewError) as caught:
        promotion._complete_locked(RUN_ID, gate="not-run", root=repository)

    assert marker.exists(), "the real commit path must reach the failing hook"
    message = str(caught.value)
    assert f"ledger row for run {RUN_ID!r} is already written" in message
    assert "do not re-promote" in message
    assert "rolled back" not in message
    assert RUN_ID in _rows(repository)


def test_a_reverted_row_is_not_reported_when_the_ledger_still_carries_the_row(
    repository,
):
    """The branch reads the row back, so a stale rollback report cannot lie.

    The rollback reports the ledger reverted while the ledger still carries the
    row — the shape a caller that restored a stale file would produce. The
    read-back is the second fact and must win: the row is present, so the
    reverted wording is not emitted.
    """
    promotion._complete_locked(RUN_ID, gate="not-run", root=repository)
    assert RUN_ID in _rows(repository), "the read-back needs a row to find"
    ledger_file = ledger.ledger_path("sample", root=repository)
    error = promotion.CrewError("could not commit the landing writes")
    setattr(
        error,
        promotion.LANDING_ROLLBACK_ATTRIBUTE,
        {str(ledger_file.resolve()): True},
    )

    rolled_back = promotion._ledger_row_was_rolled_back(
        error,
        ledger_file,
        lambda: promotion._ledger_holds_row("sample", repository, RUN_ID),
    )

    assert rolled_back is False
