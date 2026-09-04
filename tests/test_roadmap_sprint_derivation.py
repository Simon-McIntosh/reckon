import json
from pathlib import Path

from reckon.roadmap import build_roadmap
from tests import spa_module_eval

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "docs" / "ui" / "sprint.jsx"


def _plan(slug: str, *, status: str = "pending", impl: float = 0.0) -> dict:
    return {
        "slug": slug,
        "title": slug,
        "type": "plan",
        "status": status,
        "impl": impl,
        "depends_on": [],
        "effort": "S",
        "roi": "high",
        "blocking": [],
        "gates": [],
        "followups": [],
    }


def _sprint_row(inventory: list[dict], sprint: dict) -> dict:
    return build_roadmap("sample", inventory, [sprint])["sprints"][0]


def _evaluate_helpers(expression: str, tmp_path: Path):
    source = SOURCE.read_text()
    helpers = source[
        source.index("const CLOSED_ITEM_STATUSES") : source.index(
            "function SprintDetail("
        )
    ]
    helper_module = tmp_path / "sprint_payload_helpers.jsx"
    helper_module.write_text(helpers)
    return spa_module_eval.evaluate_jsx_module(helper_module, expression)


def test_roadmap_reports_member_derived_sprint_state_and_stored_drift() -> None:
    shipped = _sprint_row(
        [
            _plan("first", status="shipped", impl=1.0),
            _plan("second", status="done", impl=1.0),
        ],
        {"id": "finished", "status": "planned", "items": ["first", "second"]},
    )
    agreeing = _sprint_row(
        [_plan("started", status="active", impl=0.25)],
        {"id": "moving", "status": "active", "items": ["started"]},
    )
    empty = _sprint_row(
        [],
        {"id": "unfilled", "status": "planned", "items": []},
    )
    started = _sprint_row(
        [_plan("partial", impl=0.1)],
        {"id": "begun", "status": "active", "items": ["partial"]},
    )

    assert shipped["derived_state"] == "shipped"
    assert shipped["state_drift"] == {"stored": "planned", "derived": "shipped"}
    assert agreeing["derived_state"] == "active"
    assert "state_drift" not in agreeing
    assert empty["derived_state"] == "empty"
    assert started["derived_state"] == "active"


def test_sprint_surface_reads_state_and_drift_from_roadmap_payload(
    tmp_path: Path,
) -> None:
    inventory = [_plan("member", status="shipped", impl=1.0)]
    row = _sprint_row(
        inventory,
        {"id": "delivery", "status": "planned", "items": ["member"]},
    )
    changed = {
        **row,
        "derived_state": "active",
        "state_drift": {"stored": "queued", "derived": "active"},
    }

    result = _evaluate_helpers(
        "(() => {"
        f"const inventory = {json.dumps(inventory)};"
        f"const original = sprintStateRows([{json.dumps(row)}], inventory)[0];"
        f"const changed = sprintStateRows([{json.dumps(changed)}], inventory)[0];"
        "return {original: {state: original.state, flag: original.flag}, "
        "changed: {state: changed.state, flag: changed.flag}};"
        "})()",
        tmp_path,
    )

    assert result == {
        "original": {"state": "shipped", "flag": "was planned"},
        "changed": {"state": "active", "flag": "was queued"},
    }
    assert "deriveSprintState" not in SOURCE.read_text()
    assert "derivedState" not in SOURCE.read_text()
