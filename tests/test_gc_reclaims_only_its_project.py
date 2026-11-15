"""A project-scoped gc removes only that project's unledgered state.

The crew home is shared by every project, so a sweep that offers every
unledgered run directory and every orphan ledger index reclaims its peers'
state too. With ``--project`` named, each entry is attributed to a project —
a run directory by its live pointer, manifest or run record, a ledger index by
the ledger path it records — and only the named project's entries are removed.
An entry belonging to another project, or to none the sweep can name, is
reported as kept and left on disk.

Every fixture is synthesised under an isolated ``RECKON_HOME`` and mount
registry, so a pass that reached the real crew home would have nothing of its
own to inspect.
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
from reckon.crew.runs import crew_home

OWN = "reckon"
PEER = "nova"
RETENTION = 30
OLD_DAYS = 60


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def repository(root: Path) -> Path:
    """A committed checkout holding durable state for both fixture projects."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "test: seed")
    for name in (OWN, PEER):
        (repo / "docs" / "state" / name).mkdir(parents=True)
    return repo


def register_mounts(root: Path, projects: dict[str, Path]) -> Path:
    """Write the mount registry the sweep reads, isolated to the fixture."""
    mounts = root / "config" / "mounts.json"
    mounts.parent.mkdir(parents=True, exist_ok=True)
    mounts.write_text(
        json.dumps({"mounts": {name: str(docs) for name, docs in projects.items()}}),
        encoding="utf-8",
    )
    return mounts


def _age(path: Path, days: float) -> None:
    stamp = (datetime.now(tz=UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def write_run_directory(run_id: str, *, project: str | None, age_days: float) -> Path:
    """A run directory, attributed to ``project`` by its manifest when named."""
    directory = crew_home() / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stream.jsonl").write_text("{}\n", encoding="utf-8")
    if project is not None:
        (directory / "manifest.md").write_text(
            f"project: {project}\nstatus: complete\n"
        )
    _age(directory, age_days)
    return directory


def seed_index(name: str, recorded: str) -> Path:
    """A ledger index recording the ledger path it was built against."""
    root = _store.cache_root("pick-input")
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE metadata (name TEXT PRIMARY KEY, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO metadata VALUES (?, ?)",
            (ledger.LEDGER_INDEX_PATH_KEY, recorded),
        )
        connection.commit()
    finally:
        connection.close()
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
            OWN,
            "--retention-days",
            str(RETENTION),
            *(["--apply"] if apply else []),
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def sweep(*, apply: bool) -> dict:
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "retention-sweep",
            "--project",
            OWN,
            *(["--apply"] if apply else []),
        ],
    )
    assert result.exit_code == 0, result.output
    # The pass writes its phase timings to stderr before the one JSON report.
    return json.loads(result.output.strip().splitlines()[-1])


def test_retention_sweep_keeps_a_peer_projects_orphan_index(tmp_path, monkeypatch):
    """``crew retention-sweep --project reckon --apply`` removes reckon's orphan
    ledger index and leaves nova's on disk, naming each index's project."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    monkeypatch.setenv(
        "RECKON_MOUNTS_PATH",
        str(
            register_mounts(
                tmp_path,
                {OWN: repo / "docs", PEER: repo / "docs"},
            )
        ),
    )
    own_index = seed_index(
        "ledger-own.sqlite", str(ledger.ledger_path(OWN, repo).resolve())
    )
    peer_index = seed_index(
        "ledger-peer.sqlite", str(ledger.ledger_path(PEER, repo).resolve())
    )

    applied = sweep(apply=True)

    rows = {row["path"]: row for row in applied["ledger_indexes"]}
    assert rows[str(own_index)]["removed"] is True
    assert rows[str(own_index)]["project"] == OWN
    assert rows[str(peer_index)]["action"] == "keep"
    assert rows[str(peer_index)]["project"] == PEER
    assert rows[str(peer_index)]["removed"] is False
    assert not own_index.exists()
    assert peer_index.is_file()


def test_a_scoped_gc_removes_only_its_own_projects_entries(tmp_path, monkeypatch):
    """``gc --apply --project reckon`` removes reckon's run directory and ledger
    index, and keeps nova's and the unattributable one, naming each project."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    monkeypatch.setenv(
        "RECKON_MOUNTS_PATH",
        str(
            register_mounts(
                tmp_path,
                {OWN: repo / "docs", PEER: repo / "docs"},
            )
        ),
    )

    own_dir = write_run_directory(
        "r-20260101T000000000001-own", project=OWN, age_days=OLD_DAYS
    )
    peer_dir = write_run_directory(
        "r-20260101T000000000002-peer", project=PEER, age_days=OLD_DAYS
    )
    none_dir = write_run_directory(
        "r-20260101T000000000003-none", project=None, age_days=OLD_DAYS
    )
    own_ledger = ledger.ledger_path(OWN, repo)
    peer_ledger = ledger.ledger_path(PEER, repo)
    own_index = seed_index("ledger-own.sqlite", str(own_ledger.resolve()))
    peer_index = seed_index("ledger-peer.sqlite", str(peer_ledger.resolve()))

    dry = gc(repo, apply=False)
    unledgered = {row["run_id"]: row for row in dry["unledgered_run_directories"]}
    assert unledgered["r-20260101T000000000002-peer"]["action"] == "keep"
    assert unledgered["r-20260101T000000000002-peer"]["removed"] is False
    assert unledgered["r-20260101T000000000003-none"]["action"] == "keep"
    assert unledgered["r-20260101T000000000002-peer"]["project"] == PEER
    assert unledgered["r-20260101T000000000003-none"]["project"] == "unknown"
    assert all(path.is_dir() for path in (own_dir, peer_dir, none_dir))

    applied = gc(repo, apply=True)

    run_rows = {row["run_id"]: row for row in applied["unledgered_run_directories"]}
    assert run_rows["r-20260101T000000000001-own"]["removed"] is True
    assert run_rows["r-20260101T000000000001-own"]["project"] == OWN
    assert run_rows["r-20260101T000000000002-peer"]["action"] == "keep"
    assert run_rows["r-20260101T000000000002-peer"]["project"] == PEER
    assert run_rows["r-20260101T000000000002-peer"]["removed"] is False
    assert run_rows["r-20260101T000000000003-none"]["action"] == "keep"
    assert run_rows["r-20260101T000000000003-none"]["project"] == "unknown"
    assert run_rows["r-20260101T000000000003-none"]["removed"] is False

    index_rows = {row["path"]: row for row in applied["ledger_indexes"]}
    assert index_rows[str(own_index)]["removed"] is True
    assert index_rows[str(own_index)]["project"] == OWN
    assert index_rows[str(peer_index)]["action"] == "keep"
    assert index_rows[str(peer_index)]["project"] == PEER
    assert index_rows[str(peer_index)]["removed"] is False

    assert not own_dir.exists()
    assert peer_dir.is_dir()
    assert none_dir.is_dir()
    assert not own_index.exists()
    assert peer_index.is_file()
