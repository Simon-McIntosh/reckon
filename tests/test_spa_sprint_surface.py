import json
import subprocess
from pathlib import Path

import pytest

from tests import spa_module_eval

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "docs" / "ui" / "sprint.jsx"


def _evaluate_helpers(expression: str):
    return spa_module_eval.evaluate_jsx_module(SOURCE, expression)


def test_module_evaluator_rejects_raw_jsx(monkeypatch, tmp_path):
    source = tmp_path / "component.jsx"
    source.write_text("function component() { return <span>raw</span>; }")
    monkeypatch.setattr(
        spa_module_eval,
        "compile_jsx",
        lambda text, *, filename: text.encode(),
    )

    with pytest.raises(subprocess.CalledProcessError) as failure:
        spa_module_eval.evaluate_jsx_module(source, "component()")

    assert "Unexpected token '<'" in failure.value.stderr


def test_overview_keeps_every_active_sprint_and_folds_only_closed_rows():
    sprints = [
        {"id": "SA", "status": "active", "items": [{"slug": "alpha"}]},
        {"id": "SB", "status": "active", "items": [{"slug": "beta"}]},
        {"id": "SC", "status": "active", "items": [{"slug": "gamma"}]},
        {"id": "SD", "status": "done", "items": [{"slug": "delta"}]},
    ]
    result = _evaluate_helpers(
        "(() => {"
        f"const rows = sprintStateRows({json.dumps(sprints)}, '2026-09-01');"
        "return {visible: rows.filter(row => !row.closed), folded: rows.filter(row => row.closed)};"
        "})()"
    )

    assert [row["sprint"]["id"] for row in result["visible"]] == ["SA", "SB", "SC"]
    assert [row["sprint"]["id"] for row in result["folded"]] == ["SD"]


def test_status_transition_flag_names_effective_state_and_open_gates():
    plan = {
        "status": "active",
        "workflow_status": "active",
        "effective_status": "blocked",
        "gates": [{"verdict": "pending"}, {"verdict": "passed"}],
    }
    assert (
        _evaluate_helpers(f"planFlag({json.dumps(plan)}, [])")
        == "active → blocked · 1 open gate"
    )


def test_surface_keeps_description_concise_and_contract_reachable():
    source = SOURCE.read_text()

    assert 'className="r-card-description"' in source
    assert "<summary>Contract</summary>" in source
    assert "plan.whyNow" in source
    assert "plan.doneWhen" in source
    assert 'className="r-card-flag"' in source
    assert "aria-label={`${plan.title}: ${percent}% complete`}" in source
    assert 'surface === "overview"' in source
    assert 'surface === "board"' in source
    assert "active_sprints" in source
    assert 'className="r-sprint-conflict"' in source
    assert 'className="r-horizon-strip"' in source
    assert '<table className="r-sprint-table">' in source
    assert 'className="r-needs-you"' in source


def test_sprint_styles_do_not_force_a_sideways_canvas():
    styles = (ROOT / "docs" / "ui" / "sprints.css").read_text()
    base_styles = (ROOT / "docs" / "ui" / "styles-base.css").read_text()

    assert ".r-horizon-track { position: relative; height: 44px" in styles
    assert (
        ".r-horizon-strip > header { display: grid; grid-template-columns: repeat(2, 1fr)"
        in styles
    )
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in styles
    assert "overflow-x: clip" in styles
    surface_rule = styles.split(".r-sprint-surface {", 1)[1].split("}", 1)[0]
    assert "min-width: 0" in surface_rule
    assert "min-width: max-content" not in surface_rule
    assert "overflow: auto" not in surface_rule
    owner_rule = base_styles.split(
        ".r-sprint-view .r-reader-with-attachments > .r-body {", 1
    )[1].split("}", 1)[0]
    assert "overflow-y: auto" in owner_rule


def test_selected_sprint_opens_completed_work_newest_first_with_fixed_event_strip():
    sprints = [
        {
            "id": "SA",
            "status": "active",
            "items": [{"slug": "alpha"}, {"slug": "beta"}],
        },
        {"id": "SB", "status": "active", "items": [{"slug": "gamma"}]},
        {"id": "SC", "status": "active", "items": [{"slug": "delta"}]},
    ]
    runs = {
        "alpha": [
            {
                "run_id": "older",
                "plan": "alpha",
                "node": "implement-owner",
                "section": "delivery",
                "gate": "passed",
                "commits": ["1111111"],
                "dispatched_at": "2026-08-25T08:00:00Z",
                "completed_at": "2026-08-25T08:15:00Z",
            }
        ],
        "beta": [
            {
                "run_id": "newer",
                "plan": "beta",
                "node": "verification-owner",
                "section": "verification",
                "gate": "failed",
                "commits": ["2222222"],
                "dispatched_at": "2026-08-25T09:00:00Z",
                "completed_at": "2026-08-25T09:20:00Z",
            }
        ],
    }
    result = _evaluate_helpers(
        "(() => {"
        f"const sprints = {json.dumps(sprints)};"
        f"const runs = sprintCompletedRuns(sprints[0], {json.dumps(runs)});"
        "const strip = horizonStrip(Date.parse('2026-08-25T12:00:00Z'), runs, []);"
        "return {"
        "runIds: runs.map(run => run.run_id),"
        "eventIds: strip.events.map(event => event.run.run_id),"
        "durationHours: (Date.parse(strip.end) - Date.parse(strip.start)) / HOUR_MS,"
        "tickLabels: strip.ticks.map(tick => tick.label),"
        "active: sprintStateRows(sprints, '2026-08-25').filter(row => row.active).map(row => row.sprint.id)"
        "};"
        "})()"
    )
    source = SOURCE.read_text()

    assert result["runIds"] == ["newer", "older"]
    assert result["eventIds"] == ["older", "newer"]
    assert result["durationHours"] == 48
    assert len(result["tickLabels"]) == 9
    assert all(":" in label for label in result["tickLabels"])
    assert result["active"] == ["SA", "SB", "SC"]
    assert 'run.dispatched_at || "not recorded"' in source
    assert 'run.completed_at || "not recorded"' in source
    assert "run.node || run.plan" in source
    assert 'run.section || "unsectioned"' in source
    assert 'run.gate || "not recorded"' in source
    assert '(run.commits || [])[0] || "no commit"' in source
    assert "No completed work is recorded for this sprint." in source
    assert "SPRINT_HORIZONS" not in source
    assert "sprintAxis" not in source
