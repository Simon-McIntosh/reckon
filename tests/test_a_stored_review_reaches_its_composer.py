"""A review that stores its record tells the session that composed it.

A review run does not pass through a delivery: it works, stores its record and
is recorded. The station writes that as ``working -> recorded``, which a pane
draws as a landing. A landing used to print only where the pane had already
drawn the run's delivery, so the end of a review printed nothing at all, and
the coordinator that composed it was never told its findings were ready. Two
sessions sat idle for about ten hours on 20 November on reviews that had
finished in under ten minutes.

The rows below are the station's own, in the order it wrote them for those two
runs: one that went straight to its record, and one that was queued for
promotion on the way.
"""

from __future__ import annotations

from typing import Any

from reckon.crew import ticker

SESSION = "s-own"
RUN = "r-plan-review-of-a-plan"


def _row(from_state: str, to_state: str, **extra: Any) -> dict[str, Any]:
    return {
        "event": "transition",
        "project": "proj",
        "session": SESSION,
        "run_id": RUN,
        "node": "plan-review-of-a-plan",
        "role": "review",
        "from_state": from_state,
        "to_state": to_state,
        # The fields the station carries on a review run's row while its
        # worker is the last thing it read: nothing here raises a duty.
        "duty": "observer",
        "classification": "running",
        "recovery_classification": "running",
        "recovery": "observe",
        "process_alive": True,
        "liveness_proven": True,
        **extra,
    }


def _words(rows: list[dict[str, Any]]) -> list[str]:
    return [ticker._pane_word(row) for row in rows]


def _drawn(path: ticker.PaneRowPath, rows: list[dict[str, Any]]) -> list[dict]:
    printed: list[dict[str, Any]] = []
    moment = 1000.0
    for row in rows:
        printed += path.feed(row, now=moment)
        moment += 120.0
    printed += path.flush(now=moment + 3600.0)
    return printed


def test_a_review_recorded_from_working_is_drawn() -> None:
    path = ticker.PaneRowPath(own_session=SESSION)

    printed = _drawn(path, [_row("dispatched", "working"), _row("working", "recorded")])

    assert _words(printed) == ["working", "landed"], _words(printed)
    assert printed[-1]["to_state"] == "recorded"


def test_a_review_recorded_after_a_promotion_queue_is_drawn_once() -> None:
    path = ticker.PaneRowPath(own_session=SESSION)

    printed = _drawn(
        path,
        [
            _row("dispatched", "working"),
            _row("working", "promotion-queued"),
            _row("promotion-queued", "recorded"),
        ],
    )

    assert _words(printed).count("landed") == 1, _words(printed)
    assert printed[-1]["to_state"] == "recorded"


def test_a_landing_the_pane_never_carried_stays_held() -> None:
    """A pane that drew nothing for the run in this image prints no echo."""
    path = ticker.PaneRowPath(own_session=SESSION)

    printed = _drawn(path, [_row("working", "recorded")])

    assert "landed" not in _words(printed), _words(printed)


def test_a_second_landing_row_is_not_drawn_again() -> None:
    path = ticker.PaneRowPath(own_session=SESSION)

    printed = _drawn(
        path,
        [
            _row("dispatched", "working"),
            _row("working", "recorded"),
            _row("recorded", "promoted"),
        ],
    )

    assert _words(printed).count("landed") == 1, _words(printed)


def test_the_echo_of_a_delivery_the_pane_has_not_drawn_stays_held() -> None:
    """A landing that leaves a settled state is an echo, not the run's end."""
    path = ticker.PaneRowPath(own_session=SESSION)

    printed: list[dict[str, Any]] = []
    printed += path.feed(_row("dispatched", "working"), now=1000.0)
    printed += path.feed(_row("complete", "recorded"), now=1001.0)

    assert "landed" not in _words(printed), _words(printed)
