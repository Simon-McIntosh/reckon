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


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move pointers and the project ledger into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _blocked_pointer(home: Path, run_id: str) -> dict:
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
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "node": {"id": "ticker-node", "plan": "plan-a", "time_budget": "20m"},
            "phase": "blocked",
            "created_at": datetime.now(tz=UTC).isoformat(),
            "manifest_path": str(manifest),
            "log_path": str(stream),
            "process_alive": False,
        },
    )
    return crew.read_pointer(run_id)


def _snapshot(pointer: dict) -> dict:
    return recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)


def _record_ledger_row(run_id: str) -> None:
    """Write the per-run ledger file a promotion leaves behind."""
    path = ledger.run_path("proj", run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id}))


def test_a_discarded_block_never_reads_promoted(home) -> None:
    """A blocked run removed by crew discard departs without a promotion word."""
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
    assert state == "withdrawn"


def test_a_recorded_promotion_reads_promoted(home) -> None:
    """A departure the ledger records as a promotion still reads promoted."""
    pointer = _blocked_pointer(home, "r-promoted")
    snapshot = _snapshot(pointer)
    _record_ledger_row("r-promoted")
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


def test_an_unknown_record_withdraws_rather_than_promoting() -> None:
    """A departure whose ledger cannot be resolved never asserts a promotion.

    A snapshot naming no project, folded with no reader, leaves the record
    unknown. Unknown is answered by withdrawal: choosing promoted there promises
    a landing no reader established, which is the defect this node closes.
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

    assert folded[0][2] == "withdrawn"
