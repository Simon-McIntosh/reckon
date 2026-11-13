"""A review store's open-work selection is read once per listing, not per file.

When open work selects a subset of a review store, the station's classify stage
re-validates that subset through ``_update_store_entries``. Selecting the subset
costs a read of every live pointer, an index query per open run and a stat of
every selected file, so taking it again for each file it names makes one
refresh quadratic in the store's size. These cases count the selections.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import open_run_index, review_records

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
def counted_selection(monkeypatch: pytest.MonkeyPatch):
    calls: list[Path] = []

    def select(directory: Path) -> list[Path]:
        calls.append(directory)
        return sorted(directory.glob("*.json"))

    monkeypatch.setattr(open_run_index, "review_paths", select)
    return calls


def test_one_refresh_selects_the_store_once_inside_a_tick(tmp_path, counted_selection):
    directory = _store(tmp_path)
    with review_records.review_store_tick():
        entries = review_records._update_store_entries(directory, [])
    assert len(entries) == FILES
    assert len(counted_selection) == 1


def test_one_refresh_selects_the_store_once_outside_a_tick(tmp_path, counted_selection):
    directory = _store(tmp_path)
    entries = review_records._update_store_entries(directory, [])
    assert len(entries) == FILES
    assert len(counted_selection) == 1


def test_a_tick_reuses_its_selection_across_refreshes(tmp_path, counted_selection):
    directory = _store(tmp_path)
    with review_records.review_store_tick():
        first = review_records._update_store_entries(directory, [])
        second = review_records._update_store_entries(directory, first)
    assert len(second) == FILES
    assert len(counted_selection) == 1


def test_a_new_tick_selects_again(tmp_path, counted_selection):
    directory = _store(tmp_path)
    with review_records.review_store_tick():
        review_records._update_store_entries(directory, [])
    (directory / "r-new.json").write_text(
        json.dumps({"reviewed_run_id": "r-new", "verdict": "ok"}), encoding="utf-8"
    )
    with review_records.review_store_tick():
        entries = review_records._update_store_entries(directory, [])
    assert {entry["name"] for entry in entries} >= {"r-new.json"}
    assert len(counted_selection) == 2
