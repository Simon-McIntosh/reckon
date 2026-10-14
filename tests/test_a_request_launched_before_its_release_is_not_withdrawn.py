"""A request released after its launch still leaves without a departure row.

The admission pass does not keep the order the fold once assumed: it launches a
queued request by releasing its pointer and publishing the launch under a new
run id, but the released pointer is observed *after* the new run's own pointer
appears. A run's pointer is observed first without ``admitted_from`` and gains
the field a moment later, while the request's pointer is released last. A fold
that reads ``admitted_from`` only from the runs that arrived in the current
observation therefore never sees the launch naming the request, holds the
request one observation and then words the absence a withdrawal.

The deliverable is that the request leaves silently while *any* live run names
it in ``admitted_from``, whether that run arrived this observation or an
earlier one. A request that vanishes with no live run naming it — and no
handover record — is still held one observation and then worded, which is the
behaviour a bare reaped pointer has always had.
"""

from __future__ import annotations

from typing import Any

import pytest

from reckon.crew import recovery

PROJECT = "proj"

# The words a vanished request must never be drawn with while a launch is still
# naming it: each says the request was dropped rather than continued.
_DROP_WORDS = frozenset({"withdrawn", "discarded", "departed"})


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every pointer, discard and handover path in temporary state.

    The fold reads run directories and the discards spool, so a test that let
    them resolve to the real configuration home would read — and could write —
    outside its own tree.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _snap(
    run_id: str,
    state: str,
    *,
    admitted_from: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One live pointer reduced to the facts the fold reads."""
    snapshot = {
        "run_id": run_id,
        "node": f"n-{run_id}",
        "state": state,
        "recovery_classification": state,
        "detail": "",
        "role": "implement",
    }
    if admitted_from is not None:
        snapshot["admitted_from"] = admitted_from
    snapshot.update(extra)
    return snapshot


def _words(events: list[tuple]) -> list[str]:
    return [state for _snap, _prev, state, _counts in events]


def test_a_launch_naming_its_request_from_an_earlier_observation_leaves_it_silent() -> (
    None
):
    """The full four-observation sequence draws no drop row for the request.

    The launch's pointer appears first without ``admitted_from`` (observation 1),
    gains the field naming the request (observation 2), and the request's own
    pointer is released only then (observation 3). The launched run is already
    known by the time the request vanishes, so a fold reading ``admitted_from``
    from arriving runs alone misses it. Observation 4 exists because the unfixed
    fold defers the gone request one observation and words it on the next; the
    fixed fold must word nothing there either, and the request must be gone from
    the fold's memory after it.
    """
    request = "r-request"
    launched = "r-launch"

    known = {request: _snap(request, "queued")}

    # Observation 1: request still queued, the launched run arrives with no
    # admitted_from yet.
    events1, known = recovery.fleet_transitions(
        known,
        {
            request: _snap(request, "queued"),
            launched: _snap(launched, "working"),
        },
        ledger_run_ids=dict,
    )
    # Observation 2: the run gains admitted_from naming the request.
    events2, known = recovery.fleet_transitions(
        known,
        {
            request: _snap(request, "queued"),
            launched: _snap(launched, "working", admitted_from=request),
        },
        ledger_run_ids=dict,
    )
    # Observation 3: the request's pointer is released.
    events3, known = recovery.fleet_transitions(
        known,
        {launched: _snap(launched, "working", admitted_from=request)},
        ledger_run_ids=dict,
    )
    # Observation 4: one observation more, where the unfixed fold would word the
    # deferred request withdrawn.
    events4, known = recovery.fleet_transitions(
        known,
        {launched: _snap(launched, "working", admitted_from=request)},
        ledger_run_ids=dict,
    )

    for index, events in enumerate((events1, events2, events3, events4), start=1):
        dropped = [word for word in _words(events) if word in _DROP_WORDS]
        assert dropped == [], f"observation {index} worded the request {dropped}"

    # No row at all names the request: it left silently, continued by its launch.
    assert all(
        request not in {str(snap.get("run_id") or "") for snap, _p, _s, _c in events}
        for events in (events1, events4)
    )
    assert request not in known, "the request must be gone from the fold's memory"
    assert launched in known


def test_a_launch_naming_its_request_is_reported_as_the_continuing_run() -> None:
    """The departure of the request emits no row, and the launch keeps the link.

    The harvested transition for the launched run carries ``admitted_from``, so
    a pane that drew the request reads the launch as the same job rather than a
    second one.
    """
    request = "r-request"
    launched = "r-launch"
    known = {
        request: _snap(request, "queued"),
        launched: _snap(launched, "dispatched"),
    }

    events, running = recovery.fleet_transitions(
        known,
        {launched: _snap(launched, "working", admitted_from=request)},
        ledger_run_ids=dict,
    )

    assert _words(events) == ["working"]
    assert "r-launch" in running
    assert request not in running, "the request leaves silently while named"


def test_a_vanished_request_with_no_run_naming_it_is_still_worded_withdrawn() -> None:
    """With no live run naming it, the deferral still ends in a withdrawal.

    A request whose pointer vanishes and which no run names — no handover, no
    admitted_from — is held one observation for the launch it might still be and
    then worded the bare withdrawal a reaped or hand-removed pointer earns. The
    fix must not weaken this path.
    """
    request = "r-request"
    known = {request: _snap(request, "queued")}

    first, after_first = recovery.fleet_transitions(known, {}, ledger_run_ids=dict)
    second, _after_second = recovery.fleet_transitions(
        after_first, {}, ledger_run_ids=dict
    )

    assert first == [], "the first observation must defer the absence"
    assert _words(second) == ["withdrawn"]
    assert second[0][1] == "queued"
