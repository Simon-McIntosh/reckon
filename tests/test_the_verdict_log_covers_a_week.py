"""The admission verdict log keeps the week a drain is measured over.

The pass log was one file trimmed to a megabyte on every append. On 22 November
2026 the live log held 54 passes spanning 2,201 s, so the verdict-gap measure,
which reads a seven-day window, could not be read: a request that waited across
the start of the window had no rows to read. This pins the replacement — records
kept per day, retained for a week, and a read that tells its caller the span it
actually covers rather than handing back a window it cannot support.

The simulated week is written at the rate and row size measured on that live
log, so the byte ceiling the deliverable names is the one the fixture is held
to rather than one the fixture can never approach.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import drain_measure
from reckon.crew.admission import verdicts

#: Measured on the live log at ``~/.config/reckon/crew/admission-verdicts.jsonl``
#: on 2026-11-22 at 19:03 UTC: 54 pass records over 4,380.5 s, an average row of
#: 19,259.6 bytes. The rate is that count over that span in days. Both are the
#: figures the retained log's cost is bounded against.
LIVE_VERDICT_RATE_PER_DAY = 1_065.1
LIVE_MEAN_ROW_BYTES = 19_259.6
LIVE_MEASURED_MARK = (
    "2026-11-22T19:03:06Z at ~/.config/reckon/crew/admission-verdicts.jsonl"
)

#: The passes a simulated day writes, at the live rate.
PASSES_PER_DAY = round(LIVE_VERDICT_RATE_PER_DAY)

#: The instant the simulated week is read from: noon on the last day written, so
#: no simulated record is stamped in the future.
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
WINDOW = timedelta(days=7)
FIRST_DAY = datetime(2026, 11, 14, tzinfo=UTC)
LAST_DAY = datetime(2026, 11, 22, tzinfo=UTC)


def _week_reader():
    """The seven-day reader, or a failure naming the log's missing span.

    An absent reader rather than an empty log is the defect this file is for: a
    log that spans a minute and one that spans a week look the same from the
    outside, so the reader is asserted before anything is written and the
    message says which fact is missing.
    """
    reader = getattr(verdicts, "read_pass_verdicts", None)
    assert reader is not None, (
        "verdicts older than the retained span are missing from the read: the "
        "admission verdict log has no reader that spans its days"
    )
    return reader


def _appender():
    """The day-partitioned writer the fixture is built with."""
    append = getattr(verdicts, "verdict_log_append", None)
    assert append is not None, (
        "the admission verdict log has no day-partitioned writer, so one day's "
        "records cannot be kept apart from the rest of the week"
    )
    return append


def _record(at: datetime, run_id: str) -> dict[str, Any]:
    """One pass record, one live mean row's worth of bytes."""
    record: dict[str, Any] = {
        "at": at.isoformat(),
        "pointers": [{"run_id": run_id, "verdict": "queued"}],
        "reads": {"wait": [{"read": "pass fixture", "count": 1, "seconds": 0.0}]},
        "filler": "",
    }
    line = json.dumps(record, separators=(",", ":"), default=str)
    record["filler"] = "x" * max(0, round(LIVE_MEAN_ROW_BYTES) - len(line) - 2)
    return record


def _row_bytes() -> int:
    return (
        len(json.dumps(_record(NOW, "probe"), separators=(",", ":"), default=str)) + 1
    )


def _written_days() -> list[tuple[datetime, dict[str, Any]]]:
    """Nine days of passes: the day before the window opens, and the week."""
    written: list[tuple[datetime, dict[str, Any]]] = []
    day = FIRST_DAY
    while day <= LAST_DAY:
        last = NOW if day.date() == NOW.date() else day + timedelta(days=1)
        step = (last - day) // PASSES_PER_DAY
        at = day
        index = 0
        while at < last:
            written.append((at, _record(at, f"r-{day.date().isoformat()}-{index}")))
            at += step
            index += 1
        day += timedelta(days=1)
    return written


@pytest.fixture(scope="module")
def week(tmp_path_factory):
    """Nine simulated days written through the appender, and one read of them.

    The day before the window opens exists only so the retention horizon has
    something to prune. The fixture resolves the crew home through RECKON_HOME,
    so the appender writes where a running service writes rather than through a
    test-only path, and the environment is restored for the rest of the suite.
    """
    home = tmp_path_factory.mktemp("verdict-week")
    patch = pytest.MonkeyPatch()
    patch.setenv("RECKON_HOME", str(home))
    verdicts._RECENT_PASS_CACHE.clear()
    append = _appender()
    written = _written_days()
    try:
        for _at, record in written:
            append(record)
        read = _week_reader()(since=NOW - WINDOW, until=NOW)
        yield {"home": home, "written": written, "read": read}
    finally:
        patch.undo()
        verdicts._RECENT_PASS_CACHE.clear()


@pytest.fixture(autouse=True)
def _read_the_week_home(week, monkeypatch):
    """Point every test at the week's own crew home.

    The suite isolates each test's configuration home, so a home written once
    by a module fixture is not the one a later test resolves, so each test
    names it again before it reads.
    """
    monkeypatch.setenv("RECKON_HOME", str(week["home"]))


def _retained_start() -> datetime:
    """The oldest record the retained log holds after the horizon prunes."""
    return LAST_DAY - timedelta(days=7)


def _retained_span(
    written: list[tuple[datetime, dict[str, Any]]],
) -> tuple[datetime, datetime]:
    """The oldest and newest record a read of the retained log can return."""
    first = min(at for at, _ in written if at >= _retained_start())
    last = max(at for at, _ in written if at <= NOW)
    return first, last


def _window_span(
    written: list[tuple[datetime, dict[str, Any]]],
) -> tuple[datetime, datetime]:
    """The oldest and newest record inside the seven-day window."""
    first = min(at for at, _ in written if NOW - WINDOW <= at <= NOW)
    last = max(at for at, _ in written if NOW - WINDOW <= at <= NOW)
    return first, last


def test_a_read_of_the_last_seven_days_returns_them(week) -> None:
    """Nine days written, and the read of the last seven has every one of them."""
    read = week["read"]
    written = week["written"]
    expected = [(at, record) for at, record in written if NOW - WINDOW <= at <= NOW]
    got = {
        (record["at"], record["pointers"][0]["run_id"]) for record in read["records"]
    }
    want = {(record["at"], record["pointers"][0]["run_id"]) for _at, record in expected}
    assert not (want - got), (
        f"verdicts missing from the seven-day read: {sorted(want - got)[:3]}"
    )
    assert len(read["records"]) == len(expected), (
        f"read {len(read['records'])} of {len(expected)} verdicts in the window"
    )

    # The day that fell outside the window is gone rather than merely filtered:
    # eight files is all a week's read has to parse.
    assert len(read["days"]) == 8, [day["day"] for day in read["days"]]
    current = (week["home"] / "crew" / verdicts.ADMISSION_VERDICT_LOG).read_text()
    todays = [at for at, _ in written if at.date() == NOW.date() and at <= NOW]
    assert len(current.splitlines()) == len(todays), (
        "rotation left the current file holding more than the day in progress"
    )


def test_a_day_of_verdicts_costs_what_its_rows_cost(week) -> None:
    """The bytes kept per day are the rows' own bytes, inside the live ceiling."""
    read = week["read"]
    on_disk = {day["path"]: Path(day["path"]).stat().st_size for day in read["days"]}
    assert on_disk == {day["path"]: day["bytes"] for day in read["days"]}
    for day in read["days"]:
        rows = day["records"]
        assert day["bytes"] <= 2 * rows * LIVE_MEAN_ROW_BYTES, day

    cost = drain_measure.measure_verdict_log_cost(
        bytes_per_day=read["bytes_per_day"],
        verdicts_per_day=PASSES_PER_DAY,
        mean_row_bytes=_row_bytes(),
        measured_on=LIVE_MEASURED_MARK,
    )
    assert cost["status"] == "measured"
    assert cost["measured_on"] == LIVE_MEASURED_MARK
    assert cost["within_ceiling"] is True

    live_ceiling = 2 * LIVE_VERDICT_RATE_PER_DAY * LIVE_MEAN_ROW_BYTES
    assert max(read["bytes_per_day"].values()) <= live_ceiling
    assert (
        max(read["bytes_per_day"].values())
        >= 0.9 * PASSES_PER_DAY * LIVE_MEAN_ROW_BYTES
    )


def test_a_read_older_than_the_log_returns_the_covered_span(week) -> None:
    """A span the log cannot cover is answered with the span it does cover."""
    requested = NOW - timedelta(days=20)

    read = _week_reader()(since=requested, until=NOW)
    first, last = _retained_span(week["written"])

    assert read["covered_start"] == first.isoformat()
    assert read["covered_end"] == last.isoformat()
    assert read["covered_span_seconds"] == round((last - first).total_seconds(), 3)
    assert read["uncovered_seconds"] == round((first - requested).total_seconds(), 3)
    assert read["uncovered_seconds"] > 0


def test_the_gap_measure_reads_a_real_week(week) -> None:
    """The verdict-gap instrument reports a measured week, not a covered sliver."""
    since = NOW - WINDOW
    read = week["read"]

    gap = drain_measure.measure_verdict_gap(
        read["records"], window_start=since, now=NOW
    )
    first, last = _window_span(week["written"])

    assert gap["status"] == "measured"
    assert gap["log_starts_after_window_start_seconds"] == round(
        (first - since).total_seconds(), 3
    )
    assert gap["log_starts_after_window_start_seconds"] < 60.0
    assert gap["log_span_seconds"] == round((last - first).total_seconds(), 3)
    assert gap["waiting_requests"] == len(read["records"])
    assert gap["violations"] == 0


def test_the_append_is_written_by_the_shared_log_writer(tmp_path, monkeypatch):
    """One append owner: a pass line is written by the crew's shared writer.

    The verdict log is one of the crew home's append-only records, so the write
    goes through the writer every other one uses rather than a second copy of
    the same open-write-close beside it.
    """
    from reckon.crew import picker

    monkeypatch.setenv("RECKON_HOME", str(tmp_path))

    seen: list[tuple[str, dict[str, Any]]] = []
    real = picker._append_crew_log_line

    def observing(name: str, row) -> None:
        seen.append((name, dict(row)))
        real(name, row)

    monkeypatch.setattr(picker, "_append_crew_log_line", observing)
    record = _record(NOW, "r-shared-writer")

    verdicts.verdict_log_append(record)

    assert [name for name, _row in seen] == [verdicts.ADMISSION_VERDICT_LOG], (
        "the pass line was not written by the crew's shared log writer"
    )
    assert seen[0][1]["pointers"][0]["run_id"] == "r-shared-writer"
