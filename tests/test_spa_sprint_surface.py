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
    assert "min-width:" not in styles.replace("min-width: 0", "").replace("min-width: 4px", "")
