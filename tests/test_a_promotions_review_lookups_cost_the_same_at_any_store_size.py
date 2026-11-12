"""A promotion's review lookups cost the same at any review-store size.

A promotion reads a run's stored review twice: once under the review gate,
through the classifier, and again when it selects the record describing the
revision it is about to land. A one-shot ``crew complete`` starts with no
in-process store index, and the index it loads from the node-local cache used
to be re-derived from a listing of the whole store, so both reads paid a stat
per stored record. On a live store of about 7,000 records that was the largest
single cost of a no-code promotion.

These cases drive the review reads a promotion makes, on a cold process, over
a synthesised store of 700 and of 7,000 records for other runs, and count the
stats and opens the reads make of files under the store. The store's index is
warmed before the measured promotion, which is the state a live home is in:
another process on the node has indexed the store already. The count must not
grow with the store, and the promotion must still select the record the store
holds for its run — including one filed under another reviewer's run id — and
still read an absent review as absent.

Nothing here touches the operator's store: the crew home and the store are
built under the test's own temporary directory.
"""

from __future__ import annotations

import builtins
import io
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

from reckon import file_memo
from reckon.crew import promotion
from reckon.crew import review as review_module
from reckon.crew import review_records as records_module
from reckon.crew.recovery import classify_pointer

SMALL_PROJECT = "promotion-store-small"
LARGE_PROJECT = "promotion-store-large"
REVIEWED_RUN = "r-20261112T090000000000-reviewed-run"
REVIEWER_RUN = "r-20261112T090500000000-review-run"
ABSENT_RUN = "r-20261112T091000000000-absent-run"
PROMOTED_HEAD = "a1" * 20


def _record(reviewed_run_id: str, head: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "project": "",
        "reviewed_run_id": reviewed_run_id,
        "review_run_id": "",
        "status": "parsed",
        "scores": {"correctness": 4},
        "absent": [],
        "total": 80,
        "timestamp": "2026-11-12T09:05:00+00:00",
    }
    if head is not None:
        record["reviewed_head_sha"] = head
    return record


def _other_head(index: int) -> str:
    return f"{index + 1:040x}"


def _seed_store(project: str, count: int) -> Path:
    """A store of ``count`` records for other runs, plus this promotion's.

    The reviewed run's record is filed under its reviewer's run id, which is
    the shape a promotion's lookup reaches the store for: nothing at the run's
    own path answers, so the record is found by what it says it reviews.
    """
    directory = review_module.review_store_root() / project
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        run_id = f"r-20260101T{index // 3600:02d}{index // 60 % 60:02d}"
        f"{index % 60:02d}000000-stored-{index:05d}"
        record = _record(run_id, _other_head(index))
        record["project"] = project
        (directory / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")
    misfiled = _record(REVIEWED_RUN, PROMOTED_HEAD)
    misfiled["project"] = project
    (directory / f"{REVIEWER_RUN}.json").write_text(
        json.dumps(misfiled), encoding="utf-8"
    )
    # The index is warmed here, outside the measured promotion: a live home
    # holds one another process on the node persisted, and a cold promotion
    # starts from it rather than from a walk of the store.
    records_module._store_index(directory)
    return directory


class _WatchedEntry:
    """One ``scandir`` entry whose own ``stat`` is counted as a stat."""

    def __init__(self, entry: Any, count: Callable[[str], None]) -> None:
        self._entry = entry
        self._count = count

    def __getattr__(self, name: str) -> Any:
        return getattr(self._entry, name)

    def stat(self, *args: Any, **kwargs: Any) -> Any:
        self._count(str(self._entry.path))
        return self._entry.stat(*args, **kwargs)


class _WatchedScan:
    """A ``scandir`` iterator handing out counted entries."""

    def __init__(self, iterator: Any, count: Callable[[str], None]) -> None:
        self._iterator = iterator
        self._count = count

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> _WatchedEntry:
        return _WatchedEntry(next(self._iterator), self._count)

    def __enter__(self) -> Self:
        self._iterator.__enter__()
        return self

    def __exit__(self, *exception: object) -> Any:
        return self._iterator.__exit__(*exception)

    def close(self) -> None:
        self._iterator.close()


@contextmanager
def _store_accesses(store: Path) -> Iterator[list[str]]:
    """Count the stats and opens of files under ``store``.

    ``pathlib.Path.stat`` resolves to :func:`os.stat` and ``Path.open`` and
    ``Path.read_text`` to :func:`io.open`, so patching those two wrappers sees
    every access the store readers make through a path. A reader holding a
    directory listing takes each entry's identity from ``scandir`` instead, at
    the C level, so the listing's entries are wrapped and their own ``stat`` is
    counted too.
    """
    seen: list[str] = []
    prefix = str(store)
    real_stat = os.stat
    real_open = io.open
    real_scandir = os.scandir

    def watched(path: Any) -> None:
        if isinstance(path, (str, bytes)) and str(path).startswith(prefix):
            seen.append(str(path))

    def counting_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(path, int):
            watched(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    def counting_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(file, int):
            watched(os.fspath(file))
        return real_open(file, *args, **kwargs)

    def counting_scandir(path: Any = ".") -> _WatchedScan:
        return _WatchedScan(real_scandir(path), watched)

    os.stat = counting_stat
    io.open = counting_open
    builtins.open = counting_open
    os.scandir = counting_scandir
    try:
        yield seen
    finally:
        os.stat = real_stat
        io.open = real_open
        builtins.open = real_open
        os.scandir = real_scandir


def _promotion_record(project: str, run_id: str) -> dict[str, Any]:
    """The record shape a promotion's review gate classifies."""
    return {
        "project": project,
        "run_id": run_id,
        "role": "review",
        "launch": "in-harness",
        "node": {"id": "review-node"},
    }


def _promotion_lookups(project: str, run_id: str) -> tuple[Any, Any, Any]:
    """The review reads one promotion makes, in the order it makes them."""
    first = classify_pointer(_promotion_record(project, run_id))
    reviewed, stale = promotion._review_for_promotion(
        project, run_id, promoted_revision=PROMOTED_HEAD, tree=None
    )
    second = classify_pointer(_promotion_record(project, run_id))
    return first, (reviewed, stale), second


def _measured(project: str, count: int, run_id: str) -> tuple[list[str], Any]:
    """One promotion's review reads over a store of ``count`` records."""
    store = _seed_store(project, count)
    # A cold one-shot promotion: no in-process index and no shared memo.
    records_module._STORE_INDEXES.clear()
    file_memo.clear()
    with _store_accesses(store) as seen:
        outcome = _promotion_lookups(project, run_id)
    return seen, outcome


def test_a_promotions_review_lookups_cost_the_same_at_any_store_size() -> None:
    """A 7,000-record store costs no more than a 700-record one, plus ten."""
    small, small_outcome = _measured(SMALL_PROJECT, 700, REVIEWED_RUN)
    large, large_outcome = _measured(LARGE_PROJECT, 7000, REVIEWED_RUN)

    assert len(large) <= len(small) + 10, (
        f"the store is read {len(large)} times at 7,000 records against "
        f"{len(small)} at 700; a promotion's review lookups must not grow "
        "with the store"
    )
    # The instrument is shown to see access at all: a zero here would be a
    # counter that never fired rather than a promotion that read nothing.
    assert small, "the counted promotion read nothing, so the counter is blind"

    reviewed, stale = small_outcome[1]
    assert reviewed is not None, "the promotion selects the review for its run"
    assert large_outcome[1][0] is not None, "and does so at 7,000 records too"
    assert stale == "", "the stored record describes the promoted revision"
    assert reviewed.get("reviewed_head_sha") == PROMOTED_HEAD

    # The record is chosen by what it says it reviews, not by its file name:
    # the store holds it under the reviewer's run id, and the reader says so.
    records_module._STORE_INDEXES.clear()
    file_memo.clear()
    found_path, found = review_module.stored_record(
        LARGE_PROJECT, REVIEWED_RUN, reviewed_head_sha=PROMOTED_HEAD
    )
    assert found is not None, "a misfiled review is still found at 7,000 records"
    assert found_path.name == f"{REVIEWER_RUN}.json"
    assert found.get(review_module.MISFILED_KEY) is True


def test_an_absent_review_is_read_as_absent_at_either_store_size() -> None:
    """A run with no stored record reads as none, and the refusal is bounded."""
    small, small_outcome = _measured(SMALL_PROJECT + "-absent", 700, ABSENT_RUN)
    large, large_outcome = _measured(LARGE_PROJECT + "-absent", 7000, ABSENT_RUN)

    assert small_outcome[1][0] is None, "a run with no stored review reads as none"
    assert large_outcome[1][0] is None
    assert len(large) <= len(small) + 10, (
        f"the absent-review lookup reads the store {len(large)} times at 7,000 "
        f"records against {len(small)} at 700"
    )
