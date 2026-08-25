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
    assert all(sum(candidate == selected_view for candidate in views) == 1 for selected_view in selected)

    app = _function_source("App")
    assert "r-3col" not in app
    assert '{canvasView === "plan" ? (' in app
    for view in ("cockpit", "sprint", "graph", "crew"):
        assert f'canvasView === "{view}" &&' in app


def test_filter_chips_render_only_a_dot_and_count() -> None:
    filters = _function_source("FiltersCol")

    status = re.search(
        r'<button type="button" key=\{s\}.*?title=\{s\}>(.*?)</button>',
        filters,
        re.DOTALL,
    )
    assert status
    assert status.group(1).count("<span") == 2
    assert 'className={`dot ${s}`}' in status.group(1)
    assert 'className="n"' in status.group(1)
    assert "textTransform" not in status.group(1)

    sprint = re.search(
        r'<button type="button" key=\{s\.id\}.*?title=.*?>(.*?)</button>',
        filters,
        re.DOTALL,
    )
    assert sprint
    assert sprint.group(1).count("<span") == 2
    assert 'className="dot sprint"' in sprint.group(1)
    assert 'className="n"' in sprint.group(1)


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
    projects = [{"project": f"project-{index}", "plans_count": index % 2} for index in range(12)]
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
    assert 'p.roi, p.effort' in listing
    assert 'value !== "—"' in listing
