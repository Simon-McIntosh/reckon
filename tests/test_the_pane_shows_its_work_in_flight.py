"""The pane carries a standing line for the session's runs that are working.

A row reports a transition, so a run that is simply working is invisible once
its start has scrolled out of the pane. The line this module drives names each
of the session's working runs with its phase, its age and the age of its
stream, redrawn on a cadence and whenever the set of working runs changes. A
peer session's run is never named, and a session with nothing working prints
no line at all.

The clock is a stub and the view is synthesised in the live view's own row
shape, so each case states the instant it reads and the ages this instant
serves rather than waiting for them to move.
"""

from __future__ import annotations

import os

import pytest

from reckon.crew import ticker

SESSION = "s-own"
PEER = "s-peer"
WORKING_A = "r-own-rate-placement"
WORKING_B = "r-own-line-arm"

# The declared mutation: disable the standing line, so the first case prints
# nothing while both runs work. Set the environment variable to run the red arm.
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_IN_FLIGHT_LINE_OFF") == "1"
NEGATIVE_LINE_OFF = (
    "disable the standing in-flight line; the two working runs must print no line"
)


def _live_row(
    run_id: str,
    node: str,
    *,
    session: str = SESSION,
    phase: str = "working",
    classification: str | None = None,
    age: float = 420.0,
    stream: float | None = 4.0,
) -> dict:
    """One row in the shape the live view serves.

    ``phase`` is what the run's own evidence supports and ``classification`` the
    fleet's word for it; a working run carries both as ``working`` unless a case
    is exercising a stop that leaves the phase behind.
    """
    return {
        "run_id": run_id,
        "node": node,
        "session": session,
        "phase": phase,
        "classification": classification or phase,
        "elapsed_seconds": age,
        "log_age_seconds": stream,
    }


@pytest.fixture(autouse=True)
def _standing_line(monkeypatch):
    """Disable the line under the declared mutation, so the first case goes red."""
    if NEGATIVE_CONTROL:
        monkeypatch.setattr(
            ticker.InFlightLine,
            "emit",
            lambda self, *args, **kwargs: None,
        )


def _line() -> ticker.InFlightLine:
    return ticker.InFlightLine(ticker.Ticker(width=180, color=False))


def test_two_working_runs_print_one_line_naming_both() -> None:
    """Both working runs appear on one line, each with phase, age and stream age."""
    rows = [
        _live_row(WORKING_A, "vs1-rate-placement", age=7_320.0, stream=4.0),
        _live_row(WORKING_B, "line-arm-screen-rows", age=420.0, stream=12.0),
    ]
    line = _line().emit(rows, session=SESSION, now=1_000.0)

    assert line is not None, "two working runs must print the standing line"
    assert line.startswith("in flight"), line
    assert "vs1-rate-placement" in line, line
    assert "line-arm-screen-rows" in line, line
    for word in ("working", "2h02m", "7m", "stream 4s", "stream 12s"):
        assert word in line, (word, line)
    assert line.count("stream") == 2, line


def test_the_line_is_refreshed_after_the_cadence() -> None:
    """Within a cadence nothing is printed, and the next cadence draws the line."""
    line = _line()
    rows = [_live_row(WORKING_A, "vs1-rate-placement", age=420.0, stream=4.0)]
    first = line.emit(rows, session=SESSION, now=1_000.0)
    assert first is not None
    assert line.emit(rows, session=SESSION, now=1_030.0) is None

    refreshed = [_live_row(WORKING_A, "vs1-rate-placement", age=480.0, stream=14.0)]
    second = line.emit(refreshed, session=SESSION, now=1_070.0)
    assert second is not None, "the line must appear again after the cadence"
    assert second != first, "the refresh carries the figures this instant serves"
    assert "8m" in second and "stream 14s" in second, second


def test_a_changed_set_of_working_runs_draws_the_line_at_once() -> None:
    """A run starting or stopping is news, so it does not wait for the cadence."""
    line = _line()
    one = [_live_row(WORKING_A, "vs1-rate-placement")]
    assert line.emit(rows=one, session=SESSION, now=1_000.0) is not None
    assert line.emit(rows=one, session=SESSION, now=1_010.0) is None

    two = [*one, _live_row(WORKING_B, "line-arm-screen-rows")]
    changed = line.emit(two, session=SESSION, now=1_012.0)
    assert changed is not None, "a second working run must redraw the line"
    assert "line-arm-screen-rows" in changed, changed


def test_a_run_of_another_session_is_not_named() -> None:
    """The line is the pane's own work: a peer's working run is left out."""
    rows = [
        _live_row(WORKING_A, "vs1-rate-placement"),
        _live_row("r-peer", "peer-node", session=PEER),
    ]
    line = _line().emit(rows, session=SESSION, now=1_000.0)

    assert line is not None
    assert "vs1-rate-placement" in line, line
    assert "peer-node" not in line and "r-peer" not in line, line


def test_a_stopped_run_is_not_in_flight() -> None:
    """A stalled run keeps its pointer phase but is not working, so it is unnamed."""
    rows = [_live_row(WORKING_A, "vs1-rate-placement", classification="stalled")]
    assert _line().emit(rows, session=SESSION, now=1_000.0) is None


def test_a_session_with_nothing_working_prints_no_line() -> None:
    """No working run, no line — and the next one to start arrives at once."""
    rows = [
        _live_row(
            WORKING_A, "vs1-rate-placement", phase="queued", classification="queued"
        ),
        _live_row("r-peer", "peer-node", session=PEER),
    ]
    line = _line()
    assert line.emit(rows, session=SESSION, now=1_000.0) is None
    assert line.emit(rows, session=SESSION, now=1_005.0) is None

    started = [_live_row(WORKING_A, "vs1-rate-placement")]
    assert line.emit(started, session=SESSION, now=1_006.0) is not None
