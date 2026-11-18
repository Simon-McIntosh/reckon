"""The budget and the picker read only the rows promoted since their last read.

Both readers answer from the run store's promoted rows: the budget's ledger
readings take a verdict from the budget block each completed run recorded, and
the picker takes the projected history it routes on. Each call used to decode
the project's whole history out of the store, so a process that asked twice for
an unchanged history paid twice for the same rows.

The population here is the production shape: 2,000 promoted rows in a temporary
crew home, counted through a stand-in on the store's own read. The first read
decodes the population; a repeat with nothing promoted decodes nothing and
answers exactly as the first did; five promotions then cost five rows per reader
rather than the history again, and both answers match the full read the same
revision answers when it holds nothing.

The base revision decodes the whole population on the unchanged repeat, so the
row count asserted below measures this change rather than the fixture.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import budget, file_memo, ledger, run_store

PROJECT = "sample"
BACKEND = "alpha"
#: The population every read answers for, and the promotions a later read costs
#: rather than the population again.
POPULATION = 2000
PROMOTED = 5
NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def _record(index: int, *, run_id: str, completed: datetime) -> dict[str, Any]:
    """One promoted row, with the block a budget reading is derived from."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "backend": BACKEND,
        "node": f"node-{index:04d}",
        "plan": "plan-a",
        "section": "s1",
        "gate": "passed",
        "completed_at": completed.isoformat(),
        "completed_at_source": "run-record",
        "budget": {
            "headroom": "known",
            "utilisation_pct": 25,
            "windows": {"five_hour": {"utilisation": 0.25}},
        },
    }


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and checkout, with both readers' caches empty."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "cache"))
    root = tmp_path / "repo"
    root.mkdir()
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    ledger.clear_stored_history()
    file_memo.clear()
    yield root
    ledger.clear_stored_history()
    file_memo.clear()


def _seed(population: int) -> None:
    """The project's promoted history, as promotion leaves it in the store."""
    with run_store.RunStore(run_store.store_path()) as store:
        for index in range(population):
            store.append(
                PROJECT,
                _record(
                    index,
                    run_id=f"r-seed-{index:04d}",
                    completed=NOW - timedelta(minutes=index),
                ),
            )


def _promote(root: Path, count: int) -> list[str]:
    """Promote ``count`` runs through the path a promotion takes.

    The promotion writes the store row and advances the project's ledger-write
    generation, which is what a reader of a stored history sees move.
    """
    promoted: list[str] = []
    for index in range(count):
        run_id = f"r-promoted-{index:03d}"
        ledger.append_run(
            PROJECT,
            _record(
                POPULATION + index,
                run_id=run_id,
                completed=NOW + timedelta(minutes=index),
            ),
            root=root,
        )
        promoted.append(run_id)
    return promoted


def _count_decodes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record how many rows every read of the store decodes."""
    decoded: list[int] = []
    owner = run_store.stored_run_rows

    def recording(*args: Any, **kwargs: Any) -> Any:
        rows = owner(*args, **kwargs)
        if rows is not None:
            decoded.append(len(rows))
        return rows

    monkeypatch.setattr(run_store, "stored_run_rows", recording)
    return decoded


def _verdict(root: Path) -> dict[str, Any]:
    """The budget verdict, in the shape its reader would compare."""
    recorded = budget.latest_recorded(PROJECT, root=root)
    return {
        "backends": {
            name: {
                "record_id": reading.record_id,
                "observed_at": reading.observed_at,
                "attribution": reading.attribution,
            }
            for name, reading in sorted(recorded.items())
        },
        "budgets": {
            name: json.dumps(reading.budget, sort_keys=True)
            for name, reading in sorted(recorded.items())
        },
        "unattributed": [
            (reading.record_id, reading.observed_at)
            for reading in recorded.unattributed
        ],
    }


def _amend(root: Path, run_id: str, field: str, value: str) -> None:
    """Rewrite one promoted row in place, keeping its encoded size.

    A one-word field swapped for a word of the same length leaves the store's
    row count, rowid, indexed columns and encoded sizes exactly where they were,
    which is the rewrite a reader's per-row signature cannot describe.
    """
    assert len(value) == len("passed")
    row = _record(0, run_id=run_id, completed=NOW)
    row["gate"] = value
    ledger.write_run_file(PROJECT, row, root=root)


def test_a_repeat_read_decodes_only_what_was_promoted(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = crew_home
    _seed(POPULATION)
    decoded = _count_decodes(monkeypatch)

    first_verdict = _verdict(root)
    first_rows = ledger.picker_runs(PROJECT, root=root)
    # The instrument is shown to see what is known to be present before a later
    # zero is read as an absence.
    assert len(first_rows) == POPULATION
    assert sum(decoded) >= POPULATION
    assert first_verdict["backends"][BACKEND]["record_id"] == "r-seed-0000"

    decoded.clear()
    repeated_verdict = _verdict(root)
    repeated_rows = ledger.picker_runs(PROJECT, root=root)
    assert sum(decoded) == 0
    assert repeated_verdict == first_verdict
    assert repeated_rows == first_rows

    promoted = _promote(root, PROMOTED)
    decoded.clear()
    promoted_verdict = _verdict(root)
    promoted_rows = ledger.picker_runs(PROJECT, root=root)
    # Two readers, five rows each: the promotions are read once per reader.
    assert sum(decoded) <= 2 * PROMOTED
    assert len(promoted_rows) == POPULATION + PROMOTED
    assert promoted_verdict["backends"][BACKEND]["record_id"] == promoted[-1]

    # A reader holding nothing reads the whole history, and it answers exactly
    # what the incremental read answered.
    ledger.clear_stored_history()
    file_memo.clear()
    assert _verdict(root) == promoted_verdict
    assert ledger.picker_runs(PROJECT, root=root) == promoted_rows


def test_an_amended_row_reaches_the_next_answer(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewrite no signature can size is read again rather than cached over."""
    root = crew_home
    _seed(POPULATION)
    assert ledger.picker_runs(PROJECT, root=root)[0]["gate"] == "passed"
    _amend(root, "r-seed-0000", "gate", "failed")
    rows = {row["run_id"]: row for row in ledger.picker_runs(PROJECT, root=root)}
    assert rows["r-seed-0000"]["gate"] == "failed"
