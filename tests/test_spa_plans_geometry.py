import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    BrowserProbeError,
    authored_shell_source,
    installed_browser,
    run_browser_probe,
    served_spa,
)

ROOT = Path(__file__).resolve().parents[1]
SHELL = authored_shell_source(ROOT)
PLAN = ROOT / "docs" / "ui" / "plan.jsx"
PLANS_CSS = ROOT / "docs" / "ui" / "plans.css"
TOPBAR_CSS = ROOT / "docs" / "ui" / "topbar.css"


@pytest.fixture(scope="module")
def rendered_browser(tmp_path_factory) -> str:
    browser = installed_browser()
    if browser is None:
        pytest.skip("no supported browser binary is installed")
    try:
        run_browser_probe(
            tmp_path_factory.mktemp("browser-capability"),
            browser,
            "<!doctype html><html><body>ready</body></html>",
            "document.body.textContent",
        )
    except BrowserProbeError as error:
        pytest.skip(f"browser unavailable ({error.classification}): {error}")
    return browser


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


def test_artifact_index_uses_the_full_width_and_owns_its_scroll() -> None:
    workspace = _declarations(PLANS_CSS, ".r-canvas-view")
    content = _declarations(PLANS_CSS, ".r-canvas-view > .r-content")
    index = _declarations(PLANS_CSS, ".r-artifact-index")
    feed = _declarations(PLANS_CSS, ".r-artifact-feed")

    _assert_declarations(
        workspace,
        {
            "display": "flex",
            "flex": "1",
            "min-height": "0",
            "min-width": "0",
        },
    )
    _assert_declarations(
        index,
        {
            "display": "flex",
            "flex": "1",
            "min-width": "0",
            "min-height": "0",
            "overflow": "hidden",
        },
    )
    _assert_declarations(content, {"display": "flex", "flex": "1", "min-width": "0"})
    _assert_declarations(feed, {"flex": "1", "min-height": "0", "overflow": "auto"})
    assert ".r-attachment-rail" not in PLANS_CSS.read_text()


def test_feed_controls_share_the_artifact_index_header() -> None:
    header = _declarations(PLANS_CSS, ".r-artifact-index-head")
    actions = _declarations(
        PLANS_CSS, ".r-artifact-index-actions,\n.r-feed-status-filters"
    )
    chips = _declarations(PLANS_CSS, ".r-feed-status-filters button")
    hide_done = _declarations(PLANS_CSS, ".r-hide-done")

    _assert_declarations(
        header,
        {
            "display": "grid",
            "grid-template-columns": "minmax(180px, 1fr) auto",
            "gap": "12px 24px",
            "padding": "24px 28px 16px",
            "border-bottom": "1px solid var(--line)",
        },
    )
    _assert_declarations(
        actions, {"display": "flex", "align-items": "center", "gap": "8px"}
    )
    _assert_declarations(chips, {"border-radius": "999px", "font": "11px var(--mono)"})
    _assert_declarations(hide_done, {"display": "inline-flex", "white-space": "nowrap"})

    index = _function_source("ArtifactIndex")
    assert '<div className="r-feed-status-filters"' in index
    assert 'aria-label="Filter plans by status"' in index
    assert 'className="r-hide-done"' in index
    assert "data-count={count}" in index
    assert 'kind === "plan"' in index


def test_reader_typography_is_preserved_without_a_width_floor() -> None:
    heading = _declarations(
        PLANS_CSS, ".r-plans-view .r-reader-with-attachments > .r-body h2"
    )
    metadata = _declarations(
        PLANS_CSS,
        ".r-plans-view .r-row .meta,\n.r-plans-view .r-reader-with-attachments .meta",
    )
    topbar = _declarations(TOPBAR_CSS, ".r-topbar")

    _assert_declarations(
        heading,
        {
            "font-size": "23px",
            "font-weight": "600",
            "letter-spacing": "-.015em",
        },
    )
    _assert_declarations(
        metadata,
        {
            "font-family": "var(--mono)",
            "font-size": "11.5px",
        },
    )
    assert "min-width" not in topbar

    shell = SHELL.read_text()
    assert 'className="r-sort-more"' in shell
    assert 'className="r-attachment-rail"' not in shell
    assert 'className="r-reader-attachment-bar"' in PLAN.read_text()


@pytest.mark.parametrize("viewport_width", [1280, 1440, 1920])
def test_rendered_plan_index_fills_the_window(
    tmp_path: Path,
    rendered_browser: str,
    viewport_width: int,
) -> None:
    browser = rendered_browser

    probe = r"""(() => {
      const root = document.documentElement;
      const listing = document.querySelector(".r-artifact-index");
      const visible = [...document.body.querySelectorAll("*")].filter(element => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
      });
      const overflow = visible
        .map(element => ({
          selector: element.className || element.tagName,
          left: element.getBoundingClientRect().left,
          right: element.getBoundingClientRect().right,
        }))
        .filter(row => row.left < -1 || row.right > root.clientWidth + 1);
      return {
        clientWidth: root.clientWidth,
        scrollWidth: root.scrollWidth,
        indexWidth: listing.getBoundingClientRect().width,
        overflow,
      };
    })()"""

    with served_spa(tmp_path, browser, route="#plans") as context:
        geometry = context.run_probe(
            probe,
            viewport=(viewport_width, 900),
            ready_expression='Boolean(document.querySelector(".r-artifact-row"))',
        )

    assert geometry["scrollWidth"] == geometry["clientWidth"], geometry
    assert geometry["indexWidth"] == pytest.approx(viewport_width, abs=3), geometry
    assert geometry["overflow"] == [], geometry


def test_rendered_status_chips_reduce_the_plan_feed(
    tmp_path: Path,
    rendered_browser: str,
) -> None:
    browser = rendered_browser

    probe = r"""(async () => {
      const countRows = () => document.querySelectorAll(".r-artifact-row").length;
      const choose = async button => {
        button.click();
        await new Promise(resolve => setTimeout(resolve, 150));
        return countRows();
      };

      const status = document.querySelector('.r-feed-status-filters');
      const baseline = countRows();
      const statusOption = [...status.querySelectorAll("button")]
        .find(button => Number(button.dataset.count) > 0 && Number(button.dataset.count) < baseline);
      const statusCount = await choose(statusOption);
      const all = status.querySelector("button");
      const restored = await choose(all);
      return {
        baseline,
        statusCount,
        restored,
        statusLabel: statusOption.textContent.trim(),
      };
    })()"""

    with served_spa(tmp_path, browser, route="#plans") as context:
        result = context.run_probe(
            probe,
            viewport=(1440, 900),
            ready_expression=(
                'Boolean(document.querySelector(".r-artifact-row") '
                '&& document.querySelectorAll(".r-feed-status-filters button").length === 5)'
            ),
        )

    assert result["baseline"] > result["statusCount"] > 0, result
    assert result["restored"] == result["baseline"], result
    assert result["statusLabel"], result


@pytest.mark.parametrize("viewport_width", [1280, 1440, 1920])
def test_rendered_plan_rows_and_controls_are_legible(
    tmp_path: Path, rendered_browser: str, viewport_width: int
) -> None:
    browser = rendered_browser

    probe = r"""(() => {
      const list = document.querySelector(".r-artifact-feed");
      const rows = [...list.querySelectorAll(":scope > .r-artifact-row")];
      const clippedRows = rows.map(element => {
        const row = element.getBoundingClientRect();
        const rect = element.getBoundingClientRect();
        return {
          clippedContent: element.scrollWidth > element.clientWidth,
          outsideRow: rect.left < row.left || rect.right > row.right,
        };
      }).filter(result => result.clippedContent || result.outsideRow).length;
      const title = document.querySelector(".r-artifact-index-title h1")?.textContent.trim() || "";
      const chips = [...document.querySelectorAll(".r-feed-status-filters button")]
        .map(button => button.textContent.trim());
      return {
        rowCount: rows.length,
        clippedRows,
        stampRows: rows.filter(row => row.querySelectorAll(".r-artifact-stamps span").length === 2).length,
        statusRows: rows.filter(row => row.querySelector(".r-artifact-status-chip")).length,
        title,
        chips,
        notFoundOccurrences: (document.body.textContent.toLowerCase().match(/not found/g) || []).length,
      };
    })()"""

    with served_spa(tmp_path, browser, route="#plans") as context:
        geometry = context.run_probe(
            probe,
            viewport=(viewport_width, 900),
            ready_expression=(
                'Boolean(document.querySelector(".r-artifact-row") '
                '&& document.querySelector(".r-feed-status-filters"))'
            ),
        )

    assert geometry["rowCount"] > 0, geometry
    assert geometry["clippedRows"] == 0, geometry
    assert geometry["stampRows"] == geometry["rowCount"], geometry
    assert geometry["statusRows"] == geometry["rowCount"], geometry
    assert geometry["title"].endswith(" in reckon"), geometry
    assert len(geometry["chips"]) == 5, geometry
    assert all(
        any(chip.startswith(label) for chip in geometry["chips"])
        for label in ("All", "active", "blocked", "pending", "shipped")
    ), geometry
    assert geometry["notFoundOccurrences"] == 0, geometry


def test_project_visibility_defaults_to_all_and_respects_stored_hiding() -> None:
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
        "effectiveHiddenProjects",
        "visibleProjectRows",
    ]
    default_rows = _evaluate(
        functions,
        f"visibleProjectRows({json.dumps(projects)}, null).map(row => row.project)",
    )
    stored_preference_rows = _evaluate(
        functions,
        f"visibleProjectRows({json.dumps(projects)}, ['settled-work']).map(row => row.project)",
    )

    assert default_rows == ["open-work", "settled-work"]
    assert stored_preference_rows == ["open-work"]
    assert "localStorage.getItem(PROJECT_VISIBILITY_STORAGE)" in SHELL.read_text()
    assert "localStorage.setItem(PROJECT_VISIBILITY_STORAGE" in SHELL.read_text()
