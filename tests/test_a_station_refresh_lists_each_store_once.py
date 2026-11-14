"""A store rebuild, a revalidation and a refresh's duty pass each list a store once.

The station's per-project refresh asks every terminal run whether it still owes
its coordinator something, and each answer reads the review store. Two costs
compounded there. A from-scratch rebuild and a revalidation stat-ed each record
through ``_record_stat``, which re-takes the store's open-work selection for
every file, so one rebuild was quadratic in the store's size. And the duty pass
ran outside any store scope, so every run repeated that rebuild. These cases
count the selections and observe the scope.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import open_run_index, review_records, station_subscriber

FILES = 12


def _store(tmp_path: Path) -> Path:
    directory = tmp_path / "reviews" / "proj"
    directory.mkdir(parents=True)
    for index in range(FILES):
        (directory / f"r-{index:03d}.json").write_text(
            json.dumps({"reviewed_run_id": f"r-{index:03d}", "verdict": "ok"}),
            encoding="utf-8",
        )
    return directory


@pytest.fixture
def counted_selection(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def select(directory: Path) -> list[Path]:
        calls.append(directory)
        return sorted(directory.glob("*.json"))

    monkeypatch.setattr(open_run_index, "review_paths", select)
    return calls


def test_a_rebuild_selects_the_store_once(tmp_path, counted_selection):
    directory = _store(tmp_path)
    entries = review_records._build_store_entries(directory)
    assert len(entries) == FILES
    assert len(counted_selection) == 1


def test_a_revalidation_selects_each_store_once(tmp_path, counted_selection):
    directory = _store(tmp_path)
    entries = review_records._build_store_entries(directory)
    counted_selection.clear()
    refreshed = review_records._revalidated_entries(entries)
    assert len(refreshed) == FILES
    assert len(counted_selection) == 1


def test_a_revalidation_drops_a_removed_record(tmp_path, counted_selection):
    directory = _store(tmp_path)
    entries = review_records._build_store_entries(directory)
    (directory / "r-000.json").unlink()
    refreshed = review_records._revalidated_entries(entries)
    assert {entry["name"] for entry in refreshed} == {
        f"r-{index:03d}.json" for index in range(1, FILES)
    }


def test_a_duty_pass_shares_one_store_scope(monkeypatch: pytest.MonkeyPatch):
    from reckon.crew import obligations

    scopes: list[object] = []

    def owes(pointer, **_kwargs):
        scopes.append(review_records._TICK_REVIEW_STORES.get())
        return False

    monkeypatch.setattr(station_subscriber, "run_owes_its_coordinator", owes)
    monkeypatch.setattr(station_subscriber, "_review_grace", lambda project: 0)
    monkeypatch.setattr(obligations, "_live_review_runs", lambda *pair: set())
    pointers = [
        {"run_id": f"r-{index}", "phase": "complete", "project": "p", "session": "s"}
        for index in range(3)
    ]
    station_subscriber.live_work_populations(pointers)
    assert len(scopes) == 3
    assert all(scope is not None for scope in scopes)
    assert len({id(scope) for scope in scopes}) == 1
    assert review_records._TICK_REVIEW_STORES.get() is None
