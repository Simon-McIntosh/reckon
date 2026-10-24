"""A held settle row is never drawn after the run has left the halt behind.

``RowPolicy`` holds a printing coordinator row for the settle window so a chain
written seconds apart prints once, as its latest row. A held settle row is
resolved by a later row that moves its run on to a live pane word: a return to
the word the pane already shows drops the held row and the return together, and
a move to another live word prints the held row at once, ahead of the move. A
later row into a settled word supersedes nothing, so the terminal echo still
leaves a held completion standing.

The rows below drive ``PaneRowPath`` for the pane's own session, as the follower
does, and render every row it releases through one ``Ticker`` in order. Each
case asserts the exact rows that reach the pane: a run that returns to the word
the pane shows inside the window draws neither half, and a run that moves to a
different live word draws the halt at once, ahead of the move.
"""

from __future__ import annotations

from typing import Any

from reckon.crew import ticker

SESSION = "s-own"
RUN = "r-example-run"
PROJECT = "example"
NODE = "example"

START = ("dispatched", "working")
STOP = ("working", "stopped")
RETURN = ("stopped", "working")
WAIT = ("stopped", "waiting")
COMPLETE = ("working", "complete")
ECHO = ("complete", "recorded")
BLOCK = ("working", "blocked")


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
        "observed_at": "2026-10-15T09:19:30Z",
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


def _destinations(rows: list[tuple[dict, str]]) -> list[str]:
    return [str(row.get("to_state") or "") for row, _ in rows]


def _view(event: dict[str, Any]) -> str:
    """The line crew view draws for a row, which the stream need not carry.

    Rendered through a grid of its own, because the pane's grid suppresses a row
    that returns to the word it last drew; a reader coming to the view fresh sees
    the row.
    """
    return ticker.Ticker(width=180, color=False, model_aliases=()).render(
        event, with_session=True
    )


def _texts(rows: list[tuple[dict, str]]) -> list[str]:
    return [text for _, text in rows]


def test_a_return_inside_the_window_leaves_no_halt_on_the_pane() -> None:
    """A redispatch back to working inside the window shows the start and block.

    ``working -> stopped`` is held, and ``stopped -> working`` lands 12 s later,
    inside the settle window. The halt and its return are one round trip, so
    neither draws, and the run's block an hour on is drawn from the word the
    pane still shows.
    """
    pane = _Pane()
    released: list[tuple[dict, str]] = []
    released += pane.feed(_row(*START), now=1000.0)
    released += pane.feed(_row(*STOP, recovery_classification="stopped"), now=1008.0)
    released += pane.feed(_row(*RETURN), now=1020.0)
    released += pane.flush(now=1040.0)
    released += pane.feed(_row(*BLOCK, recovery_classification="blocked"), now=4608.0)
    released += pane.flush(now=4909.0)

    kinds = [(r["from_state"], r["to_state"]) for r, _ in released]
    assert kinds == [START, BLOCK], released
    assert all(text for text in _texts(released)), released
    assert STOP not in kinds, released
    # The launch opens the attempt and is drawn once; the return inside the
    # window is the same round trip, so it draws nothing at all.
    assert kinds.count(START) == 1, released
    assert _view(_row(*START)).strip(), _view(_row(*START))
    assert RETURN not in kinds, released


def test_a_return_after_the_window_draws_both_halves_as_before() -> None:
    """A return past the settle window is a real departure and a real return.

    The stop's window closes before the return arrives, so the pane draws
    ``working -> halted`` and then ``halted -> working``, exactly as it did
    before the hold was resolved on the row that moved the run on.
    """
    pane = _Pane()
    released: list[tuple[dict, str]] = []
    released += pane.feed(_row(*START), now=1000.0)
    released += pane.feed(_row(*STOP, recovery_classification="stopped"), now=1008.0)
    released += pane.feed(_row(*RETURN), now=1048.0)
    released += pane.flush(now=1060.0)
    released += pane.feed(_row(*BLOCK, recovery_classification="blocked"), now=4608.0)
    released += pane.flush(now=4909.0)

    kinds = [(r["from_state"], r["to_state"]) for r, _ in released]
    assert kinds == [START, STOP, RETURN, BLOCK], released
    assert all(text for text in _texts(released)), released
    assert "halted" in released[1][1], released[1][1]
    # The launch and the return each open an attempt, so both are drawn once,
    # beside the halt the run left behind.
    assert kinds.count(START) == 1 and kinds.count(RETURN) == 1, released


def test_a_move_to_another_live_word_draws_the_halt_at_once() -> None:
    """A run that moves to a different live word prints the halt ahead of it.

    ``stopped -> waiting`` lands inside the window and does not return the run to
    the word the pane shows, so the held halt prints at once and the move is
    judged with the run already told: ``working -> halted`` then
    ``halted -> waiting``, both in the same step.
    """
    pane = _Pane()
    released: list[tuple[dict, str]] = []
    released += pane.feed(_row(*START), now=1000.0)
    released += pane.feed(_row(*STOP, recovery_classification="stopped"), now=1008.0)
    released += pane.feed(_row(*WAIT), now=1020.0)

    assert [(r["from_state"], r["to_state"]) for r, _ in released] == [
        START,
        STOP,
    ], released
    assert all(text for text in _texts(released)), released
    assert "halted" in released[1][1], released[1][1]
    # A move to another live word asks nothing unless it also declares a wait, so
    # the stream withholds it and crew view still draws it.
    assert _view(_row(*WAIT)).strip(), _view(_row(*WAIT))
    assert WAIT not in [(r["from_state"], r["to_state"]) for r, _ in released], released


def test_the_terminal_echo_still_supersedes_nothing() -> None:
    """A held completion is released on its own window, not by the echo.

    ``complete -> recorded`` lands in a settled word, so it resolves nothing: the
    held completion still reaches the pane when its own window closes.
    """
    pane = _Pane()
    released: list[tuple[dict, str]] = []
    released += pane.feed(_row(*START), now=1000.0)
    released += pane.feed(
        _row(*COMPLETE, recovery_classification="complete"), now=1008.0
    )
    released += pane.feed(_row(*ECHO), now=1012.0)
    released += pane.flush(now=1030.0)

    kinds = [(r["from_state"], r["to_state"]) for r, _ in released]
    assert kinds == [START, COMPLETE], released
    assert all(text for text in _texts(released)), released
    # The launch opens the attempt. The echo arrives before the pane has shown
    # the delivery it follows, so it is not yet a landing and draws nothing,
    # while the held completion reaches the pane on its own window.
    assert ECHO not in kinds, released
    assert _view(_row(*ECHO)).strip(), _view(_row(*ECHO))
