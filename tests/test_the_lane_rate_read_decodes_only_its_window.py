"""The lane rate read decodes only the ledger rows its window can use.

``recovery_stream._lane_reference_rates`` judges a run against the median
committed rate of its lane over the last seven days. It used to answer that by
decoding every promoted row in the ledger, so a station holding a long history
paid a whole-history decode once per five-minute bucket — measured at 10,770
rows, 5.9 s and a 1.61 GB peak — and a station that pays that peak repeatedly
is eventually killed by the kernel for it.

The read is now bounded by the window. The ledger's index carries an activity
scalar per row and serves an ``activity_since`` read from it, so a warm index
fetches only the rows whose newest stamp is no older than the window's start.
These tests build a ledger shaped like the live one — 2,000 promoted rows
distinct from the window, 30 inside it across two lanes — warm its index, and
count the payload records the read actually decodes. They then hold the read to
its other obligation: the rates it returns are the rates the unbounded read
returns for the same ledger, so the decode the bound saves is not bought with a
different answer.

Nothing here touches the operator's store: the ledger lives under the test's
own temporary config home, and the clock is the fixture's own instant rather
than today's, so the fixture and its expectations move together.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from reckon import ledger
from reckon.crew import recovery_stream

PROJECT = "proj"
OLD_ROW_COUNT = 2000
WINDOW_ROW_COUNT = 30
#: The instant the fixture is built against. A fixed date rather than the
#: current clock, so the window is a property of the fixture.
CLOCK = datetime(2026, 11, 10, 12, 0, tzinfo=UTC).timestamp()
WINDOW_BUCKET = int(CLOCK // 300)
WINDOW_SECONDS = 7 * 86400
#: The rows a windowed read may decode: the 30 inside the window, with room for
#: the in-flight rows a bound on the newest stamp also admits.
DECODE_CEILING = 60
LANES = (("backend-a", "model-a"), ("backend-b", "model-b"))


def _stamp(seconds: float) -> str:
    return (
        datetime.fromtimestamp(seconds, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _row(position: int, *, completed: float, backend: str, model: str) -> dict:
    """One completed run row carrying the fields the rate read consumes."""
    return {
        "run_id": f"r-fixture-{position:05d}",
        "project": PROJECT,
        "node": f"fixture-node-{position:05d}",
        "plan": "plan-a",
        "role": "implement",
        "status": "complete",
        "backend": backend,
        "agent": {"model": model},
        "dispatched_at": _stamp(completed - 240),
        "completed_at": _stamp(completed),
        "throughput": {"tokens_per_second": 10.0 + position},
    }


def _fixture_rows() -> tuple[list[dict], list[dict]]:
    """The old population and the in-window population, separately."""
    window_end = WINDOW_BUCKET * 300
    # More than eight days before the instant the read is judged at, completed
    # rows whose every stamp is older than the window's start.
    old = [
        _row(
            position,
            completed=window_end - 9 * 86400 - position,
            backend="backend-old",
            model="model-old",
        )
        for position in range(OLD_ROW_COUNT)
    ]
    # Inside the window, alternating between the two lanes.
    window = [
        _row(
            OLD_ROW_COUNT + position,
            completed=window_end - 3600 - position * 60,
            backend=LANES[position % len(LANES)][0],
            model=LANES[position % len(LANES)][1],
        )
        for position in range(WINDOW_ROW_COUNT)
    ]
    return old, window


@pytest.fixture()
def warm_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """A 2,030-row ledger whose on-disk index is already built."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    old, window = _fixture_rows()
    ledger.write(PROJECT, {"members": [], "runs": old + window, "holds": []}, 0)
    # The live ledger reads through its index; warm it so the count below is a
    # decode of the stored index rather than a rebuild of it.
    rows, _version = ledger.load(PROJECT)
    assert len(rows["runs"]) == OLD_ROW_COUNT + WINDOW_ROW_COUNT
    return window


def test_the_read_decodes_only_its_window(warm_ledger: list[dict]) -> None:
    decoded: list[str] = []
    original = ledger.normalize_outside_scope_paths

    def counting_decode(record):
        decoded.append(str(record.get("run_id") or ""))
        return original(record)

    recovery_stream._lane_reference_rates.cache_clear()
    with mock.patch.object(ledger, "normalize_outside_scope_paths", counting_decode):
        rates = recovery_stream._lane_reference_rates(PROJECT, WINDOW_BUCKET)

    # The stand-in must be shown to see something known present before its
    # count is evidence of an absence.
    assert decoded, "the recording stand-in decoded no row at all"
    # The count is only evidence of a narrow read if the read answered
    # something: an empty answer decodes nothing for the wrong reason.
    assert rates and sorted(rates) == sorted(LANES)
    assert len(decoded) <= DECODE_CEILING, (
        f"decoded {len(decoded)} rows for a {WINDOW_ROW_COUNT}-row window"
    )
    assert set(decoded) == {row["run_id"] for row in warm_ledger}


def test_the_windowed_read_returns_the_unbounded_read_s_rates(
    warm_ledger: list[dict],
) -> None:
    """The bound must not change which rates a lane is judged against."""
    recovery_stream._lane_reference_rates.cache_clear()
    windowed = recovery_stream._lane_reference_rates(PROJECT, WINDOW_BUCKET)

    rows = ledger.load(PROJECT)[0]["runs"]
    end = WINDOW_BUCKET * 300
    start = end - WINDOW_SECONDS
    collected: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        completed = row.get("completed_at")
        if not isinstance(completed, str):
            continue
        moment = (
            datetime.strptime(completed, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .timestamp()
        )
        if not start <= moment <= end:
            continue
        lane = (str(row.get("backend") or ""), str(row["agent"]["model"]))
        collected.setdefault(lane, []).append(row["throughput"]["tokens_per_second"])
    unbounded = {lane: tuple(values) for lane, values in collected.items()}

    # The judgement the caller makes is the lane's median, so that is what the
    # two reads must agree on; the ordering inside a lane is the index's.
    assert {lane: statistics.median(values) for lane, values in windowed.items()} == {
        lane: statistics.median(values) for lane, values in unbounded.items()
    }
    assert sorted(windowed) == sorted(unbounded)
    assert sorted(windowed) == sorted(LANES)
    assert all(
        len(values) == WINDOW_ROW_COUNT // len(LANES) for values in windowed.values()
    )
