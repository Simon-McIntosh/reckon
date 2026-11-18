"""A bucket roll refreshes the lane reference rates from the rows that changed.

A run that overran its budget is judged against the median recent rate of its
own backend and model. The window that judges it moves by one five-minute
bucket at a time, so between two consecutive buckets it changes only by the
rows completed since the previous bucket and the rows completed just before the
new window's start. Reading the whole window on every roll decoded 14.6% of the
station's profile time, once per project every five minutes.

The population here is the production shape: 2,000 promoted rows across three
lanes in a temporary crew home, of which three age out between the two buckets
and one sits exactly on the boundary the two windows share. The population is
built around a fixed instant rather than the current clock, so the buckets and
their rows move together. The first bucket is the moment a process holds no
previous bucket and must read its window; the second is a roll, and the rows
every read of the store decodes are counted by a stand-in. Whatever the path,
every lane's rates and every overrun answer are the ones the whole-window read
gives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger, run_store
from reckon.crew import recovery_stream

PROJECT = "lane-rate-proj"
#: The instant the fixture is built against.
CLOCK = datetime(2026, 11, 10, 12, 0, tzinfo=UTC).timestamp()
BUCKET = int(CLOCK // 300)
BUCKET_END = BUCKET * 300
NEXT_END = BUCKET_END + 300
WINDOW_SECONDS = 7 * 86400
WINDOW_START = BUCKET_END - WINDOW_SECONDS
LANES = (("clive", "model-a"), ("codex", "model-b"), ("claude", "model-c"))
#: The rows the store holds inside the first bucket's window.
POPULATION = 2000
#: Rows completed just after the window's start: inside the first window and
#: outside the next one.
AGED = 3
#: A row completed exactly at the first bucket's end, which both windows admit.
BOUNDARY = 1
ORDINARY = POPULATION - AGED - BOUNDARY
#: The rows promoted between the two buckets.
PROMOTED = 5
#: The rows a bucket roll may decode: the promoted rows, the boundary row the
#: previous bucket also read, and room for the store's own selection.
DECODE_CEILING = 10
#: The live runs whose overrun answer the roll must not move.
LIVE_RUNS = 20


def _stamp(seconds: float) -> str:
    return (
        datetime.fromtimestamp(seconds, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _row(
    position: int, *, completed: float, lane: tuple[str, str], rate: float
) -> dict:
    """One promoted row carrying the fields the rate read consumes."""
    return {
        "run_id": f"r-lane-{position:05d}",
        "project": PROJECT,
        "plan": "plan-a",
        "role": "implement",
        "status": "complete",
        "backend": lane[0],
        "agent": {"model": lane[1]},
        "dispatched_at": _stamp(completed - 240),
        "completed_at": _stamp(completed),
        "throughput": {"tokens_per_second": rate},
    }


def _ordinary() -> list[dict[str, Any]]:
    """The rows spread across the first window, 300 seconds apart."""
    return [
        _row(
            index,
            completed=WINDOW_START + 3000 + index * 300,
            lane=LANES[index % len(LANES)],
            rate=20.0 + (index % 17),
        )
        for index in range(ORDINARY)
    ]


def _aged() -> list[dict[str, Any]]:
    """The rows the first window holds and the second one drops.

    They complete within thirty seconds of the first window's start, so the
    second window's start, five minutes on, passes them.
    """
    return [
        _row(
            ORDINARY + offset,
            completed=WINDOW_START + 10 * (offset + 1),
            lane=LANES[0],
            rate=400.0 + offset,
        )
        for offset in range(AGED)
    ]


def _boundary() -> dict[str, Any]:
    """One row completed exactly at the first bucket's end, in both windows."""
    return _row(
        ORDINARY + AGED,
        completed=BUCKET_END,
        lane=LANES[0],
        rate=250.0,
    )


def _population() -> list[dict[str, Any]]:
    """The 2,000 rows the first bucket's window holds."""
    return [*_ordinary(), _boundary(), *_aged()]


def _promoted() -> list[dict[str, Any]]:
    """The rows promoted between the two buckets: three to one lane, two to another.

    None reaches the lane the aged rows left, so that lane's cohort must shrink
    by exactly the rows that aged out.
    """
    return [
        _row(
            ORDINARY + AGED + BOUNDARY + index,
            completed=BUCKET_END + 60 * (index + 1),
            lane=LANES[1] if index < 3 else LANES[2],
            rate=30.0 + index,
        )
        for index in range(PROMOTED)
    ]


def _seed(records: list[dict[str, Any]]) -> None:
    """Write the records to the test's own store, as promotion writes them."""
    with run_store.RunStore(run_store.store_path()) as store:
        for record in records:
            store.append(PROJECT, record)


def _expected_rates(bucket: int) -> dict[tuple[str, str], tuple[float, ...]]:
    """The rates a whole-window read answers for this bucket.

    Taken from the store through the ledger's own reader, which orders rows by
    completion, and filtered by exactly the window the production read judges
    against. That is what the base revision answers, whatever the roll did.
    """
    end = bucket * 300
    start = end - WINDOW_SECONDS
    rates: dict[tuple[str, str], list[float]] = {}
    for row in ledger.load(PROJECT)[0]["runs"]:
        completed = row.get("completed_at")
        if not isinstance(completed, str):
            continue
        moment = datetime.fromisoformat(completed).timestamp()
        if not start <= moment <= end:
            continue
        lane = (str(row.get("backend") or ""), str(row["agent"]["model"]))
        rates.setdefault(lane, []).append(float(row["throughput"]["tokens_per_second"]))
    return {lane: tuple(values) for lane, values in rates.items()}


def _count_decodes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record how many rows every read of the store decodes."""
    decoded: list[int] = []
    owner = run_store.stored_run_rows

    def recording(*args: Any, **kwargs: Any) -> Any:
        rows = owner(*args, **kwargs)
        if rows is not None:
            decoded.append(len(rows))
        return rows

    monkeypatch.setattr(run_store, "stored_run_rows", recording)
    return decoded


def _live_record(index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """One live overrun and the timing block that carries its verdict."""
    lane = LANES[index % len(LANES)]
    record: dict[str, Any] = {
        "run_id": f"r-live-{index:02d}",
        "project": PROJECT,
        "backend": lane[0],
        "agent": {"model": lane[1]},
    }
    timing: dict[str, Any] = {
        "budget_overrun": True,
        "budget_seconds": 1800,
        "budget_overrun_tokens": 0,
    }
    shape = index % 4
    if shape == 0:
        record["throughput"] = {"tokens_per_second": 1.0}
    elif shape == 1:
        record["throughput"] = {"tokens_per_second": 1000.0, "generated_tokens": 100}
        timing["budget_overrun_tokens"] = 5000
    elif shape == 3:
        record["throughput"] = {
            "tokens_per_second": 1000.0,
            "generated_tokens": 1_000_000_000,
        }
    return record, timing


def _causes(now_seconds: float) -> list[dict[str, Any]]:
    """The overrun answer for the live runs at one moment."""
    return [
        recovery_stream._budget_overrun_cause(record, timing, now_seconds=now_seconds)
        for record, timing in (_live_record(index) for index in range(LIVE_RUNS))
    ]


def test_a_bucket_roll_decodes_only_the_rows_added(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second bucket moves the rates by the rows that changed, no more."""
    _seed(_population())
    decoded = _count_decodes(monkeypatch)
    recovery_stream._lane_reference_rates.cache_clear()
    try:
        rates_first = recovery_stream._lane_reference_rates(PROJECT, BUCKET)
        opening_read = sum(decoded)
        assert opening_read >= POPULATION, (
            "a process holding no previous bucket must read its window, or the "
            f"counter measures nothing: decoded {opening_read}"
        )
        del decoded[:]
        assert rates_first == _expected_rates(BUCKET)

        causes_first = _causes(BUCKET_END)
        # A process holding no previous bucket answers the same overrun causes.
        recovery_stream._lane_reference_rates.cache_clear()
        base_causes_first = _causes(BUCKET_END)
        assert causes_first == base_causes_first
        assert {cause["budget_overrun_cause"] for cause in causes_first} == {
            "lane-saturated",
            "over-large",
            "unknown",
        }

        _seed(_promoted())
        del decoded[:]
        rates_next = recovery_stream._lane_reference_rates(PROJECT, BUCKET + 1)
        roll_read = sum(decoded)
        assert roll_read > 0, (
            "the roll decoded nothing, so its count cannot show a narrow read"
        )
        assert roll_read <= DECODE_CEILING, (
            f"a roll over {PROMOTED} promoted rows decoded {roll_read} rows"
        )

        del decoded[:]
        expected_next = _expected_rates(BUCKET + 1)
        expected_first = _expected_rates(BUCKET)
        assert rates_next == expected_next
        assert rates_first == expected_first

        aged_lane = LANES[0]
        assert len(expected_first[aged_lane]) == len(expected_next[aged_lane]) + AGED
        assert len(expected_next[LANES[1]]) == len(expected_first[LANES[1]]) + 3
        assert len(expected_next[LANES[2]]) == len(expected_first[LANES[2]]) + 2

        causes_next = _causes(NEXT_END)
        recovery_stream._lane_reference_rates.cache_clear()
        base_causes_next = _causes(NEXT_END)
        assert causes_next == base_causes_next
    finally:
        recovery_stream._lane_reference_rates.cache_clear()
