"""Appending a run owns one immutable file; the index follows that record."""

from __future__ import annotations

import json
import multiprocessing
import subprocess
from pathlib import Path

import pytest

from reckon import ledger, run_store

PROJECT = "reckon"


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(root / "docs" / "state"))
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "index.db"))
    (state / "index.json").write_text("{}\n")
    (state / "crew.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "data": {
                    "_version": 7,
                    "members": [{"id": "existing-member"}],
                    "holds": [{"id": "existing-hold"}],
                    "runs": [{"run_id": "r-historical", "outcome": "retained"}],
                },
            }
        )
        + "\n"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", "docs/state/reckon/index.json", "docs/state/reckon/crew.json")
    _git(
        root, "commit", "-qm", "chore: seed fixture", "-m", "Record the initial state."
    )
    with run_store.RunStore():
        pass
    return root


def _append_in_process(root, run_id, barrier, results):
    """Record actual file opens and attempts in independently scheduled writers."""
    original_open = Path.open
    original_append = run_store.append
    opened = []
    index_attempts = []

    def observed_open(path, mode="r", *args, **kwargs):
        if path.parent == ledger.run_path(PROJECT, run_id, root).parent:
            opened.append((path.name, mode))
        return original_open(path, mode, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("append must not rewrite the ledger or retry")

    def observed_index(project, record):
        index_attempts.append(record["run_id"])
        assert ledger.run_path(project, run_id, root).is_file()
        return original_append(project, record)

    Path.open = observed_open
    ledger.write = forbidden
    ledger._retry_backoff = forbidden
    run_store.append = observed_index
    try:
        barrier.wait(timeout=10)
        result = ledger.append_run(PROJECT, {"run_id": run_id}, root=root)
        results.put({"result": result, "opened": opened, "index": index_attempts})
    except (ledger.LedgerError, AssertionError) as exc:
        results.put({"error": f"{type(exc).__name__}: {exc}"})


@pytest.mark.parametrize("same_id", [False, True])
def test_concurrent_appends_keep_crew_bytes_untouched(repository, same_id):
    aggregate = ledger.ledger_path(PROJECT, repository)
    before = aggregate.read_bytes()
    head = _git(repository, "rev-parse", "HEAD")
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    ids = ["r-alpha", "r-alpha" if same_id else "r-beta"]
    processes = [
        context.Process(
            target=_append_in_process,
            args=(repository, run_id, barrier, results),
        )
        for run_id in ids
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=20)
            assert not process.is_alive(), "bounded append worker did not finish"
            assert process.exitcode == 0
        receipts = [results.get(timeout=2) for _ in processes]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()

    assert aggregate.read_bytes() == before, "crew.json bytes changed during append"
    successes = [receipt for receipt in receipts if "result" in receipt]
    assert len(successes) == (1 if same_id else 2), receipts
    for receipt in successes:
        result = receipt["result"]
        run_id = result["run"]["run_id"]
        assert receipt["opened"] == [(f"{run_id}.json", "x")]
        assert receipt["index"] == [run_id]
        assert result["store"] == {"status": "written"}
    if same_id:
        refusal = next(receipt["error"] for receipt in receipts if "error" in receipt)
        assert str(ledger.run_path(PROJECT, "r-alpha", repository)) in refusal
        print(refusal)
    assert {
        path.stem for path in aggregate.parent.joinpath("runs").glob("*.json")
    } == set(ids)
    assert _git(repository, "rev-parse", "HEAD") == head
    assert not _git(repository, "diff", "--cached", "--name-only")


def test_append_preserves_complete_record_and_writes_index_after_file(
    repository, monkeypatch
):
    row = {
        "run_id": "r-complete",
        "outcome": "mesuré",
        "node_definition": {"goal": "retain full detail"},
        "budget": {"tokens": {"input": 123}},
        "store_write": {"status": "obsolete"},
    }
    expected = {key: value for key, value in row.items() if key != "store_write"}
    target = ledger.run_path(PROJECT, row["run_id"], repository)
    attempts = []
    original = run_store.append

    def check_order(project, record):
        attempts.append(record)
        assert target.read_text() == ledger.serialize_run(expected)
        return original(project, record)

    monkeypatch.setattr(run_store, "append", check_order)
    result = ledger.append_run(PROJECT, row, root=repository, attempts=99)
    assert result["path"] == str(target)
    assert result["run"] == expected
    assert result["store"] == {"status": "written"}
    assert attempts == [expected]
    assert "store_write" in row, "caller-owned record was mutated"


def test_append_leaves_crew_bytes_untouched(repository):
    aggregate = ledger.ledger_path(PROJECT, repository)
    before = aggregate.read_bytes()
    result = ledger.append_run(PROJECT, {"run_id": "r-single"}, root=repository)
    assert aggregate.read_bytes() == before, "crew.json bytes changed during append"
    target = ledger.run_path(PROJECT, "r-single", repository)
    assert target.read_text() == ledger.serialize_run({"run_id": "r-single"})
    assert result["path"] == str(target)


@pytest.mark.parametrize("kind", ["file", "directory", "dangling-symlink"])
def test_existing_target_is_refused_without_reading_other_records(
    repository, monkeypatch, kind
):
    target = ledger.run_path(PROJECT, "r-existing", repository)
    target.parent.mkdir()
    if kind == "file":
        target.write_text("existing bytes")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(target.parent / "missing-target")

    def forbidden(*args, **kwargs):
        raise AssertionError("duplicate must be refused before reading or indexing")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(run_store, "append", forbidden)
    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.append_run(PROJECT, {"run_id": "r-existing"}, root=repository)
    assert str(target) in str(excinfo.value)
    print(str(excinfo.value))


def test_aggregate_duplicate_is_refused_and_names_crew_file(repository, monkeypatch):
    aggregate = ledger.ledger_path(PROJECT, repository)
    before = aggregate.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("refused append must not update the index")

    monkeypatch.setattr(run_store, "append", forbidden)
    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.append_run(PROJECT, {"run_id": "r-historical"}, root=repository)
    assert str(aggregate) in str(excinfo.value)
    assert "r-historical" in str(excinfo.value)
    assert aggregate.read_bytes() == before
    assert not ledger.run_path(PROJECT, "r-historical", repository).exists()
    print(str(excinfo.value))


def test_nested_id_hit_does_not_refuse_append(repository, monkeypatch):
    aggregate = ledger.ledger_path(PROJECT, repository)
    envelope = json.loads(aggregate.read_bytes())
    envelope["data"]["runs"][0]["detail"] = {"run_id": "r-nested"}
    aggregate.write_text(json.dumps(envelope))
    before = aggregate.read_bytes()
    original = ledger.json.loads
    parses = []

    def observed(raw, *args, **kwargs):
        if raw == before:
            parses.append(raw)
        return original(raw, *args, **kwargs)

    monkeypatch.setattr(ledger.json, "loads", observed)
    result = ledger.append_run(PROJECT, {"run_id": "r-nested"}, root=repository)
    assert result["run"]["run_id"] == "r-nested"
    assert parses == [before]
    assert aggregate.read_bytes() == before


def test_byte_scan_miss_never_parses_aggregate_or_opens_other_run(
    repository, monkeypatch
):
    other = ledger.run_path(PROJECT, "r-unrelated", repository)
    other.parent.mkdir()
    other.write_text("deliberately unreadable JSON")
    original_open = Path.open
    opened = []

    def observed_open(path, mode="r", *args, **kwargs):
        assert path != other, "append opened another run's file"
        if path.parent == other.parent:
            opened.append((path.name, mode))
        return original_open(path, mode, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("a byte scan miss must not decode the ledger")

    monkeypatch.setattr(Path, "open", observed_open)
    monkeypatch.setattr(ledger.json, "loads", forbidden)
    monkeypatch.setattr(run_store, "append", lambda *args: None)
    ledger.append_run(PROJECT, {"run_id": "r-independent"}, root=repository)
    assert opened == [("r-independent.json", "x")]


def test_index_lag_observes_failed_insert_without_rebuilding(repository, monkeypatch):
    assert ledger.index_lag(PROJECT, repository) == 0
    row = {"run_id": "r-index-failure", "node_definition": {"goal": "retained"}}
    target = ledger.run_path(PROJECT, row["run_id"], repository)
    calls = []

    def failed(project, record):
        calls.append(record)
        assert target.read_text() == ledger.serialize_run(row)
        raise RuntimeError("deliberate index failure")

    with monkeypatch.context() as patch:
        patch.setattr(run_store, "append", failed)
        result = ledger.append_run(PROJECT, row, root=repository)
    assert calls == [row]
    assert result["store"] == {
        "status": "failed",
        "error": "RuntimeError: deliberate index failure",
    }
    assert result["run"] == row
    before = run_store.store_path().read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("observing lag must not load records or rebuild the index")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "load", forbidden)
        patch.setattr(run_store.RunStore, "_refresh", forbidden)
        patch.setattr(Path, "read_text", forbidden)
        assert ledger.index_lag(PROJECT, repository) == 1
        assert ledger.index_lag(PROJECT, repository) == 1
    assert run_store.store_path().read_bytes() == before
    assert target.read_text() == ledger.serialize_run(row)
    with run_store.RunStore(root=repository) as store:
        assert store.get_run(row["run_id"])["run_id"] == row["run_id"]
    assert ledger.index_lag(PROJECT, repository) == 0


def test_no_index_is_reported_without_creating_one(repository, monkeypatch):
    absent = repository / "absent" / "index.db"
    monkeypatch.setenv("RECKON_RUN_STORE", str(absent))
    assert ledger.index_lag(PROJECT, repository) == "no index"
    assert not absent.parent.exists()


def test_index_lag_is_scoped_to_the_project(repository):
    path = ledger.run_path(PROJECT, "r-owned", repository)
    path.parent.mkdir()
    path.write_text(ledger.serialize_run({"run_id": "r-owned"}))
    run_store.append("another-project", {"run_id": "r-owned"})
    assert ledger.index_lag(PROJECT, repository) == 1
    assert ledger.index_lag("another-project", repository) == 0


def test_invalid_record_does_not_leave_a_target_file(repository):
    with pytest.raises(TypeError):
        ledger.append_run(
            PROJECT, {"run_id": "r-invalid", "detail": object()}, root=repository
        )
    assert not ledger.run_path(PROJECT, "r-invalid", repository).exists()


@pytest.mark.parametrize("run_id", ["../elsewhere", "a/b", ""])
def test_invalid_id_cannot_escape_run_directory(repository, run_id):
    with pytest.raises(ledger.LedgerError):
        ledger.append_run(PROJECT, {"run_id": run_id}, root=repository)
