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
rather than the history again. What the incremental path answers is compared
against ``ledger.load(project, root, picker_only=True)``, the read it replaced,
rows and order alike, so a projection or an ordering only the new path produces
would fail here rather than leave the picker's routing input changed in silence.
The held histories are bounded, and readers in several threads keep their own.

The base revision decodes the whole population on the unchanged repeat, so the
row count asserted below measures this change rather than the fixture.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
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


def _forget_history() -> None:
    """Empty the stored-history cache a tree that keeps one carries.

    The arm this test measures keeps the rows a read decoded; the arm it is
    compared against reads the project's history every time and holds nothing
    to empty, so the call is made only where the cache exists.
    """
    clear = getattr(ledger, "clear_stored_history", None)
    if clear is not None:
        clear()
    file_memo.clear()


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and checkout, with both readers' caches empty."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "cache"))
    root = tmp_path / "repo"
    root.mkdir()
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    _forget_history()
    yield root
    _forget_history()


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


def _order(rows: list[dict[str, Any]]) -> list[str]:
    """The run ids of one answer, in the order the answer carries them."""
    return [str(row["run_id"]) for row in rows]


def _amend(root: Path, run_id: str, value: str) -> None:
    """Rewrite one promoted row in place, keeping its encoded size.

    A one-word field swapped for a word of the same length leaves the store's
    row count, rowid, indexed columns and encoded sizes exactly where they were,
    which is the rewrite a reader's per-row signature cannot describe.
    """
    assert len(value) == len("passed")
    row = _record(0, run_id=run_id, completed=NOW)
    row["gate"] = value
    ledger.run_path(PROJECT, run_id, root).parent.mkdir(parents=True, exist_ok=True)
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
    repeated_decodes = sum(decoded)
    assert repeated_decodes == 0, (
        f"the unchanged repeat decoded {repeated_decodes} rows"
    )
    assert repeated_verdict == first_verdict
    assert repeated_rows == first_rows

    promoted = _promote(root, PROMOTED)
    decoded.clear()
    promoted_verdict = _verdict(root)
    promoted_rows = ledger.picker_runs(PROJECT, root=root)
    # Two readers, five rows each: the promotions are pulled once per reader.
    promoted_decodes = sum(decoded)
    assert promoted_decodes <= 2 * PROMOTED, (
        f"the repeat after {PROMOTED} promotions decoded {promoted_decodes} rows"
    )
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
    _amend(root, "r-seed-0000", "failed")
    rows = {row["run_id"]: row for row in ledger.picker_runs(PROJECT, root=root)}
    assert rows["r-seed-0000"]["gate"] == "failed"


def test_the_held_histories_stay_bounded(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows a process holds are bounded, and an evicted history is read.

    A monitor reads every project it watches from one process, so what a read
    keeps has to be bounded by count rather than by how many projects exist.
    The project whose history fell out of the bound answers the same rows and
    pays for them again, which is what every read used to pay.
    """
    root = crew_home
    projects = [
        f"{PROJECT}-{index:02d}" for index in range(ledger._MAX_STORED_HISTORIES + 4)
    ]
    with run_store.RunStore(run_store.store_path()) as store:
        for index, project in enumerate(projects):
            for row in range(5):
                store.append(
                    project,
                    _record(
                        row,
                        run_id=f"r-seed-{index:02d}-{row:02d}",
                        completed=NOW - timedelta(minutes=row),
                    ),
                )
    decoded = _count_decodes(monkeypatch)
    for project in projects:
        assert len(ledger.picker_runs(project, root=root)) == 5
    assert ledger._history_cell.cache_info().currsize == ledger._MAX_STORED_HISTORIES

    decoded.clear()
    first = {row["run_id"] for row in ledger.picker_runs(projects[0], root=root)}
    assert first == {f"r-seed-00-{row:02d}" for row in range(5)}
    assert sum(decoded) == 5


def test_the_stored_history_answers_what_the_full_read_answers(
    crew_home: Path,
) -> None:
    """The incremental path answers the rows and order the full read answers.

    ``ledger.load(project, root, picker_only=True)`` is the read this path
    replaced, and the picker routes on its row order. Comparing the stored
    history against that read is what catches a projection or an ordering the
    new path introduces: an answer that only equals the new path's own earlier
    answer would keep the suite green while the picker's input changed.
    """
    root = crew_home
    _seed(POPULATION)
    stored, auxiliary = ledger.stored_history(PROJECT, root, picker_only=True)
    full, _version = ledger.load(PROJECT, root, True)
    assert _order(stored) == _order(full["runs"])
    assert stored == full["runs"]
    assert auxiliary["members"] == full["members"]
    assert auxiliary["holds"] == full["holds"]
    assert _order(stored) == _order(ledger.picker_runs(PROJECT, root=root))
    assert stored == ledger.picker_runs(PROJECT, root=root)

    # The same equality after a promotion, where the answer is a held history
    # with decoded rows merged into it rather than a cold read.
    _promote(root, PROMOTED)
    assert ledger.picker_runs(PROJECT, root=root)[-1]["run_id"].startswith(
        "r-promoted-"
    )
    cold, _version = ledger.load(PROJECT, root, True)
    assert _order(ledger.picker_runs(PROJECT, root=root)) == _order(cold["runs"])
    assert ledger.picker_runs(PROJECT, root=root) == cold["runs"]


def test_concurrent_reads_keep_their_own_history(crew_home: Path) -> None:
    """Readers in several threads keep their own history and none is lost.

    The server answers one MCP read per handler thread, and every handler
    touches the held histories. Evicting and replacing an entry from two
    threads at once used to be able to take the entry one of them was about to
    store, which surfaced as a failed read rather than a cache miss.
    """
    root = crew_home
    projects = [
        f"{PROJECT}-{index:02d}" for index in range(ledger._MAX_STORED_HISTORIES + 4)
    ]
    with run_store.RunStore(run_store.store_path()) as store:
        for index, project in enumerate(projects):
            for row in range(3):
                store.append(
                    project,
                    _record(
                        row,
                        run_id=f"r-seed-{index:02d}-{row:02d}",
                        completed=NOW - timedelta(minutes=row),
                    ),
                )

    def read(project: str) -> list[str]:
        return [row["run_id"] for row in ledger.picker_runs(project, root=root)]

    rounds = 6
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = [
            (project, pool.submit(read, project)) for project in projects * rounds
        ]
    for project, future in answers:
        expected = {
            f"r-seed-{projects.index(project):02d}-{row:02d}" for row in range(3)
        }
        assert set(future.result()) == expected


def test_an_answer_a_caller_changes_does_not_change_the_held_history(
    crew_home: Path,
) -> None:
    """What a reader is handed is its own, so its change stays with it.

    The rows a read decoded are held for the next reader, and ``load`` always
    handed out a dict of its own for every row. A caller that writes into the
    answer it got — a patched field, a row it drops — must not thereby change
    what the next reader is handed.
    """
    root = crew_home
    _seed(POPULATION)
    cold, _version = ledger.load(PROJECT, root, True)
    answer = ledger.picker_runs(PROJECT, root=root)
    assert answer[0]["gate"] == "passed"
    answer[0]["gate"] = "failed"
    answer.pop()
    repeated = ledger.picker_runs(PROJECT, root=root)
    assert _order(repeated) == _order(cold["runs"])
    assert repeated == cold["runs"]
