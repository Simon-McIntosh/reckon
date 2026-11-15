"""Classifying a pointer reads that pointer's records, not the whole store.

A producer tick classifies every live pointer, and each classification asks
which records the store holds for that run. A reader that answers by listing
against the store, or by walking every name the store's index holds, pays the
store's size once per pointer, so a snapshot costs pointers times records. This
case builds two indexed stores ten times apart and shows that classifying the
same pointers against each examines the same number of record names -- the
pointer's own -- so the same 200 pointers cost no more against 20,000 records
than against 2,000.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon.crew import recovery_memo, review, review_records

HEAD = "0123456789abcdef0123456789abcdef01234567"
RECORDS_PER_RUN = 10
CLASSIFIED_RUNS = 200
SMALL_RUNS = CLASSIFIED_RUNS
LARGE_RUNS = 2_000
PROJECT_SMALL = "fixture-pointers-small"
PROJECT_LARGE = "fixture-pointers-large"

# The primitives are captured once, before any counter replaces them, so a
# second counter delegates to the real reader rather than to the first counter.
_REAL: dict[str, object] = {}


def _run_id(index: int) -> str:
    return f"r-20261115T0900{index:04d}-node-{index:04d}"


def _payload(project: str, run_id: str, index: int) -> dict:
    return {
        "project": project,
        "reviewed_run_id": run_id,
        "review_run_id": f"review-of-{run_id}-{index:08x}",
        "reviewed_head_sha": HEAD,
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review.REVIEW_DIMENSIONS),
        "run_id": run_id,
    }


def _populate(project: str, runs: int) -> Path:
    """Write the store's records at the paths its own naming chooses, then index it.

    The files are written directly so a twenty-thousand-record store costs one
    write each; ``store_review`` would take the index lock and rewrite the
    document for every record. The index is then built once, from one listing,
    before anything is measured: the two stores are handed to the
    classification already indexed, which is the state a running store is in.
    """
    directory = review.review_store_root() / project
    directory.mkdir(parents=True, exist_ok=True)
    for run in range(runs):
        run_id = _run_id(run)
        for index in range(RECORDS_PER_RUN):
            name = f"{run_id}.json" if index == 0 else f"{run_id}.at-{index:040x}.json"
            payload = _payload(project, run_id, index)
            (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    review_records.store_index_document(directory)
    return directory


class _Counter:
    """Counts the store names one classification pass examines.

    A name is examined when the reader is handed every name the store holds --
    the whole-store stat view or a listing of the directory -- or when the
    index is asked for one run's names. The arms are held apart so a reader
    that still walks the store is visible as such rather than folded into a
    total that could pass by coincidence.
    """

    def __init__(self) -> None:
        self.whole_store = 0
        self.per_run = 0
        self.listing_names = 0
        self._directory: Path | None = None
        self._armed = False

    @property
    def total(self) -> int:
        return self.whole_store + self.per_run + self.listing_names

    def arm(self, directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        if not _REAL:
            _REAL["tick"] = review_records._tick_store
            _REAL["names"] = getattr(review_records, "review_run_record_names", None)
            _REAL["scandir"] = os.scandir
            _REAL["glob"] = Path.glob
        if self._armed:
            return
        self._armed = True
        self._directory = directory
        real_tick = _REAL["tick"]
        real_names = _REAL["names"]
        real_scandir = _REAL["scandir"]
        real_glob = _REAL["glob"]
        owner = self

        def tick(path: Path):
            files = real_tick(path)
            if path == directory and files:
                owner.whole_store += len(files)
            return files

        class CountedScan:
            def __init__(self, scanned):
                self._scanned = scanned
                self._names = 0

            def __iter__(self):
                return self

            def __next__(self):
                entry = next(self._scanned)
                if entry.name.endswith(".json"):
                    self._names += 1
                    owner.listing_names += 1
                return entry

            def __enter__(self):
                self._scanned.__enter__()
                return self

            def __exit__(self, *args):
                return self._scanned.__exit__(*args)

            def close(self):
                return self._scanned.close()

        def counted_scandir(value="."):
            scanned = real_scandir(value)
            try:
                target = Path(os.fsdecode(value))
            except (TypeError, ValueError):
                return scanned
            if target != directory:
                return scanned
            return CountedScan(scanned)

        def counted_glob(self_path, pattern, **kwargs):
            found = list(real_glob(self_path, pattern, **kwargs))
            if self_path == directory:
                owner.listing_names += len(found)
            return iter(found)

        monkeypatch.setattr(review_records, "_tick_store", tick)
        monkeypatch.setattr(recovery_memo, "_tick_store", tick)
        if real_names is not None:

            def names(path: Path, run_id: str):
                held = real_names(path, run_id)
                if path == directory and held is not None:
                    owner.per_run += len(held)
                return held

            monkeypatch.setattr(recovery_memo, "review_run_record_names", names)
        monkeypatch.setattr(os, "scandir", counted_scandir)
        monkeypatch.setattr(Path, "glob", counted_glob)


def _classify(project: str) -> None:
    """Classify every one of the first 200 pointers of one store."""
    for run in range(CLASSIFIED_RUNS):
        record = {"project": project, "run_id": _run_id(run), "manifest_path": ""}
        recovery_memo._review_input_identities(record)


@pytest.fixture()
def stores():
    small = _populate(PROJECT_SMALL, SMALL_RUNS)
    large = _populate(PROJECT_LARGE, LARGE_RUNS)
    return small, large


def test_a_pointer_reads_only_its_runs_records(stores, monkeypatch) -> None:
    small, large = stores

    # The instrument must be shown to see a whole-store read before any
    # absence is believed: the store's own stat view hands back every name.
    control = _Counter()
    control.arm(small, monkeypatch)
    control_tick = review_records._tick_store
    control_tick(small)
    expected_names = SMALL_RUNS * RECORDS_PER_RUN
    assert control.whole_store == expected_names, (
        "the counting stand-in did not see the store's own names: "
        f"{control.whole_store} of {expected_names}"
    )

    counts = {}
    for project, directory in ((PROJECT_SMALL, small), (PROJECT_LARGE, large)):
        counter = _Counter()
        counter.arm(directory, monkeypatch)
        _classify(project)
        counts[project] = counter

    small_total = counts[PROJECT_SMALL].total
    large_total = counts[PROJECT_LARGE].total
    belonging = CLASSIFIED_RUNS * RECORDS_PER_RUN

    assert small_total > 0, (
        "the instrument recorded no store name, so it cannot tell a per-run "
        "read from a whole-store one"
    )
    assert large_total == small_total, (
        "classifying the same pointers examined more names against the larger "
        f"store: {large_total} against {small_total} "
        f"(whole-store names {counts[PROJECT_LARGE].whole_store} vs "
        f"{counts[PROJECT_SMALL].whole_store}; listing names "
        f"{counts[PROJECT_LARGE].listing_names} vs "
        f"{counts[PROJECT_SMALL].listing_names}; per-run names "
        f"{counts[PROJECT_LARGE].per_run} vs {counts[PROJECT_SMALL].per_run})"
    )
    assert small_total <= belonging, (
        "classifying 200 pointers examined more record names than those runs "
        f"hold: {small_total} > {belonging}"
    )
