"""A wait that recovers from its aged form prints no second row.

``wait-aged`` and ``waiting`` draw one pane word, ``waiting``. The move into
the aged state is news because the wait has become overdue; the return to a
pending wait is not, because nothing the coordinator must act on has changed —
the same declaration is still being waited on. Yet both rows carry the wait
declaration, so a same-word rule that reads one row alone keeps both and the
pane draws ``waiting -> waiting``.

The row policy therefore remembers the wait facts of the row it last showed
for a run, and the same-word rule reads that memory: a same-word row is news
where it newly sets
``wait_overdue``, or where its declaration — the probe's verdict, the last
observation, the horizon, the brief — differs from the shown one. A row that
only clears the overdue flag with the declaration otherwise unchanged is a
re-derivation and draws nothing.

Each case drives ``RowPolicy`` as the follower does, spacing the rows past the
settle window so each is judged on its own account rather than as one chain,
and renders every row it releases through one ``Ticker`` in order so a
released row is also checked never to draw blank.
"""

from __future__ import annotations

from typing import Any

from reckon.crew import ticker

SESSION = "s-own"
RUN = "r-example-run"
PROJECT = "example"
NODE = "example"

# The declaration a run parks on, as the transition event carries it.
WAIT = {
    "wait_condition_state": "pending",
    "wait_observed": "QUEUED",
    "expected_horizon_seconds": 3600.0,
    "resume_brief": "collect the scheduler result",
}

# Every row is spaced past the settle window so a coordinator row is judged on
# its own account rather than superseded by the next one in a chain.
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
        "observed_at": "2026-10-15T09:19:30Z",
    }
    event.update(extra)
    return event


def _sequence(return_row: dict[str, Any]) -> list[tuple[dict[str, Any], float]]:
    """The declared wait, its aging, then the return to waiting."""
    return [
        (_row("working", "waiting", wait_overdue=False, **WAIT), 0.0),
        (_row("waiting", "wait-aged", wait_overdue=True, **WAIT), STEP),
        (return_row, 2 * STEP),
    ]


def _printed(sequence: list[tuple[dict[str, Any], float]]) -> list[tuple[str, str]]:
    """Feed the rows through the policy and render each released row in turn.

    A released row is drawn through the grid the follower uses, so the case
    also asserts the pane never receives an empty line: the policy's and the
    renderer's same-word judgement are one rule and must release and draw the
    same rows.
    """
    policy = ticker.RowPolicy(own_session=SESSION)
    grid = ticker.Ticker(width=180, color=False, model_aliases=())
    printed: list[tuple[str, str]] = []
    for event, now in sequence:
        for released in policy.feed(event, now=now):
            assert grid.render(released), f"the pane drew {released} as a blank line"
            printed.append(
                (str(released.get("from_state")), str(released.get("to_state")))
            )
    for released in policy.flush(now=float("inf")):
        assert grid.render(released), f"the pane drew {released} as a blank line"
        printed.append((str(released.get("from_state")), str(released.get("to_state"))))
    return printed


def test_a_return_to_waiting_after_the_wait_aged_prints_no_row() -> None:
    """A return clearing only the overdue flag, declaration unchanged, is silent."""
    printed = _printed(
        _sequence(_row("wait-aged", "waiting", wait_overdue=False, **WAIT))
    )

    assert printed == [("working", "waiting"), ("waiting", "wait-aged")], printed


def test_a_return_with_a_changed_condition_prints() -> None:
    """A return whose declaration differs from the shown one is news."""
    changed = {**WAIT, "wait_condition_state": "terminal"}
    printed = _printed(
        _sequence(_row("wait-aged", "waiting", wait_overdue=False, **changed))
    )

    assert printed == [
        ("working", "waiting"),
        ("waiting", "wait-aged"),
        ("wait-aged", "waiting"),
    ], printed


def test_a_wait_that_newly_sets_overdue_is_news() -> None:
    """The aging row itself is news even though only the overdue flag moved."""
    policy = ticker.RowPolicy(own_session=SESSION)
    waiting = _row("working", "waiting", wait_overdue=False, **WAIT)
    assert policy.feed(waiting, now=0.0) == [], (
        "the wait itself is held for its settle window"
    )
    printed = policy.feed(
        _row("waiting", "wait-aged", wait_overdue=True, **WAIT), now=STEP
    )
    printed.extend(policy.flush(now=float("inf")))
    assert [(r["from_state"], r["to_state"]) for r in printed] == [
        ("working", "waiting"),
        ("waiting", "wait-aged"),
    ], printed
