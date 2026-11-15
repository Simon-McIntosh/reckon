"""A promotion refused because its inputs moved is judged again.

Acceptance reads a run's pointer, review and gate, and re-reads them under its
lease before writing. A run whose state moved between the two readings is
refused, and that refusal is right for the run it read and wrong as the run's
fate: the state it describes no longer exists. Three runs on 15 November reached
``promotion-refused`` on those words and nothing judged them again, so each
needed a coordinator to land it by hand.

The refusal is terminal as the durable queue records it, because a later request
for the same run returns the receipt unchanged. So the sweep is what takes the
run up again: the promotion is judged once more against the inputs the run now
has, lands if they pass, is refused on them if they fail, and is given up on
after a stated number of attempts, naming what moved. While a repair attempt of
the run is live the sweep attempts no promotion at all, because the repair is
what moves the head the promotion would land.

A temporary repository with a bare origin stands in for the project, a temporary
crew home for the checkouts, and the real pointer directory is asserted
untouched afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion, recovery
from reckon.crew import promotion_requests as requests
from reckon.crew import recovery_review_acceptance as acceptance
from reckon.crew.runs import _write_json, pointer_path, read_pointer, run_dir
from tests import test_review_acceptance_promotes as fixture

PROJECT = fixture.PROJECT
RUN_ID = fixture.RUN_ID
REPAIR_RUN_ID = f"r-20261115T120000000000-repair-of-{fixture.PLAN}"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"

#: The refusal the base revision states for this defect. The retry must not
#: reword it, because the sentence is what a coordinator reads on the row that
#: refused the run.
INPUTS_MOVED = acceptance.INPUTS_MOVED_REASON


@pytest.fixture(autouse=True)
def the_real_pointer_directory_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    for run_id in (RUN_ID, REPAIR_RUN_ID):
        assert not (REAL_LIVE / f"{run_id}.json").exists()
    yield
    for run_id in (RUN_ID, REPAIR_RUN_ID):
        assert not (REAL_LIVE / f"{run_id}.json").exists()


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """The acceptance fixture's checkout, carrying a bare origin."""
    return fixture.repository.__wrapped__(isolated_reckon_home, tmp_path)


def _sweep() -> dict:
    """One reflex sweep, handing its promotions to the durable queue."""
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT,
        launcher=lambda *args, **kwargs: 0,
        promotions=requests.PromotionRequests(),
    )


def _drain() -> None:
    """Run the durable promotion queue as the fleet service does."""
    service = requests.PromotionService(width=1)
    service.tick()
    service.settle(timeout=120)


def _receipt() -> dict:
    """The queue's current outcome for the run."""
    return requests.read_promotion(RUN_ID)


def _move_pointer() -> None:
    """Advance the run's attempt, as a resume or a dispose does mid-judgement."""
    pointer = read_pointer(RUN_ID)
    pointer["attempt"] = int(pointer.get("attempt") or 1) + 1
    _write_json(pointer_path(RUN_ID), pointer)


def _move_inputs_on_judgement(
    monkeypatch: pytest.MonkeyPatch, *, moves: int = 1
) -> dict[str, int]:
    """Move the run's pointer between acceptance's two readings of it.

    Acceptance re-runs the recorded gate at the merged head between reading its
    inputs and re-reading them under the lease, which is the window the three
    refused runs moved in: the run advanced while the judgement was reading it.
    The gate is the seam the fixture already drives, so a move here reproduces
    the condition the refusal describes rather than stubbing the refusal itself.
    ``moves`` bounds how many judgements see a move, so a run that will not hold
    still passes a count above every attempt it is allowed.
    """
    original = promotion.rerun_gate_at_integrated_revision
    seen = {"judgements": 0, "moves": 0}

    def moving(*args, **kwargs):
        seen["judgements"] += 1
        report = original(*args, **kwargs)
        if seen["moves"] < moves:
            seen["moves"] += 1
            _move_pointer()
        return report

    monkeypatch.setattr(promotion, "rerun_gate_at_integrated_revision", moving)
    return seen


def test_a_promotion_refused_for_moved_inputs_lands_on_the_next_sweep(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal for a state that no longer exists is not the run's fate.

    The first judgement finds the pointer moved under it and records the
    refusal the base revision stops at. The next sweep reads that refusal, sees
    the run still has attempts in hand, and asks for the run to be judged
    again; the judgement against the inputs the run now has promotes it.
    """
    fixture._land_and_review(repository, tmp_path)
    seen = _move_inputs_on_judgement(monkeypatch, moves=1)

    _sweep()
    _drain()
    refused = _receipt()
    retried = refused.get("result") or {}
    print(
        f"first judgement: phase={refused.get('phase')} "
        f"retry={retried.get('retry')} attempts={retried.get('attempts')} "
        f"moved={retried.get('moved')}"
    )
    assert refused["phase"] == requests.REFUSED, refused
    assert refused["error"] == INPUTS_MOVED, refused
    assert retried.get("retry") is True, refused
    assert retried.get("attempts") == 1, refused
    assert any("pointer.attempt" in move for move in retried.get("moved") or []), (
        retried
    )
    assert seen["moves"] == 1
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(RUN_ID).exists()

    second = _sweep()
    print(
        f"second sweep: requested={second['promotion_requested']} "
        f"failed={second['failed']} accepted={second['accepted']}"
    )
    assert second["failed"] == [], second
    assert second["promotion_requested"] == [RUN_ID], second

    _drain()
    promoted = _receipt()
    rows = ledger.runs(PROJECT, root=repository)
    print(f"after the retry: phase={promoted.get('phase')} rows={len(rows)}")
    assert promoted["phase"] == requests.PROMOTED, promoted
    assert [row["run_id"] for row in rows] == [RUN_ID], rows
    assert rows[0]["promoted_by"] == "review-acceptance"
    assert not pointer_path(RUN_ID).exists()


def test_a_run_whose_inputs_keep_moving_is_reported_after_its_attempts(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that will not hold still is given up on, naming what moved.

    Every judgement finds the pointer moved under it, so the sweep spends the
    stated attempt budget and then stops asking: the run stays refused, and the
    refusal names the field that moved rather than the bare fact of movement.
    """
    fixture._land_and_review(repository, tmp_path)
    seen = _move_inputs_on_judgement(monkeypatch, moves=99)

    receipts = []
    for _ in range(acceptance.INPUTS_MOVED_ATTEMPTS + 2):
        _sweep()
        _drain()
        receipts.append(_receipt())

    attempts = [(receipt.get("result") or {}).get("attempts") for receipt in receipts]
    retries = [(receipt.get("result") or {}).get("retry") for receipt in receipts]
    print(f"attempts per judgement: {attempts}, retried: {retries}")
    print(f"given-up refusal: {receipts[-1].get('error')}")

    assert attempts[:3] == [1, 2, 3], attempts
    assert retries[:3] == [True, True, None], retries
    assert seen["judgements"] == acceptance.INPUTS_MOVED_ATTEMPTS, seen
    assert receipts[-1]["phase"] == requests.REFUSED, receipts[-1]
    assert "pointer.attempt" in receipts[-1]["error"], receipts[-1]
    # The budget is spent, so the later sweeps report the same refusal rather
    # than judging the run a fourth time.
    assert len({receipt["request_id"] for receipt in receipts[2:]}) == 1, receipts
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_run_whose_new_inputs_fail_is_refused_on_them(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retried judgement reads the new inputs, and refuses for their failure.

    The movement is real, so the second judgement is taken against a state it
    did not plan for: that state has a merged gate which no longer passes, and
    the refusal names the gate rather than the movement that led here.
    """
    fixture._land_and_review(repository, tmp_path)
    original = promotion.rerun_gate_at_integrated_revision
    seen = {"judgements": 0}

    def gate(*args, **kwargs):
        seen["judgements"] += 1
        if seen["judgements"] > 1:
            return {
                "integrated_verdict": "failed",
                "reason": "the merged gate failed on the new inputs",
            }
        report = original(*args, **kwargs)
        _move_pointer()
        return report

    monkeypatch.setattr(promotion, "rerun_gate_at_integrated_revision", gate)

    _sweep()
    _drain()
    first = _receipt()
    assert first["error"] == INPUTS_MOVED, first

    _sweep()
    _drain()
    second = _receipt()
    print(f"judged against the new inputs: {second.get('error')}")
    assert seen["judgements"] == 2, seen
    assert second["phase"] == requests.REFUSED, second
    assert "merged gate failed on the new inputs" in second["error"], second
    assert INPUTS_MOVED not in second["error"], second
    assert ledger.runs(PROJECT, root=repository) == []


def _stand_a_repair_attempt(*, ended: bool = False) -> None:
    """Record a repair round on the run, and the attempt it dispatched."""
    _write_json(
        pointer_path(REPAIR_RUN_ID),
        {
            "run_id": REPAIR_RUN_ID,
            "project": PROJECT,
            "phase": "working",
            "node": {"id": f"repair-of-{RUN_ID}", "role": "repair"},
        },
    )
    if ended:
        (run_dir(REPAIR_RUN_ID) / "exit.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (run_dir(REPAIR_RUN_ID) / "exit.json").write_text(
            json.dumps({"run_id": REPAIR_RUN_ID, "exit_code": 0}), encoding="utf-8"
        )
    pointer = read_pointer(RUN_ID)
    pointer["repair_dispatch"] = {
        "status": "dispatched",
        "run_id": REPAIR_RUN_ID,
        "node_id": f"repair-of-{RUN_ID}",
        "round_id": "round-1",
        "attempt": 1,
    }
    _write_json(pointer_path(RUN_ID), pointer)


def test_a_live_repair_attempt_holds_the_promotion(
    repository: Path, tmp_path: Path
) -> None:
    """While a repair attempt is live the sweep attempts no promotion at all.

    The repair is the run's own move: it is about to commit into the head the
    promotion would land, so a promotion taken now would reserve a merge against
    a change that has not arrived. Once that attempt has ended the same sweep
    promotes, which is what shows the guard reads liveness rather than the mere
    presence of a repair record.
    """
    fixture._land_and_review(repository, tmp_path)
    _stand_a_repair_attempt()

    held_report = recovery.dispatch_awaiting_reviews(
        project=PROJECT, launcher=lambda *args, **kwargs: 0
    )
    held = [
        entry
        for entry in held_report["repair_held"]
        if entry.get("run_id") == RUN_ID and entry.get("repair_run_id")
    ]
    print(f"held promotion: {held}")
    assert held and held[0]["repair_run_id"] == REPAIR_RUN_ID, held_report
    assert held_report["accepted"] == [], held_report
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(RUN_ID).exists()

    _stand_a_repair_attempt(ended=True)
    promoted_report = recovery.dispatch_awaiting_reviews(
        project=PROJECT, launcher=lambda *args, **kwargs: 0
    )
    print(f"after the attempt ended: accepted={promoted_report['accepted']}")
    assert promoted_report["accepted"] == [RUN_ID], promoted_report
    rows = ledger.runs(PROJECT, root=repository)
    assert [row["run_id"] for row in rows] == [RUN_ID], rows
