"""A run resumed out of a stop draws the halt and then its launch.

The producer records a resume as a move out of the state the run stopped on
into ``working`` — ``blocked -> working``, not ``dispatched -> working`` and not
``starting -> working``. A move out of any stop state the pane draws as
``halted`` into ``working`` opens a new attempt, so the pane draws it as the
launch: the stream carries the halt the run stopped on and one row for the
resume.

The rows below drive ``PaneRowPath`` with an injected clock and render every row
it releases through one ``Ticker``, so what is measured is the pane's own output
rather than the policy's opinion of it.
"""

from __future__ import annotations

from typing import Any

from reckon.crew import ticker

SESSION = "s-own"
PEER = "s-peer"
RUN = "r-resumed"
PROJECT = "example"
NODE = "example"

# The state the run stopped on, the worked word, and the delivered state the
# manifest reaches at the end of the observed stream.
STOP = "blocked"
WORKING = "working"
DELIVERED = "completed_unpromoted"

# The observed rows are more than the settle window apart, as the producer wrote
# them: the stop at 06:28:04Z, a repeated ``blocked`` at 06:29:38Z, the resume at
# 06:30:42Z, delivery at 06:38:58Z.
STEP = ticker.SETTLE_WINDOW + 1.0


def _row(from_state: str, to_state: str, **extra: Any) -> dict[str, Any]:
    """One transition row as the producer writes it for this run."""
    event: dict[str, Any] = {
        "project": PROJECT,
        "event": "transition",
        "run_id": RUN,
        "node": NODE,
        "session": SESSION,
        "from_state": from_state,
        "to_state": to_state,
        "observed_at": "2026-10-16T06:30:42Z",
    }
    event.update(extra)
    return event


class _Pane:
    """The follower's row path and the grid it draws through, wired together."""

    def __init__(self) -> None:
        self.path = ticker.PaneRowPath(own_session=SESSION)
        self.grid = ticker.Ticker(width=180, color=False, model_aliases=())

    def feed(self, event: dict[str, Any], *, now: float) -> list[tuple[dict, str]]:
        return [
            (row, self.grid.render(row, with_session=True))
            for row in self.path.feed(event, now=now)
        ]

    def flush(self, *, now: float) -> list[tuple[dict, str]]:
        return [
            (row, self.grid.render(row, with_session=True))
            for row in self.path.flush(now=now)
        ]


def _drive(
    pane: _Pane, rows: list[dict[str, Any]], *, start: float = 1000.0
) -> tuple[list[tuple[dict, str]], float]:
    """Feed the rows a step apart, returning what printed and the closing clock."""
    released: list[tuple[dict, str]] = []
    now = start
    for row in rows:
        released += pane.feed(row, now=now)
        now += STEP
    return released, now


def _kinds(rows: list[tuple[dict, str]]) -> list[tuple[Any, Any]]:
    return [(row.get("from_state"), row.get("to_state")) for row, _ in rows]


def _view(event: dict[str, Any]) -> str:
    """The line crew view draws for a row the stream leaves out."""
    return ticker.Ticker(width=180, color=False, model_aliases=()).render(
        event, with_session=True
    )


def _observed_stream() -> list[dict[str, Any]]:
    """The stream the pane watched: a stop, a repeat, the resume, delivery."""
    return [
        _row(WORKING, STOP),
        _row(STOP, STOP),
        _row(STOP, WORKING),
        _row(WORKING, DELIVERED),
    ]


def test_a_resume_out_of_a_stop_asks_the_coordinator_for_nothing() -> None:
    """A resume into a working word prints the halt and one launch row.

    The repeated ``blocked`` row releases the held stop opener, so the pane shows
    the halt. The resume opens the run's next attempt — the worker is working
    again — so it prints as the launch, once, drawn in the view too.
    """
    pane = _Pane()
    released, now = _drive(pane, _observed_stream())

    assert _kinds(released) == [(WORKING, STOP), (STOP, WORKING)], released
    assert all(text for _, text in released), released
    assert _kinds(released).count((STOP, WORKING)) == 1, released
    assert _view(_row(STOP, WORKING)).strip()

    delivered = pane.flush(now=now)
    assert _kinds(delivered) == [(WORKING, DELIVERED)], delivered
    assert all(text for _, text in delivered), delivered


def test_the_halted_row_is_followed_by_the_delivered_row() -> None:
    """The printed rows read halted then delivered, with the resume in crew view."""
    pane = _Pane()
    released, now = _drive(pane, _observed_stream())
    released += pane.flush(now=now)

    words = [ticker._pane_word(row) for row, _ in released]
    assert words == ["halted", "working", "delivered"], released
    assert _view(_row(STOP, WORKING)).strip()


def test_a_peer_sessions_resume_prints_no_working_row() -> None:
    """A peer session's resume is not this pane's to draw.

    The peer's halt still reaches the pane — a coordinator row is not session
    gated — but the resume carries no row, because a start is delivered for the
    pane's own session alone.
    """
    pane = _Pane()
    peer_stream = [
        _row(WORKING, STOP, session=PEER, run_id="r-peer"),
        _row(STOP, STOP, session=PEER, run_id="r-peer"),
        _row(STOP, WORKING, session=PEER, run_id="r-peer"),
        _row(WORKING, DELIVERED, session=PEER, run_id="r-peer"),
    ]
    released, _ = _drive(pane, peer_stream)

    assert (STOP, WORKING) not in _kinds(released), released


def test_each_stop_state_the_pane_draws_halted_leaves_the_resume_out() -> None:
    """Every stop state the pane draws ``halted`` is followed by one launch row.

    The stop vocabulary is the pane's own, so a resume out of ``stalled`` or
    ``exited-unfinished`` opens an attempt just as one out of ``blocked`` does:
    the halt reaches the stream and so does the launch, once, whichever stop
    word the run came out of.
    """
    for stop in ("blocked", "stalled", "exited-unfinished", "abandoned"):
        pane = _Pane()
        released, now = _drive(
            pane,
            [
                _row(WORKING, stop),
                _row(stop, stop),
                _row(stop, WORKING),
            ],
        )
        released += pane.flush(now=now)
        assert _kinds(released).count((stop, WORKING)) == 1, (stop, released)
        assert _view(_row(stop, WORKING)).strip(), stop


def test_a_delivered_run_read_as_working_is_left_to_crew_view() -> None:
    """A producer re-reading a rewritten manifest as working asks for nothing.

    The run reached a finished word the pane shows, so the departure out of it
    and its return are one round trip: the pane draws neither half while the
    window is open, and past the window the move into a working word still asks
    the coordinator for nothing, so crew view is where the departure is drawn.
    """
    pane = _Pane()
    pane.path.remember(RUN, DELIVERED)
    departure = _row(DELIVERED, WORKING)

    released = pane.feed(departure, now=1000.0)
    assert released == [], released

    delivered = pane.flush(now=1000.0 + ticker.SETTLE_WINDOW)
    assert delivered == [], delivered
    assert _view(departure).strip()
