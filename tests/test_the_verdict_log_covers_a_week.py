"""The admission verdict log keeps the week a drain is measured over.

The pass log was one file trimmed to a megabyte on every append. On 22 November
2026 the live log held 54 passes spanning 2,201 s, so the verdict-gap measure,
which reads a seven-day window, could not be read: a request that waited across
the start of the window had no rows to read. This pins the replacement — records
kept per day, retained for a week, and a read that tells its caller the span it
actually covers rather than handing back a window it cannot support.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon.crew import drain_measure
from reckon.crew.admission import verdicts

#: Measured on the live log at ``~/.config/reckon/crew/admission-verdicts.jsonl``
#: on 2026-11-22 at 19:03 UTC: 54 pass records over 4,380.5 s, an average row of
#: 19,259.6 bytes. The rate is that count over that span in days. Both are the
#: figures the retained log's cost is bounded against.
LIVE_VERDICT_RATE_PER_DAY = 1_065.1
LIVE_MEAN_ROW_BYTES = 19_259.6
LIVE_MEASURED_MARK = (
    "2026-11-22T19:03:39Z at ~/.config/reckon/crew/admission-verdicts.jsonl"
)

#: The instant the simulated week is read from: noon on the last day written, so
#: no simulated record is stamped in the future.
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
WINDOW = timedelta(days=7)
FIRST_DAY = datetime(2026, 11, 14, tzinfo=UTC)
LAST_DAY = datetime(2026, 11, 22, tzinfo=UTC)

#: How many passes a simulated day holds, two hours apart. A day stays light
#: while still giving the gap measure several verdicts to read.
PASSES_PER_DAY = 12


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
    """One pass record, sized near a live row so the byte figures mean something."""
    return {
        "at": at.isoformat(),
        "pointers": [{"run_id": run_id, "verdict": "queued"}],
        "reads": {"wait": [{"read": "pass fixture", "count": 1, "seconds": 0.0}]},
        "filler": "x" * 2000,
    }


def _written_days() -> list[tuple[datetime, dict[str, Any]]]:
    """Eight days of passes, plus the day before the window opens.

    2026-11-14 is written only so the retention horizon has something to prune.
    The eight days from 11-15 to 11-22 are the week a read must return, with the
    final day written only up to :data:`NOW`.
    """
    written: list[tuple[datetime, dict[str, Any]]] = []
    step = timedelta(hours=24 // PASSES_PER_DAY)
    day = FIRST_DAY
    while day <= LAST_DAY:
        last = NOW if day.date() == NOW.date() else day + timedelta(days=1)
        at = day
        index = 0
        while at < last:
            written.append((at, _record(at, f"r-{day.date().isoformat()}-{index}")))
            at += step
            index += 1
        day += timedelta(days=1)
    return written


def _write(home: Path) -> list[tuple[datetime, dict[str, Any]]]:
    append = _appender()
    written = _written_days()
    for _at, record in written:
        append(record, home=home)
    return written


def _log_path(home: Path) -> Path:
    return home / verdicts.ADMISSION_VERDICT_LOG


def _retained_start() -> datetime:
    """The oldest record the retained log holds after the horizon prunes."""
    return LAST_DAY - timedelta(days=7)


def _retained_span() -> tuple[datetime, datetime]:
    """The oldest and newest record a read of the retained log can return."""
    written = _written_days()
    first = min(at for at, _ in written if at >= _retained_start())
    last = max(at for at, _ in written if at <= NOW)
    return first, last


def _window_span() -> tuple[datetime, datetime]:
    """The oldest and newest record inside the seven-day window."""
    written = _written_days()
    first = min(at for at, _ in written if NOW - WINDOW <= at <= NOW)
    last = max(at for at, _ in written if NOW - WINDOW <= at <= NOW)
    return first, last


def test_a_read_of_the_last_seven_days_returns_them(tmp_path: Path) -> None:
    """Eight days written, and the read of the last seven has every one of them."""
    reader = _week_reader()
    written = _write(tmp_path)

    read = reader(since=NOW - WINDOW, until=NOW, home=tmp_path)

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
    current = _log_path(tmp_path).read_text(encoding="utf-8")
    assert len(current.splitlines()) == 6, (
        "rotation left the current file holding more than the day in progress"
    )


def test_a_day_of_verdicts_costs_what_its_rows_cost(tmp_path: Path) -> None:
    """The bytes kept per day are reported and stay inside the ceiling."""
    reader = _week_reader()
    _write(tmp_path)
    read = reader(since=NOW - WINDOW, until=NOW, home=tmp_path)

    records = sum(day["records"] for day in read["days"])
    total = sum(day["bytes"] for day in read["days"])
    mean_row = total / records

    cost = drain_measure.measure_verdict_log_cost(
        bytes_per_day=read["bytes_per_day"],
        verdicts_per_day=PASSES_PER_DAY,
        mean_row_bytes=mean_row,
        measured_on=LIVE_MEASURED_MARK,
    )
    assert cost["status"] == "measured"
    assert cost["measured_on"] == LIVE_MEASURED_MARK
    assert cost["within_ceiling"] is True
    assert abs(cost["one_copy_bytes_per_day"] - PASSES_PER_DAY * mean_row) < 1.0

    # The bound the deliverable names, at the rate and the row size measured on
    # the live log: a day may cost at most twice one copy of that day's rows.
    live_ceiling = 2 * LIVE_VERDICT_RATE_PER_DAY * LIVE_MEAN_ROW_BYTES
    assert max(read["bytes_per_day"].values()) <= live_ceiling


def test_a_read_older_than_the_log_returns_the_covered_span(tmp_path: Path) -> None:
    """A span the log cannot cover is answered with the span it does cover."""
    reader = _week_reader()
    _write(tmp_path)
    requested = NOW - timedelta(days=20)

    read = reader(since=requested, until=NOW, home=tmp_path)
    first, last = _retained_span()

    assert read["covered_start"] == first.isoformat()
    assert read["covered_end"] == last.isoformat()
    assert read["covered_span_seconds"] == (last - first).total_seconds()
    assert read["uncovered_seconds"] == (first - requested).total_seconds()
    assert read["uncovered_seconds"] > 0


def test_the_gap_measure_reads_a_real_week(tmp_path: Path) -> None:
    """The verdict-gap instrument reports a measured week, not a covered sliver."""
    reader = _week_reader()
    _write(tmp_path)
    since = NOW - WINDOW
    read = reader(since=since, until=NOW, home=tmp_path)

    gap = drain_measure.measure_verdict_gap(
        read["records"], window_start=since, now=NOW
    )
    first, last = _window_span()

    assert gap["status"] == "measured"
    assert gap["log_starts_after_window_start_seconds"] == 0.0
    assert gap["log_span_seconds"] == (last - first).total_seconds()
    assert gap["waiting_requests"] == len(read["records"])
    assert gap["violations"] == 0
