"""The signature mirror agrees with the store after every removal.

The signature read answers from a payload-free mirror of the store's rows, so a
project whose mirror disagrees with its rows falls back to measuring every
payload on every read. A row removed by a path that does not write the mirror
leaves its mirror row marked present, so the disagreement used to be permanent
once it started: the live store held such rows for two of thirteen projects,
which were the two busiest.

A mirror found in disagreement is repaired once, at a cost bounded by the rows
that differ, and the read after it is answered from the mirror. The population
here is a store whose payloads total more than 200 MB, which is large enough
that a whole-store scan cannot fit inside the bound the second read is held to:
the base revision fails the first case with the removed run still marked
present, which is this file's negative control.

Every removal path the store offers is checked against the mirror: a delete, an
archive, a wholesale rebuild, and a direct row discard of the removed run,
which is what a refused landing performs.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import file_memo, ledger, run_store

PROJECT = "sample"
BACKEND = "alpha"
#: Rows in the prepared store, and the size of each row's durable payload. The
#: product is the store's payload total, held above 200 MB so a repair bounded
#: by the whole store cannot be mistaken for one bounded by the rows that
#: differ.
ROWS = 50
PAYLOAD_BYTES = 4_200_000
#: What a signature read may cost once the mirror agrees with the store.
READ_BOUND = 5 * 1024 * 1024
NOW = datetime(2026, 11, 22, 22, tzinfo=UTC)


def _record(
    index: int, *, run_id: str, filler: str, gate: str = "passed"
) -> dict[str, Any]:
    """One promoted row carrying a durable payload of the stated length."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "backend": BACKEND,
        "node": f"node-{index:04d}",
        "plan": "plan-a",
        "section": "s1",
        "gate": gate,
        "completed_at": (NOW - timedelta(minutes=index)).isoformat(),
        "note": filler,
    }


def _filler(scale: int, index: int) -> str:
    """A payload body unique per row, whose length scales with the store."""
    body = f"{index:08d}"
    return body + "x" * (PAYLOAD_BYTES * scale - len(body))


def _rchar() -> int:
    """The bytes this process has read, from its own kernel accounting."""
    with open("/proc/self/io", encoding="ascii") as handle:
        for line in handle:
            if line.startswith("rchar: "):
                return int(line.split()[1])
    raise AssertionError("the process's read counter is not readable")


def _measure(call):
    """Run one call, returning the bytes it read and what it answered."""
    before = _rchar()
    value = call()
    return _rchar() - before, value


def _forget_history() -> None:
    """Empty the history the reader holds, so a probe starts cold."""
    clear = getattr(ledger, "clear_stored_history", None)
    if clear is not None:
        clear()
    file_memo.clear()


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, with the store empty and every cache cleared."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "cache"))
    root = tmp_path / "repo"
    root.mkdir()
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    (root / "docs" / "state" / PROJECT / "runs").mkdir(parents=True, exist_ok=True)
    _forget_history()
    yield root
    _forget_history()


def _seed(rows: int = ROWS, *, scale: int = 1) -> int:
    """Write the prepared store, returning its payload total."""
    total = 0
    with run_store.RunStore(run_store.store_path()) as store:
        for index in range(rows):
            payload = _filler(scale, index)
            total += len(payload)
            store.append(
                PROJECT,
                _record(index, run_id=f"r-seed-{index:03d}", filler=payload),
            )
    return total


def _stored_ids(target: Path | None = None) -> set[str]:
    """The run ids the store holds for the project, hot and retired."""
    target = target or run_store.store_path()
    connection = run_store._open_readonly(target)
    try:
        ids: set[str] = set()
        for schema in run_store._read_attached_tables(connection, target):
            try:
                ids |= {
                    str(row[0])
                    for row in connection.execute(
                        f'SELECT "run_id" FROM {schema}.runs WHERE "project" = ?',  # noqa: S608 - fixed schema names
                        (PROJECT,),
                    )
                }
            except sqlite3.Error:
                continue
        return ids
    finally:
        connection.close()


def _mirror_rows(target: Path | None = None) -> dict[str, tuple[Any, ...]]:
    """Each mirror row for the project, keyed by run id, with its schema."""
    target = target or run_store.store_path()
    connection = run_store._open_readonly(target)
    try:
        return {
            str(row[0]): tuple(row)
            for row in connection.execute(
                'SELECT "run_id", "project", "present", "schema_name" '
                'FROM "run_signatures" WHERE "project" = ? ORDER BY "run_id"',
                (PROJECT,),
            )
        }
    finally:
        connection.close()


def _present_ids(target: Path | None = None) -> set[str]:
    """The run ids the mirror marks present for the project."""
    return {run_id for run_id, row in _mirror_rows(target).items() if row[2]}


def _assert_agrees(target: Path | None = None) -> None:
    """The mirror's present rows are exactly the stored rows."""
    assert _present_ids(target) == _stored_ids(target)


def _signature_rows() -> dict[str, tuple[Any, ...]]:
    """Each present row's whole signature, keyed by run id."""
    return {
        str(row[0]): tuple(row)
        for row in (run_store.stored_run_signature(PROJECT) or ())
        if len(row) <= 13 or row[13]
    }


def _raw_delete_run(run_id: str) -> None:
    """Delete a row the way a refused landing's discard does.

    The store's own delete leaves a tombstone; this is the direct table delete
    a caller outside the store performs, which is what left the mirror behind.
    """
    store = run_store.store_path()
    connection = sqlite3.connect(str(store))
    try:
        with connection:
            hot = connection.execute(
                'SELECT 1 FROM "runs" WHERE "run_id" = ?',
                (run_id,),
            ).fetchone()
            if hot is None:
                connection.execute(
                    "ATTACH DATABASE ? AS archive",
                    (str(run_store._archive_path(store)),),
                )
                runs, details = "archive.runs", "archive.run_details"
            else:
                runs, details = "runs", "run_details"
            connection.execute(
                f'DELETE FROM {details} WHERE "run_id" = ?',  # noqa: S608 - fixed table names
                (run_id,),
            )
            connection.execute(
                f'DELETE FROM {runs} WHERE "run_id" = ?',  # noqa: S608 - fixed table names
                (run_id,),
            )
    finally:
        connection.close()


def _raw_delete_mirror_row(run_id: str) -> None:
    """Drop one mirror row, as a store written by older code never had it."""
    connection = sqlite3.connect(str(run_store.store_path()))
    try:
        with connection:
            connection.execute(
                'DELETE FROM "run_signatures" WHERE "run_id" = ?', (run_id,)
            )
    finally:
        connection.close()


def test_a_disagreeing_mirror_is_reconciled_by_one_read_and_read_cheaply_after(
    crew_home: Path,
) -> None:
    """One signature read repairs the mirror, and the read after it is bounded.

    The first case: a mirror row for a run the store no longer holds and a
    stored run no mirror row describes. At the base revision the removed run
    stays marked present, the counts never agree, and the read keeps measuring
    every payload, so this is the negative control the file declares.
    """
    del crew_home
    payload_total = _seed()
    assert payload_total > 200 * 1024 * 1024, payload_total

    _raw_delete_run("r-seed-000")
    _raw_delete_mirror_row("r-seed-001")

    mirror = _mirror_rows()
    assert mirror["r-seed-000"][2] == 1, "the discarded run is still in the mirror"
    assert "r-seed-001" not in mirror
    assert _present_ids() != _stored_ids(), "the prepared store already agrees"

    repair_bytes, _answer = _measure(lambda: run_store.stored_run_signature(PROJECT))
    _assert_agrees()
    reconciled = _signature_rows()
    assert "r-seed-000" not in reconciled
    assert "r-seed-001" in reconciled
    # The repair reads the one row the mirror lacked and no other payload.
    assert repair_bytes < 2 * PAYLOAD_BYTES + READ_BOUND, repair_bytes

    second_bytes, signature = _measure(lambda: run_store.stored_run_signature(PROJECT))
    assert signature and len(signature) == ROWS - 1
    assert second_bytes < READ_BOUND, (
        f"the read after the repair cost {second_bytes} bytes against a payload "
        f"total of {payload_total}"
    )
    assert _mirror_rows()["r-seed-000"][2] == 0


def test_after_each_removal_path_the_mirror_matches_the_store(
    crew_home: Path,
) -> None:
    """A delete, an archive, a rebuild and a direct discard all leave it agreeing."""
    del crew_home
    _seed(rows=6)
    _assert_agrees()

    with run_store.RunStore(run_store.store_path()) as store:
        assert store.delete_run("r-seed-000")
    _assert_agrees()
    assert _mirror_rows()["r-seed-000"][2] == 0

    with run_store.RunStore(run_store.store_path()) as store:
        retired = store.retire(
            before=NOW + timedelta(minutes=1),
            protected=set(),
            projects={PROJECT},
            apply=True,
        )["retired"]
    assert retired
    _assert_agrees()
    for run_id in retired:
        assert _mirror_rows()[run_id][2] == 1
        assert _mirror_rows()[run_id][3] == "archive"

    # A direct discard is repaired by the next signature read.
    _raw_delete_run("r-seed-001")
    assert _present_ids() != _stored_ids()
    run_store.stored_run_signature(PROJECT)
    _assert_agrees()

    # A wholesale rebuild from the store's own rows keeps them in step.
    with run_store.RunStore(run_store.store_path()) as store:
        store._rebuild_signatures(force=True)
    _assert_agrees()


def test_the_signature_moves_only_when_a_row_changes(crew_home: Path) -> None:
    """A rewrite, an archive and a delete move it; an untouched store does not."""
    del crew_home
    _seed(rows=4)
    first = _signature_rows()
    assert len(first) == 4
    assert _signature_rows() == first, "an untouched store moved"

    with run_store.RunStore(run_store.store_path()) as store:
        store.sync_run(
            PROJECT,
            _record(0, run_id="r-seed-000", filler=_filler(1, 1), gate="failed"),
        )
    rewritten = _signature_rows()
    assert rewritten != first
    assert {row for key, row in rewritten.items() if key != "r-seed-000"} == {
        row for key, row in first.items() if key != "r-seed-000"
    }

    with run_store.RunStore(run_store.store_path()) as store:
        retired = store.retire(
            before=NOW + timedelta(minutes=10),
            protected=set(),
            projects={PROJECT},
            apply=True,
        )["retired"]
    assert retired
    archived_rows = _signature_rows()
    assert archived_rows != rewritten
    assert archived_rows[retired[-1]][1] == "archive"

    with run_store.RunStore(run_store.store_path()) as store:
        assert store.delete_run(retired[-1])
    deleted = _signature_rows()
    assert retired[-1] not in deleted
    assert set(deleted) == set(archived_rows) - {retired[-1]}
