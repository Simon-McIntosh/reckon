"""An admitted request continues its row rather than departing and re-arrival.

The admission pass launches a queued request by releasing its pointer and
publishing the launch under a new run id, so for the length of a launch the
request is absent from the fleet while no departure explains it. The fold must
read that absence as the request continuing its story: it leaves silently once a
run naming it in ``admitted_from`` arrives, keeps its remembered state while a
handover record names it, and holds one more observation otherwise. A fold that
instead worded the absence a withdrawal would draw ``queued -> dropped`` and
``dropped -> queued`` for a request that never stopped being queued.

Each transition also carries ``session_counts``, the fleet partition per owning
session taken at that transition, so a scoped follower reads its own entry
rather than re-reading the live pointers when it draws the row.
"""

from __future__ import annotations

from typing import Any

import pytest

from reckon.crew import recovery, recovery_watch
from reckon.crew.ticker import Ticker

PROJECT = "proj"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every pointer, discard and handover path in temporary state.

    The fold reads the run directories and the discards spool, so a test that
    let them resolve to the real configuration home would both read and could
    write outside its own tree.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _snap(
    run_id: str,
    state: str,
    *,
    session: str = "",
    admitted_from: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One live pointer reduced to the facts the fold and the row read."""
    snapshot = {
        "run_id": run_id,
        "node": f"n-{run_id}",
        "state": state,
        "recovery_classification": state,
        "session": session,
        "detail": "",
        "role": "implement",
    }
    if admitted_from is not None:
        snapshot["admitted_from"] = admitted_from
    snapshot.update(extra)
    return snapshot


def _transition(event: tuple) -> dict[str, Any]:
    """One fold event built into the lossless transition the pane renders."""
    snapshot, previous, state, counts = event
    return recovery._watch_transition(
        PROJECT,
        kind="transition",
        snapshot=snapshot,
        previous=previous,
        current=state,
        counts=counts,
        spend_runs=[],
        rate_statuses={},
    )


def _rows(transitions: list[dict[str, Any]]) -> list[str]:
    grid = Ticker(width=180, color=False, model_aliases=())
    return [recovery.format_watch_transition(t, ticker=grid) for t in transitions]


def test_an_arrival_naming_its_request_leaves_the_request_with_no_departure_row() -> (
    None
):
    """A launch continues its request: no withdrawal, one arrival.

    The request's pointer vanishes as the launch publishes under a new id, and
    the arrival names the request in ``admitted_from``. The absence is the
    request continuing, so no row words it withdrawn, discarded or departed, and
    the arrival carries the link the pane reads to keep the request's identity.
    """
    known = {"r-request": _snap("r-request", "queued")}
    current = {"r-launch": _snap("r-launch", "working", admitted_from="r-request")}

    events, running = recovery.fleet_transitions(known, current, ledger_run_ids=dict)

    words = [state for _snap, _prev, state, _counts in events]
    assert words == ["dispatched"]
    assert not any(word in {"withdrawn", "discarded", "departed"} for word in words)
    assert "r-request" not in running

    transitions = [_transition(event) for event in events]
    assert transitions[0]["admitted_from"] == "r-request"
    rendered = _rows(transitions)
    assert rendered and "r-launch" in rendered[0]


def test_a_vanished_queued_request_named_in_handovers_keeps_its_state() -> None:
    """While a handover record names the request, the fold holds it in place.

    Two observations in a row present the same absence with the request named in
    ``handovers``, and neither words it — the request keeps the state the pane
    last showed, so no row is drawn for it.
    """
    known = {"r-request": _snap("r-request", "queued")}

    first, after_first = recovery.fleet_transitions(
        known, {}, handovers={"r-request"}, ledger_run_ids=dict
    )
    second, after_second = recovery.fleet_transitions(
        after_first, {}, handovers={"r-request"}, ledger_run_ids=dict
    )

    assert first == []
    assert second == []
    assert after_first["r-request"]["state"] == "queued"
    assert after_second["r-request"]["state"] == "queued"


def test_a_vanished_queued_request_with_neither_is_held_then_worded_withdrawn() -> None:
    """One observation defers the absence, the next words it withdrawn.

    With no handover naming the request and no arrival continuing it, the first
    observation holds the request for the launch it might still be, and the
    second — the launch having never come — words the bare withdrawal a reaped
    or hand-removed pointer earns. It is worded withdrawn, never departed or
    somewhere other than the pane's own word.
    """
    known = {"r-request": _snap("r-request", "queued")}

    first, after_first = recovery.fleet_transitions(known, {}, ledger_run_ids=dict)
    second, _after_second = recovery.fleet_transitions(
        after_first, {}, ledger_run_ids=dict
    )

    assert first == [], "the first observation must defer the absence"

    words = [state for _snap, _prev, state, _counts in second]
    assert words == ["withdrawn"]
    assert second[0][1] == "queued", "the row leaves the state the pane showed"


def test_session_counts_scope_each_sessions_own_figures_without_a_live_read(
    monkeypatch,
) -> None:
    """A transition's per-session partition renders scoped and reads no pointer.

    The transition carries each owning session's own partition beside the fleet's
    totals, so a scoped follower renders its own figures from the fleet the
    transition left. The live-pointer reader is made to raise, so a render that
    re-read the fleet instead of using the carried partition fails.
    """
    rows = {
        "a1": _snap("a1", "working", session="mine"),
        "p1": _snap("p1", "working", session="peers"),
        "p2": _snap("p2", "blocked", session="peers"),
    }
    counts = recovery._transition_counts(rows)

    def _no_live_read(**_kwargs):
        raise AssertionError("a scoped render must not read the live pointers")

    monkeypatch.setattr(recovery_watch.runs, "_list_live_records", _no_live_read)

    event = recovery._watch_transition(
        PROJECT,
        kind="baseline",
        snapshot=rows["a1"],
        previous=None,
        current="working",
        counts=counts,
        spend_runs=[],
        rate_statuses={},
    )

    sessions = event["session_counts"]
    assert sessions["mine"] == {"working": 1, "blocked": 0, "unpromoted": 0}
    assert sessions["peers"] == {"working": 1, "blocked": 1, "unpromoted": 0}
    assert sessions[""] == {"working": 0, "blocked": 0, "unpromoted": 0}

    scoped = recovery.format_watch_transition(
        event, session="mine", ticker=Ticker(width=180, color=False)
    )
    assert " 1w  0h  0d  0q" in scoped
    unscoped = recovery.format_watch_transition(
        event, ticker=Ticker(width=180, color=False)
    )
    assert " 2w  1h  0d  0q" in unscoped
