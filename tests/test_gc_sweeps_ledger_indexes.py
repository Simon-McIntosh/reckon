"""gc sweeps the per-checkout ledger indexes a removed worktree leaves behind.

Every worktree resolves a different ledger path, and ``_run_index_path`` keys
an index by that path, so each dispatched worktree builds a full-size index and
one whose checkout is later removed is never resolved again. Each index records
the ledger path it indexes; gc reports, and under ``--apply`` removes, every
``ledger-*.sqlite`` whose recorded path is gone, and every one that records no
path and has not been opened for :data:`~reckon.crew.routing
.LEDGER_INDEX_UNRECORDED_MAX_AGE_DAYS` days. Every fixture is synthesised under
an isolated ``RECKON_HOME``, so a run that reached the real cache would have
nothing of its own to look at.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from reckon import _store, cli, ledger

PROJECT = "gc-index"
OLD_DAYS = 8


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def repository(root: Path) -> Path:
    """A minimal repository with a committed tree, so gc's HEAD resolves."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "test: seed")
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    return repo


def _age(path: Path, days: float) -> None:
    stamp = (datetime.now(tz=UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def cache_root() -> Path:
    return _store.cache_root("pick-input")


def seed_index(
    name: str, recorded: str | None, *, age_days: float | None = None
) -> Path:
    """Write a ledger index carrying the ledger path it records.

    ``recorded`` of ``None`` omits the path row, as an index built before the
    path was recorded does, so the sweep falls back to the age rule.
    """
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE metadata (name TEXT PRIMARY KEY, payload TEXT)"
        )
        if recorded is not None:
            connection.execute(
                "INSERT INTO metadata VALUES (?, ?)",
                (ledger.LEDGER_INDEX_PATH_KEY, recorded),
            )
        connection.commit()
    finally:
        connection.close()
    if age_days is not None:
        _age(path, age_days)
    return path


def gc(repo: Path, *, apply: bool) -> dict:
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "gc",
            "--repo",
            str(repo),
            "--project",
            PROJECT,
            *(["--apply"] if apply else []),
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def create_ledger(repo: Path) -> Path:
    """Create the project's ledger file, so its recorded path is live."""
    ledger.write(
        PROJECT,
        {"members": [], "holds": [], "runs": []},
        0,
        root=repo,
        allow_member_removal=True,
    )
    return ledger.ledger_path(PROJECT, repo)


def plant(repo: Path) -> dict[str, Path]:
    """Seed two orphans, a live index, a stale unrecorded index and a picker file."""
    live_ledger = create_ledger(repo)
    gone = repo.parent / "gone"
    return {
        "orphan_a": seed_index("ledger-a.sqlite", str(gone / "ledger-a.json")),
        "orphan_b": seed_index("ledger-b.sqlite", str(gone / "ledger-b.json")),
        "live": seed_index("ledger-c.sqlite", str(live_ledger.resolve())),
        "unrecorded": seed_index("ledger-d.sqlite", None, age_days=OLD_DAYS),
        "picker": cache_root() / "picker-x.json",
    }


def test_gc_reports_and_removes_orphan_ledger_indexes(tmp_path, monkeypatch):
    """gc lists exactly the two orphans and the stale unrecorded index, and
    ``--apply`` removes exactly those, stating each path and size."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    stores = plant(repo)
    stores["picker"].write_text("{}", encoding="utf-8")

    dry = gc(repo, apply=False)
    assert dry["dry_run"] is True

    rows = {row["path"]: row for row in dry["ledger_indexes"]}
    assert set(rows) == {
        str(stores["orphan_a"]),
        str(stores["orphan_b"]),
        str(stores["unrecorded"]),
    }
    assert rows[str(stores["orphan_a"])]["reason"] == "recorded-checkout-gone"
    assert rows[str(stores["unrecorded"])]["reason"] == "unrecorded-stale"
    # Each row states its path and the bytes it holds, and the dry run removed
    # nothing.
    assert all(
        row["size"] == Path(row["path"]).stat().st_size > 0 for row in rows.values()
    )
    assert dry["ledger_indexes_removed"] == 0
    assert all(path.exists() for path in stores.values())

    applied = gc(repo, apply=True)

    removed = {row["path"] for row in applied["ledger_indexes"] if row["removed"]}
    assert removed == {
        str(stores["orphan_a"]),
        str(stores["orphan_b"]),
        str(stores["unrecorded"]),
    }
    assert applied["ledger_indexes_removed"] == 3
    assert not stores["orphan_a"].exists()
    assert not stores["orphan_b"].exists()
    assert not stores["unrecorded"].exists()
    # The live index and the picker's own input file in the same cache root are
    # never matched.
    assert stores["live"].is_file()
    assert stores["picker"].is_file()


def test_an_index_built_through_the_reader_records_its_path(tmp_path, monkeypatch):
    """An index built by a ledger read carries the ledger path it indexes."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    create_ledger(repo)

    ledger.load(PROJECT, root=repo)

    index = ledger._run_index_path(PROJECT, repo)
    assert index.is_file()
    expected = str(ledger.ledger_path(PROJECT, repo).resolve())
    assert ledger.recorded_ledger_path(index) == expected


def test_the_real_cache_is_untouched(tmp_path, monkeypatch):
    """The sweep reads and removes only the fixture's own cache root."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    plant(repo)

    real = Path.home() / ".cache" / "reckon" / "pick-input"
    before = sorted(path.name for path in real.iterdir()) if real.is_dir() else None

    gc(repo, apply=True)

    after = sorted(path.name for path in real.iterdir()) if real.is_dir() else None
    assert after == before
    assert str(cache_root()).startswith(os.environ["RECKON_HOME"])


def test_a_live_process_holds_its_index(tmp_path, monkeypatch):
    """An index a live process holds a write lock on is never removed."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    create_ledger(repo)
    orphan = seed_index("ledger-held.sqlite", str(repo.parent / "gone.json"))

    held = sqlite3.connect(orphan, timeout=0.0)
    held.execute("BEGIN IMMEDIATE")
    try:
        applied = gc(repo, apply=True)
        assert orphan.is_file()
        assert all(
            not row["removed"]
            for row in applied["ledger_indexes"]
            if row["path"] == str(orphan)
        )
        assert applied["ledger_indexes_removed"] == 0
    finally:
        held.rollback()
        held.close()
