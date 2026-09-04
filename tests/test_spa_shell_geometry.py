from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    BrowserProbeError,
    installed_browser,
    run_browser_probe,
    served_spa,
)

REPO_ROOT = Path(__file__).parents[1]
UI_ROOT = REPO_ROOT / "docs" / "ui"


def _declarations(source: str, selector: str) -> dict[str, str]:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", source)
    assert match is not None, f"missing CSS rule for {selector}"
    return {
        name.strip(): value.strip()
        for declaration in match.group(1).split(";")
        if ":" in declaration
        for name, value in [declaration.split(":", 1)]
    }


def test_shell_uses_the_canvas_root_geometry():
    source = (UI_ROOT / "styles.css").read_text()

    assert _declarations(source, ".r-app") == {
        "display": "flex",
        "flex-direction": "column",
        "height": "100vh",
        "min-width": "0",
        "overflow-x": "hidden",
        "overflow-y": "hidden",
    }


def test_shell_has_one_fluid_view_owner_and_no_retired_columns():
    source = (UI_ROOT / "styles.css").read_text()
    plans = (UI_ROOT / "plans.css").read_text()

    assert _declarations(source, ".r-app > .r-topbar") == {"flex": "none"}
    assert ".r-app > div.r-3col" not in source
    assert ".r-filters" not in source
    assert ".r-3col.plans-mode" not in plans
    assert ".r-filters" not in plans


def test_retired_grid_and_collapse_rules_are_absent():
    shared = (UI_ROOT / "styles.css").read_text()
    base = (UI_ROOT / "styles-base.css").read_text()

    for retired in (
        "--c1",
        "--c2",
        ".r-3col.filters-collapsed",
        ".r-filter-handle",
        ".r-3col.graph-mode",
        "grid-template-columns: var(--c1) var(--c2) 1fr",
        "transition: grid-template-columns",
    ):
        assert retired not in shared

    assert "--sb-width" not in base
    assert not re.search(r"\.r-app\s*\{", base)


def test_stylesheets_have_no_fixed_canvas_width() -> None:
    declarations = {
        path.relative_to(REPO_ROOT): line
        for path in (REPO_ROOT / "docs").rglob("*.css")
        for line in path.read_text().splitlines()
        if "1374px" in line
    }

    assert declarations == {}


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


def test_rendered_topbar_children_follow_the_canvas_order(
    tmp_path: Path, rendered_browser: str
) -> None:
    expression = """(() => {
      const bar = document.querySelector('.r-topbar');
      const order = [
        '.r-topbar-brand', '.r-topbar-search', '.r-tabs-artifact',
        '.r-tabs-work', '.r-live-receipt', '.r-project-manage', '.settings',
      ];
      const positions = order.map(sel => {
        const el = bar.querySelector(sel);
        let index = 0;
        for (const child of bar.children) {
          if (child === el || child.contains(el)) return index;
          index += 1;
        }
        return -1;
      });
      return { positions, text: bar.textContent };
    })()"""

    with served_spa(tmp_path, rendered_browser, route="#cockpit") as spa:
        result = spa.run_probe(
            expression,
            viewport=(1440, 900),
            ready_expression="Boolean(document.querySelector('.r-topbar .r-tabs-work'))",
        )

    positions = result["positions"]
    assert all(index >= 0 for index in positions)
    assert positions == sorted(positions)
    assert "Overview" not in result["text"]
    assert "reckon" not in result["text"]


def test_shell_styles_do_not_carry_version_label_comments():
    source = "\n".join(
        (UI_ROOT / name).read_text() for name in ("styles.css", "styles-base.css")
    )

    for version_label in (
        r"\bv\d+\s+styles\b",
        r"\breuse\s+v\d+\s+components\b",
        r"\bcockpit\s+body\s+in\s+v\d+\b",
        r"\bhide\s+v\d+\s+view\s+headers\b",
    ):
        assert not re.search(version_label, source, re.IGNORECASE)
