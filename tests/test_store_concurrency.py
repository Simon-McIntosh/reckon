"""Concurrency checks for versioned JSON envelope writers."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reckon import ledger


PROJECT = "proj"


def _repository(tmp_path: Path, monkeypatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def _simultaneous_appends(
    root: Path, monkeypatch, count: int
) -> list[dict[str, object]]:
    """Race ``count`` appends to the point where they create their run files.

    Each append serialises its own record before it opens its target, so the
    barrier lands every writer at that same point and they contend on the
    exclusive file creation that is the one shared step of an append.
    """

    barrier = threading.Barrier(count)
    thread_state = threading.local()
    real_serialize = ledger.serialize_run

    def synchronized_serialize(record):
        text = real_serialize(record)
        if not getattr(thread_state, "synchronized", False):
            thread_state.synchronized = True
            barrier.wait(timeout=10)
        return text

    monkeypatch.setattr(ledger, "serialize_run", synchronized_serialize)
    records = [
        ledger.build_record(
            run_id=f"run-{index}", plan="concurrent-work", gate="passed"
        )
        for index in range(count)
    ]
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [
            pool.submit(
                ledger.append_run, PROJECT, record, root=root, allow_create=True
            )
            for record in records
        ]
    try:
        return [future.result(timeout=10) for future in futures]
    finally:
        monkeypatch.setattr(ledger, "serialize_run", real_serialize)


def test_two_simultaneous_writers_land_consecutive_versions(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository(tmp_path, monkeypatch)

    results = _simultaneous_appends(root, monkeypatch, 2)
    data, _version = ledger.load(PROJECT, root)

    # An append writes its own immutable run file and advances no aggregate
    # version, so the writers' guarantee is that each record lands once rather
    # than that they share one envelope revision.
    assert all(result["version"] is None for result in results)
    assert len({str(result["path"]) for result in results}) == 2
    assert all(Path(str(result["path"])).is_file() for result in results)
    assert {record["run_id"] for record in data["runs"]} == {"run-0", "run-1"}


def test_contention_beyond_five_racers_preserves_every_run(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository(tmp_path, monkeypatch)
    racers = 8

    results = _simultaneous_appends(root, monkeypatch, racers)
    data, _version = ledger.load(PROJECT, root)

    # Every racer owns a distinct immutable file and no record is overwritten,
    # so contention degrades to independent creates rather than a lost update.
    assert all(result["version"] is None for result in results)
    assert len({str(result["path"]) for result in results}) == racers
    assert all(Path(str(result["path"])).is_file() for result in results)
    assert {record["run_id"] for record in data["runs"]} == {
        f"run-{index}" for index in range(racers)
    }
