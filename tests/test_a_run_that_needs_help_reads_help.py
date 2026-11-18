"""A run that needs its coordinator draws ``help``; one waiting on other work draws ``waiting``.

``needs you`` was ambiguous: "you" did not say who, and many runs in the bucket
needed nothing from the reader. The bucket held two different situations — a run
that cannot move until the coordinator acts, and one that moves on its own once
other work finishes — so the pane told them apart. The requirement for help is
now the word ``help``, counted under ``h``; a run waiting on an upstream section,
a held refusal, an interruption or a readiness is ``waiting`` and counts under
``q``, because nothing about it asks the reader for anything.

The fleet's own partition is the authority for both figures: a row's word is the
bucket that state belongs to and a counter's letter names the bucket it counts,
so this test drives the pane's figures through the producer's partition rather
than asserting a fixture's opinion of them.

A checkpoint, a stored history and a delivered-state record hold the word the
pane drew when each was written, so a record written before this rename says
``needs you`` for the states that now draw ``help``. One mapping reads such a
word as the word the pane draws now, and the grid's remembered-word path reads
it there.
"""

from __future__ import annotations

from typing import Any

from reckon.crew import recovery_watch
from reckon.crew import ticker as ticker_module
from reckon.crew.recovery import _fleet_counts, _watch_transition

WIDTH = 180

# The word the pane drew before this rename, written here as a literal because a
# record already on disk holds it and nothing in the tree produces it any more.
OLD_WORD = "needs you"


def _snapshot(
    run_id: str,
    state: str,
    *,
    classification: str | None = None,
) -> dict[str, Any]:
    """One live pointer reduced to the facts the partition and the row read."""
    return {
        "run_id": run_id,
        "node": run_id,
        "state": state,
        "detail": "",
        "recovery_classification": classification or state,
        "recovery": "",
        "role": "implement",
        "model": "dsv4-flash",
        "effort": "high",
        "alias": "",
        "session": "",
    }


def _render(state: str, *, classification: str | None = None) -> str:
    """The pane's row for one run in this state, drawn through the producer."""
    snapshot = _snapshot(f"r-{state}", state, classification=classification)
    counts = _fleet_counts({snapshot["run_id"]: snapshot})
    row = _snapshot(f"r-{state}", state, classification=classification)
    event = _watch_transition(
        "proj",
        kind="baseline",
        snapshot=row,
        previous=None,
        current=state,
        counts=counts,
        spend_runs=[],
        rate_statuses={},
    )
    return ticker_module.Ticker(width=WIDTH).render(event)


def test_a_blocked_run_draws_help_and_counts_under_h() -> None:
    """A blocked run cannot move without its coordinator, so the word says help."""
    line = _render("blocked")

    assert "→ help" in line, line
    assert "1h" in line, line
    assert "1q" not in line, line
    assert ticker_module.STAT_LETTER["help"] == "h"


def test_a_run_waiting_on_other_work_draws_waiting_and_counts_under_q() -> None:
    """Work that lifts itself asks for nothing, so it reads as waiting and counts q."""
    for state in ("blocked-upstream", "held"):
        line = _render(state)

        assert "→ waiting" in line, (state, line)
        assert "1q" in line, (state, line)
        assert "1h" not in line, (state, line)
        assert f"→ {OLD_WORD}" not in line, (state, line)


def test_a_stored_needs_you_word_restores_as_help() -> None:
    """A record written before the rename draws the word the pane draws now."""
    assert ticker_module._remembered_word(OLD_WORD) == "help"

    policy = ticker_module.RowPolicy()
    policy.seed("r-stored", OLD_WORD)
    assert policy._shown["r-stored"] == "help"

    # The older word of the same group still reads as the group's word: the
    # mapping added here must not displace the one that was already there.
    assert ticker_module._remembered_word("halted") == "help"


def test_every_state_the_fleet_calls_blocked_draws_help() -> None:
    """Every state in the fleet's blocked set is the coordinator's, so each reads help."""
    blocked = set(recovery_watch.FLEET_BLOCKED_STATES)

    assert blocked <= set(ticker_module.PANE_STATES["help"])
    for state in sorted(blocked):
        line = _render(state)

        assert "→ help" in line, (state, line)

    # And no word the pane can emit or count still names the superseded word:
    # every set here is what a re-armed pane draws or labels its figures with.
    words = (
        set(ticker_module.PANE_STATES)
        | set(ticker_module.STAT_LETTER)
        | set(ticker_module._MAX_CELLS)
        | set(ticker_module.CARRIED_PANE_WORDS)
    )
    assert OLD_WORD not in words
    for theme in ("light", "dark"):
        assert OLD_WORD not in ticker_module.DISPLAY_HUE[theme]
