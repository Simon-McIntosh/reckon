"""A run the pane's session owns prints one row when it starts working.

A run can reach the ``working`` word from more than a fresh launch. An
admission pass launches a request the pane already listed as ``queued``, so the
first row a reader sees is ``queued -> working``; and a follower that attaches
after a run began delivers that run's first row already in ``working``, with no left
side at all. Both are the work beginning, which is the news the pane exists to
show, so each prints exactly one row — for the pane's own session, and for no
other.

The clock is a stub, so a case states the instant each row arrives and a hold's
window is measured rather than waited out.
"""

from __future__ import annotations

from reckon.crew import ticker

SESSION = "s-own"
PEER = "s-peer"
RUN = "r-own-1"


def _row(from_state, to_state, **overrides):
    row = {
        "event": "transition",
        "run_id": RUN,
        "node": "claude-1",
        "session": SESSION,
        "project": "reckon",
        "lane": "local",
        "model": "deepseek-v4.1-flash",
        "from_state": from_state,
        "to_state": to_state,
    }
    row.update(overrides)
    return row


def test_a_queued_run_starting_prints_one_row() -> None:
    """``queued -> working`` is the admission pass launching the work.

    The pane already showed the request queued, so this row is the work
    beginning under it. It prints once, and a repeated row of the same pair adds
    nothing because the pane already shows the run working.
    """
    path = ticker.PaneRowPath(own_session=SESSION)
    rows = path.feed(_row("queued", "working"), now=0.0)
    assert len(rows) == 1, rows
    assert ticker._pane_word(rows[0]) == "working", rows
    assert path.feed(_row("queued", "working"), now=1.0) == []
    assert path.flush(now=1e9) == []


def test_a_first_row_already_working_prints_one_row() -> None:
    """A follower attaching mid-run delivers the row with no left side.

    The row carries ``None`` for ``from_state`` and already reads ``working``.
    The pane paints its missing left word itself, so the row reads as the work
    beginning, and it prints once.
    """
    path = ticker.PaneRowPath(own_session=SESSION)
    rows = path.feed(_row(None, "working"), now=0.0)
    assert len(rows) == 1, rows
    assert ticker._pane_word(rows[0]) == "working", rows
    assert "working" in ticker.Ticker().render(rows[0])
    assert path.feed(_row(None, "working"), now=1.0) == []
    assert path.flush(now=1e9) == []


def test_inventory_already_working_is_not_a_start() -> None:
    """A re-arm baseline describes a run already on screen, so it draws nothing.

    The baseline carries the same ``None -> working`` pair a follower's first
    row does, but it is inventory taken when the pane attaches rather than a
    run beginning, and the pane has no reader waiting to see a run that was
    already working before this pane existed.
    """
    path = ticker.PaneRowPath(own_session=SESSION)
    baseline = {**_row(None, "working"), "event": "baseline"}
    assert path.feed(baseline, now=0.0) == []
    assert path.flush(now=1e9) == []


def test_a_follower_attaching_after_the_start_prints_no_second_row() -> None:
    """One attempt prints one start, whichever row announces it first.

    The admission pass prints ``queued -> working``. The follower that attaches
    while the run works then delivers the run's first row already in
    ``working``, with no left side, and the pane already shows this run working:
    that row is the same attempt's start seen again and adds nothing.
    """
    path = ticker.PaneRowPath(own_session=SESSION)
    first = path.feed(_row("queued", "working"), now=0.0)
    assert len(first) == 1, first
    assert path.feed(_row(None, "working"), now=2.0) == []
    assert path.flush(now=1e9) == []


def test_another_sessions_start_prints_nothing() -> None:
    """Neither start is this pane's to show when the run belongs to a peer."""
    path = ticker.PaneRowPath(own_session=SESSION)
    peer = {"session": PEER, "run_id": "r-peer-1"}
    assert path.feed(_row("queued", "working", **peer), now=0.0) == []
    assert path.feed(_row(None, "working", **peer), now=1.0) == []
    assert path.flush(now=1e9) == []
