from __future__ import annotations

import re
from pathlib import Path


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
        "min-width": "1374px",
        "overflow-x": "auto",
        "overflow-y": "hidden",
    }


def test_each_view_and_plan_column_uses_the_canvas_flex_geometry():
    source = (UI_ROOT / "styles.css").read_text()

    assert _declarations(source, ".r-app > div.r-3col") == {
        "display": "flex",
        "flex": "1",
        "min-height": "0",
        "min-width": "1374px",
    }
    assert _declarations(source, ".r-app > .r-topbar") == {"flex": "none"}
    assert _declarations(source, ".r-app > div.r-3col > .r-filters") == {
        "display": "flex",
        "flex-direction": "column",
        "flex": "none",
        "gap": "14px",
        "overflow": "auto",
        "padding": "10px 8px",
        "width": "64px",
        "border-right": "1px solid var(--line)",
        "background": "var(--bg-2)",
    }
    assert _declarations(source, ".r-app > div.r-3col > .r-list") == {
        "display": "flex",
        "flex-direction": "column",
        "flex": "none",
        "min-height": "0",
        "overflow-y": "auto",
        "width": "390px",
        "border-right": "1px solid var(--line)",
        "background": "var(--bg)",
    }
    assert _declarations(source, ".r-app > div.r-3col > .r-content") == {
        "display": "flex",
        "flex-direction": "column",
        "flex": "1",
        "min-width": "0",
        "min-height": "0",
        "overflow": "hidden",
        "background": "var(--bg)",
    }


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
