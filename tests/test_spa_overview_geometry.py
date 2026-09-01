import json
import re
import subprocess
from pathlib import Path

from tests.spa_browser_harness import authored_shell_source

ROOT = Path(__file__).resolve().parents[1]
STYLES = (ROOT / "docs" / "ui" / "overview.css").read_text()
SHELL = authored_shell_source(ROOT).read_text()


def _function_source(name: str) -> str:
    start = SHELL.index(f"function {name}(")
    brace = SHELL.index("{", start)
    depth = 0
    for index in range(brace, len(SHELL)):
        if SHELL[index] == "{":
            depth += 1
        elif SHELL[index] == "}":
            depth -= 1
            if depth == 0:
                return SHELL[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _overview_sections(state: dict) -> dict:
    script = (
        f"{_function_source('overviewOptionalSections')}\n"
        f"console.log(JSON.stringify(overviewOptionalSections({json.dumps(state)})));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _declarations(selector: str) -> dict[str, str]:
    match = re.search(
        rf"(?m)(?<!,\n)^{re.escape(selector)}[ \t]*\{{([^}}]+)\}}",
        STYLES,
    )
    assert match, f"missing CSS rule for {selector}"
    return {
        key.strip(): value.strip()
        for declaration in match.group(1).split(";")
        if ":" in declaration
        for key, value in [declaration.split(":", 1)]
    }


def test_overview_owns_the_canvas_scroll_geometry() -> None:
    assert 'route.view === "cockpit" ? "r-overview-container" : ""' in SHELL
    assert 'route.view === "cockpit" ? "r-overview-view" : ""' in SHELL
    container = _declarations(".r-overview-container")
    view = _declarations(".r-reader-with-attachments > .r-body.r-overview-view")

    assert container["display"] == "flex"
    assert container["flex"] == "1"
    assert container["min-height"] == "0"
    assert view["flex"] == "1"
    assert view["overflow"] == "auto"
    assert view["padding"] == "20px 26px 40px"


def test_statistics_are_one_bordered_row_with_fixed_cell_geometry() -> None:
    strip = _declarations(".r-overview-stats")
    cell = _declarations(".r-overview-stat")

    assert strip == {
        "display": "flex",
        "align-items": "center",
        "gap": "0",
        "margin-bottom": "18px",
        "border": "1px solid var(--line)",
        "border-radius": "8px",
        "background": "var(--bg)",
        "overflow": "hidden",
    }
    assert cell["padding"] == "11px 16px"
    assert cell["min-width"] == "130px"
    assert cell["border-right"] == "1px solid var(--line)"


def test_blockers_keep_the_declared_card_and_type_scale() -> None:
    cards = _declarations(".r-overview-blocker-list")
    blocker = _declarations(".r-overview-blockers article")
    summary = _declarations(".r-overview-blocker-summary")
    next_action = _declarations(".r-overview-blocker-next")

    assert cards["display"] == "flex"
    assert cards["gap"] == "8px"
    assert blocker["padding"] == "12px 14px"
    assert blocker["border"] == "1px solid var(--bad)"
    assert summary["font-size"] == "13.5px"
    assert summary["max-width"] == "110ch"
    assert next_action["padding"] == "6px 9px"
    assert next_action["font"] == "11.5px var(--mono)"


def test_projects_table_matches_the_canvas_columns_and_row_paddings() -> None:
    shared = _declarations(".r-overview-project-head,\n.r-overview-project-row")
    head = _declarations(".r-overview-project-head")
    row = _declarations(".r-overview-project-row")
    project = _declarations(".r-overview-project-row > strong")
    sprint = _declarations(".r-overview-sprints")

    assert shared["display"] == "grid"
    assert shared["grid-template-columns"] == (
        "130px minmax(0, 1fr) 70px 70px 60px 60px"
    )
    assert shared["gap"] == "12px"
    assert head["padding"] == "8px 12px"
    assert head["font"] == "10px var(--mono)"
    assert row["align-items"] == "center"
    assert row["padding"] == "10px 12px"
    assert project["font-size"] == "12.5px"
    assert sprint["font-size"] == "13px"


def test_empty_overview_sections_omit_their_heading_at_canvas_width() -> None:
    empty = _overview_sections({"project": "reckon"})
    populated = _overview_sections(
        {
            "project": "reckon",
            "north_stars": [{"id": "clarity"}],
            "projects": [{"milestones": [{"id": "delivery"}]}],
        }
    )

    assert empty == {"northStars": [], "milestones": []}
    assert populated == {
        "northStars": [{"id": "clarity"}],
        "milestones": [{"id": "delivery"}],
    }
