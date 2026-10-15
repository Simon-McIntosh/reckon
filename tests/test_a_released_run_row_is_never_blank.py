"""A row the pane releases is never drawn blank.

``Ticker.render`` draws a transition only where its two halves differ, so a
released row whose destination repeats the word the pane already shows for its
run draws nothing — and a duty the reader never saw is the failure the pane
exists to prevent. ``RowPolicy`` and the renderer read one predicate,
``same_word_row_is_news``, for the same-word case: the policy withholds every
such row that carries no duty, and the renderer draws the ones that do, taking
its left word from the row's own ``from_state`` where that names a different
word and repeating the destination word otherwise.

Every case below drives ``PaneRowPath`` as the follower does and renders each
row it releases through one ``Ticker`` in order. The invariant the file asserts
end to end is that no released row renders empty.
"""

from __future__ import annotations

from typing import Any

from reckon.crew import ticker

SESSION = "s-own"
RUN = "r-example-run"
PROJECT = "example"
NODE = "example"

START = ("dispatched", "working")
UNREADABLE = ("working", "unreadable")
BLOCK = ("unreadable", "blocked")
PROMOTION = ("completed_unpromoted", "complete")
WAIT = ("working", "waiting")

# Every internal state the pane can draw, the lift states excepted: a lift row
# is keyed on its group and decided by ``_feed_lift``, so it is outside this
# node's change.
INTERNAL_STATES = sorted(
    state
    for states in ticker.PANE_STATES.values()
    for state in states
    if state not in ticker.LIFT_STATES
)


def _row(from_state: Any, to_state: str, **extra: Any) -> dict[str, Any]:
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


def _texts(rows: list[tuple[dict, str]]) -> list[str]:
    return [text for _, text in rows]


def test_a_block_the_pane_already_shows_prints_its_duty() -> None:
    """The block lands on the halt the pane already drew, and still prints.

    ``working -> unreadable`` draws ``working -> halted``. A minute later
    ``unreadable -> blocked``, classified ``blocked``, lands on the same pane
    word. It is a duty, so the policy releases it and the renderer draws it
    rather than the empty line the pane would otherwise have recorded as
    delivered.
    """
    pane = _Pane()
    released: list[tuple[dict, str]] = []
    released += pane.feed(_row(*START), now=1000.0)
    released += pane.feed(
        _row(*UNREADABLE, recovery_classification="unreadable"), now=1008.0
    )
    released += pane.flush(now=1040.0)
    released += pane.feed(_row(*BLOCK, recovery_classification="blocked"), now=1060.0)
    released += pane.flush(now=1090.0)

    assert all(text for text in _texts(released)), released
    assert "halted" in released[-1][1], released[-1][1]
    assert released[-1][0]["to_state"] == "blocked", released[-1][0]


def test_a_promotion_echo_is_not_released() -> None:
    """A row that adds nothing to the word the pane shows is withheld.

    The pane drew ``working -> delivered``. The promotion echo
    ``completed_unpromoted -> complete`` lands on that same word and carries no
    duty, so the policy withholds it and it is not recorded as delivered.
    """
    pane = _Pane()
    released: list[tuple[dict, str]] = []
    released += pane.feed(_row(*START), now=1000.0)
    released += pane.feed(
        _row("working", "completed_unpromoted", recovery_classification="promotable"),
        now=1008.0,
    )
    released += pane.flush(now=1040.0)
    released += pane.feed(
        _row(*PROMOTION, recovery_classification="promotable"), now=1060.0
    )
    released += pane.flush(now=1090.0)

    assert "complete" not in [row.get("to_state") for row, _ in released], released
    assert all(text for text in _texts(released)), released


def test_a_same_word_row_with_no_duty_still_renders_empty() -> None:
    """The renderer keeps dropping a duty-free row that repeats the pane word.

    A direct render carries no row policy in front of it, so the renderer is
    the thing that decides: the first streamed row draws from ``new``, and the
    same row again lands on the word it just drew and, carrying no duty, draws
    nothing.
    """
    grid = ticker.Ticker(width=180, color=False, model_aliases=())
    echo = _row(*PROMOTION, recovery_classification="promotable")

    assert grid.render(echo) != "", grid.render(echo)
    assert grid.render(echo) == "", grid.render(echo)


def test_a_declared_wait_prints_when_the_word_repeats() -> None:
    """A baseline wait the pane already shows is still drawn.

    A manifest that asks to wait emits its horizon and overdue flag, so the
    row's condition is readable. The pane already shows the run as waiting, and
    the declaration is a duty, so the baseline row renders rather than
    vanishing.
    """
    pane = _Pane()
    released: list[tuple[dict, str]] = []
    released += pane.feed(_row(*START), now=1000.0)
    released += pane.feed(_row(*WAIT, recovery_classification="waiting"), now=1008.0)
    released += pane.flush(now=1040.0)
    released += pane.feed(
        _row(None, "waiting", expected_horizon_seconds=600, wait_overdue=False),
        now=1060.0,
    )
    released += pane.flush(now=1090.0)

    assert all(text for text in _texts(released)), released
    assert released[-1][0]["expected_horizon_seconds"] == 600, released[-1][0]
    assert "waiting" in released[-1][1], released[-1][1]


def test_every_released_row_renders_non_empty_across_the_pane_words() -> None:
    """Every ordered pair of distinct pane states releases only drawn rows.

    For each pair, a fresh path draws ``dispatched -> first`` and is then fed
    ``first -> second``, every step flushed past its hold. Whatever the policy
    releases for the pair must render to a non-empty line, so the same-word
    case can never be the blank row the follower would record as delivered.
    """
    released_total = 0
    for first in INTERNAL_STATES:
        for second in INTERNAL_STATES:
            if second == first:
                continue
            pane = _Pane()
            released: list[tuple[dict, str]] = []
            released += pane.feed(_row("dispatched", first), now=1000.0)
            released += pane.flush(now=10_000.0)
            released += pane.feed(_row(first, second), now=10_010.0)
            released += pane.flush(now=20_000.0)
            released_total += sum(1 for _, text in released if text)
            blanks = [text for _, text in released if not text]
            assert not blanks, (first, second, released)

    assert released_total > 0, released_total