import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    BrowserProbeError,
    installed_browser,
    run_browser_probe,
    served_spa,
)

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "docs" / "ui" / "shell.jsx"
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


def test_plan_workspace_uses_a_fluid_clamped_index_and_reader() -> None:
    workspace = _declarations(PLANS_CSS, ".r-canvas-view")
    listing = _declarations(PLANS_CSS, ".r-plans-view > .r-list")
    content = _declarations(PLANS_CSS, ".r-canvas-view > .r-content")
    reader = _declarations(
        PLANS_CSS, ".r-plans-view .r-reader-with-attachments > .r-body"
    )

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
        listing,
        {
            "width": "clamp(300px, 30%, 480px)",
            "flex": "0 0 clamp(300px, 30%, 480px)",
            "min-width": "0",
            "border-right": "1px solid var(--line)",
        },
    )
    _assert_declarations(content, {"display": "flex", "flex": "1", "min-width": "0"})
    _assert_declarations(
        reader,
        {
            "flex": "1",
            "min-width": "300px",
            "padding": "22px 26px 34px",
        },
    )
    assert ".r-attachment-rail" not in PLANS_CSS.read_text()


def test_filter_controls_share_the_plan_list_header() -> None:
    header = _declarations(PLANS_CSS, ".r-plans-view .r-sort-bar")
    controls = _declarations(PLANS_CSS, ".r-list-filter-controls")
    filter_control = _declarations(PLANS_CSS, ".r-list-filter")
    select = _declarations(PLANS_CSS, ".r-list-filter select")
    square_buttons = _declarations(PLANS_CSS, ".r-sort-dir,\n.r-sort-more")
    body = _declarations(PLANS_CSS, ".r-list-body")

    _assert_declarations(
        header,
        {
            "display": "flex",
            "flex-wrap": "wrap",
            "gap": "8px",
            "padding": "10px 12px",
            "border-bottom": "1px solid var(--line)",
            "flex": "none",
        },
    )
    _assert_declarations(
        controls,
        {"display": "flex", "flex": "1 0 100%", "gap": "6px", "min-width": "0"},
    )
    _assert_declarations(
        filter_control, {"display": "grid", "flex": "1", "min-width": "0"}
    )
    _assert_declarations(select, {"width": "100%", "min-width": "0", "height": "25px"})
    _assert_declarations(square_buttons, {"width": "25px", "height": "25px"})
    _assert_declarations(body, {"flex": "1", "overflow": "auto"})

    shell = SHELL.read_text()
    listing = _function_source("ListCol")
    filters = _function_source("ListFilterControls")
    assert "FiltersCol" not in shell
    assert 'className="r-filters"' not in shell
    assert "<ListFilterControls" in listing
    assert 'aria-label="Filter plans by status"' in filters
    assert 'aria-label="Filter plans by sprint"' in filters
    assert "data-count={count}" in filters


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


@pytest.mark.parametrize(
    ("viewport_width", "expected_index_width"),
    [(1280, 384), (1440, 432), (1920, 480)],
)
def test_rendered_plan_workspace_fits_the_window_and_clamps_the_index(
    tmp_path: Path,
    rendered_browser: str,
    viewport_width: int,
    expected_index_width: int,
) -> None:
    browser = rendered_browser

    probe = r"""(() => {
      const root = document.documentElement;
      const listing = document.querySelector(".r-plans-view > .r-list");
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
            ready_expression='Boolean(document.querySelector(".r-list-body > .r-row"))',
        )

    assert geometry["scrollWidth"] == geometry["clientWidth"], geometry
    assert geometry["indexWidth"] == pytest.approx(expected_index_width, abs=3), (
        geometry
    )
    assert geometry["overflow"] == [], geometry


def test_rendered_header_status_and_sprint_filters_reduce_the_plan_list(
    tmp_path: Path,
    rendered_browser: str,
) -> None:
    browser = rendered_browser

    probe = r"""(async () => {
      const countRows = () => document.querySelectorAll(".r-list-body > .r-row").length;
      const chooseReducingOption = select => [...select.options]
        .find(option => option.value && Number(option.dataset.count) > 0 && Number(option.dataset.count) < countRows());
      const choose = async (select, option) => {
        select.value = option.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        await new Promise(resolve => setTimeout(resolve, 150));
        return countRows();
      };

      const status = document.querySelector('[aria-label="Filter plans by status"]');
      const sprint = document.querySelector('[aria-label="Filter plans by sprint"]');
      const baseline = countRows();
      const statusOption = chooseReducingOption(status);
      const statusCount = await choose(status, statusOption);
      status.value = "";
      status.dispatchEvent(new Event("change", { bubbles: true }));
      await new Promise(resolve => setTimeout(resolve, 150));
      const restored = countRows();
      const sprintOption = chooseReducingOption(sprint);
      const sprintCount = await choose(sprint, sprintOption);
      return {
        baseline,
        statusCount,
        restored,
        sprintCount,
        statusValue: statusOption.value,
        sprintValue: sprintOption.value,
      };
    })()"""

    with served_spa(tmp_path, browser, route="#plans") as context:
        result = context.run_probe(
            probe,
            viewport=(1440, 900),
            ready_expression=(
                'Boolean(document.querySelector(".r-list-body > .r-row") '
                '&& document.querySelectorAll("[aria-label=\\"Filter plans by sprint\\"] option").length > 1)'
            ),
        )

    assert result["baseline"] > result["statusCount"] > 0, result
    assert result["restored"] == result["baseline"], result
    assert result["baseline"] > result["sprintCount"] > 0, result


@pytest.mark.parametrize("viewport_width", [1280, 1440, 1920])
def test_rendered_plan_rows_and_controls_are_legible(
    tmp_path: Path, rendered_browser: str, viewport_width: int
) -> None:
    browser = rendered_browser

    probe = r"""(() => {
      const list = document.querySelector(".r-list-body");
      const rows = [...list.querySelectorAll(":scope > .r-row")];
      const transitions = [...list.querySelectorAll(".r-status-transition")];
      const clippedTransitions = transitions.map(element => {
        const row = element.closest(".r-row").getBoundingClientRect();
        const rect = element.getBoundingClientRect();
        return {
          clippedContent: element.scrollWidth > element.clientWidth,
          outsideRow: rect.left < row.left || rect.right > row.right,
        };
      }).filter(result => result.clippedContent || result.outsideRow).length;
      const trailingSeparators = rows.filter(row =>
        row.querySelector(":scope > div > .meta")?.lastElementChild?.classList.contains("sp")
      ).length;
      const shown = document.querySelector(".r-sort-n")?.textContent.trim() || "";
      const statusControl = document.querySelector('[aria-label="Filter plans by status"]');
      const sprintControl = document.querySelector('[aria-label="Filter plans by sprint"]');
      const status = statusControl?.selectedOptions[0]?.textContent.trim() || "";
      const sprint = sprintControl?.selectedOptions[0]?.textContent.trim() || "";
      const emptyState = document.querySelector(".r-reader-empty-state");
      return {
        transitionCount: transitions.length,
        clippedTransitions,
        trailingSeparators,
        counts: {
          shown,
          status,
          sprint,
          statusLabel: statusControl?.closest("label")?.querySelector("span")?.textContent.trim() || "",
          sprintLabel: sprintControl?.closest("label")?.querySelector("span")?.textContent.trim() || "",
        },
        emptyState: emptyState?.textContent.trim() || "",
        notFoundOccurrences: (document.body.textContent.toLowerCase().match(/not found/g) || []).length,
      };
    })()"""

    with served_spa(tmp_path, browser, route="#plans") as context:
        geometry = context.run_probe(
            probe,
            viewport=(viewport_width, 900),
            ready_expression=(
                'Boolean(document.querySelector(".r-list-body > .r-row .meta") '
                '&& document.querySelector(".r-reader-empty-state"))'
            ),
        )

    assert geometry["transitionCount"] > 0, geometry
    assert geometry["clippedTransitions"] == 0, geometry
    assert geometry["trailingSeparators"] == 0, geometry
    assert geometry["counts"]["shown"].endswith(" shown"), geometry
    assert geometry["counts"]["statusLabel"] == "Status · total plans", geometry
    assert geometry["counts"]["sprintLabel"] == "Sprint · assigned plans", geometry
    assert geometry["counts"]["status"].startswith("All · "), geometry
    assert geometry["counts"]["sprint"].startswith("All · "), geometry
    assert "Select a plan to read" in geometry["emptyState"], geometry
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
