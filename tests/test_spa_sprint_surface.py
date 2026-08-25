import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "docs" / "ui" / "sprint.jsx"


def _evaluate_helpers(expression: str):
    source = SOURCE.read_text()
    helpers = source[: source.index("function Sprint(")]
    script = f"{helpers}\nconsole.log(JSON.stringify({expression}));"
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_overview_keeps_every_active_sprint_and_folds_only_closed_rows():
    sprints = [
        {"id": "SA", "status": "active", "items": [{"slug": "alpha"}]},
        {"id": "SB", "status": "active", "items": [{"slug": "beta"}]},
        {"id": "SC", "status": "active", "items": [{"slug": "gamma"}]},
        {"id": "SD", "status": "done", "items": [{"slug": "delta"}]},
    ]
    inventory = [
        {"slug": "alpha", "status": "active"},
        {"slug": "beta", "status": "blocked"},
        {"slug": "gamma", "status": "pending"},
        {"slug": "delta", "status": "shipped"},
    ]
    result = _evaluate_helpers(
        f"sprintOverviewRows({json.dumps(sprints)}, {json.dumps(inventory)}, true)"
    )

    assert [row["sprint"]["id"] for row in result["visible"]] == ["SA", "SB", "SC"]
    assert [row["sprint"]["id"] for row in result["folded"]] == ["SD"]
    assert result["foldedCount"] == 1


def test_status_transition_flag_names_effective_state_and_open_gates():
    plan = {
        "status": "active",
        "workflow_status": "active",
        "effective_status": "blocked",
        "gates": [{"verdict": "pending"}, {"verdict": "passed"}],
    }
    assert _evaluate_helpers(f"planFlag({json.dumps(plan)}, [])") == "active → blocked · 1 open gate"


def test_surface_keeps_description_concise_and_contract_reachable():
    source = SOURCE.read_text()

    assert 'className="r-card-description"' in source
    assert '<summary>Contract</summary>' in source
    assert "plan.whyNow" in source
    assert "plan.doneWhen" in source
    assert 'className="r-card-flag"' in source
    assert 'aria-label={`${plan.title}: ${percent}% complete`}' in source
    assert 'surface === "overview"' in source
    assert 'surface === "board"' in source
    assert "active_sprints" in source
    assert "legacy focus" in source
    assert 'className="r-needs-you"' in source


def test_sprint_styles_do_not_force_a_sideways_canvas():
    styles = (ROOT / "docs" / "ui" / "sprints.css").read_text()

    assert "grid-template-columns: minmax(9rem, 15rem) minmax(0, 1fr)" in styles
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in styles
    assert "overflow-x: clip" in styles
    surface_rule = styles.split(".r-sprint-surface {", 1)[1].split("}", 1)[0]
    assert "min-width: 0" in surface_rule
    assert "min-width: max-content" not in surface_rule


def test_selected_sprint_opens_completed_work_newest_first_with_subday_horizons():
    sprints = [
        {"id": "SA", "status": "active", "items": [{"slug": "alpha"}, {"slug": "beta"}]},
        {"id": "SB", "status": "active", "items": [{"slug": "gamma"}]},
        {"id": "SC", "status": "active", "items": [{"slug": "delta"}]},
    ]
    runs = {
        "alpha": [{
            "run_id": "older",
            "plan": "alpha",
            "node": "implement-owner",
            "section": "delivery",
            "gate": "passed",
            "commits": ["1111111"],
            "dispatched_at": "2026-08-25T08:00:00Z",
            "completed_at": "2026-08-25T08:15:00Z",
        }],
        "beta": [{
            "run_id": "newer",
            "plan": "beta",
            "node": "verification-owner",
            "section": "verification",
            "gate": "failed",
            "commits": ["2222222"],
            "dispatched_at": "2026-08-25T09:00:00Z",
            "completed_at": "2026-08-25T09:20:00Z",
        }],
    }
    result = _evaluate_helpers(
        "(() => {"
        f"const sprints = {json.dumps(sprints)};"
        f"const runs = sprintCompletedRuns(sprints[0], {json.dumps(runs)});"
        "const hour = sprintAxis(sprints, '1hr', '2026-08-25', runs);"
        "const day = sprintAxis(sprints, '1D', '2026-08-25', runs);"
        "return {runIds: runs.map(run => run.run_id), hour, day, active: sprintOverviewRows(sprints, [], false).visible.map(row => row.sprint.id)};"
        "})()"
    )
    source = SOURCE.read_text()

    assert result["runIds"] == ["newer", "older"]
    assert result["hour"]["subDay"] is True
    assert result["day"]["subDay"] is True
    assert all(":" in tick["label"] for tick in result["hour"]["ticks"])
    assert all(":" in tick["label"] for tick in result["day"]["ticks"])
    assert result["active"] == ["SA", "SB", "SC"]
    assert 'run.dispatched_at || "not recorded"' in source
    assert 'run.completed_at || "not recorded"' in source
    assert "run.node || run.plan" in source
    assert "run.section || \"unsectioned\"" in source
    assert "run.gate || \"not recorded\"" in source
    assert '(run.commits || [])[0] || "no commit"' in source
    assert "No completed work is recorded for this sprint." in source
    assert '"1D"' in source and '"1hr"' in source
