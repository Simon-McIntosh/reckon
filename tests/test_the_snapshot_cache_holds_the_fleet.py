"""The watch snapshot cache holds a whole fleet and trims without thrashing."""

from __future__ import annotations

from pathlib import Path

from reckon.crew import recovery_watch, runs


def _count_directory_checks(monkeypatch, live: set[str]) -> list[str]:
    asked: list[str] = []

    class _Directory:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        def is_dir(self) -> bool:
            asked.append(self.run_id)
            return self.run_id in live

    monkeypatch.setattr(runs, "run_dir", _Directory)
    return asked


def test_a_fleet_larger_than_the_former_bound_keeps_every_snapshot(
    monkeypatch,
) -> None:
    fleet = [f"r-{index:04d}" for index in range(430)]
    asked = _count_directory_checks(monkeypatch, set(fleet))
    cache: dict = {}

    for _poll in range(3):
        for run_id in fleet:
            recovery_watch._remember_snapshot(
                run_id, "key", {"id": run_id}, cache=cache
            )

    assert set(cache) == set(fleet)
    assert asked == []


def test_a_trim_is_paid_once_per_margin_not_once_per_store(monkeypatch) -> None:
    limit = 64
    monkeypatch.setattr(recovery_watch, "_SNAPSHOT_CACHE_LIMIT", limit)
    fleet = [f"r-{index:04d}" for index in range(4 * limit)]
    asked = _count_directory_checks(monkeypatch, set(fleet))
    cache: dict = {}

    for run_id in fleet:
        recovery_watch._remember_snapshot(run_id, "key", {"id": run_id}, cache=cache)

    stores_past_the_bound = len(fleet) - limit
    margin = limit // recovery_watch._SNAPSHOT_CACHE_TRIM
    trims = stores_past_the_bound // margin + 1
    assert len(cache) <= limit
    assert fleet[-1] in cache
    assert len(asked) <= trims * limit
    assert len(asked) < stores_past_the_bound * limit // 2


def test_a_run_whose_directory_has_gone_is_dropped_first(monkeypatch) -> None:
    limit = 16
    monkeypatch.setattr(recovery_watch, "_SNAPSHOT_CACHE_LIMIT", limit)
    gone = [f"gone-{index}" for index in range(limit // 2)]
    kept = [f"live-{index}" for index in range(limit // 2 + 1)]
    _count_directory_checks(monkeypatch, set(kept))
    cache: dict = {}

    for run_id in [*kept[:-1], *gone, kept[-1]]:
        recovery_watch._remember_snapshot(run_id, "key", {"id": run_id}, cache=cache)

    assert not set(gone) & set(cache)
    assert set(kept) <= set(cache)


def test_the_module_keeps_its_bound_above_a_working_fleet() -> None:
    assert recovery_watch._SNAPSHOT_CACHE_LIMIT >= 2048
    assert Path(recovery_watch.__file__).name == "recovery_watch.py"
