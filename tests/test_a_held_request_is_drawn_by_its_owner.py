"""A held request is drawn by whose move lifts its hold.

The pane's word set puts ``held`` under "needs you" while its own counter counts
a held run as waiting, so five sessions read "needs you" for a request the queue
was already going to launch — held on a live run's claim, on the session's lane
allowance, or on a failed attempt's retry back-off. Nothing in those rows asked
anyone for anything. The rows that do ask for a coordinator are the ones whose
hold lifts on a coordinator act: a request parked with ``crew hold``, whose
release is ``reckon crew queue release``, and a request parked for an owed plan
review, which lifts when a review of that plan is composed and stored.

These cases hold one population: a request the fleet has stored. Each row's word
is what a reader sees, and the counter is what the pane's ``q`` and ``n`` cells
add up, so a case asserts both and the agreement of the two for every hold the
code defines.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import runs
from reckon.crew.recovery_watch import (
    _fleet_counts,
    _watch_snapshot,
    _watch_verdict,
    classify_pointer,
)
from reckon.crew.ticker import PANE_STATES, _bucket_of, _display_state

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "a-plan"
STALL_SECONDS = 300

#: The hold kinds the admission pass records on a request it parks, as the
#: vocabulary in the admission module names them. Every one of them lifts on
#: something the queue does — a live run's claim landing, the lane publishing an
#: allowance, a back-off elapsing — and none of them is a coordinator's act.
QUEUE_HOLD_REASONS = (
    "held-on-claim",
    "held-for-allowance",
    "held-on-retry-backoff",
    "held-by-gate",
    "held-until-changed",
    "held-behind-earlier-request",
)

#: The one automatic hold whose lifting condition is a coordinator act: nothing
#: but a composed and stored plan review releases it, so its row must say so.
PLAN_REVIEW_HELD_REASON = "plan-review-owed"


def _crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    home.mkdir(parents=True, exist_ok=True)
    return home


def _pointer(
    run_id: str,
    phase: str,
    *,
    hold: dict[str, Any] | None = None,
    age_seconds: int = 0,
) -> dict[str, Any]:
    """One live pointer, in the shape a launcher leaves for a stored request."""
    moment = datetime.now(tz=UTC).timestamp() - age_seconds
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "launch": "cli",
        "node": {"id": run_id, "plan": PLAN, "section": "s1"},
        "created_at": datetime.fromtimestamp(moment, tz=UTC).isoformat(),
    }
    if hold is not None:
        pointer["queue_hold"] = hold
    path = runs.pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer), encoding="utf-8")
    return pointer


def _held(**hold: Any) -> dict[str, Any]:
    """A hold record as ``crew hold`` or the admission pass writes one."""
    return {
        "prior_phase": "queued",
        "started_at": datetime.now(tz=UTC).isoformat(),
        **hold,
    }


def _snapshots() -> dict[str, Mapping]:
    moment = datetime.now(tz=UTC).timestamp()
    cache: dict = {}
    return {
        str(pointer["run_id"]): _watch_snapshot(
            pointer, moment=moment, stall_seconds=STALL_SECONDS, cache=cache
        )
        for pointer in runs._list_live_records(project=PROJECT)
    }


def _counts(snapshots: Mapping[str, Mapping]) -> dict[str, int]:
    return _fleet_counts(snapshots, session=SESSION)


def _owners() -> Any:
    """The hold-owner vocabulary, read where it is defined.

    Imported here rather than at module scope so a run against the revision
    before this reading existed fails on the words it draws, not on an import
    that never reached an assertion.
    """
    from reckon.crew import recovery_watch

    return recovery_watch


def _pane_word(state: Any) -> str | None:
    """The word the pane draws for a state, or None for one it never draws.

    A row's destination is an internal state; a word the pane already holds as
    its own spelling — ``needs you`` among them — maps to itself.
    """
    drawn = _display_state(state)
    if drawn:
        return drawn
    return str(state) if str(state) in PANE_STATES else None


@pytest.mark.parametrize("reason", QUEUE_HOLD_REASONS)
def test_a_request_the_queue_parks_is_drawn_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    _crew_home(tmp_path, monkeypatch)
    _pointer(
        "r-parked",
        "held",
        hold=_held(reason=reason, automatic=True),
        age_seconds=STALL_SECONDS * 24,
    )

    snapshots = _snapshots()
    row = snapshots["r-parked"]

    # The word a reader sees, the bucket the counters add, and the pane
    # vocabulary that decides which pane the word sits in all agree.
    assert row["state"] == "waiting", row
    assert _pane_word(_display_state(row["state"])) == "waiting", row
    assert _bucket_of(row["state"]) == "waiting", row
    # Nothing a coordinator does lifts this hold, so the row hands no command:
    # the release the classifier names for a held pointer would launch over the
    # admission pass's own release path.
    assert not row["next_action"], row

    counts = _counts(snapshots)
    assert counts.get("waiting") == 1, counts
    assert counts.get("blocked", 0) == 0, counts


def test_a_coordinator_hold_is_drawn_needs_you_and_names_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _crew_home(tmp_path, monkeypatch)
    _pointer("r-hold", "held", hold=_held(reason="wait for the lead's answer"))

    row = _snapshots()["r-hold"]

    assert row["state"] == "held", row
    assert _pane_word(_display_state(row["state"])) == "needs you", row
    assert _bucket_of(row["state"]) == "needs you", row
    # The command is the classifier's own for this pointer, not a second
    # composition of it in the pane: one surface names it, so the two cannot
    # drift apart.
    assert (
        row["next_action"]
        == classify_pointer(runs.read_pointer("r-hold"))["next_action"]
    ), row
    assert "reckon crew queue release --run r-hold" in row["next_action"], row
    assert "crew queue release" in row["detail"], row
    assert _counts(_snapshots()).get("blocked") == 1


def test_an_owed_plan_review_is_drawn_needs_you_and_names_the_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _crew_home(tmp_path, monkeypatch)
    _pointer(
        "r-review",
        "held",
        hold=_held(
            reason=PLAN_REVIEW_HELD_REASON,
            automatic=True,
            detail=(
                f"plan {PLAN!r} in project {PROJECT!r} changed after this request "
                "was queued and no stored review covers the new text"
            ),
        ),
    )

    row = _snapshots()["r-review"]

    assert row["state"] == "held", row
    assert _pane_word(_display_state(row["state"])) == "needs you", row
    assert _bucket_of(row["state"]) == "needs you", row
    # The act that lifts the hold is a composition, not a release: releasing it
    # would launch work over unreviewed plan text.
    assert "reckon crew review" in row["next_action"], row
    assert "reckon crew review" in row["detail"], row
    assert PLAN in row["next_action"], row
    assert _counts(_snapshots()).get("blocked") == 1


def _row(**fields: Any) -> dict[str, Any]:
    """One classified row, in the shape the classifier hands the reducer."""
    row: dict[str, Any] = {
        "classification": "paused",
        "recovery_classification": "held",
        "detail": "paused: the lane allowance is spent for this window",
        "recovery": "resume",
        "next_action": "reckon crew resume --run r-live",
    }
    row.update(fields)
    return row


@pytest.mark.parametrize("classification", ["paused", "blocked"])
def test_a_launched_run_held_by_the_lane_is_drawn_waiting(
    classification: str,
) -> None:
    """A launched run parked by the lane, whether it waits or stops.

    Neither word is new — a paused run already read waiting — but the counter
    filed every row whose classification said ``held`` under waiting while the
    pane drew a stopped one "needs you", so the two disagreed about the same
    row. A hold the lane or the clock lifts belongs in one place on both
    surfaces: the waiting pane, and the ``q`` cell.
    """
    pointer = {
        "run_id": "r-live",
        "project": PROJECT,
        "session": SESSION,
        "phase": classification,
        "backend": "local",
    }
    verdict = _watch_verdict(
        pointer,
        _row(classification=classification),
        moment=datetime.now(tz=UTC).timestamp(),
        stall_seconds=300,
    )

    # Each surface keeps its own claim: the classification still names the
    # hold, and the word — which the bucket follows — says whose move it is.
    assert verdict["recovery_classification"] == "held", verdict
    assert _pane_word(_display_state(verdict["state"])) == "waiting", verdict
    assert _bucket_of(verdict["state"]) == "waiting", verdict
    # The live counter agrees, read from the snapshot the pane is drawn from,
    # and the row hands no command for a hold the lane releases itself.
    counts = _fleet_counts({"r-live": verdict})
    assert counts.get("waiting") == 1, counts
    assert counts.get("blocked", 0) == 0, counts
    assert not verdict["next_action"], verdict
    assert verdict["detail"], verdict


def test_every_hold_kind_agrees_between_its_word_and_its_bucket() -> None:
    """The owner of every hold the code defines, read from its own record."""
    module = _owners()
    hold_owner = module.hold_owner
    parked = {
        reason: hold_owner(
            {"phase": "held", "queue_hold": _held(reason=reason, automatic=True)}
        )
        for reason in QUEUE_HOLD_REASONS
    }
    assert set(parked.values()) == {module.HOLD_LIFTED_BY_QUEUE}, parked

    assert (
        hold_owner(
            {
                "phase": "held",
                "queue_hold": _held(reason=PLAN_REVIEW_HELD_REASON, automatic=True),
            }
        )
        == module.HOLD_LIFTED_BY_COORDINATOR
    )
    assert (
        hold_owner({"phase": "held", "queue_hold": _held(reason="anything at all")})
        == module.HOLD_LIFTED_BY_COORDINATOR
    )
    # A held phase with no readable record: only a coordinator's release lifts
    # it, so the row keeps asking for that rather than reading as a slot wait.
    assert hold_owner({"phase": "held"}) == module.HOLD_LIFTED_BY_COORDINATOR
    # A launched run parked on a window or a back-off: time lifts it.
    assert hold_owner({"phase": "blocked"}) == module.HOLD_LIFTED_BY_QUEUE
