"""A deliberate discard reads discarded, distinct from a pointer that vanished.

A run leaves the fleet in three shapes a live pointer cannot tell apart by
itself: a promotion that wrote its ledger row, a discard a coordinator asked
for, and a pointer reaped or removed by hand. The first two are deliberate and
leave a trail — a ledger row and a run-directory marker respectively — so a
reader can act on the word instead of guessing. Reading a discard as the same
``withdrawn`` a vanished pointer produces hides the coordinator's own act, and
reading it as ``promoted`` promises work that never landed.

These tests drive the producer's own call shape — ``fleet_transitions`` with no
ledger reader supplied, the run's project carried on its snapshot — against a
synthetic config home, and assert the real crew directories are untouched.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import promotion, recovery


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move pointers, run directories and the ledger into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _blocked_run(home: Path, run_id: str) -> dict:
    """A stopped run whose manifest reads blocked, with its run directory made.

    The run directory is created because a real run always has one — that is
    where the discard marker lives — and because a discard must never write
    into a directory that does not already exist.
    """
    stream = home / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: ticker-node\nstatus: blocked\n"
        "blockers: the fence excludes the file the repair needs\n"
    )
    crew.run_dir(run_id).mkdir(parents=True, exist_ok=True)
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


def test_a_discarded_run_departs_as_discarded(home: Path) -> None:
    """A run removed by crew discard reads discarded and leaves its marker."""
    pointer = _blocked_run(home, "r-discarded")
    snapshot = _snapshot(pointer)
    assert snapshot["state"] == "blocked"

    result = crew.discard("r-discarded")
    assert not crew.pointer_path("r-discarded").exists()

    # The departure word is asserted before the marker, so a mutation that
    # stops the record being written fails on the reading it corrupts -- a
    # departure that reads withdrawn -- rather than on a missing file.
    folded, _ = recovery.fleet_transitions({"r-discarded": snapshot}, {})

    assert len(folded) == 1
    _emitted, previous, state, _counts = folded[0]
    assert previous == "blocked"
    assert state == "discarded"

    marker = promotion.discard_record_path("r-discarded")
    assert Path(str(result["discard_record"])) == marker
    assert marker.is_file()
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["run_id"] == "r-discarded"
    assert record["discarded_at"]


def test_a_recorded_promotion_still_reads_promoted(home: Path) -> None:
    """A departure the ledger records as a promotion reads promoted."""
    pointer = _blocked_run(home, "r-promoted")
    snapshot = _snapshot(pointer)
    _record_ledger_row("r-promoted")
    crew.pointer_path("r-promoted").unlink()

    folded, _ = recovery.fleet_transitions({"r-promoted": snapshot}, {})

    assert folded[0][2] == "promoted"


def test_a_promotion_outweighs_a_discard_marker(home: Path) -> None:
    """A recorded promotion reads promoted whatever else the run directory holds.

    A discard marker cannot be present on a genuinely promoted run, but a stale
    marker left by an earlier act must never outvote the ledger: the row is the
    fleet's evidence that work landed, and a directory file is not.
    """
    pointer = _blocked_run(home, "r-both")
    snapshot = _snapshot(pointer)
    marker = promotion.discard_record_path("r-both")
    marker.write_text(json.dumps({"run_id": "r-both"}), encoding="utf-8")
    _record_ledger_row("r-both")
    crew.pointer_path("r-both").unlink()

    folded, _ = recovery.fleet_transitions({"r-both": snapshot}, {})

    assert folded[0][2] == "promoted"


def test_a_pointer_that_vanishes_with_neither_reads_withdrawn(home: Path) -> None:
    """A pointer removed with nothing recorded behind it still reads withdrawn."""
    pointer = _blocked_run(home, "r-vanished")
    snapshot = _snapshot(pointer)
    # The run directory survives, but holds no discard marker.
    assert not promotion.discard_record_path("r-vanished").exists()
    crew.pointer_path("r-vanished").unlink()

    folded, _ = recovery.fleet_transitions({"r-vanished": snapshot}, {})

    assert folded[0][2] == "withdrawn"


def test_a_discard_leaves_no_record_when_its_run_directory_is_gone(home: Path) -> None:
    """A discard whose run directory has gone is not resurrected by its marker.

    The run directory is the run's own durable home; a discard that recreated it
    would bring a discarded run back into existence. A run whose home is already
    gone departs withdrawn, which is the safe word, and no directory is made.
    """
    pointer = _blocked_run(home, "r-homeless")
    snapshot = _snapshot(pointer)
    crew.run_dir("r-homeless").rmdir()

    result = crew.discard("r-homeless")

    assert result["discard_record"] is None
    assert not crew.run_dir("r-homeless").exists()
    folded, _ = recovery.fleet_transitions({"r-homeless": snapshot}, {})
    assert folded[0][2] == "withdrawn"


def test_the_real_crew_directories_are_untouched(home: Path) -> None:
    """The fixture redirects every crew path, so no real run is written.

    A reader is only wrong when it reads outside state; a writer makes others
    wrong. Every path this module touches resolves under the temporary home, and
    the marker a discard writes is asserted to land there rather than in the
    operator's own crew directory.
    """
    assert crew.crew_home() == home / "crew"
    assert crew.runs_dir() == home / "crew" / "runs"
    pointer = _blocked_run(home, "r-isolated")
    _snapshot(pointer)

    crew.discard("r-isolated")

    assert promotion.discard_record_path("r-isolated") == (
        home / "crew" / "runs" / "r-isolated" / "discard.json"
    )
