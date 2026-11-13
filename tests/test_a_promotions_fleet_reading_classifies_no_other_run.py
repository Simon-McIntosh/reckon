"""A promotion's fleet reading costs the same at any number of live runs.

The reading stamps a live-run count, an unreconciled count, the actionable
runs and the occupied lanes on the promotion result. Every one of those is a
fact about the fleet that the node's station already derives on its tick and
keeps in its store, so the reading takes them from there: one store read for
the whole fleet, however many runs are live. These tests hold that reading to
the answer the per-pointer composition gives, and count the files it opens
under other runs' directories — the cost the store read exists to remove.

The expected fleet fields are computed here with the composition the base
revision used, which this change leaves in place: ``runs.drain`` for the
unreconciled count and ``recovery.classify_pointer`` for each run's recovery
word. A reading taken from the store must agree with those numbers while
opening nothing to reach them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import (
    promotion,
    promotion_release,
    recovery,
    review,
    runs,
    station_store,
)
from reckon.crew.run_paths import crew_home
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "stored-fleet-project"
# The station's tick window, and a moment far enough past every seeded file's
# stamp that no run reads as stalled: a stalled run's word is the fleet
# reducer's own reading, and such a run is classified afresh on purpose.
STALL_SECONDS = 900

_OPENED: list[str] = []
_RECORDING = {"on": False}


def _record_open(event: str, args: tuple[Any, ...]) -> None:
    """Note every path the process opens while the counting instrument is on.

    Installed once, because an audit hook cannot be removed; the flag is what
    makes it an instrument for the block of work that arms it and nothing else.
    """
    if _RECORDING["on"] and event == "open" and args:
        _OPENED.append(str(args[0]))


sys.addaudithook(_record_open)


def _pointer(
    run_id: str,
    repository: Path,
    *,
    backend: str = "alpha",
    disposition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": "0" * 40,
        "launch": "in-harness",
        "role": "implement",
        "backend": backend,
        "created_at": "2030-01-02T03:00:00Z",
        "manifest_path": "",
        "closure_disposition": disposition,
        "node": {
            "id": f"node-{run_id}",
            "plan": "p",
            "section": "s",
            "time_budget": "20m",
            "write_paths": [],
        },
    }


def _seed_run(record: dict[str, Any], home: Path) -> None:
    """Give one live run the artifacts a classification of it reads.

    A rollout stream and a stored review are what the per-pointer step opens,
    so a run carrying them is a run the reading would pay for if it classified
    the fleet itself.
    """
    directory = run_dir(record["run_id"])
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stdout.log"
    stream.write_text(
        '{"type": "assistant", "message": {"content": '
        '[{"type": "text", "text": "working"}]}}\n'
    )
    manifest = directory / "manifest.md"
    manifest.write_text(f"node: node-{record['run_id']}\nstatus: in-progress\n")
    record["log_path"] = str(stream)
    record["manifest_path"] = str(manifest)
    review_document = review.review_path(PROJECT, record["run_id"], home)
    review_document.parent.mkdir(parents=True, exist_ok=True)
    review_document.write_text(
        f'{{"project": "{PROJECT}", "reviewed_run_id": '
        f'"{record["run_id"]}", "status": "complete"}}\n'
    )


def _install_fleet(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    count: int,
    *,
    store_every: bool = True,
    refine: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], Path]:
    """Seed ``count`` live runs, and the station's stored verdict for each."""
    home = root / "crew-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    repository = root / "repository"
    repository.mkdir()
    seeds: list[dict[str, Any]] = []
    for index in range(count):
        record = _pointer(f"r-{index:03d}", repository, backend=f"lane-{index % 3}")
        _seed_run(record, home)
        _write_json(pointer_path(record["run_id"]), record)
        seeds.append(record)

    store = station_store.StationStore(root / "station")
    store.set_station_value(station_store.CREW_HOME_KEY, str(crew_home()))
    for record in seeds:
        if not store_every and record["run_id"] != seeds[0]["run_id"]:
            continue
        snapshot = {
            "run_id": record["run_id"],
            "project": PROJECT,
            "classification": "running",
            "recovery_classification": "running",
            "process_alive": True,
        }
        if refine and record["run_id"] in refine:
            snapshot["recovery_classification"] = refine[record["run_id"]]
        store._write_classification(
            record["run_id"], str(pointer_path(record["run_id"])), "seed-key", snapshot
        )
    store.close()

    monkeypatch.setattr(
        station_store.StationStore,
        "at_runtime_root",
        classmethod(
            lambda cls, **_kwargs: station_store.StationStore(root / "station")
        ),
    )
    return seeds, home


def _opened_under_other_runs(run_ids: list[str]) -> int:
    """The files opened under any seeded run's own directory."""
    directories = [str(run_dir(run_id)) for run_id in run_ids]
    return sum(
        1 for path in _OPENED if any(path.startswith(f"{d}/") for d in directories)
    )


def _count_opened(pointers: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    """Run the reading once under the counting instrument."""
    _OPENED.clear()
    _RECORDING["on"] = True
    try:
        reading = promotion._fleet_state_reading(PROJECT)
    finally:
        _RECORDING["on"] = False
    return _opened_under_other_runs([str(p["run_id"]) for p in pointers]), reading


def _expected_fields() -> dict[str, Any]:
    """The fleet fields the per-pointer composition derives, as base recorded them."""
    pointers = runs.list_live(project=PROJECT)
    words = [
        str(recovery.classify_pointer(pointer)["recovery_classification"])
        for pointer in pointers
    ]
    return {
        "live_runs": len(pointers),
        "unreconciled_runs": runs.drain(PROJECT)["unreconciled_runs"],
        "actionable_runs": sum(
            1 for word in words if word in recovery.ACTIONABLE_RECOVERY_CLASSIFICATIONS
        ),
        "actionable_classifications": sorted(
            {
                word
                for word in words
                if word in recovery.ACTIONABLE_RECOVERY_CLASSIFICATIONS
            }
        ),
        "occupied_lanes": len(
            {
                str(pointer.get("backend") or "").strip()
                for pointer in pointers
                if str(pointer.get("backend") or "").strip()
            }
        ),
    }


def test_the_reading_opens_no_more_files_as_the_fleet_grows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    counts: dict[int, int] = {}
    for population in (10, 100):
        root = tmp_path / f"fleet-{population}"
        root.mkdir()
        monkeypatch.undo()
        pointers, _home = _install_fleet(monkeypatch, root, population)
        counts[population], reading = _count_opened(pointers)
        assert reading["fleet_state"] == "measured"
        assert reading["live_runs"] == population

    assert counts[100] <= counts[10] + 10


def test_the_per_pointer_composition_would_grow_with_the_fleet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The instrument's positive control: with no stored verdict to serve, the
    # reading classifies each run and the count grows with the fleet. Without
    # this, a zero would only show the instrument saw nothing.
    counts: dict[int, int] = {}
    for population in (10, 100):
        root = tmp_path / f"unserved-{population}"
        root.mkdir()
        monkeypatch.undo()
        pointers, _home = _install_fleet(
            monkeypatch, root, population, store_every=False
        )
        monkeypatch.setattr(
            promotion_release, "_served_fleet_classifications", lambda *a, **k: {}
        )
        counts[population], reading = _count_opened(pointers)
        assert reading["fleet_state"] == "measured"

    assert counts[10] > 0
    assert counts[100] > counts[10] + 10


def test_the_recorded_fields_are_the_ones_the_per_pointer_composition_gives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "fleet"
    root.mkdir()
    pointers, _home = _install_fleet(monkeypatch, root, 12)
    assert len(pointers) == 12

    reading = promotion._fleet_state_reading(PROJECT)
    expected = _expected_fields()

    assert reading["fleet_state"] == "measured"
    for field, value in expected.items():
        assert reading[field] == value, field


def test_a_run_with_no_stored_verdict_is_classified_and_the_rest_are_served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "partial"
    root.mkdir()
    pointers, _home = _install_fleet(monkeypatch, root, 6, store_every=False)
    stored_run = str(pointers[0]["run_id"])

    unserved = str(pointers[1]["run_id"])
    _OPENED.clear()
    _RECORDING["on"] = True
    try:
        reading = promotion._fleet_state_reading(PROJECT)
    finally:
        _RECORDING["on"] = False

    served_files = [p for p in _OPENED if p.startswith(f"{run_dir(stored_run)}/")]
    unserved_files = [p for p in _OPENED if p.startswith(f"{run_dir(unserved)}/")]
    assert served_files == []
    assert unserved_files
    assert reading["live_runs"] == 6
    assert reading["unreconciled_runs"] == _expected_fields()["unreconciled_runs"]


def test_a_run_whose_stored_word_the_fleet_reducer_refined_is_classified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # "stalled" is the reducer's reading of a run, not the run's own recovery
    # word, so that run is classified afresh: serving the stored word would
    # record an actionable classification the per-pointer composition never
    # gives this run.
    root = tmp_path / "refined"
    root.mkdir()
    pointers, _home = _install_fleet(monkeypatch, root, 4, refine={"r-000": "stalled"})

    opened, reading = _count_opened(pointers)

    assert opened >= 1
    assert reading["actionable_classifications"] == sorted(
        _expected_fields()["actionable_classifications"]
    )
