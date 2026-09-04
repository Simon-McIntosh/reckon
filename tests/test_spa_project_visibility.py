import json
import subprocess
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    AuthoredSource,
    BrowserProbeError,
    authored_shell_source,
    installed_browser,
    run_browser_probe,
    served_spa,
    temporary_browser_profile,
)
from tests.test_spa_rendered_semantics import (
    INDEX_STATE,
    NODE_PROBE,
    _served_fixture,
)

ROOT = Path(__file__).resolve().parents[1]
SHELL = authored_shell_source(ROOT)
SHARED = ROOT / "docs" / "ui" / "_shared.jsx"
CREW = ROOT / "docs" / "ui" / "crew.jsx"
HOME = ROOT / "docs" / "ui" / "home.jsx"
TOPBAR = ROOT / "docs" / "ui" / "topbar.css"


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


def _function_source(path: Path | AuthoredSource, name: str) -> str:
    source = path.read_text()
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


def _evaluate(
    functions: list[tuple[Path | AuthoredSource, str]],
    expression: str,
):
    script = "\n".join(_function_source(path, name) for path, name in functions)
    result = subprocess.run(
        ["node", "-e", f"{script}\nconsole.log(JSON.stringify({expression}));"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_topbar_is_one_row_with_four_within_project_tabs() -> None:
    source = _function_source(SHELL, "TopBar")
    shell = SHELL.read_text()
    css = TOPBAR.read_text()

    assert "display: flex" in css
    assert "flex-wrap: nowrap" in css
    assert "margin-left: auto" in css
    order = [
        'className="r-topbar-brand"',
        'className="r-topbar-search"',
        'className="r-tabs-artifact"',
        'className="r-tabs-work"',
        'className="r-live-receipt"',
        'className="r-project-manage"',
        "<SM",
    ]
    indices = [source.index(marker) for marker in order]
    assert indices == sorted(indices)
    assert ">Overview<" not in source
    assert source.count("{tab.label}") == 2
    assert [
        f'label: "{label}"' in shell
        for label in ("Plans", "Sprints", "Graph", "Crew")
    ] == [True] * 4


@pytest.mark.parametrize("width", [1280, 1440, 1920])
def test_topbar_keeps_one_ordered_row_with_tools_flush_right(
    tmp_path: Path,
    rendered_browser: str,
    width: int,
) -> None:
    browser = rendered_browser

    expression = """(() => {
      const header = document.querySelector('.r-topbar');
      const brand = header?.querySelector('.r-topbar-brand');
      const search = header?.querySelector('.r-topbar-search');
      const artifactTabs = header?.querySelector('.r-tabs-artifact');
      const workTabs = header?.querySelector('.r-tabs-work');
      const receipt = header?.querySelector('.r-live-receipt');
      const project = header?.querySelector('.r-project-manage');
      const tools = header?.querySelector('.top-r');
      const rect = element => element?.getBoundingClientRect();
      const boxes = [brand, search, artifactTabs, workTabs, receipt, project, tools].map(rect);
      const headerBox = rect(header);
      const paddingRight = parseFloat(getComputedStyle(header).paddingRight);
      return {
        centers: boxes.map(box => box.top + box.height / 2),
        lefts: boxes.map(box => box.left),
        toolsInset: headerBox.right - tools.getBoundingClientRect().right,
        paddingRight,
        headerReceipt: Boolean(header.querySelector('.r-snapshot-receipt')),
      };
    })()"""

    with served_spa(tmp_path, browser, route="#home") as spa:
        geometry = spa.run_probe(
            expression,
            viewport=(width, 900),
            ready_expression="Boolean(document.querySelector('.r-topbar .r-tabs-work'))",
        )

    assert max(geometry["centers"]) - min(geometry["centers"]) <= 1
    assert geometry["lefts"] == sorted(geometry["lefts"])
    assert abs(geometry["toolsInset"] - geometry["paddingRight"]) <= 1
    assert geometry["headerReceipt"] is False


def test_mounted_rows_include_projects_reporting_zero_plans() -> None:
    """Mount means registered; plans_count is a label, never a predicate."""
    projects = [
        {"project": "alpha", "plans_count": 0},
        {"project": "beta", "plans_count": 0},
        {"project": "gamma", "plans_count": 5},
    ]
    mounted = _evaluate(
        [(SHELL, "mountedProjectRows")],
        f"mountedProjectRows({json.dumps(projects)}).map(project => project.project)",
    )

    assert mounted == ["alpha", "beta", "gamma"]


def test_picker_renders_only_the_visible_set_with_no_hidden_suffix() -> None:
    projects = [
        {"project": "alpha", "plans_count": 4},
        {"project": "beta", "plans_count": 7},
        {"project": "gamma", "plans_count": 2},
        {"project": "delta", "plans_count": 1},
    ]
    hidden = ["beta", "delta"]
    shown = _evaluate(
        [
            (SHELL, "mountedProjectRows"),
            (SHELL, "effectiveHiddenProjects"),
            (SHELL, "visibleProjectRows"),
        ],
        f"visibleProjectRows({json.dumps(projects)}, {json.dumps(hidden)})"
        ".map(project => project.project)",
    )
    source = _function_source(SHELL, "TopBar")

    assert shown == ["alpha", "gamma"]
    assert "visibleProjects.map(project => (" in source
    assert "manageableProjects.map(project" in source
    assert "onClick={() => navProject(project.project)}" in source
    assert "project.plans_count" in source
    assert "project.live_count" in source
    dropdown = source[source.index("r-project-menu") : source.index("Configure visibility")]
    assert "is-hidden" not in dropdown
    assert ">hidden<" not in dropdown
    assert "Configure visibility…" in source


def test_first_visit_renders_live_runs_from_every_mounted_project(
    tmp_path: Path, rendered_browser: str
) -> None:
    projects = [
        {
            "project": f"project-{index}",
            "data": {
                "projects": [{"project": f"project-{index}", "plans_count": 1}],
                "inventory": [{"slug": "finished", "status": "done"}],
            },
        }
        for index in range(12)
    ]
    runs = [
        {
            "project": f"project-{index}",
            "run_id": f"run-{index}",
            "plan": "finished",
            "section": "delivery",
            "phase": "working",
        }
        for index in range(1, 9)
    ]
    fixture_source = (
        f"const fixtureProjects = {json.dumps({'projects': projects})};"
        f"const fixtureRuns = {json.dumps({'runs': runs})};"
        'localStorage.removeItem("reckon:hidden-projects");'
    )
    fetch_source = """
            if (url.endsWith("/_projects/index.json")) {
              return Promise.resolve(new Response(JSON.stringify(fixtureProjects), {
                status: 200,
                headers: { "Content-Type": "application/json" },
              }));
            }
            if (url.endsWith("/crew")) {
              return Promise.resolve(new Response(JSON.stringify(fixtureRuns), {
                status: 200,
                headers: { "Content-Type": "application/json" },
              }));
            }
"""
    probe_script = NODE_PROBE.replace(
        "const fixtureSprints = fixtureIndex.data.sprints;",
        "const fixtureSprints = fixtureIndex.data.sprints;" + fixture_source,
    ).replace(
        "            const url = String(resource);\n",
        "            const url = String(resource);\n" + fetch_source,
    )
    probe = """(() => ({
      cards: document.querySelectorAll('.r-crew-card').length,
      summary: document.querySelector('.r-crew-heading span')?.textContent.trim() || '',
    }))()"""

    with (
        temporary_browser_profile(tmp_path) as profile,
        _served_fixture(tmp_path, "#crew") as url,
    ):
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                probe_script,
                json.dumps(
                    {
                        "browser": rendered_browser,
                        "profile": str(profile),
                        "url": url,
                        "waitSelector": ".r-crew-card",
                        "probe": probe,
                        "prepareSignal": "undefined",
                        "removeSignal": "null",
                        "failPlanHtml": False,
                        "fixtureIndex": INDEX_STATE,
                    }
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)["baseline"]
    assert rendered["cards"] == len(runs)
    assert "12 shown / 12 mounted" in rendered["summary"]


def test_configure_shortcut_and_gear_both_open_the_visibility_sheet() -> None:
    topbar = _function_source(SHELL, "TopBar")
    settings = _function_source(SHARED, "SettingsMenu")

    assert "openVisibilitySheet" in topbar
    assert topbar.count("<VS") == 1
    assert "onOpenVisibility?.()" in settings
    assert settings.count("<ProjectVisibilitySheet") == 0
    assert "<ProjectVisibilityPanel" not in topbar
    assert "<ProjectVisibilityPanel" not in settings


def test_sheet_renders_one_row_per_registered_project_and_the_consequence_sentence() -> (
    None
):
    sheet = _function_source(SHARED, "ProjectVisibilitySheet")

    assert "rows.map(project =>" in sheet
    assert (
        "Hidden projects leave the picker, the crew feed and the fleet roll-up; "
        "registration is unaffected." in sheet
    )


def test_last_visible_project_is_locked_in_the_sheet_and_the_change_helper() -> None:
    sheet = _function_source(SHARED, "ProjectVisibilitySheet")

    assert 'locked = isVisible && visible.size === 1' in sheet
    assert 'disabled={locked}' in sheet
    assert '"locked"' in sheet

    projects = [
        {"project": "alpha", "plans_count": 4},
        {"project": "beta", "plans_count": 7},
    ]
    change = _evaluate(
        [
            (SHELL, "mountedProjectRows"),
            (SHELL, "effectiveHiddenProjects"),
            (SHELL, "visibleProjectRows"),
            (SHELL, "projectVisibilityChange"),
        ],
        f"projectVisibilityChange({json.dumps(projects)}, ['beta'], 'alpha', 'alpha')",
    )

    assert change["changed"] is False
    assert change["locked"] is True


def test_toggling_a_hidden_project_back_on_reports_changed() -> None:
    projects = [
        {"project": "alpha", "plans_count": 4},
        {"project": "beta", "plans_count": 7},
    ]
    change = _evaluate(
        [
            (SHELL, "mountedProjectRows"),
            (SHELL, "effectiveHiddenProjects"),
            (SHELL, "visibleProjectRows"),
            (SHELL, "projectVisibilityChange"),
        ],
        f"projectVisibilityChange({json.dumps(projects)}, ['beta'], 'alpha', 'beta')",
    )

    assert change["changed"] is True
    assert change["locked"] is False


def test_hidden_projects_leave_crew_and_home_aggregates() -> None:
    runs = [{"project": "alpha"}, {"project": "beta"}, {"project": "beta"}]
    crew_projects = _evaluate(
        [(CREW, "crewRunsForVisibleProjects")],
        f"crewRunsForVisibleProjects({json.dumps(runs)}, ['alpha']).map(run => run.project)",
    )
    home_projects = _evaluate(
        [(HOME, "homeProjectRows")],
        "homeProjectRows([{project:'alpha',plans_count:1}]).map(row => row.project)",
    )

    assert crew_projects == ["alpha"]
    assert home_projects == ["alpha"]


def test_hidden_project_remains_selectable_through_palette() -> None:
    projects = [
        {"project": "alpha", "state": {"inventory": []}},
        {
            "project": "beta",
            "state": {"inventory": [{"slug": "hidden-plan", "title": "Hidden plan"}]},
        },
    ]
    palette_projects = _evaluate(
        [(SHELL, "paletteItems")],
        f"paletteItems({{}}, {json.dumps(projects)}).map(item => item.repository)",
    )

    assert palette_projects == ["beta"]
    assert "paletteItems(M, projects)" in SHELL.read_text()


def test_fleet_counts_distinguish_shown_from_mounted() -> None:
    home = HOME.read_text()
    crew = CREW.read_text()
    shared = SHARED.read_text()

    assert "{projects.length} of {mountedProjectCount} shown" in home
    assert "shown / {mountedProjectCount || 0} mounted" in crew
    assert "shown · {(projects || []).length} mounted" in shared
