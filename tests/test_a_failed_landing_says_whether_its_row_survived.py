"""A failed landing's receipt decides the row's fate from durable state only.

A landing that fails to stage or commit rolls its own writes back, and the
ledger row is one of them. The receipt raised around that landing tells the
reader whether the row survived, and its wording carries the recovery: the
"already written" branch warns against re-promoting, which is exactly what
recovers a row the rollback took back. So the branch must rest on state a
reader can still open — the run's own per-run file, or a row the aggregate
itself holds — never on the derived ledger index.

That index is a disposable cache of history the aggregate and the per-run files
already carry. When its refresh is refused — a locked or unwritable index —
``ledger.load`` serves the last published snapshot, which can still name a run
whose per-run file the rollback has since removed and whose row the aggregate
never held. A rollback that really happened then reads as a surviving row, and
a coordinator who obeys the receipt leaves the run unpromoted.

Measured 2026-10-17 at 15:37Z: promoting a run hit a peer's ``.git/index.lock``
while a concurrent promotion held it. The landing refused, the rollback removed
the run's per-run file, and the receipt said the row "is already written; do
not re-promote" — false, since the file was absent, no promote commit existed
and the aggregate held no row. The coordinator re-promoted against the warning,
and the run landed.
"""

import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion, promotion_scope
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"
RUN_ID = "failed-landing-row"


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _rows(repository) -> list[str]:
    return [
        str(row.get("run_id") or "")
        for row in ledger.load(PROJECT, root=repository)[0]["runs"]
    ]


@pytest.fixture
def repository(tmp_path, monkeypatch):
    """A temporary repository whose ledger index resolves inside the test tree."""
    pick_cache = tmp_path / "pick-cache"
    pick_cache.mkdir()
    monkeypatch.setenv("RECKON_PICK_CACHE", str(pick_cache))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    tracked = root / "tracked file.txt"
    tracked.write_text("committed content\n")
    data, version = ledger.load(PROJECT, root=root)
    ledger.write(PROJECT, data, version, root=root)
    _git(root, "add", "--", str(tracked), str(ledger.ledger_path(PROJECT, root)))
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


def test_a_rolled_back_row_is_reported_when_only_a_stale_index_names_it(
    repository, monkeypatch
):
    """A cached index cannot make a real rollback read as a surviving row.

    The promotion appends the run's per-run file, then stages and commits it
    under a pinned index directory so the derived ledger index is built while
    that file exists. A peer's ``.git/index.lock`` refuses the staging, so the
    rollback drops the per-run file, then the run file is absent and the
    aggregate holds no row. The index is then unwritable, so the receipt's
    read-back is refused and falls back to the last-good snapshot — the one
    built while the file existed. The receipt must report the rollback, not the
    already-written wording, because the only source still naming the row is
    the cache the reader's next promotion will rebuild.
    """
    index = ledger._run_index_path(PROJECT, repository)
    run_file = ledger.run_path(PROJECT, RUN_ID, repository)
    aggregate = ledger.ledger_path(PROJECT, repository)
    lock = repository / ".git" / "index.lock"
    lock.touch()
    real_git = promotion_scope._git
    state = {"flipped": False}

    def hook(checkout, *args, **kwargs):
        # On the landing's own staging call, the per-run file this promotion
        # appended is on disk: publish the index now, so it names the row, then
        # make it unwritable so the receipt's refresh is refused.
        if not state["flipped"] and args and args[0] == "add":
            assert run_file.is_file(), "the append must precede the staging"
            ledger.load(PROJECT, repository, use_index=True)
            assert RUN_ID in _rows(repository)
            index.chmod(0o400)
            state["flipped"] = True
        return real_git(checkout, *args, **kwargs)

    monkeypatch.setattr(promotion_scope, "_git", hook)

    with pytest.raises(promotion.CrewError) as caught:
        promotion._complete_locked(RUN_ID, gate="not-run", root=repository)

    assert state["flipped"], "the provisional index must have been published"
    message = str(caught.value)
    assert "was written and has been rolled back" in message
    assert "re-promote once the landing failure is resolved" in message
    assert "do not re-promote" not in message
    # The facts behind the wording: the file is gone and the aggregate has no row.
    assert not run_file.exists()
    assert RUN_ID not in [
        str(row.get("run_id") or "")
        for row in ledger._load_aggregate(PROJECT, repository)[0]["runs"]
    ]

    # Releasing the peer's lock and making the cache writable again lets the
    # retry land, which the already-written wording would forbid.
    lock.unlink()
    index.chmod(0o600)
    monkeypatch.setattr(promotion_scope, "_git", real_git)
    promotion._complete_locked(RUN_ID, gate="not-run", root=repository)
    assert run_file.is_file()
    assert RUN_ID in _rows(repository)
    assert not aggregate.is_symlink()


def test_a_row_in_the_aggregate_still_reads_already_written(repository):
    """The aggregate's own row is durable evidence, so the receipt keeps its warning.

    The second case seeds the run's row in the aggregate itself with no per-run
    file.  The read-back must find it, because the row a reader will open is
    there, and the receipt keeps the wording that warns against re-promoting.
    """
    data, version = ledger.load(PROJECT, root=repository)
    data["runs"].append(
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "completed_at": "2026-10-17T15:37:00+00:00",
        }
    )
    ledger.write(PROJECT, data, version, root=repository)
    run_file = ledger.run_path(PROJECT, RUN_ID, repository)
    assert not run_file.is_file()
    aggregate = ledger.ledger_path(PROJECT, repository)
    assert promotion._ledger_holds_row(PROJECT, repository, RUN_ID) is True

    error = promotion.CrewError("could not stage the landing writes")
    setattr(
        error,
        promotion.LANDING_ROLLBACK_ATTRIBUTE,
        {str(Path(aggregate).resolve()): True},
    )
    with (
        pytest.raises(promotion.CrewError) as caught,
        promotion._report_written_ledger_row(
            RUN_ID,
            ledger_path=aggregate,
            row_present=lambda: promotion._ledger_holds_row(
                PROJECT, repository, RUN_ID
            ),
        ),
    ):
        raise error

    message = str(caught.value)
    assert "is already written" in message
    assert "do not re-promote" in message
    assert "rolled back" not in message
