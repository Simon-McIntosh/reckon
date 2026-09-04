from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tomllib
import zipfile
from contextlib import contextmanager
from http.client import HTTPConnection
from importlib.metadata import version
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import __version__, cli, pages, serve
from tests.spa_browser_harness import (
    BrowserProbeError,
    _evaluate_browser_url,
    _served_document,
    installed_browser,
    run_browser_probe,
    served_spa,
)

REPO_ROOT = Path(__file__).parents[1]
SPA_COMPILED_MODULES = (
    "glyphs",
    "_shared",
    "ui",
    "bits",
    "decision",
    "plan",
    "sprint",
    "graph",
    "crew",
    "shell",
)


def test_cli_version_matches_installed_distribution():
    expected = version("reckon-plans")

    result = CliRunner().invoke(cli.main, ["--version"])

    assert expected != "dev"
    assert __version__ == expected
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"reckon, version {expected}"


def _write_plan(
    docs_dir: Path,
    *,
    project: str,
    slug: str,
    sprint: str,
    north_star: str | None = None,
) -> None:
    direction_meta = (
        f'<meta name="plan-north-star" content="{north_star}">' if north_star else ""
    )
    (docs_dir / f"{slug}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug.title()}">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-sprint" content="{sprint}">'
        f"{direction_meta}"
        '<meta name="plan-impl" content="0.5">'
        f"<title>{slug.title()}</title>"
        "</head><body><main></main></body></html>"
    )


def _write_discovery_pages(docs_dir: Path) -> None:
    sprint_dir = docs_dir / "sprints"
    sprint_dir.mkdir()
    (sprint_dir / "delivery.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="sprint-id" content="S8">'
        '<meta name="sprint-theme" content="Static delivery">'
        '<meta name="sprint-status" content="active">'
        '<meta name="sprint-starts" content="2026-07-01">'
        '<meta name="sprint-ends" content="2026-07-31">'
        "</head><body></body></html>"
    )
    milestone_dir = docs_dir / "milestones"
    milestone_dir.mkdir()
    (milestone_dir / "portable.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="milestone-id" content="M8">'
        '<meta name="milestone-name" content="Portable build">'
        '<meta name="milestone-status" content="active">'
        '<meta name="milestone-pct" content="60">'
        "</head><body></body></html>"
    )


def _seed_project_index(docs_dir: Path, project: str) -> Path:
    state_dir = docs_dir / "state" / project
    state_dir.mkdir(parents=True)
    index_path = state_dir / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "updated": "2026-07-01T00:00:00",
                "project": project,
                "doc": "index",
                "data": {
                    "_version": 4,
                    "active_sprint_id": "S7",
                    "sprints": [
                        {
                            "id": "S7",
                            "theme": "Authored sprint",
                            "status": "active",
                            "items": [{"slug": "kept-item"}],
                        }
                    ],
                    "milestones": [
                        {
                            "id": "M7",
                            "name": "Authored milestone",
                            "status": "active",
                            "evidence": ["kept-evidence"],
                        }
                    ],
                    "timeline": [{"date": "2026-07-01", "label": "Keep this"}],
                    "blockers": [{"id": "network", "owner": "ops"}],
                },
            },
            indent=2,
        )
        + "\n"
    )
    return index_path


def _invoke_build(docs_dir: Path, project: str = "fixture"):
    return CliRunner().invoke(cli.main, ["build", str(docs_dir), "--project", project])


def _loaded_spa_modules(html: str) -> tuple[str, ...]:
    return tuple(
        reference.lstrip("/")
        for reference in re.findall(r'<script[^>]+src="([^"]+\.(?:js|jsx))"', html)
        if reference.lstrip("/").startswith("_ui/")
    )


def _authored_spa_source(module: str) -> Path:
    relative = Path(*Path(module).parts[1:])
    source = REPO_ROOT / "docs" / "ui" / relative
    if source.is_file():
        return source
    jsx_source = source.with_suffix(".jsx")
    assert jsx_source.is_file(), f"missing authored source for {module}"
    return jsx_source


def _version_describe_command(repository: Path) -> list[str]:
    configuration = tomllib.loads((repository / "pyproject.toml").read_text())
    raw_options = configuration["tool"]["hatch"]["version"]["raw-options"]
    return shlex.split(raw_options["git_describe_command"])


def _clone_with_current_version_configuration(source: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(source), str(destination)],
        check=True,
        text=True,
        capture_output=True,
    )
    shutil.copy2(source / "pyproject.toml", destination / "pyproject.toml")


def _build_wheel(repository: Path, destination: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(destination)],
        cwd=repository,
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
    )


def _wheel_version(wheel: Path) -> str:
    match = re.fullmatch(r"reckon_plans-(.+)-py3-none-any\.whl", wheel.name)
    assert match is not None
    return match.group(1)


def _assert_spa_module_lists_match(
    loaded: dict[str, tuple[str, ...]], expected: tuple[str, ...]
) -> None:
    assert loaded == dict.fromkeys(loaded, expected)


def _loaded_stylesheets(html: str) -> tuple[str, ...]:
    return tuple(
        reference.lstrip("/")
        for reference in re.findall(r'<link[^>]+href="([^"]+\.css)"', html)
        if not reference.startswith(("https://", "http://"))
    )


@contextmanager
def _skip_when_browser_is_unavailable():
    try:
        yield
    except BrowserProbeError as error:
        pytest.skip(f"browser unavailable ({error.classification}): {error}")


@pytest.fixture(scope="module")
def rendered_browser(tmp_path_factory):
    browser = installed_browser()
    if browser is None:
        pytest.skip("no supported browser binary is installed")

    with _skip_when_browser_is_unavailable():
        run_browser_probe(
            tmp_path_factory.mktemp("browser-capability"),
            browser,
            "<!doctype html><html><body>ready</body></html>",
            "document.body.textContent",
        )
    return browser


def _rendered_north_star_state(
    tmp_path: Path,
    docs_dir: Path,
    browser: str,
) -> dict:

    with served_spa(
        tmp_path,
        browser,
        docs=docs_dir,
        project="fixture",
        route="#plan/alpha",
    ) as spa:
        return spa.run_probe(
            """
          (() => {
            const badge = document.querySelector('.r-north-star-badge');
            return {
              badgeName: badge?.querySelector('.v')?.textContent.trim() || null,
              badgeStatement: badge?.title || null,
            };
          })()
            """,
            viewport=(1374, 900),
            ready_expression="Boolean(document.querySelector('.r-list-body'))",
        )


def _extract_component(source: str, start: str, end: str) -> str:
    start_anchor = source.index(start)
    end_anchor = source.index(end, start_anchor)
    return source[start_anchor:end_anchor]


@pytest.fixture()
def built_source_site(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_plan(docs_dir, project="fixture", slug="alpha", sprint="S8")
    _write_discovery_pages(docs_dir)
    index_path = _seed_project_index(docs_dir, "fixture")

    result = _invoke_build(docs_dir)
    assert result.exit_code == 0, result.output
    return docs_dir, index_path, result


def test_build_copies_every_canonical_asset(built_source_site):
    docs_dir, _, _ = built_source_site
    expected_ui = {path.name for path in (REPO_ROOT / "docs" / "ui").iterdir()}
    compiled_ui = {
        f"{path.stem}.js" for path in (REPO_ROOT / "docs" / "ui").glob("*.jsx")
    }
    expected_shared = {path.name for path in (REPO_ROOT / "docs" / "_shared").iterdir()}

    assert {path.name for path in (docs_dir / "_ui").iterdir()} == (
        expected_ui | compiled_ui
    )
    assert {path.name for path in (docs_dir / "_shared").iterdir()} == expected_shared
    assert {path.name for path in (docs_dir / "_runtime").iterdir()} == {
        "react.js",
        "react-dom.js",
    }
    assert (docs_dir / "_shared" / "state.js").read_bytes() == (
        REPO_ROOT / "docs" / "_shared" / "state.js"
    ).read_bytes()


def test_build_writes_relative_index_and_nojekyll(built_source_site):
    docs_dir, _, _ = built_source_site
    index_html = (docs_dir / "index.html").read_text()
    local_references = [
        value
        for value in re.findall(r'(?:href|src)="([^"]+)"', index_html)
        if not value.startswith(("https://", "http://"))
    ]

    assert local_references
    assert all(not value.startswith("/") for value in local_references)
    assert "_runtime/react.js" in local_references
    assert "_runtime/react-dom.js" in local_references
    assert "_ui/glyphs.js" in local_references
    assert "_ui/_shared.js" in local_references
    assert "_ui/prompts.js" in local_references
    assert "_ui/crew.js" in local_references
    assert (docs_dir / ".nojekyll").is_file()


def test_spa_entry_points_load_exactly_the_canonical_modules(
    built_source_site, tmp_path
):
    docs_dir, _, _ = built_source_site
    synced_docs = tmp_path / "synced" / "docs"
    synced_docs.mkdir(parents=True)
    sync_result = CliRunner().invoke(
        cli.main,
        [
            "sync",
            str(synced_docs),
            "--project",
            "synced",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "state"),
        ],
    )
    assert sync_result.exit_code == 0, sync_result.output

    entry_points = {
        "checked-in": (REPO_ROOT / "docs" / "index.html").read_text(),
        "served": serve._render_spa_html("fixture"),
        "synced": (synced_docs / "index.html").read_text(),
        "built": (docs_dir / "index.html").read_text(),
    }
    loaded = {name: _loaded_spa_modules(html) for name, html in entry_points.items()}
    expected_modules = loaded["checked-in"]

    assert len(entry_points) == 4
    _assert_spa_module_lists_match(loaded, expected_modules)
    mutated = {**loaded, "served": loaded["served"][:-1]}
    with pytest.raises(AssertionError):
        _assert_spa_module_lists_match(mutated, expected_modules)
    retired_modules = ("cockpit", "shell-overview", "shell-prompt")
    assert all(
        all(f"_ui/{name}.js" not in modules for name in retired_modules)
        for modules in loaded.values()
    )
    assert all(
        not (REPO_ROOT / "docs" / "ui" / f"{name}.jsx").exists()
        for name in retired_modules
    )
    assert all(
        not (docs_dir / "_ui" / f"{name}.jsx").exists() for name in retired_modules
    )
    assert all((docs_dir / module).is_file() for module in expected_modules)
    authored_sources = "\n".join(
        _authored_spa_source(module).read_text() for module in expected_modules
    )
    assert authored_sources.count("function CockpitBody(") == 0
    assert authored_sources.count("function FleetHome(") == 1
    assert not (REPO_ROOT / "docs" / "home.html").exists()
    assert not (docs_dir / "home.html").exists()
    assert "ReckonShell.prompt.FleetPrompt" not in authored_sources
    route_source = (REPO_ROOT / "docs" / "ui" / "shell-route.jsx").read_text()
    assert 'if (h === "cockpit") return { view: "home" };' in route_source


def test_server_root_redirects_to_the_first_mounted_project_home(tmp_path, monkeypatch):
    first = tmp_path / "first" / "docs"
    second = tmp_path / "second" / "docs"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    mounts = tmp_path / "mounts.json"
    mounts.write_text(
        json.dumps({"first-project": str(first), "second-project": str(second)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts)
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        assert response.status == 302
        assert response.getheader("Location") == "/first-project/#home"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_spa_entry_points_use_local_runtime_without_browser_transforms(
    built_source_site, tmp_path
):
    docs_dir, _, _ = built_source_site
    synced_docs = tmp_path / "synced-runtime" / "docs"
    synced_docs.mkdir(parents=True)
    sync_result = CliRunner().invoke(
        cli.main,
        [
            "sync",
            str(synced_docs),
            "--project",
            "synced-runtime",
            "--mounts",
            str(tmp_path / "mounts-runtime.json"),
            "--state-root",
            str(tmp_path / "state-runtime"),
        ],
    )
    assert sync_result.exit_code == 0, sync_result.output

    entry_points = {
        "checked-in": (REPO_ROOT / "docs" / "index.html").read_text(),
        "served": serve._render_spa_html("fixture"),
        "synced": (synced_docs / "index.html").read_text(),
        "built": (docs_dir / "index.html").read_text(),
    }
    for name, html in entry_points.items():
        assert "text/babel" not in html, name
        assert "@babel/standalone" not in html, name
        assert "unpkg.com" not in html, name
        assert "fonts.googleapis.com" not in html, name
        assert "fonts.gstatic.com" not in html, name
        assert "react.js" in html, name
        assert "react-dom.js" in html, name
        assert not re.search(r'<script[^>]+src="[^"]+\.jsx"', html), name


def test_compiled_spa_modules_isolate_scope_and_preserve_window_exports(
    built_source_site, tmp_path
):
    docs_dir, _, _ = built_source_site
    compiled_dir = docs_dir / "_ui"
    compiled_sources = {
        path.stem: (compiled_dir / f"{path.stem}.js").read_text()
        for path in (REPO_ROOT / "docs" / "ui").glob("*.jsx")
    }
    assert set(SPA_COMPILED_MODULES) <= set(compiled_sources)
    for source in compiled_sources.values():
        assert source.startswith("(function () {\n")
        assert "\n}).call(window);\n//# sourceURL=" in source

    exports = {
        "glyphs": ("GLYPHS", "ACCENTS"),
        "_shared": (
            "ProjectPicker",
            "ProjectVisibilitySheet",
            "SettingsMenu",
            "Sparkline",
            "Chip",
            "ProjectCard",
        ),
        "ui": (
            "Status",
            "Roi",
            "Bar",
            "Stack",
            "Heat",
            "Spark",
            "Tag",
            "Who",
            "Icon",
            "Persist",
            "flashSaved",
        ),
        "bits": (
            "reckon",
            "planUtils",
            "planSave",
            "planLoad",
            "withHandoffProvenance",
            "PromptModal",
            "CommentPopover",
            "CommentReviewPopover",
            "useSelectionToComment",
            "SectionComments",
        ),
        "decision": ("Decision", "DecisionRow"),
        "plan": ("Plan", "GenericBody"),
        "sprint": ("Sprint", "SprintView"),
        "graph": (
            "GraphView",
            "DependencyChainView",
            "CriticalPathView",
            "PathPromptModal",
            "RadialFan",
        ),
        "crew": ("CrewView",),
        "shell": (),
    }
    prelude = """
globalThis.window = globalThis;
window.location = {
  pathname: "/fixture/",
  hostname: "127.0.0.1",
  origin: "http://127.0.0.1",
  hash: "#plans",
};
globalThis.location = window.location;
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.document = {
  querySelector() { return null; },
  getElementById() { return {}; },
  createElement() { return { dataset: {}, style: {}, appendChild() {} }; },
  body: { appendChild() {} },
  addEventListener() {},
  removeEventListener() {},
};
const noop = () => {};
globalThis.React = {
  createElement(type, props, ...children) { return { type, props, children }; },
  Fragment: Symbol("Fragment"),
  useState(value) { return [typeof value === "function" ? value() : value, noop]; },
  useEffect: noop,
  useLayoutEffect: noop,
  useMemo(fn) { return fn(); },
  useRef(value) { return { current: value }; },
  useCallback(fn) { return fn; },
};
globalThis.ReactDOM = { createRoot() { return { render: noop }; } };
globalThis.navigator = { clipboard: null };
globalThis.alert = noop;
function assertExports(moduleName, names) {
  const missing = names.filter(name => !(name in window));
  if (missing.length) throw new Error(`${moduleName} missing exports: ${missing.join(", ")}`);
}
"""
    bundle = [prelude]
    for module in SPA_COMPILED_MODULES:
        bundle.append(compiled_sources[module])
        bundle.append(
            f"assertExports({json.dumps(module)}, {json.dumps(exports[module])});\n"
        )
    expected_export_count = sum(len(names) for names in exports.values())
    bundle.append(
        "process.stdout.write(JSON.stringify("
        f"{{ modules: {len(SPA_COMPILED_MODULES)}, exports: {expected_export_count} }}"
        "));\n"
    )
    bundle_path = tmp_path / "compiled-spa.js"
    bundle_path.write_text("\n".join(bundle))

    parsed = subprocess.run(
        ["node", "--check", str(bundle_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stderr
    executed = subprocess.run(
        ["node", str(bundle_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert executed.returncode == 0, executed.stderr
    assert json.loads(executed.stdout) == {
        "modules": len(SPA_COMPILED_MODULES),
        "exports": expected_export_count,
    }


def test_served_and_static_pages_request_only_their_own_origin(
    tmp_path, built_source_site, rendered_browser
):
    browser = rendered_browser
    docs_dir, _, _ = built_source_site
    expression = """
      (() => ({
        pageOrigin: location.origin,
        resourceOrigins: [...new Set(
          performance.getEntriesByType('resource').map(entry => new URL(entry.name).origin)
        )],
      }))()
    """

    with served_spa(
        tmp_path,
        browser,
        docs=docs_dir,
        project="fixture",
        route="#plan/alpha",
    ) as spa:
        served = spa.run_probe(
            expression,
            ready_expression="Boolean(document.querySelector('.r-list-body'))",
        )

    with _served_document(tmp_path, docs_dir / "index.html") as page_url:
        static = _evaluate_browser_url(
            tmp_path,
            browser,
            f"{page_url}#plan/alpha",
            expression,
            viewport=(1374, 900),
            ready_expression="Boolean(document.querySelector('.r-list-body'))",
        )

    for result in (served, static):
        assert result["resourceOrigins"] == [result["pageOrigin"]]


def test_served_component_edit_is_visible_on_the_next_load(
    tmp_path, monkeypatch, built_source_site, rendered_browser
):
    browser = rendered_browser
    docs_dir, _, _ = built_source_site
    ui_root = tmp_path / "editable-ui"
    shutil.copytree(REPO_ROOT / "docs" / "ui", ui_root)
    monkeypatch.setenv("RECKON_UI_ROOT", str(ui_root))
    shell = ui_root / "shell.jsx"

    with served_spa(
        tmp_path,
        browser,
        docs=docs_dir,
        project="fixture",
        route="#plan/alpha",
    ) as spa:
        before = spa.run_probe(
            "document.querySelector('.r-topbar-brand span')?.textContent",
            ready_expression="Boolean(document.querySelector('.r-topbar-brand span'))",
        )
        shell.write_text(
            shell.read_text().replace("<span>reckon</span>", "<span>edited live</span>")
        )
        after = spa.run_probe(
            "document.querySelector('.r-topbar-brand span')?.textContent",
            ready_expression="Boolean(document.querySelector('.r-topbar-brand span'))",
        )

    assert before == "reckon"
    assert after == "edited live"


def test_spa_entry_points_load_the_same_surface_stylesheets(
    built_source_site, tmp_path
):
    docs_dir, _, _ = built_source_site
    synced_docs = tmp_path / "synced" / "docs"
    synced_docs.mkdir(parents=True)
    sync_result = CliRunner().invoke(
        cli.main,
        [
            "sync",
            str(synced_docs),
            "--project",
            "synced",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "state"),
        ],
    )
    assert sync_result.exit_code == 0, sync_result.output

    expected_surface_files = (
        "topbar.css",
        "plans.css",
        "reader.css",
        "home.css",
        "sprints.css",
        "crew.css",
        "graph.css",
    )
    expected_surface_assets = {f"_ui/{name}" for name in expected_surface_files}
    entry_points = {
        "checked-in": (REPO_ROOT / "docs" / "index.html").read_text(),
        "served": serve._render_spa_html("fixture"),
        "synced": (synced_docs / "index.html").read_text(),
        "built": (docs_dir / "index.html").read_text(),
    }
    loaded = {name: _loaded_stylesheets(html) for name, html in entry_points.items()}

    assert all(assets == loaded["checked-in"] for assets in loaded.values())
    for name in expected_surface_files:
        asset = f"_ui/{name}"
        assert all(asset in references for references in loaded.values())
        assert (docs_dir / "_ui" / name).is_file()
        assert (REPO_ROOT / "docs" / "ui" / name).is_file()


def test_plan_and_sprint_views_surface_only_matching_live_work():
    plan = (REPO_ROOT / "docs" / "ui" / "plan.jsx").read_text()
    sprint = (REPO_ROOT / "docs" / "ui" / "sprint.jsx").read_text()

    band = _extract_component(
        plan,
        "function PlanInFlightBand",
        "function ReaderSourceFailure",
    )
    assert 'className="r-inflight-band"' in band
    assert 'aria-label="Work in flight"' in band
    assert 'run.member || "unassigned"' in band
    assert 'run.section || "whole plan"' in band
    assert "Copy run command" in band
    assert "reckon crew observe --run ${run.run_id}" in band

    # Exercise the sprint's live-work projection rather than pinning its wording.
    live_work_projection = _extract_component(
        sprint,
        "function completedRunTime",
        "function Sprint(",
    )
    script = (
        "const HORIZON_HOURS = 48;\n"
        "const HOUR_MS = 60 * 60 * 1000;\n"
        + live_work_projection
        + "\n"
        + """
const sprint = {
  items: [{ slug: "focus" }, { slug: "also-focus" }],
};
const runs = [{
  run_id: "matching-run",
  plan: "focus",
  node: "matching-work",
  dispatched_at: "2026-09-01T10:00:00Z",
}, {
  run_id: "unrelated-run",
  plan: "elsewhere",
  node: "unrelated-work",
  dispatched_at: "2026-09-01T10:30:00Z",
}];
const strip = sprintActivityStrip(sprint, "2026-09-01T12:00:00Z", [], runs);
console.log(JSON.stringify({
  liveEvents: strip.events.map(event => ({
    kind: event.kind,
    runId: event.run.run_id,
  })),
}));
"""
    )
    rendered = subprocess.run(
        ["node", "-e", script],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(rendered.stdout) == {
        "liveEvents": [{"kind": "live", "runId": "matching-run"}],
    }


def test_build_bakes_discovery_and_preserves_authored_project_state(
    built_source_site,
):
    _, index_path, _ = built_source_site
    data = json.loads(index_path.read_text())["data"]

    assert "alpha" in {item["slug"] for item in data["inventory"]}
    assert {item["id"] for item in data["sprints"]} == {"S7", "S8"}
    # Markerless typed project-state destinations are migration staging, not
    # canonical state. The authored legacy index remains the only source.
    assert {item["id"] for item in data["milestones"]} == {"M7"}
    assert data["sprints"][0]["items"] == [{"slug": "kept-item"}]
    assert data["milestones"][0]["evidence"] == ["kept-evidence"]
    assert data["timeline"] == [{"date": "2026-07-01", "label": "Keep this"}]
    assert data["blockers"] == [{"id": "network", "owner": "ops"}]
    assert data["active_sprint_id"] == "S7"
    assert data["_version"] == 5


def test_build_carries_directions_into_the_project_surfaces(
    tmp_path,
    rendered_browser,
):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    directions = [
        {
            "id": "reliable-delivery",
            "name": "Reliable delivery",
            "statement": "Every release remains reproducible and observable.",
        },
        {
            "id": "clear-work",
            "name": "Clear work",
            "statement": "Every active plan states what it advances.",
        },
    ]
    _write_plan(
        docs_dir,
        project="fixture",
        slug="alpha",
        sprint="S8",
        north_star="reliable-delivery",
    )
    _write_discovery_pages(docs_dir)
    index_path = _seed_project_index(docs_dir, "fixture")
    envelope = json.loads(index_path.read_text())
    envelope["data"]["north_stars"] = directions
    index_path.write_text(json.dumps(envelope, indent=2) + "\n")

    result = _invoke_build(docs_dir)

    assert result.exit_code == 0, result.output
    data = json.loads(index_path.read_text())["data"]
    alpha = next(item for item in data["inventory"] if item["slug"] == "alpha")
    rendered = _rendered_north_star_state(tmp_path, docs_dir, rendered_browser)
    assert data["north_stars"] == directions
    assert alpha["north_star"] == "reliable-delivery"
    assert rendered == {
        "badgeName": "Reliable delivery",
        "badgeStatement": "Every release remains reproducible and observable.",
    }


def test_build_without_directions_preserves_the_unlabelled_shape(
    tmp_path, built_source_site, rendered_browser
):
    docs_dir, index_path, _ = built_source_site
    data = json.loads(index_path.read_text())["data"]
    alpha = next(item for item in data["inventory"] if item["slug"] == "alpha")
    rendered = _rendered_north_star_state(tmp_path, docs_dir, rendered_browser)

    assert "north_stars" not in data
    assert "north_star" not in alpha
    assert rendered == {
        "badgeName": None,
        "badgeStatement": None,
    }


def test_available_browser_does_not_mask_rendered_assertion_failure():
    with (
        pytest.raises(AssertionError, match="rendered assertion is wrong"),
        _skip_when_browser_is_unavailable(),
    ):
        raise AssertionError("rendered assertion is wrong")


@pytest.mark.parametrize("destination", ["_ui", "_shared"])
def test_build_rejects_non_directory_asset_destination(tmp_path, destination):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / destination).write_text("not a directory")

    result = _invoke_build(docs_dir)

    assert result.exit_code == 1
    assert f"{destination} exists but is not a directory" in result.output


def test_build_fails_loudly_when_packaged_index_is_missing(tmp_path, monkeypatch):
    fake_package = tmp_path / "installed" / "reckon"
    fake_package.mkdir(parents=True)
    packaged_assets = fake_package / "_assets"
    shutil.copytree(REPO_ROOT / "docs/ui", packaged_assets / "ui")
    shutil.copytree(REPO_ROOT / "docs/_shared", packaged_assets / "_shared")
    monkeypatch.setattr(cli, "__file__", str(fake_package / "cli.py"))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    result = _invoke_build(docs_dir)

    assert result.exit_code == 1
    assert "frontend assets are missing or incomplete" in result.output


def test_sync_generates_pinned_uv_workflow(tmp_path, monkeypatch):
    repo_dir = tmp_path / "consumer"
    docs_dir = repo_dir / "docs"
    docs_dir.mkdir(parents=True)
    monkeypatch.chdir(repo_dir)
    monkeypatch.setattr(
        pages,
        "detect_publication_strategy",
        lambda _docs_dir: pages.select_publication_strategy(None),
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "sync",
            "docs",
            "--project",
            "consumer",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "state"),
            "--generate-ci",
        ],
    )

    assert result.exit_code == 0, result.output
    workflow = (repo_dir / ".github" / "workflows" / "reckon-pages.yml").read_text()
    assert "astral-sh/setup-uv@v6" in workflow
    assert (
        'uvx --from "git+https://github.com/Simon-McIntosh/reckon@v0.2.0rc25"'
        in workflow
    )
    assert "reckon build docs" in workflow
    assert "pip install" not in workflow


def test_repository_workflow_is_uv_native():
    workflow = (REPO_ROOT / ".github" / "workflows" / "reckon-pages.yml").read_text()

    assert "astral-sh/setup-uv@v6" in workflow
    assert "uv run --frozen reckon build docs" in workflow
    assert "pip install" not in workflow


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory):
    wheel_dir = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(wheel_dir.glob("reckon_plans-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_wheel_contains_canonical_frontend_assets(built_wheel):
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())

    expected_ui = {
        f"reckon/_assets/ui/{path.name}"
        for path in (REPO_ROOT / "docs" / "ui").iterdir()
        if path.is_file()
    }
    expected_shared = {
        f"reckon/_assets/_shared/{path.name}"
        for path in (REPO_ROOT / "docs" / "_shared").iterdir()
        if path.is_file()
    }
    assert expected_ui <= names
    assert expected_shared <= names
    assert "reckon/_assets/index.html" in names
    assert "reckon/_assets/_shared/state.js" in names


def test_wheel_version_ignores_non_release_tag(tmp_path, built_wheel):
    repository = tmp_path / "repository"
    _clone_with_current_version_configuration(REPO_ROOT, repository)
    subprocess.run(
        ["git", "tag", "rescue/build-recovery-deadbeef9"],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    )

    describe = subprocess.run(
        _version_describe_command(repository),
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    )
    selected_tag = describe.stdout.split("-", 1)[0]
    wheel_dir = tmp_path / "wheel"
    result = _build_wheel(repository, wheel_dir)

    assert re.fullmatch(r"v\d+\.\d+\.\d+(?:rc\d+)?", selected_tag)
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(wheel_dir.glob("reckon_plans-*.whl"))
    assert len(wheels) == 1
    assert _wheel_version(wheels[0]) == _wheel_version(built_wheel)


def test_release_tag_match_reports_when_no_release_tags_exist(tmp_path):
    repository = tmp_path / "repository"
    _clone_with_current_version_configuration(REPO_ROOT, repository)
    release_tags = subprocess.run(
        ["git", "tag", "--list", "v*"],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    assert release_tags
    subprocess.run(
        ["git", "tag", "--delete", *release_tags],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    )

    describe_command = _version_describe_command(repository)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        subprocess.run(
            describe_command,
            cwd=repository,
            check=True,
            text=True,
            capture_output=True,
        )

    message = str(raised.value)
    assert "v[0-9]*" in message
    assert "rescue/spec-level-names-a-backend-957c042" not in message


@pytest.fixture(scope="session")
def installed_static_site(tmp_path_factory, built_wheel):
    root = tmp_path_factory.mktemp("installed-build")
    site_packages = root / "site-packages"
    install = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            str(site_packages),
            str(built_wheel),
        ],
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    docs_dir = root / "docs"
    docs_dir.mkdir()
    _write_plan(docs_dir, project="installed", slug="portable", sprint="S8")
    _write_discovery_pages(docs_dir)
    index_path = _seed_project_index(docs_dir, "installed")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site_packages)
    env["PYTHONNOUSERSITE"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from reckon.cli import main; main()",
            "build",
            str(docs_dir),
            "--project",
            "installed",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    version_result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from reckon.cli import main; main()",
            "--version",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    metadata_result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlib.metadata import version; print(version('reckon-plans'))",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert version_result.returncode == 0, version_result.stdout + version_result.stderr
    assert metadata_result.returncode == 0, (
        metadata_result.stdout + metadata_result.stderr
    )
    return docs_dir, index_path, result, version_result, metadata_result


def test_installed_wheel_build_emits_complete_static_site(installed_static_site):
    docs_dir, _, result, version_result, metadata_result = installed_static_site

    source_assets = list((REPO_ROOT / "docs" / "ui").iterdir())
    assert len(list((docs_dir / "_ui").iterdir())) == len(source_assets) + sum(
        path.suffix == ".jsx" for path in source_assets
    )
    assert len(list((docs_dir / "_shared").iterdir())) == len(
        list((REPO_ROOT / "docs" / "_shared").iterdir())
    )
    assert (docs_dir / "_shared" / "state.js").is_file()
    assert (docs_dir / ".nojekyll").is_file()
    assert _loaded_spa_modules((docs_dir / "index.html").read_text()) == (
        _loaded_spa_modules((REPO_ROOT / "docs/index.html").read_text())
    )
    assert "Build complete" in result.stdout
    installed_version = metadata_result.stdout.strip()
    assert installed_version != "dev"
    assert version_result.stdout.strip() == f"reckon, version {installed_version}"


def test_installed_wheel_build_emits_usable_state(installed_static_site):
    docs_dir, index_path, _, _, _ = installed_static_site
    index_html = (docs_dir / "index.html").read_text()
    state = json.loads(index_path.read_text())["data"]

    assert 'src="_ui/shell.js"' in index_html
    assert 'href="_shared/foundation.css"' in index_html
    assert "portable" in {item["slug"] for item in state["inventory"]}
    assert {item["id"] for item in state["sprints"]} == {"S7", "S8"}
    assert state["timeline"]
    assert state["blockers"]
