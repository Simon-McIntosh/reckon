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