from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "docs" / "ui" / "shell.jsx"
TOPBAR = ROOT / "docs" / "ui" / "topbar.css"


def _function_source(name: str) -> str:
    source = SHELL.read_text()
    start = source.index(f"function {name}(")
    brace = source.index(") {", start) + 2
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _evaluate(functions: list[str], expression: str) -> object:
    script = "\n".join(_function_source(name) for name in functions)
    result = subprocess.run(
        ["node", "-e", f"{script}\nconsole.log(JSON.stringify({expression}));"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _declarations(selector: str) -> dict[str, str]:
    source = TOPBAR.read_text()
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", source)
    assert match, f"missing CSS rule for {selector}"
    return {
        key.strip(): value.strip()
        for declaration in match.group(1).split(";")
        if ":" in declaration
        for key, value in [declaration.split(":", 1)]
    }


def test_each_route_selects_exactly_one_canvas_view() -> None:
    views = ["cockpit", "plan", "sprint", "graph", "crew"]
    selected = _evaluate(
        ["canvasViewForRoute"],
        f"{json.dumps(views)}.map(view => canvasViewForRoute({{view}}))",
    )

    assert selected == views
    assert all(
        sum(candidate == selected_view for candidate in views) == 1
        for selected_view in selected
    )

    app = _function_source("App")
    assert "r-3col" not in app
    assert '{canvasView === "plan" ? (' in app
    for view in ("cockpit", "sprint", "graph", "crew"):
        assert f'canvasView === "{view}" &&' in app
    assert ".r-3col.plans-mode" not in (ROOT / "docs/ui/plans.css").read_text()


def test_list_header_filters_render_labels_and_counts() -> None:
    filters = _function_source("ListFilterControls")
    labels = _evaluate(
        ["readableFilterLabel"],
        '["active", "in-progress", "on-hold"].map(readableFilterLabel)',
    )

    assert labels == ["Active", "In Progress", "On Hold"]

    status = re.search(
        r'<select aria-label="Filter plans by status".*?>(.*?)</select>',
        filters,
        re.DOTALL,
    )
    assert status
    assert '<option value="">All · {actionable.length}</option>' in status.group(1)
    assert "{readableFilterLabel(status)} · {count}" in status.group(1)
    assert "data-count={count}" in status.group(1)

    sprint = re.search(
        r'<select aria-label="Filter plans by sprint".*?>(.*?)</select>',
        filters,
        re.DOTALL,
    )
    assert sprint
    assert (
        '{sprint.id}{sprint.theme ? ` · ${sprint.theme}` : ""} · {count}'
        in sprint.group(1)
    )
    assert "data-count={count}" in sprint.group(1)
    assert '<button type="button" className="r-list-filter-clear"' in filters

    listing = _function_source("ListCol")
    assert "<ListFilterControls" in listing
    assert "FiltersCol" not in SHELL.read_text()


def test_topbar_contains_tabs_in_its_single_row() -> None:
    topbar = _function_source("TopBar")
    declarations = _declarations(".r-topbar")

    assert '<div className="r-glyph-tabs">' in topbar
    assert topbar.index('<div className="r-glyph-tabs">') < topbar.rindex("</div>")
    assert declarations["display"] == "flex"
    assert declarations["gap"] == "18px"
    assert declarations["padding"] == "9px 18px"
    assert declarations["border-bottom"] == "1px solid var(--line)"
    assert "grid-template-rows" not in declarations


def test_manage_lists_and_counts_every_mounted_project() -> None:
    projects = [
        {"project": f"project-{index}", "plans_count": index % 2} for index in range(12)
    ]
    rows = _evaluate(
        ["manageableProjectRows"],
        f"manageableProjectRows({json.dumps(projects)}).map(row => row.project)",
    )

    assert rows == [project["project"] for project in projects]
    topbar = _function_source("TopBar")
    assert "{manageableProjects.length} mounted" in topbar
    assert "{manageableProjects.map(project =>" in topbar


def test_plan_rows_omit_missing_metadata_and_the_decisions_badge() -> None:
    listing = _function_source("ListCol")

    assert 'p.last || "unknown"' not in listing
    assert "edited {edited}" in listing
    assert "{edited &&" in listing
    assert "Decisions {p.dec_open}" not in listing
    assert "p.roi, p.effort" in listing
    assert 'value !== "—"' in listing
