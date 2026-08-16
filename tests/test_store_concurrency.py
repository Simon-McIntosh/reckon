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
    barrier = threading.Barrier(count)
    thread_state = threading.local()
    real_load = ledger.load

    def synchronized_first_load(project, load_root=None):
        result = real_load(project, load_root)
        if not getattr(thread_state, "synchronized", False):
            thread_state.synchronized = True
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(ledger, "load", synchronized_first_load)
    records = [
        ledger.build_record(
            run_id=f"run-{index}", plan="concurrent-work", gate="passed"
        )
        for index in range(count)
    ]
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [
            pool.submit(ledger.append_run, PROJECT, record, root=root)
            for record in records
        ]
    results = [future.result(timeout=10) for future in futures]
    monkeypatch.setattr(ledger, "load", real_load)
    return results


def test_two_simultaneous_writers_land_consecutive_versions(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository(tmp_path, monkeypatch)

    results = _simultaneous_appends(root, monkeypatch, 2)
    data, version = ledger.load(PROJECT, root)

    assert sorted(result["version"] for result in results) == [1, 2]
    assert version == 2
    assert {record["run_id"] for record in data["runs"]} == {"run-0", "run-1"}


def test_contention_beyond_five_racers_preserves_every_run(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository(tmp_path, monkeypatch)
    racers = 8

    results = _simultaneous_appends(root, monkeypatch, racers)
    data, version = ledger.load(PROJECT, root)

    assert sorted(result["version"] for result in results) == list(range(1, racers + 1))
    assert version == racers
    assert {record["run_id"] for record in data["runs"]} == {
        f"run-{index}" for index in range(racers)
    }
