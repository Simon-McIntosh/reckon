"""A grid seeded from a follower's remembered map shows pane words on the left.

The follower hands the grid the states the pane already showed, merged from its
checkpoint and the stored history, and that map can hold producer states. The
grid reads a row's left word straight from its memory, so a remembered state
left unmapped prints a raw internal word. Every left word the pane draws is
``new`` on a run's first row or one of the seven pane words.
"""

from __future__ import annotations

import re

from reckon.crew import ticker
from reckon.crew_follow_commands import _seed_ticker_memory

# A following transition's destination is chosen so its pane word differs from
# the seeded state's, so a row is always drawn rather than collapsing to a
# ``from == to`` pair. ``promotable`` maps to ``delivered``; a seeded
# ``delivered`` run therefore follows ``promoted`` (``landed``) instead.
_DELIVERED = "promotable"
_NON_DELIVERED = "promoted"


def _left_word(row: str) -> str:
    match = re.search(r"(\S+)\s+" + re.escape(ticker.ARROW), row)
    assert match, f"no transition drawn in {row!r}"
    return match.group(1)


def _render_after_seed(seed_state: str) -> str:
    """Seed one run's memory and return the left word its following row draws."""
    painter = ticker.Ticker(width=200)
    _seed_ticker_memory(painter, {"run": seed_state})
    delivered = ticker._remembered_word(seed_state) == "delivered"
    destination = _NON_DELIVERED if delivered else _DELIVERED
    row = painter.render(
        {
            "event": "transition",
            "run_id": "run",
            "node": "run",
            "from_state": "dispatched",
            "to_state": destination,
        }
    )
    assert row, f"seeding {seed_state!r} drew no transition"
    return _left_word(row)


def test_a_remembered_producer_state_renders_its_pane_word() -> None:
    """The reproduced defect: a run remembered as `completed_unpromoted` and one
    remembered as `working` draw the left words `delivered` and `working`."""
    painter = ticker.Ticker(width=200)
    _seed_ticker_memory(painter, {"done": "completed_unpromoted", "live": "working"})
    done = painter.render(
        {
            "event": "transition",
            "run_id": "done",
            "node": "done",
            "from_state": "dispatched",
            "to_state": _NON_DELIVERED,
        }
    )
    live = painter.render(
        {
            "event": "transition",
            "run_id": "live",
            "node": "live",
            "from_state": "dispatched",
            "to_state": _DELIVERED,
        }
    )
    assert _left_word(done) == "delivered"
    assert _left_word(live) == "working"


def test_every_internal_state_and_pane_word_renders_mapped() -> None:
    """Every internal state draws its pane word on the left, and every one of the
    seven words — the four `_display_state` blanks included — draws itself."""
    for word, states in ticker.PANE_STATES.items():
        for state in states:
            assert _render_after_seed(state) == word
    for word in ticker.PANE_STATES:
        assert _render_after_seed(word) == word
