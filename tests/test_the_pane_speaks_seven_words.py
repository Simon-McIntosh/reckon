"""The pane's state words cover the producer and recovery vocabularies."""

from __future__ import annotations

import re

from reckon.crew import (
    lift_watch,
    recovery_vocabulary,
    recovery_watch,
    ticker,
    watch_unit,
)

WORDS = frozenset(
    {
        "queued",
        "working",
        "waiting",
        "halted",
        "delivered",
        "landed",
        "dropped",
    }
)


def test_every_internal_state_has_exactly_one_pane_word() -> None:
    states = (
        set(recovery_vocabulary.RECOVERY_VERBS)
        | set(recovery_vocabulary.RECOVERY_CLASSES)
        | set(watch_unit.WATCH_ATTENTION_STATES)
        | set(watch_unit.WATCH_PROGRESS_STATES)
        | set(ticker.CLASSIFIER_STATE_WORDS)
        | set(recovery_watch.FLEET_WORKING_STATES)
        | set(recovery_watch.FLEET_UNPROMOTED_STATES)
        | set(recovery_watch.FLEET_WAITING_STATES)
        | set(recovery_watch.FLEET_BLOCKED_STATES)
        | set(recovery_watch.FLEET_SETTLED_STATES)
        | {lift_watch.LIFT_GRANTED, lift_watch.LIFT_IN_FORCE, lift_watch.LIFT_ENDED}
    )
    assert set(ticker.PANE_STATES) == WORDS
    assert {
        state: sum(state in members for members in ticker.PANE_STATES.values())
        for state in states
    } == dict.fromkeys(states, 1)


def test_rows_use_the_pane_words_and_first_row_says_new() -> None:
    painter = ticker.Ticker(width=200)
    event = {
        "event": "transition",
        "run_id": "sample",
        "node": "sample",
        "from_state": "dispatched",
        "to_state": "completed_unpromoted",
    }
    first = painter.render(event)
    assert re.search(r"\bnew\s+→\s+delivered\b", first)
    assert "completed_unpromoted" not in first
    event.update(from_state="completed_unpromoted", to_state="complete")
    assert painter.render(event) == ""
    event["to_state"] = "promoted"
    assert re.search(r"\bdelivered\s+→\s+landed\b", painter.render(event))


def test_counter_labels_and_hues_share_the_pane_words() -> None:
    assert set(ticker._MAX_CELLS) <= WORDS
    assert ticker._MAX_CELLS == ("working", "halted", "delivered", "waiting")
    assert ticker._COUNT_FIELD == {
        "halted": "blocked",
        "delivered": "unpromoted",
        "waiting": "waiting",
    }
    assert all(
        ticker._state_hue(theme, word) != "dim"
        for theme in ("light", "dark")
        for word in WORDS
    )
    text = "".join(
        cell
        for cell, _ in ticker.Ticker()._stats(
            {
                "working": 1,
                "blocked": 2,
                "unpromoted": 3,
                "waiting": 4,
            }
        )
    )
    assert re.search(r"1w\s+2h\s+3d\s+4W", text)
