"""``crew review`` reports a queued composition the way dispatch does.

A lane hold on a local lane stores the request as a queued run the admission
service will launch, exactly as a held dispatch does. The dispatch command
answers such a hold as an acceptance — ``ok``, the queued run's id and its
position, exit 0 — and reserves ``lane-paused`` for a gate that queued nothing.
The review command built its report independently and answered every hold with
``error: lane-paused`` at exit 1, so a caller following the contract either
respects a hold that does not exist or composes the review again.

These cases drive the command through the click runner with the composition
stubbed, so the assertions are about the CLI's reading of a hold rather than
about whether a lane held. The composition's own report is untouched — it still
carries ``error: lane-paused`` — so the sweep that counts held reviews by that
field still counts them.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from reckon import cli, review_tiers
from reckon.crew import plan_review, recovery
from reckon.crew import recovery_review_acceptance as acceptance_module

# The ids the held composition reports: the composed report directory, which
# names no crew run, and the queued crew run the admission service will launch.
COMPOSED_ID = "r-20261019T074610975860"
QUEUED_RECORD = {
    "run_id": "r-20261019T074644060201-plan-review-of-the-suite-is-green",
    "position": 2,
    "phase": "queued",
    "reason": "the router granted the session no worker",
}
LANE_GATE = {
    "state": "measured",
    "reason": "the router grants 0 extra workers",
    "detail": "the lane's router grants 0 extra workers; the gate is full",
}


def _review(*arguments: str):
    return CliRunner().invoke(cli.main, ["crew", "review", *arguments])


def _composition_report(*, queued) -> dict:
    """The report ``_dispatch_composed_review`` builds for a lane hold.

    The shape is the one the composition produces today: ``error: lane-paused``
    with the composed directory's id as ``run_id`` and, when the lane stored the
    request, the queued record beside it under ``queued``.
    """
    report = {
        "run_id": COMPOSED_ID,
        "dispatched": False,
        "error": "lane-paused",
        "backend": "local",
        "lane_gate": dict(LANE_GATE),
        "reason": "the local lane stored the request as a queued run",
    }
    if queued is not None:
        report["queued"] = dict(queued)
    return report


def _stub_plan_composition(monkeypatch, report: dict) -> dict:
    subject = {"project": "proj", "plan": "p", "session": "s"}
    monkeypatch.setattr(recovery, "plan_review_subject", lambda *a, **k: subject)
    monkeypatch.setattr(
        recovery, "dispatch_review_for_run", lambda *a, **k: dict(report)
    )
    return subject


def _lane_held(*, queued):
    from reckon.crew.dispatch import LaneHeld

    held = LaneHeld(dict(LANE_GATE))
    if queued is not None:
        held.queued = dict(queued)
    return held


def _lane_paused():
    """The gate hold, raised without the router's queued record.

    ``LaneHeld`` is the subclass a router hold arrives as; the parent is what a
    lane whose gate is simply shut raises, and the code-review dispatch can
    reach it without a queued record ever existing.
    """
    from reckon.crew.dispatch import LanePaused

    return LanePaused(dict(LANE_GATE))


def test_a_queued_review_composition_reports_itself_queued(monkeypatch) -> None:
    """The first case: a queued composition is an acceptance at exit 0."""
    _stub_plan_composition(monkeypatch, _composition_report(queued=QUEUED_RECORD))

    result = _review("--project", "proj", "--plan", "p", "--session", "s")
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["queued"] is True
    assert payload["run_id"] == QUEUED_RECORD["run_id"]
    assert payload["position"] == QUEUED_RECORD["position"]
    # The composed report directory's id survives under its own key, so a reader
    # can still find the brief the queued run will read.
    assert payload["report_run_id"] == COMPOSED_ID
    assert "error" not in payload


def test_a_paused_gate_that_queued_nothing_reports_lane_paused(monkeypatch) -> None:
    """The second case: a paused gate with no queued record, at exit 75."""
    _stub_plan_composition(monkeypatch, _composition_report(queued=None))

    result = _review("--project", "proj", "--plan", "p", "--session", "s")
    payload = json.loads(result.output)

    assert result.exit_code == 75
    assert payload["ok"] is False
    assert payload["error"] == "lane-paused"
    assert "queued" not in payload


def test_the_sweep_still_counts_a_queued_review_as_lane_paused(monkeypatch) -> None:
    """The third case: the composition's report is unchanged, so the sorting
    that keys on ``error == "lane-paused"`` still counts a held review.

    The sweep reaches its composition through a scoring run whose review tier is
    not none, so the two upstream guards — classification and tier — are stubbed
    and the sorting under test runs for real over the same lane-hold report.
    """
    run_id = "r-source"
    pointer = {"run_id": run_id, "project": "proj", "node": {"id": run_id}}
    monkeypatch.setattr(acceptance_module, "list_live", lambda *a, **k: [dict(pointer)])
    monkeypatch.setattr(
        acceptance_module,
        "classify_pointer",
        lambda record: {"classification": "scoring", "manifest_commits": ["abc1234"]},
    )
    monkeypatch.setattr(
        acceptance_module, "_sweep_review_tier", lambda *a, **k: review_tiers.FULL
    )
    monkeypatch.setattr(
        acceptance_module,
        "dispatch_review_for_run",
        lambda *a, **k: _composition_report(queued=QUEUED_RECORD),
    )
    monkeypatch.setattr(
        acceptance_module,
        "dispatch_repair_for_run",
        lambda *a, **k: {"dispatched": False},
    )

    result = acceptance_module.dispatch_awaiting_reviews(project="proj", session="")

    # The sweep keys on the report's own ``error`` and reports the report's
    # ``run_id`` — the composed directory — not the source run it composed for.
    assert result["lane_paused"] == [COMPOSED_ID]
    assert result["dispatched"] == []
    assert result["refused"] == []
    assert result["reports"][0]["error"] == "lane-paused"


def test_a_held_code_review_that_queued_reports_queued(monkeypatch) -> None:
    """The fourth case: the code-review branch reports a held compose the same way."""
    subject = {"project": "proj", "named_paths": ["reckon/x.py"], "modules": []}
    monkeypatch.setattr(
        plan_review, "code_review_subject", lambda *a, **k: dict(subject)
    )
    held = _lane_held(queued=QUEUED_RECORD)
    monkeypatch.setattr(
        plan_review,
        "dispatch_code_review",
        lambda *a, **k: (_ for _ in ()).throw(held),
    )

    result = _review("--project", "proj", "--around", "p", "--session", "s")
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["queued"] is True
    assert payload["run_id"] == QUEUED_RECORD["run_id"]
    assert payload["position"] == QUEUED_RECORD["position"]
    assert (
        payload["report_run_id"]
        == plan_review.code_review_report_directory(subject).name
    )
    assert "error" not in payload


def test_a_held_code_review_with_no_queued_record_is_lane_paused(monkeypatch) -> None:
    """The fifth case: no queued record is lane-paused at exit 75, not crew_error."""
    subject = {"project": "proj", "named_paths": ["reckon/x.py"], "modules": []}
    monkeypatch.setattr(
        plan_review, "code_review_subject", lambda *a, **k: dict(subject)
    )
    held = _lane_held(queued=None)
    monkeypatch.setattr(
        plan_review,
        "dispatch_code_review",
        lambda *a, **k: (_ for _ in ()).throw(held),
    )

    result = _review("--project", "proj", "--around", "p", "--session", "s")
    payload = json.loads(result.output)

    assert result.exit_code == 75
    assert payload["error"] == "lane-paused"
    assert payload["error"] != "crew_error"
    assert "queued" not in payload


def test_a_gate_hold_on_a_code_review_is_lane_paused(monkeypatch) -> None:
    """The sixth case: the hold's own class, raised by the gate rather than the
    router, is caught before the generic handler too.

    ``LaneHeld`` is a subclass of ``LanePaused``, so a branch catching only the
    subclass lets a gate hold fall through to the generic handler and be
    reported as ``crew_error`` at exit 1 — a failure of the command where the
    lane was merely shut. This is the same report at the same exit code.
    """
    subject = {"project": "proj", "named_paths": ["reckon/x.py"], "modules": []}
    monkeypatch.setattr(
        plan_review, "code_review_subject", lambda *a, **k: dict(subject)
    )
    monkeypatch.setattr(
        plan_review,
        "dispatch_code_review",
        lambda *a, **k: (_ for _ in ()).throw(_lane_paused()),
    )

    result = _review("--project", "proj", "--around", "p", "--session", "s")
    payload = json.loads(result.output)

    assert result.exit_code == 75
    assert payload["error"] == "lane-paused"
    assert payload["error"] != "crew_error"
    assert payload["lane_gate"] == LANE_GATE
    assert payload["detail"] == LANE_GATE["detail"]
    assert "queued" not in payload
