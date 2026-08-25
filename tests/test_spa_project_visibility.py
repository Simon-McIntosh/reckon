import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "docs" / "ui" / "shell.jsx"
SHARED = ROOT / "docs" / "ui" / "_shared.jsx"
CREW = ROOT / "docs" / "ui" / "crew.jsx"
TOPBAR = ROOT / "docs" / "ui" / "topbar.css"


def _function_source(path: Path, name: str) -> str:
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


def _evaluate(functions: list[tuple[Path, str]], expression: str):
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
    css = TOPBAR.read_text()

    assert "display: flex" in css
    assert "flex-wrap" not in css
    assert source.index('className="r-topbar-brand"') < source.index('className="r-project-manage"')
    assert source.index('className="r-project-manage"') < source.index('className="r-glyph-tabs"')
    assert source.index('className="r-glyph-tabs"') < source.index('className="r-cmdk-trigger"')
    assert '>Overview<' not in source
    assert [f"\n          {label}\n" in source for label in ("Plans", "Sprints", "Graph", "Crew")] == [True] * 4


def test_primary_project_control_lists_every_mounted_project_and_routes_hidden_entry() -> None:
    projects = [
        {"project": "alpha", "plans_count": 4},
        {"project": "beta", "plans_count": 7},
    ]
    mounted = _evaluate(
        [(SHELL, "manageableProjectRows")],
        f"manageableProjectRows({json.dumps(projects)}).map(project => project.project)",
    )
    shown = _evaluate(
        [
            (SHELL, "mountedProjectRows"),
            (SHELL, "projectHasOpenWork"),
            (SHELL, "effectiveHiddenProjects"),
            (SHELL, "visibleProjectRows"),
        ],
        f"visibleProjectRows({json.dumps(projects)}, ['beta']).map(project => project.project)",
    )
    source = _function_source(SHELL, "TopBar")

    assert mounted == ["alpha", "beta"]
    assert shown == ["alpha"]
    assert "manageableProjects.map(project" in source
    assert "{visibleProjects.map(project => (" not in source
    assert "onClick={() => navProject(project.project)}" in source
    assert "visibleProjectNames.has(project.project)" in source
    assert "project.plans_count" in source
    assert "project.live_count" in source
    assert "Configure visibility…" in source


def test_configure_shortcut_opens_the_settings_visibility_panel() -> None:
    topbar = _function_source(SHELL, "TopBar")
    settings = _function_source(SHARED, "SettingsMenu")
    shared = SHARED.read_text()

    assert 'setRequestedSettingsPanel("visibility")' in topbar
    assert 'requestedPanel !== "visibility"' in settings
    assert settings.count("<ProjectVisibilityPanel") == 1
    assert topbar.count("<ProjectVisibilityPanel") == 0
    assert shared.count('className="settings-project-visibility"') == 1


def test_hidden_projects_leave_crew_and_overview_aggregates() -> None:
    runs = [{"project": "alpha"}, {"project": "beta"}, {"project": "beta"}]
    crew_projects = _evaluate(
        [(CREW, "crewRunsForVisibleProjects")],
        f"crewRunsForVisibleProjects({json.dumps(runs)}, ['alpha']).map(run => run.project)",
    )
    overview_projects = _evaluate(
        [
            (SHELL, "blockerIsUnresolved"),
            (SHELL, "projectActiveSprints"),
            (SHELL, "overviewProjectRows"),
        ],
        "overviewProjectRows([{project:'alpha',state:{} }], {}, []).map(row => row.project)",
    )

    assert crew_projects == ["alpha"]
    assert overview_projects == ["alpha"]


def test_hidden_project_remains_selectable_through_palette() -> None:
    projects = [
        {"project": "alpha", "state": {"inventory": []}},
        {"project": "beta", "state": {"inventory": [{"slug": "hidden-plan", "title": "Hidden plan"}]}},
    ]
    palette_projects = _evaluate(
        [(SHELL, "paletteItems")],
        f"paletteItems({{}}, {json.dumps(projects)}).map(item => item.repository)",
    )

    assert palette_projects == ["beta"]
    assert "paletteItems(M, projects)" in SHELL.read_text()


def test_fleet_counts_distinguish_shown_from_mounted() -> None:
    shell = SHELL.read_text()
    crew = CREW.read_text()
    shared = SHARED.read_text()

    assert "shown / ${mountedProjectCount} mounted" in shell
    assert "shown / {mountedProjectCount || 0} mounted" in crew
    assert "shown · {(projects || []).length} mounted" in shared
