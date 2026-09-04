import json
import subprocess
from pathlib import Path

import pytest

from reckon.serve import _attach_ready_set
from tests import spa_module_eval

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "docs" / "ui" / "sprint.jsx"
GRAPH = ROOT / "docs" / "ui" / "graph.jsx"


def _evaluate_helpers(expression: str, tmp_path: Path):
    source = SOURCE.read_text()
    helpers = source[
        source.index("const CLOSED_ITEM_STATUSES") : source.index("function SprintDetail(")
    ]
    helper_module = tmp_path / "sprint_helpers.jsx"
    helper_module.write_text(helpers)
    return spa_module_eval.evaluate_jsx_module(helper_module, expression)


def _evaluate_with_layout(expression: str, tmp_path: Path):
    """Sprint helpers plus the real exported `window.ReckonGraph.layout`."""
    source = SOURCE.read_text()
    helpers = source[
        source.index("const CLOSED_ITEM_STATUSES") : source.index("function SprintDetail(")
    ]
    combined = f"{GRAPH.read_text()}\n{helpers}"
    module = tmp_path / "sprint_with_graph.jsx"
    module.write_text(combined)
    return spa_module_eval.evaluate_jsx_module(module, expression)


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


def test_overview_keeps_every_active_sprint_and_folds_only_closed_rows(tmp_path):
    inventory = [
        {"slug": "alpha", "status": "active", "impl": 0.5, "effort_hours": 4},
        {"slug": "beta", "status": "active", "impl": 0.5, "effort_hours": 4},
        {"slug": "gamma", "status": "active", "impl": 0.5, "effort_hours": 4},
        {"slug": "delta", "status": "shipped", "impl": 1.0, "effort_hours": 4},
    ]
    sprints = [
        {"id": "SA", "status": "active", "items": [{"slug": "alpha"}]},
        {"id": "SB", "status": "active", "items": [{"slug": "beta"}]},
        {"id": "SC", "status": "active", "items": [{"slug": "gamma"}]},
        {"id": "SD", "status": "done", "items": [{"slug": "delta"}]},
    ]
    result = _evaluate_helpers(
        "(() => {"
        f"const rows = sprintStateRows({json.dumps(sprints)}, {json.dumps(inventory)});"
        "return {visible: rows.filter(row => !row.closed), folded: rows.filter(row => row.closed)};"
        "})()",
        tmp_path,
    )

    assert [row["sprint"]["id"] for row in result["visible"]] == ["SA", "SB", "SC"]
    assert [row["sprint"]["id"] for row in result["folded"]] == ["SD"]


def test_status_transition_flag_names_effective_state_and_open_gates(tmp_path):
    plan = {
        "status": "active",
        "workflow_status": "active",
        "effective_status": "blocked",
        "gates": [{"verdict": "pending"}, {"verdict": "passed"}],
    }
    assert (
        _evaluate_helpers(f"readyLaneState({json.dumps(plan)})", tmp_path)
        == "active → blocked · 1 open gate"
    )


def test_surface_keeps_description_concise_and_contract_reachable():
    source = SOURCE.read_text()

    assert 'className="r-ready-lane-description"' in source
    assert "title={lane.description}" in source
    assert "<summary>Contract</summary>" in source
    assert "lane.whyNow" in source
    assert "lane.doneWhen" in source
    assert 'className="r-ready-lane-plan-state"' in source
    assert 'className="r-ready-lane-invocation"' in source
    assert 'surface === "overview"' in source
    assert 'surface === "ready"' in source
    assert "active_sprints" in source
    assert 'className="r-sprint-conflict"' in source
    assert '<table className="r-sprint-table">' in source
    assert 'className="r-needs-you"' in source


def test_sprint_styles_do_not_force_a_sideways_canvas():
    styles = (ROOT / "docs" / "ui" / "sprints.css").read_text()
    base_styles = (ROOT / "docs" / "ui" / "styles-base.css").read_text()

    assert ".r-ready-lane { display: grid; gap: 8px; min-width: 0" in styles
    assert "overflow-wrap: anywhere" in styles
    assert "overflow-x: clip" in styles
    surface_rule = styles.split(".r-sprint-surface {", 1)[1].split("}", 1)[0]
    assert "min-width: 0" in surface_rule
    assert "min-width: max-content" not in surface_rule
    assert "overflow: auto" not in surface_rule
    owner_rule = base_styles.split(
        ".r-sprint-view .r-reader-with-attachments > .r-body {", 1
    )[1].split("}", 1)[0]
    assert "overflow-y: auto" in owner_rule


def test_horizon_strip_and_recorded_work_are_retired():
    source = SOURCE.read_text()
    assert "HORIZON_HOURS" not in source
    assert "horizonStrip" not in source
    assert "r-horizon-strip" not in source
    assert "r-completed-work" not in source
    assert 0 == source.count("HORIZON_HOURS")


def test_sprint_state_flag_reads_roadmap_payload(tmp_path):
    inventory = [
        {"slug": "one", "status": "shipped", "impl": 1.0, "effort_hours": 5},
        {"slug": "two", "status": "shipped", "impl": 1.0, "effort_hours": 3},
    ]
    sprint = {
        "id": "delivery",
        "status": "planned",
        "items": [{"slug": "one"}, {"slug": "two"}],
        "derived_state": "shipped",
        "state_drift": {"stored": "planned", "derived": "shipped"},
        "implementation_pct": 100.0,
        "blocked": 0,
    }
    changed = {
        **sprint,
        "derived_state": "active",
        "state_drift": {"stored": "queued", "derived": "active"},
    }
    result = _evaluate_helpers(
        "(() => {"
        f"const inventory = {json.dumps(inventory)};"
        f"const original = sprintStateRows([{json.dumps(sprint)}], inventory)[0];"
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


def test_http_roadmap_projection_enriches_sprints_with_derived_state() -> None:
    result = {
        "inventory": [
            {
                "slug": "member",
                "title": "Member",
                "type": "plan",
                "status": "shipped",
                "impl": 1.0,
                "depends_on": [],
            }
        ],
        "sprints": [
            {
                "id": "delivery",
                "status": "planned",
                "items": [{"slug": "member"}],
            }
        ],
        "active_sprint_id": None,
    }

    _attach_ready_set(result, "sample")

    assert result["sprints"][0]["derived_state"] == "shipped"
    assert result["sprints"][0]["state_drift"] == {
        "stored": "planned",
        "derived": "shipped",
    }
    assert result["sprints"][0]["implementation_pct"] == 100.0
    assert result["sprints"][0]["items"] == [{"slug": "member"}]
    assert result["ready_set"]["sprints"][0]["derived_state"] == "shipped"


def test_sprint_detail_pulls_out_of_sprint_prerequisites_as_ghosts(tmp_path):
    inventory = [
        {"slug": "prereq-a", "status": "shipped", "impl": 1.0, "effort_hours": 4, "depends_on": [], "sprint": "S1"},
        {"slug": "prereq-b", "status": "pending", "impl": 0.0, "effort_hours": 4, "depends_on": [], "sprint": "S1"},
        {"slug": "member-a", "status": "active", "impl": 0.3, "effort_hours": 6, "depends_on": ["prereq-a"], "sprint": "S2"},
        {"slug": "member-b", "status": "blocked", "impl": 0.0, "effort_hours": 6, "depends_on": ["prereq-b"], "sprint": "S2"},
    ]
    sprint = {"id": "S2", "status": "active", "items": [{"slug": "member-a"}, {"slug": "member-b"}]}

    result = _evaluate_with_layout(
        "(() => {"
        f"const sprint = {json.dumps(sprint)};"
        f"const inventory = {json.dumps(inventory)};"
        "const plans = sprintDagPlans(sprint, inventory);"
        "const layout = window.ReckonGraph.layout(plans, 'test');"
        "return {"
        "nodeCount: layout.nodes.length,"
        "ghostCount: layout.nodes.filter(n => n.ghost).length,"
        "edgeCount: layout.edges.length,"
        "dashedFromUnshipped: layout.edges.find(e => e.from === 'prereq-b').dash !== '0',"
        "solidFromShipped: layout.edges.find(e => e.from === 'prereq-a').dash === '0',"
        "heldStroke: layout.edges.find(e => e.from === 'prereq-b').held,"
        "notHeldFromShipped: layout.edges.find(e => e.from === 'prereq-a').held,"
        "};"
        "})()",
        tmp_path,
    )

    assert result["nodeCount"] == 4
    assert result["ghostCount"] == 2
    assert result["edgeCount"] == 2
    assert result["dashedFromUnshipped"] is True
    assert result["solidFromShipped"] is True
    assert result["heldStroke"] is True
    assert result["notHeldFromShipped"] is False


def test_sprint_ship_control_disabled_with_open_decision_count(tmp_path):
    inventory = [
        {
            "slug": "member-a",
            "status": "active",
            "impl": 0.2,
            "effort_hours": 4,
            "depends_on": [],
            "decisions": [{"key": "d1"}, {"key": "d2", "choice": "x"}],
        },
        {
            "slug": "member-b",
            "status": "active",
            "impl": 0.2,
            "effort_hours": 4,
            "depends_on": [],
            "decisions": [{"key": "d3"}],
        },
    ]
    sprint = {"id": "S3", "status": "active", "items": [{"slug": "member-a"}, {"slug": "member-b"}]}

    result = _evaluate_with_layout(
        "(() => {"
        f"const sprint = {json.dumps(sprint)};"
        f"const inventory = {json.dumps(inventory)};"
        "return sprintDetailStats(sprint, inventory).openDecisions;"
        "})()",
        tmp_path,
    )

    assert result == 2
    source = SOURCE.read_text()
    assert 'disabled={openDecisions > 0}' in source
    assert '`${openDecisions} open decision${openDecisions === 1 ? "" : "s"}`' in source


def test_sprint_detail_header_stats_render_fixture_counts(tmp_path):
    inventory = [
        {"slug": "prereq-a", "status": "shipped", "impl": 1.0, "effort_hours": 4, "depends_on": []},
        {"slug": "prereq-b", "status": "pending", "impl": 0.0, "effort_hours": 4, "depends_on": []},
        {
            "slug": "member-a",
            "status": "active",
            "impl": 0.3,
            "effort_hours": 6,
            "depends_on": ["prereq-a"],
            "decisions": [{"key": "d1"}],
        },
        {
            "slug": "member-b",
            "status": "blocked",
            "impl": 0.0,
            "effort_hours": 6,
            "depends_on": ["prereq-b"],
            "decisions": [{"key": "d2"}],
        },
    ]
    sprint = {"id": "S2", "status": "active", "items": [{"slug": "member-a"}, {"slug": "member-b"}]}

    stats = _evaluate_with_layout(
        "(() => {"
        f"const sprint = {json.dumps(sprint)};"
        f"const inventory = {json.dumps(inventory)};"
        "return sprintDetailStats(sprint, inventory);"
        "})()",
        tmp_path,
    )

    assert stats["plans"] == 2
    assert stats["workerHours"] == 12
    assert stats["depth"] == 1
    assert stats["held"] == 1
    assert stats["prerequisites"] == 2
    assert stats["openDecisions"] == 2

    source = SOURCE.read_text()
    for label in ("plans", "worker-hours", "depth", "held", "prerequisites", "open decisions"):
        assert f"<span>{label}</span>" in source
