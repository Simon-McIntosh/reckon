"""Dispatch counts workers against the router's worker slots, not headroom.

The local lane's router publishes an admission block whose figures are its own
arithmetic: a per-session fair share, a share offered to a session it has not
admitted before, and a global pool of extra workers the gate can carry. A
worker holds a request only while it generates and holds nothing while it runs
tools, so holding dispatch on request headroom under-fills the lane by the
inverse of that duty cycle.

Each case below builds a lane document the way the router publishes it — the
document is resolved through the lane-document reader's own path, so the rule
is driven by the same reading production takes — with and without a
``sessions`` block, one case per rung of the rule's order. The declared
negative control restores the headroom-only rule, and the positive per-session
case then holds.
"""

from __future__ import annotations

import importlib

from reckon.crew import lane_document

dispatch_module = importlib.import_module("reckon.crew.dispatch")

SESSION = "s22-coord"

# The observation window the router reports once its rolling ratio has filled
# 15 minutes of history. A figure below it, or no figure at all, leaves the
# router unable to show its slot arithmetic rests on a filled window.
FULL_WINDOW_SECONDS = 900
MEASURED_YOUNG_WINDOW_SECONDS = 320

# The router's own shares, as it publishes them.
PER_SESSION_SLOTS = 4
NEW_SESSION_SLOTS = 5
GLOBAL_SLOTS = 8

# An engine headroom that would hold the dispatch on its own, so a case that
# proceeds proves the slot figure overrode it rather than agreeing with it.
HEADROOM_THAT_WOULD_HOLD = -3


def _admission(fields: dict[str, object]) -> dict[str, object]:
    """One admission block whose keys are the reader's own."""
    return dict(fields)


def _lane_document(
    *,
    admission: dict[str, object] | None = None,
    headroom: float = 20.0,
) -> dict[str, object]:
    """A lane document shaped as the router publishes one."""
    document: dict[str, object] = {"headroom": headroom}
    if admission is not None:
        document[lane_document.ADMISSION_KEY] = admission
    return document


def _allowance(document: dict[str, object]) -> dict[str, object]:
    """Drive the dispatch rule over a document through the reader's path."""
    return dispatch_module._lane_worker_allowance(document, session=SESSION)


def test_a_positive_per_session_share_proceeds_over_negative_headroom() -> None:
    """Rung one: the session's own share, even beside a headroom that would hold.

    A worker holds a request only while it generates, so a headroom of -3 on
    the request count is not a worker count of none. The router's own share for
    this session is the figure dispatch is to count against.
    """
    document = _lane_document(
        headroom=HEADROOM_THAT_WOULD_HOLD,
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: GLOBAL_SLOTS,
                lane_document.ADMISSION_NEW_SESSION_WORKER_SLOTS_KEY: NEW_SESSION_SLOTS,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                lane_document.ADMISSION_SESSIONS_KEY: {
                    SESSION: {
                        lane_document.SESSION_LIVE_RUNS_KEY: 5,
                        lane_document.SESSION_WORKER_SLOTS_KEY: PER_SESSION_SLOTS,
                    }
                },
                "headroom": HEADROOM_THAT_WOULD_HOLD,
                "verdict": "congested",
            }
        ),
    )

    decision = _allowance(document)

    assert decision["allowance"] == PER_SESSION_SLOTS
    assert "session's own" in decision["source"]
    assert decision["held"] is False


def test_an_unlisted_session_uses_the_new_session_share() -> None:
    """Rung two: a session the router has not admitted before takes its share.

    ``sessions`` is present and does not list this session, so the
    new-session figure. The global pool would be the wrong rung.
    """
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: GLOBAL_SLOTS,
                lane_document.ADMISSION_NEW_SESSION_WORKER_SLOTS_KEY: NEW_SESSION_SLOTS,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                lane_document.ADMISSION_SESSIONS_KEY: {
                    "s22-nova": {
                        lane_document.SESSION_LIVE_RUNS_KEY: 2,
                        lane_document.SESSION_WORKER_SLOTS_KEY: 3,
                    }
                },
                "headroom": 6,
                "verdict": "open",
            }
        )
    )

    decision = _allowance(document)

    assert decision["allowance"] == NEW_SESSION_SLOTS
    assert "new-session" in decision["source"]
    assert decision["held"] is False


def test_no_sessions_block_and_a_positive_global_share_proceeds() -> None:
    """Rung three: with no per-session map, the global worker slots are used."""
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: GLOBAL_SLOTS,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                "headroom": 1,
                "verdict": "open",
            }
        )
    )

    decision = _allowance(document)

    assert decision["allowance"] == GLOBAL_SLOTS
    assert "global" in decision["source"]
    assert decision["held"] is False


def test_an_allowance_of_zero_holds_and_names_the_router_verdict() -> None:
    """Rung four: no room granted holds the node, naming the router's verdict."""
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: 0,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                "headroom": 1,
                "verdict": "full",
            }
        )
    )

    decision = _allowance(document)

    assert decision["allowance"] == 0
    assert decision["held"] is True
    assert "full" in decision["reason"]
    assert "the gate is full" in decision["reason"]


def test_a_null_worker_slots_falls_back_to_headroom() -> None:
    """Rung five: no slot figure published, so headroom is the allowance."""
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: None,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                "headroom": 3,
                "verdict": "open",
            }
        ),
        headroom=20,
    )

    decision = _allowance(document)

    assert decision["allowance"] == 3
    assert "headroom" in decision["source"]
    assert decision["held"] is False


def test_a_young_router_without_a_window_falls_back_to_headroom() -> None:
    """Rung six: a router that states no observation history is not counted.

    Its slot ratio cannot be shown to rest on a filled window, so the slot
    figures are distrusted and the request headroom is the allowance.
    """
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: GLOBAL_SLOTS,
                lane_document.ADMISSION_SESSIONS_KEY: {
                    SESSION: {
                        lane_document.SESSION_LIVE_RUNS_KEY: 5,
                        lane_document.SESSION_WORKER_SLOTS_KEY: PER_SESSION_SLOTS,
                    }
                },
                "headroom": 2,
                "verdict": "open",
            }
        )
    )

    decision = _allowance(document)

    assert decision["allowance"] == 2
    assert "headroom" in decision["source"]
    assert decision["held"] is False


def test_a_router_still_filling_its_window_is_distrusted() -> None:
    """The measured young router: 320 s of history is not a filled window.

    Measured on 2026-10-01, a router at 320 s published 57 slots against about
    11 true, so a stated window below the 15-minute bound is not counted either.
    """
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: 57,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: MEASURED_YOUNG_WINDOW_SECONDS,
                "headroom": 9,
                "verdict": "open",
            }
        )
    )

    decision = _allowance(document)

    assert decision["allowance"] == 9
    assert "headroom" in decision["source"]


def test_an_unreadable_allowance_never_holds_a_dispatch() -> None:
    """Absence of a signal is not exhaustion: no figure holds nothing."""
    document = _lane_document(headroom=12)

    decision = _allowance(document)

    assert decision["allowance"] == 12
    assert decision["held"] is False

    empty = dispatch_module._lane_worker_allowance({}, session=SESSION)
    assert empty["allowance"] is None
    assert empty["held"] is False
    assert empty["state"] == "unknown"
