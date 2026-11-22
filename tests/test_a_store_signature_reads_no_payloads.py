"""A change signature costs the rows that changed, not the store's payloads.

The signature a stored-history reader compares is taken again whenever the
store's change stamp moves, which under a working fleet is continuously. It
used to measure the encoded size of every row by reading every payload, so one
call on the live store read 1,165,905,463 bytes and the picker's read beside it
read 2,319,775,486. The signature now lives in a payload-free table and every
row carries a sequence number, so a reader asks for the rows written since its
last read and nothing else.

The population here is a store whose payloads total more than 200 MB, which is
large enough that reading them cannot fit inside the bound the read is held to.
The process's own read counter (``/proc/self/io``) measures the bytes, and the
store's statement counters measure the rows each statement returned. The base
revision fails the first case on the byte bound, with the signature read
costing more than the store's payload total — the negative control this file
declares.

What the incremental answer returns is compared against the base
implementation's own query, value for value, over a sequence of store states:
untouched, rewritten in place, archived, and deleted.
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
#: product is the store's payload total, held above 200 MB so a read bounded by
#: the rows that changed cannot be confused with a read of the whole store.
ROWS = 50
PAYLOAD_BYTES = 4_200_000
#: What one read after a single row changed may cost.
READ_BOUND = 5 * 1024 * 1024
NOW = datetime(2026, 11, 22, 9, tzinfo=UTC)


def _record(index: int, *, run_id: str, filler: str) -> dict[str, Any]:
    """One promoted row carrying a durable payload of the stated length."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "backend": BACKEND,
        "node": f"node-{index:04d}",
        "plan": "plan-a",
        "section": "s1",
        "gate": "passed",
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
    _forget_history()
    yield root
    _forget_history()


def _seed(scale: int = 1) -> int:
    """Write the prepared store, returning its payload total."""
    total = 0
    with run_store.RunStore(run_store.store_path()) as store:
        for index in range(ROWS):
            payload = _filler(scale, index)
            total += len(payload)
            store.append(
                PROJECT,
                _record(index, run_id=f"r-seed-{index:03d}", filler=payload),
            )
    return total


def _change_one_row(index: int = 0, scale: int = 1) -> None:
    """Rewrite one row in place through the store's own write path."""
    with run_store.RunStore(run_store.store_path()) as store:
        store.sync_run(
            PROJECT,
            _record(
                index,
                run_id=f"r-seed-{index:03d}",
                filler=_filler(scale, index),
            )
            | {"gate": "failed"},
        )


def _cursor() -> int:
    """The sequence number the project's signature has reached.

    An implementation whose signature carries no sequence number answers zero,
    which asks for the whole signature, as its readers ask for it.
    """
    signature = run_store.stored_run_signature(PROJECT) or ()
    return max(
        (int(row[14]) for row in signature if len(row) > 14),
        default=0,
    )


def _base_signature() -> list[tuple[Any, ...]]:
    """The signature as the base implementation read it, for comparison.

    The query is the one the payload-free table replaced: every row's encoded
    payload and detail lengths, read out of the runs table itself.
    """
    target = run_store.store_path()
    connection = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
    try:
        archive = run_store._archive_path(target)
        if archive.is_file():
            connection.execute(
                "ATTACH DATABASE ? AS archive",
                (f"{archive.resolve().as_uri()}?mode=ro",),
            )
        selected = ", ".join(
            f'r."{name}"' for name in (*run_store.QUERY_KEYS, run_store.ROW_REVISION)
        )
        rows: list[tuple[Any, ...]] = []
        for schema in ("main", "archive"):
            try:
                found = connection.execute(
                    f'SELECT r."run_id", r.rowid, LENGTH(r."payload"), '  # noqa: S608 - fixed names
                    "LENGTH(COALESCE(d.\"detail\", '')), "
                    f'{selected}, r."activity_seconds" '
                    f'FROM "{schema}"."runs" r '
                    f'LEFT JOIN "{schema}"."run_details" d '
                    'ON d."run_id" = r."run_id" '
                    f'WHERE r."project" = ? ORDER BY r.rowid',
                    (PROJECT,),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            rows.extend((str(row[0]), schema, *row[1:]) for row in found)
        return rows
    finally:
        connection.close()


def _signature_rows() -> dict[str, tuple[Any, ...]]:
    """Each present row's whole signature, keyed by run id.

    The last field is the sequence number the row was written at, which is what
    moves when a row is rewritten in place with an answer of the same size.
    """
    return {
        str(row[0]): tuple(row)
        for row in (run_store.stored_run_signature(PROJECT) or ())
        if len(row) <= 13 or row[13]
    }


def _signature_values() -> dict[str, tuple[Any, ...]]:
    """Each present row's signature as the base query read it.

    The base query answered from the rows themselves, so it carried neither the
    present flag nor the sequence number the payload-free table adds.
    """
    return {run_id: row[:-2] for run_id, row in _signature_rows().items()}


def test_a_read_after_one_row_changes_costs_less_than_the_payloads(
    crew_home: Path,
) -> None:
    """Both readers cost a bounded amount after a single row changes.

    The bound is stated against the store's payload total, so the first case
    fails at the base revision with the signature read costing more than the
    whole store: that is this file's negative control.
    """
    root = crew_home
    payload_total = _seed()
    assert payload_total > 200 * 1024 * 1024, payload_total

    # Warm the picker, and record where the signature had reached.
    ledger.picker_runs(PROJECT, root=root)
    cursor = _cursor()

    _change_one_row()

    signature_bytes, signature = _measure(
        lambda: run_store.stored_run_signature(PROJECT)
    )
    assert signature, "the signature read answered nothing"
    assert signature_bytes < READ_BOUND, (
        f"the signature read cost {signature_bytes} bytes against a payload "
        f"total of {payload_total}"
    )

    picker_bytes, rows = _measure(lambda: ledger.picker_runs(PROJECT, root=root))
    assert len(rows) == ROWS
    assert picker_bytes < READ_BOUND, (
        f"the picker read cost {picker_bytes} bytes against a payload total of "
        f"{payload_total}"
    )

    # The rows the statements returned, from the store's own counters.
    run_store.reset_statement_counters()
    delta = run_store.stored_run_signature(PROJECT, since=cursor)
    counters = run_store.statement_counters()
    assert [str(row[0]) for row in delta] == ["r-seed-000"]
    assert counters["signature_rows"] <= 1, counters

    _change_one_row(1)
    run_store.reset_statement_counters()
    updated = ledger.picker_runs(PROJECT, root=root)
    counters = run_store.statement_counters()
    assert {row["run_id"]: row["gate"] for row in updated}["r-seed-001"] == "failed"
    assert counters["decoded_rows"] <= 1, counters


def test_doubling_every_payload_leaves_both_counts_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reads are bounded by what changed, not by how large the rows are.

    A second, identical population with every payload doubled must cost the
    same within a tenth, for both the bytes read and the rows touched.
    """
    measured: dict[int, tuple[int, int, int, int, int]] = {}
    for scale in (1, 2):
        monkeypatch.setenv("RECKON_HOME", str(tmp_path / f"home-{scale}"))
        monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / f"cache-{scale}"))
        root = tmp_path / f"repo-{scale}"
        root.mkdir()
        ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
        _forget_history()
        _seed(scale)
        ledger.picker_runs(PROJECT, root=root)
        cursor = _cursor()
        _change_one_row(scale=scale)

        run_store.reset_statement_counters()
        signature_bytes, _signature = _measure(
            lambda cursor=cursor: run_store.stored_run_signature(PROJECT, since=cursor)
        )
        signature_rows = run_store.statement_counters()["signature_rows"]

        # The read that takes the change decodes the one changed row, so its
        # bytes carry that row's payload and scale with it. The steadiness is
        # measured on the repeat, which is the read a station takes over a
        # store that stands still.
        first_bytes, _rows = _measure(
            lambda root=root: ledger.picker_runs(PROJECT, root=root)
        )
        run_store.reset_statement_counters()
        repeat_bytes, _rows = _measure(
            lambda root=root: ledger.picker_runs(PROJECT, root=root)
        )
        decoded_rows = run_store.statement_counters()["decoded_rows"]
        measured[scale] = (
            signature_bytes,
            signature_rows,
            repeat_bytes,
            decoded_rows,
            first_bytes,
        )
        _forget_history()

    small, large = measured[1], measured[2]
    for name, one, two in (
        ("signature bytes", small[0], large[0]),
        ("signature rows", small[1], large[1]),
        ("repeat picker bytes", small[2], large[2]),
        ("decoded rows", small[3], large[3]),
    ):
        if one == two:
            continue
        allowed = max(one, two) * 0.10
        assert abs(one - two) <= allowed, f"{name}: {one} became {two}"
    # The read that takes the picker's change costs about one row at either
    # size, and never the store's payload total.
    for scale, first_bytes in ((1, small[4]), (2, large[4])):
        assert first_bytes < scale * PAYLOAD_BYTES + READ_BOUND, (scale, first_bytes)


def test_the_signature_moves_on_a_rewrite_an_archive_and_a_delete(
    crew_home: Path,
) -> None:
    """The signature changes for each way a row's answer changes, and no other."""
    _seed(scale=1)
    first = _signature_rows()
    assert len(first) == ROWS
    assert _signature_rows() == first, "an untouched store moved"

    # A rewrite in place, through the store's own write path.
    _change_one_row(0)
    rewritten = _signature_rows()
    assert rewritten != first
    assert {row for row_id, row in rewritten.items() if row_id != "r-seed-000"} == {
        row for row_id, row in first.items() if row_id != "r-seed-000"
    }

    # An archive moves the row to the retired history.
    with run_store.RunStore(run_store.store_path()) as store:
        retired = store.retire(
            before=NOW - timedelta(minutes=10),
            protected=set(),
            projects={PROJECT},
            apply=True,
        )["retired"]
    assert retired, "no row was archived"
    archived = _signature_rows()
    assert archived != rewritten
    assert archived[retired[0]][1] == "archive"

    # A delete removes the row from the signature's present rows.
    with run_store.RunStore(run_store.store_path()) as store:
        assert store.delete_run(retired[-1])
    deleted = _signature_rows()
    assert retired[-1] not in deleted
    assert set(deleted) == set(archived) - {retired[-1]}


def test_both_reads_answer_what_the_base_implementation_answers(
    crew_home: Path,
) -> None:
    """Every state answers the base query's values, and the picker its rows.

    The comparison is made over the sequence of states a row goes through, so
    a signature or a projection that only its own earlier answer agrees with
    fails here rather than changing the picker's input in silence.
    """
    root = crew_home
    _seed(scale=1)

    def compare() -> None:
        assert _signature_values() == {
            str(row[0]): tuple(row) for row in _base_signature()
        }
        rows = ledger.picker_runs(PROJECT, root=root)
        held, _version = ledger.load(PROJECT, root, picker_only=True)
        assert rows == held["runs"]

    compare()
    assert _signature_values() == {str(row[0]): tuple(row) for row in _base_signature()}

    _change_one_row(2)
    compare()

    with run_store.RunStore(run_store.store_path()) as store:
        terminated = store.retire(
            before=NOW - timedelta(minutes=20),
            protected=set(),
            projects={PROJECT},
            apply=True,
        )["retired"]
    assert terminated
    compare()

    with run_store.RunStore(run_store.store_path()) as store:
        assert store.delete_run(terminated[0])
    compare()
