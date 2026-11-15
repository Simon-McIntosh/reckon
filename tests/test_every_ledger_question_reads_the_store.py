"""Every ledger reader answers from the run store once the documents are gone.

The per-run directory is the ledger's last file-shaped copy of history. Once a
project holds none, the run store under the ledger's config home is its only
store, so a reader that still walks the directory answers an empty ledger and
reports a project with no history rather than one whose history moved.

Each reader is measured twice over one population: once against the project's
own run documents, then with that directory removed and the store holding the
same rows. The two answers must agree, and the population is asserted nonzero
so an empty read has to announce itself rather than agree with an empty
directory.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime

import pytest

from reckon import ledger, run_store
from reckon.crew import open_run_index

PROJECT = "example"

READERS = (
    "load",
    "load_version",
    "runs",
    "records",
    "run_ids",
    "open_rows",
    "open_row",
    "open_has_row",
    "open_version",
)


def _rows() -> list[dict]:
    """Three promoted runs, each still owing its retained worktree."""
    return [
        {
            "run_id": f"r-2026-01-0{day}T000000Z-node-{day}",
            "project": PROJECT,
            "member": f"worker-{day}",
            "node": f"node-{day}",
            "role": "implement",
            "plan": "a-plan",
            "gate": "passed",
            "completed_at": f"2026-01-0{day}T01:00:00Z",
            "dispatched_at": f"2026-01-0{day}T00:10:00Z",
            "worktree_retention": True,
        }
        for day in (1, 2, 4)
    ]


def _ids() -> list[str]:
    return [row["run_id"] for row in _rows()]


@pytest.fixture()
def aggregate(tmp_path, monkeypatch):
    """Isolate the config home and return the aggregate's path."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.delenv("RECKON_MOUNTS_PATH", raising=False)
    return ledger.ledger_path(PROJECT)


def write_aggregate() -> None:
    """Write the aggregate naming the roster, holding no run rows of its own."""
    path = ledger.ledger_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "data": {
                    "_version": 7,
                    "members": [{"id": "member"}],
                    "holds": [{"id": "hold"}],
                }
            }
        )
    )


def write_documents(rows: list[dict]):
    """Write each run beside the aggregate, and return the directory."""
    directory = ledger.ledger_path(PROJECT).parent / "runs"
    directory.mkdir(parents=True, exist_ok=True)
    for row in rows:
        (directory / f"{row['run_id']}.json").write_text(ledger.serialize_run(row))
    return directory


def fill_store(rows: list[dict]) -> None:
    """Hold every row in the run store, the way a promotion does."""
    with run_store.RunStore() as store:
        for row in rows:
            store.append(PROJECT, row)


def reset_open_index() -> None:
    """Drop the operational cache so its answer is rebuilt from its sources."""
    path = open_run_index.index_path(PROJECT)
    if path.exists():
        path.unlink()


def answer(reader: str):
    if reader == "load":
        return ledger.load(PROJECT)[0]["runs"]
    if reader == "load_version":
        return ledger.load(PROJECT)[1]
    if reader == "runs":
        return ledger.runs(PROJECT)
    if reader == "records":
        return ledger.read_records(PROJECT, with_figures=False)[0]
    if reader == "run_ids":
        return sorted(ledger.run_ids(PROJECT))
    if reader == "open_rows":
        return open_run_index.read_rows(PROJECT)
    if reader == "open_row":
        return [open_run_index.row_for_run(PROJECT, run_id) for run_id in _ids()]
    if reader == "open_has_row":
        return [open_run_index.has_row(PROJECT, run_id) for run_id in _ids()]
    if reader == "open_version":
        return open_run_index.ledger_version(PROJECT)
    raise AssertionError(f"unknown reader {reader!r}")


def assert_answered(reader: str, result) -> None:
    """The answer names the whole population, so an empty read cannot pass."""
    ids = _ids()
    if reader in {"load", "runs", "records", "open_rows"}:
        assert [row["run_id"] for row in result] == ids
    elif reader == "run_ids":
        assert result == sorted(ids)
    elif reader == "open_row":
        assert [row["run_id"] for row in result] == ids
    elif reader == "open_has_row":
        assert result == [True] * len(ids)
    elif reader in {"load_version", "open_version"}:
        assert result == 7
    else:  # pragma: no cover - guards the table against a reader going unchecked
        raise AssertionError(f"unknown reader {reader!r}")


@pytest.mark.parametrize("reader", READERS)
def test_a_reader_answers_the_store_when_the_documents_are_gone(aggregate, reader):
    rows = _rows()
    write_aggregate()
    directory = write_documents(rows)
    fill_store(rows)
    reset_open_index()

    from_documents = answer(reader)
    assert_answered(reader, from_documents)

    shutil.rmtree(directory)
    assert not directory.exists(), "the absent-runs-directory case was not reached"
    assert not ledger.run_path(PROJECT, rows[0]["run_id"]).exists()
    reset_open_index()

    assert answer(reader) == from_documents


@pytest.mark.parametrize("reader", ["load", "runs", "open_row", "run_ids"])
def test_the_store_answers_with_no_aggregate(aggregate, reader):
    fill_store(_rows())
    assert not aggregate.exists()
    reset_open_index()
    result = answer(reader)
    if reader == "run_ids":
        assert result == sorted(_ids())
        return
    assert [row["run_id"] for row in result] == _ids()


def test_run_ids_decodes_no_stored_payload(aggregate, monkeypatch):
    """Membership is the store's id column, so answering it parses no record.

    A station tick asks this once per delivered run, so ``run_ids`` reading the
    id column is the whole point. The instrument is the store's JSON parser: any
    payload decode raises, and the answer must still name every run the store
    holds, including one no document names.
    """
    rows = _rows()
    extra = {
        "run_id": "r-2026-01-05T000000Z-node-5",
        "project": PROJECT,
        "member": "worker-5",
        "node": "node-5",
        "completed_at": "2026-01-05T01:00:00Z",
    }
    write_aggregate()
    write_documents(rows)
    ledger.load(PROJECT)
    fill_store([*rows, extra])

    class NoDecode:
        def __init__(self, module):
            self._module = module

        def __getattr__(self, name):
            return getattr(self._module, name)

        def loads(self, *args, **kwargs):
            raise AssertionError("run_ids decoded a stored payload")

    monkeypatch.setattr(run_store, "json", NoDecode(json))
    assert ledger.run_ids(PROJECT) == {*_ids(), extra["run_id"]}


def test_one_document_beside_many_store_rows_answers_both(aggregate):
    """The documents cover the store rather than replacing it, run id by run id.

    A half-migrated project holds one surviving document and many stored runs.
    Choosing a source for the whole project would answer only the document, so
    the surviving row must not hide the store's rows — and for the run id they
    both carry, the document is the fuller record and wins.
    """
    rows = _rows()
    stored = [{**row, "member": "store-member"} for row in rows]
    surviving = {**rows[0], "member": "document-member"}
    write_aggregate()
    write_documents([surviving])
    fill_store(stored)

    data, _version = ledger.load(PROJECT)
    answered = {row["run_id"]: row for row in data["runs"]}
    assert sorted(answered) == _ids()
    assert answered[surviving["run_id"]]["member"] == "document-member"
    assert ledger.run_ids(PROJECT) == set(_ids())


def test_a_retired_row_still_answers_with_its_detail(aggregate):
    """Retirement moves a run out of the hot store, not out of its history.

    The retired row lives in the store's own attached archive, so a history
    reader must reach it there — and reach its detail with it, since a
    retirement is not a rotation and drops no wide field.
    """
    rows = _rows()
    write_aggregate()
    fill_store(rows)
    with run_store.RunStore() as store:
        store.retire(
            before=datetime(2026, 2, 1, tzinfo=UTC),
            protected=set(),
            projects={PROJECT},
            apply=True,
        )
        assert store.archive_path.is_file()

    data, _version = ledger.load(PROJECT)
    by_id = {row["run_id"]: row for row in data["runs"]}
    assert sorted(by_id) == _ids()
    assert all(row["worktree_retention"] is True for row in by_id.values())
    assert ledger.run_ids(PROJECT) == set(_ids())


def test_a_rotated_detail_still_answers_the_durable_half(aggregate):
    """A rotation washes detail out of the store, so the two arms are not equal.

    The durable half of a record is what the store keeps permanently; its wide
    detail may be removed once a derived value exists. A reader answered by the
    store must then name the run and its durable fields with the detail absent,
    which is exactly the answer a lossless fixture cannot distinguish.
    """
    rows = _rows()
    write_aggregate()
    write_documents(rows)
    fill_store(rows)

    from_documents = {row["run_id"]: row for row in ledger.load(PROJECT)[0]["runs"]}
    rotated = rows[1]["run_id"]
    assert from_documents[rotated]["worktree_retention"] is True

    shutil.rmtree(ledger.ledger_path(PROJECT).parent / "runs")
    reset_open_index()
    connection = sqlite3.connect(run_store.store_path())
    try:
        with connection:
            connection.execute("DELETE FROM run_details WHERE run_id = ?", (rotated,))
    finally:
        connection.close()

    answered = {row["run_id"]: row for row in ledger.load(PROJECT)[0]["runs"]}
    assert sorted(answered) == _ids()
    assert answered[rotated]["member"] == rows[1]["member"]
    assert "worktree_retention" not in answered[rotated]
    assert ledger.run_ids(PROJECT) == set(_ids())


def test_a_load_decodes_no_other_project_payload(aggregate, monkeypatch):
    """One project's load pays for one project's rows, not the whole store.

    One config home serves every project on the machine, so a read that decodes
    the whole store makes every load pay for every project's history, on a path
    a station tick takes. The instrument is the store's JSON parser: a payload
    belonging to another project fails the read the moment it is decoded.
    """
    write_aggregate()
    write_documents(_rows())
    fill_store(_rows())
    with run_store.RunStore() as store:
        store.append(
            "another",
            {
                "run_id": "r-2026-01-06T000000Z-node-6",
                "project": "another",
                "member": "another-projects-member",
                "node": "node-6",
                "completed_at": "2026-01-06T01:00:00Z",
            },
        )

    class NoForeignPayload:
        def __init__(self, module):
            self._module = module

        def __getattr__(self, name):
            return getattr(self._module, name)

        def loads(self, text, *args, **kwargs):
            if "another-projects-member" in str(text):
                raise AssertionError("the load decoded another project's payload")
            return self._module.loads(text, *args, **kwargs)

    monkeypatch.setattr(run_store, "json", NoForeignPayload(json))
    data, _version = ledger.load(PROJECT)
    assert sorted(row["run_id"] for row in data["runs"]) == _ids()
    assert all(row.get("project") == PROJECT for row in data["runs"])


def test_a_load_against_an_absent_store_creates_nothing(aggregate):
    """A read may not create the store it reads.

    The store's constructor creates the file, its directory and its schema, so a
    reader that went through it turned a station tick into a write. Absent means
    empty here, and the path must still be absent afterwards.
    """
    write_aggregate()
    write_documents(_rows())
    store = run_store.store_path()
    assert not store.exists()
    home = store.parent
    assert not home.exists()

    data, _version = ledger.load(PROJECT)
    assert [row["run_id"] for row in data["runs"]] == _ids()
    assert not store.exists(), "the read created the store"
    assert not store.parent.exists(), "the read created the store's directory"


def test_a_store_deleted_and_rebuilt_returns_an_equal_answer_set(aggregate):
    fill_store(_rows())

    first = ledger.load(PROJECT)[0]["runs"]
    assert [row["run_id"] for row in first] == _ids()

    run_store.store_path().unlink()
    assert ledger.load(PROJECT)[0]["runs"] == [], (
        "the rows survived the store's deletion, so the read was not answered by it"
    )

    fill_store(first)
    assert ledger.load(PROJECT)[0]["runs"] == first
