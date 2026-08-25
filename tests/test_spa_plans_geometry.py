import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "docs" / "ui" / "shell.jsx"
PLANS_CSS = ROOT / "docs" / "ui" / "plans.css"
TOPBAR_CSS = ROOT / "docs" / "ui" / "topbar.css"


def _function_source(name: str) -> str:
    source = SHELL.read_text()
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _evaluate(functions: list[str], expression: str):
    script = "\n".join(_function_source(name) for name in functions)
    result = subprocess.run(
        ["node", "-e", f"{script}\nconsole.log(JSON.stringify({expression}));"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _declarations(path: Path, selector: str) -> dict[str, str]:
    source = path.read_text()
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", source)
    assert match, f"missing CSS rule for {selector}"
    return {
        key.strip(): value.strip()
        for declaration in match.group(1).split(";")
        if ":" in declaration
        for key, value in [declaration.split(":", 1)]
    }


def _assert_declarations(actual: dict[str, str], expected: dict[str, str]) -> None:
    assert actual.items() >= expected.items()


def test_plan_workspace_preserves_the_four_column_canvas_widths() -> None:
    workspace = _declarations(PLANS_CSS, ".r-3col.plans-mode")
    filters = _declarations(PLANS_CSS, ".r-3col.plans-mode > .r-filters")
    listing = _declarations(PLANS_CSS, ".r-3col.plans-mode > .r-list")
    content = _declarations(PLANS_CSS, ".r-3col.plans-mode > .r-content")
    reader = _declarations(
        PLANS_CSS, ".r-3col.plans-mode .r-reader-with-attachments > .r-body"
    )
    attachments = _declarations(
        PLANS_CSS, ".r-3col.plans-mode .r-attachment-rail"
    )

    _assert_declarations(workspace, {
        "display": "flex",
        "flex": "1",
        "min-height": "0",
        "min-width": "1374px",
    })
    _assert_declarations(filters, {
        "width": "64px",
        "flex": "none",
        "padding": "10px 8px",
        "border-right": "1px solid var(--line)",
        "gap": "14px",
        "overflow": "auto",
    })
    _assert_declarations(listing, {
        "width": "390px",
        "flex": "none",
        "border-right": "1px solid var(--line)",
    })
    _assert_declarations(content, {"display": "flex", "flex": "1", "min-width": "0"})
    _assert_declarations(reader, {
        "flex": "1",
        "min-width": "300px",
        "padding": "22px 26px 34px",
    })
    _assert_declarations(attachments, {
        "width": "300px",
        "flex": "none",
        "border-left": "1px solid var(--line)",
        "padding": "16px 14px",
    })


def test_filter_and_list_interior_geometry_matches_the_canvas() -> None:
    statuses = _declarations(PLANS_CSS, ".r-3col.plans-mode .r-filter-group")
    sprints = _declarations(PLANS_CSS, ".r-3col.plans-mode .r-sprint-filters")
    divider = _declarations(PLANS_CSS, ".r-filter-divider")
    header = _declarations(PLANS_CSS, ".r-3col.plans-mode .r-sort-bar")
    square_buttons = _declarations(PLANS_CSS, ".r-sort-dir,\n.r-sort-more")
    body = _declarations(PLANS_CSS, ".r-list-body")

    assert statuses["gap"] == "4px"
    assert sprints["gap"] == "3px"
    assert divider["height"] == "1px"
    _assert_declarations(header, {
        "display": "flex",
        "gap": "8px",
        "padding": "10px 12px",
        "border-bottom": "1px solid var(--line)",
        "flex": "none",
    })
    _assert_declarations(square_buttons, {"width": "25px", "height": "25px"})
    _assert_declarations(body, {"flex": "1", "overflow": "auto"})


def test_reader_typography_and_topbar_hold_the_declared_geometry() -> None:
    heading = _declarations(
        PLANS_CSS, ".r-3col.plans-mode .r-reader-with-attachments > .r-body h2"
    )
    metadata = _declarations(
        PLANS_CSS,
        ".r-3col.plans-mode .r-row .meta,\n.r-3col.plans-mode .r-reader-with-attachments .meta",
    )
    topbar = _declarations(TOPBAR_CSS, ".r-topbar")

    _assert_declarations(heading, {
        "font-size": "23px",
        "font-weight": "600",
        "letter-spacing": "-.015em",
    })
    _assert_declarations(metadata, {
        "font-family": "var(--mono)",
        "font-size": "11.5px",
    })
    assert topbar["min-width"] == "1374px"

    shell = SHELL.read_text()
    assert 'className="r-sort-more"' in shell
    assert 'className="r-attachment-empty"' in shell


def test_idle_projects_are_hidden_until_the_browser_records_an_override() -> None:
    projects = [
        {
            "project": "open-work",
            "plans_count": 1,
            "state": {"inventory": [{"type": "plan", "status": "active"}]},
        },
        {
            "project": "settled-work",
            "plans_count": 1,
            "state": {"inventory": [{"type": "plan", "status": "done"}]},
        },
    ]
    functions = [
        "mountedProjectRows",
        "projectHasOpenWork",
        "effectiveHiddenProjects",
        "visibleProjectRows",
    ]
    default_rows = _evaluate(
        functions,
        f"visibleProjectRows({json.dumps(projects)}, null).map(row => row.project)",
    )
    overridden_rows = _evaluate(
        functions,
        f"visibleProjectRows({json.dumps(projects)}, []).map(row => row.project)",
    )

    assert default_rows == ["open-work"]
    assert overridden_rows == ["open-work", "settled-work"]
    assert "localStorage.getItem(PROJECT_VISIBILITY_STORAGE)" in SHELL.read_text()
    assert "localStorage.setItem(PROJECT_VISIBILITY_STORAGE" in SHELL.read_text()
