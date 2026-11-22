"""A lane admits no run past twice the width of its gate, whatever its slot figure.

The router's slot figure divides served requests by live runs. A saturated gate
serves exactly its width however many runs wait behind it, so under load the
figure reads every live count as fitting and cannot shed runs. A run admitted
past a fixed multiple of the width only queues at the gate, where each model
turn waits and its time budget still runs, so the allowance is capped there.
"""

from __future__ import annotations

import importlib

from reckon.crew import lane_document

dispatch_plan = importlib.import_module("reckon.crew.dispatch_plan")

SESSION = "coordinator-a"
WIDTH = 12


def _document(
    *, live_runs: object, worker_slots: object, width: object = WIDTH
) -> dict:
    admission = {
        lane_document.ADMISSION_WORKER_SLOTS_KEY: worker_slots,
        lane_document.ADMISSION_OBSERVED_SECONDS_KEY: 900,
        "live_runs": live_runs,
    }
    if width is not None:
        admission["utilisation"] = {"width": width}
    return {"headroom": 20.0, lane_document.ADMISSION_KEY: admission}


def _allowance(document: dict) -> dict:
    return dispatch_plan._lane_worker_allowance(document, session=SESSION)


def test_a_lane_at_twice_its_width_holds_whatever_the_slots_say() -> None:
    decision = _allowance(_document(live_runs=50, worker_slots=8))
    assert decision["held"] is True, decision
    assert decision["allowance"] == 2 * WIDTH - 50
    assert "live-run cap of 24" in decision["source"], decision


def test_room_under_the_cap_limits_the_router_figure() -> None:
    decision = _allowance(_document(live_runs=20, worker_slots=8))
    assert decision["held"] is False
    assert decision["allowance"] == 4


def test_a_quiet_lane_takes_the_router_figure_unchanged() -> None:
    decision = _allowance(_document(live_runs=5, worker_slots=8))
    assert decision["allowance"] == 8
    assert "live-run cap" not in decision["source"]


def test_a_document_without_a_gate_width_is_admitted_as_before() -> None:
    decision = _allowance(_document(live_runs=50, worker_slots=8, width=None))
    assert decision["allowance"] == 8
    assert decision["held"] is False
