"""A pointer that leaves without a recorded promotion never reads as promoted.

A run leaves the fleet for two reasons a pointer cannot tell apart: a promotion
that wrote its ledger row, and a pointer that vanished with nothing recorded
behind it — a discard, a reaped pointer, a file removed by hand. A coordinator
acts on the departure word, so one is finished and the other needs recovering.
When the word is chosen without reading a ledger, a discard reads as work that
landed, which is exactly the reading this file forbids.

The producer that publishes the fleet stream folds its transitions without a
ledger reader, so these tests exercise the same call shape it uses: no reader
supplied, the run's own project carried on its snapshot.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import recovery
from tests.helpers.git import git as _git


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move pointers and the project ledger into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _blocked_pointer(home: Path, run_id: str, *, repo: Path | None = None) -> dict:
    """A stopped run whose manifest reads blocked, as a deref'd pointer."""
    stream = home / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: ticker-node\nstatus: blocked\n"
        "blockers: the fence excludes the file the repair needs\n"
    )
    pointer = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": "ticker-node", "plan": "plan-a", "time_budget": "20m"},
        "phase": "blocked",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "process_alive": False,
    }
    if repo is not None:
        pointer["repo"] = str(repo)
        pointer["worktree"] = str(repo)
    crew._write_json(crew.pointer_path(run_id), pointer)
    return crew.read_pointer(run_id)


def _snapshot(pointer: dict) -> dict:
    return recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)


def _landed_repository(home: Path, tmp_path: Path) -> tuple[Path, str]:
    """A checkout whose project state resolves to the temporary home's own.

    A ledger row names the checkout it was promoted into, because the departure
    word asks that checkout's primary branch whether the delivered head reached
    it: a promoted run whose head no branch carries is a landing still owed and
    reads delivered rather than promoted. The state directory is carried into
    the checkout as the symlink a mounted project carries, so the row is
    reached by the discovery path and by the checkout's own path alike.
    """
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    project_state = home / "state" / "proj"
    project_state.mkdir(parents=True)
    (project_state / "index.json").write_text("{}\n", encoding="utf-8")
    (repo / "docs" / "state").symlink_to(home / "state")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed the checkout")
    return repo, _git(repo, "rev-parse", "HEAD")


def _record_ledger_row(run_id: str, repo: Path, head: str) -> None:
    """Write the per-run ledger file a landed promotion leaves behind.

    The row names the checkout and the head its landing delivered, and that
    head is on the checkout's primary branch, which is what the departure word
    reads to word the run promoted rather than delivered.
    """
    path = ledger.run_path("proj", run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "repo": str(repo), "commits": [head]})
    )


def test_a_discarded_block_never_reads_promoted(home) -> None:
    """A blocked run removed by crew discard departs discarded, never promoted.

    A run with no run directory records its departure in the shared spool, so
    the discard is corroborated there as well as in the run's own directory.
    """
    pointer = _blocked_pointer(home, "r-discarded")
    snapshot = _snapshot(pointer)
    assert snapshot["state"] == "blocked"

    crew.discard("r-discarded")
    assert not crew.pointer_path("r-discarded").exists()

    # The producer's call shape: no ledger reader is passed.
    folded, _ = recovery.fleet_transitions({"r-discarded": snapshot}, {})

    assert len(folded) == 1
    _emitted, previous, state, _counts = folded[0]
    assert previous == "blocked"
    assert state != "promoted"
    assert state == "discarded"


def test_a_recorded_promotion_reads_promoted(home, tmp_path) -> None:
    """A departure the ledger records as a landed promotion reads promoted.

    The row is what makes the word ``promoted`` honest, and the row only earns
    it once the head it delivered is on the checkout's primary branch: a row
    whose head no branch carries is a landing still owed and reads delivered.
    So the fixture promotes into a real checkout whose head is the branch's.
    """
    repo, head = _landed_repository(home, tmp_path)
    pointer = _blocked_pointer(home, "r-promoted", repo=repo)
    snapshot = _snapshot(pointer)
    _record_ledger_row("r-promoted", repo, head)
    crew.pointer_path("r-promoted").unlink()

    folded, _ = recovery.fleet_transitions({"r-promoted": snapshot}, {})

    assert folded[0][2] == "promoted"


def test_a_pointer_that_vanishes_without_a_row_reads_withdrawn(home) -> None:
    """A pointer removed with nothing recorded behind it reads withdrawn."""
    pointer = _blocked_pointer(home, "r-vanished")
    snapshot = _snapshot(pointer)
    crew.pointer_path("r-vanished").unlink()

    folded, _ = recovery.fleet_transitions({"r-vanished": snapshot}, {})

    assert folded[0][2] == "withdrawn"


def test_an_unknown_record_reads_departed() -> None:
    """A departure whose ledger cannot be resolved reads its own word.

    A snapshot naming no project, folded with no reader, leaves the record
    unknown. Unknown is answered by departed, a word that promises neither a
    landing nor a withdrawal: choosing promoted there promises a landing no
    reader established, and choosing withdrawn there asserts a withdrawal no
    evidence holds. Departed claims only that the run has left the fleet.
    """
    known = {
        "r-1": {
            "run_id": "r-1",
            "node": "n-blocked",
            "state": "blocked",
            "detail": "the installed writer does not satisfy the interface",
            "needs_help_complete": True,
        }
    }

    folded, _ = recovery.fleet_transitions(known, {})

    assert folded[0][2] == "departed"
