import json
import re
import subprocess
from pathlib import Path

from tests.spa_browser_harness import authored_shell_source

ROOT = Path(__file__).resolve().parents[1]
SHELL = authored_shell_source(ROOT)
TOPBAR_CSS = ROOT / "docs" / "ui" / "topbar.css"


def _run_shell_helpers(expression: str) -> object:
    source = SHELL.read_text()
    helpers = source[
        source.index("const PROJECT_VISIBILITY_STORAGE =") : source.index(
            "function TopBar("
        )
    ]
    script = f"""
{helpers}
console.log(JSON.stringify({expression}));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def _projects(*names: str) -> list[dict]:
    return [{"project": name, "plans_count": 1} for name in names]


def test_hiding_focused_project_moves_focus_to_a_survivor() -> None:
    change = _run_shell_helpers(
        "projectVisibilityChange("
        f'{json.dumps(_projects("alpha", "beta", "gamma"))}, [], "beta", "beta")'
    )

    assert change == {
        "changed": True,
        "locked": False,
        "hidden": ["beta"],
        "focus": "alpha",
    }


def test_last_visible_project_refuses_to_hide() -> None:
    change = _run_shell_helpers(
        "projectVisibilityChange("
        f'{json.dumps(_projects("alpha", "beta"))}, ["beta"], "alpha", "alpha")'
    )

    assert change == {
        "changed": False,
        "locked": True,
        "hidden": ["beta"],
        "focus": "alpha",
    }
    assert _run_shell_helpers(
        "visibleProjectRows("
        f'{json.dumps(_projects("alpha", "beta"))}, ["alpha", "beta"])'
        ".map(row => row.project)"
    ) == ["alpha"]


def test_project_registered_with_zero_plans_is_still_mounted_and_visible() -> None:
    """Mount means registered; plans_count is a label, never a predicate."""
    visible = _run_shell_helpers(
        "visibleProjectRows("
        '[{"project":"empty","plans_count":0},'
        '{"project":"ready","plans_count":2}], []).map(row => row.project)'
    )

    assert visible == ["empty", "ready"]


def test_snapshot_receipt_counts_payload_resource_versions() -> None:
    payload = {
        "source_format": "distributed",
        "resource_versions": {f"resource:{index}": index for index in range(12)},
        "loaded_at": "2000-01-01T06:00:00+00:00",
    }
    receipt = _run_shell_helpers(f"snapshotReceipt({json.dumps(payload)})")

    assert receipt["sourceFormat"] == "distributed"
    assert receipt["resourceCount"] == 12
    assert receipt["loadedAt"] != "unknown time"


def test_topbar_is_data_driven_and_owns_one_navigation_shell() -> None:
    source = SHELL.read_text()
    css = TOPBAR_CSS.read_text()

    assert '<details className="r-project-manage">' in source
    assert "PROJECT_VISIBILITY_STORAGE" in source
    assert 'fetch("/_projects/index.json")' in source
    assert 'fetch("/crew")' in source
    assert "window.Cockpit" not in source
    assert "r-project-chip" in css
    assert all(
        project not in source for project in ("imas-ambix", "imas-codex", "nova-jax")
    )


def _topbar_function_source() -> str:
    source = SHELL.read_text()
    start = source.index("function TopBar(")
    brace = source.index(") {", start) + 2
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError("unterminated function TopBar")


def test_topbar_renders_in_canvas_order() -> None:
    source = _topbar_function_source()

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


def test_search_control_precedes_the_project_selector() -> None:
    source = _topbar_function_source()

    assert source.index('className="r-topbar-search"') < source.index(
        'className="r-project-manage"'
    )


def test_brand_button_is_a_wordless_compass_mark() -> None:
    source = _topbar_function_source()
    glyphs = (ROOT / "docs" / "ui" / "glyphs.jsx").read_text()

    brand_start = source.index('className="r-topbar-brand"')
    brand_end = source.index("</button>", brand_start)
    brand = source[brand_start:brand_end]

    assert ">reckon<" not in brand
    assert ">Overview<" not in source
    assert "GLYPHS?.brand" in brand

    brand_glyph_start = glyphs.index("brand: (")
    brand_glyph_end = glyphs.index("),", brand_glyph_start)
    glyph = glyphs[brand_glyph_start:brand_glyph_end]

    assert glyph.count("<svg") == 1
    assert glyph.count("<circle") == 2
    assert '<g transform="rotate(38 8 8)">' in glyph
    assert glyph.count("<polygon") == 2
    assert ">" not in re.sub(r"<[^>]+>", "", glyph).strip()


def test_tab_groups_are_owned_by_the_route_module() -> None:
    source = _topbar_function_source()
    route = (ROOT / "docs" / "ui" / "shell-route.jsx").read_text()

    assert "window.ReckonShell.route.ARTIFACT_TABS" in source
    assert "window.ReckonShell.route.WORK_TABS" in source
    assert '"Plans"' in route
    assert '"Sprints"' in route
    assert '"Graph"' in route
    assert '"Crew"' in route
    assert "ARTIFACT_TABS.map(tab" in source
    assert "WORK_TABS.map(tab" in source
