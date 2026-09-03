from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.spa_browser_harness import BrowserProbeError, ServedSpa, installed_browser

ROOT = Path(__file__).resolve().parents[1]
CREW_CSS = ROOT / "docs" / "ui" / "crew.css"
CREW_JSX = ROOT / "docs" / "ui" / "crew.jsx"


def _declarations(selector: str, source: str | None = None) -> dict[str, str]:
    text = source if source is not None else CREW_CSS.read_text(encoding="utf-8")
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]+)\}}", text)
    assert match is not None, f"missing CSS selector {selector}"
    return {
        name.strip(): value.strip()
        for declaration in match.group("body").split(";")
        if ":" in declaration
        for name, value in [declaration.split(":", 1)]
    }


@contextmanager
def _skip_when_browser_is_unavailable():
    try:
        yield
    except BrowserProbeError as error:
        pytest.skip(f"browser unavailable ({error.classification}): {error}")


def test_crew_view_and_header_match_canvas_geometry() -> None:
    view = _declarations(".r-crew-view")
    heading = _declarations(".r-crew-heading")
    title = _declarations(".r-crew-heading h1")
    metadata = _declarations(".r-crew-heading > span")

    assert view == {
        "display": "block",
        "flex": "1",
        "min-height": "0",
        "overflow-y": "auto",
    }
    assert heading["display"] == "flex"
    assert heading["gap"] == "12px"
    assert title["font-size"] == "17px"
    assert title["font-weight"] == "600"
    assert title["letter-spacing"] == "-0.012em"
    assert metadata["font"] == "11.5px var(--mono)"
    assert 'className="r-crew-view"' not in CREW_JSX.read_text(encoding="utf-8")
    assert 'className="r-crew-surface"' in CREW_JSX.read_text(encoding="utf-8")


def test_crew_card_declares_exact_identity_budget_gate_grid() -> None:
    card = _declarations(".r-crew-card")

    assert card["display"] == "grid"
    assert card["grid-template-columns"] == "minmax(0, 1fr) 150px 200px"
    assert card["gap"] == "18px"


def test_card_interior_uses_canvas_type_and_meter_measurements() -> None:
    identity = _declarations(".r-crew-identity")
    location = _declarations(".r-crew-location")
    label = _declarations(".r-crew-label")
    figures = _declarations(".r-crew-budget-values")
    meter = _declarations(".r-crew-meter")
    activity = _declarations(".r-crew-activity")

    assert identity["font"] == "11.5px var(--mono)"
    assert identity["overflow"] == "hidden"
    assert location["gap"] == "8px"
    assert label["font"] == "10.5px var(--mono)"
    assert label["letter-spacing"] == ".06em"
    assert figures["font"] == "12px var(--mono)"
    assert meter["height"] == "3px"
    assert activity["font"] == "11px var(--mono)"


def _crew_fixture_document(styles: str, card_count: int) -> str:
    cards = "".join(
        f'<article class="r-crew-card">run {index}</article>'
        for index in range(card_count)
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{styles}
.r-crew-card {{ height: 90px; margin-bottom: 8px; }}
</style></head>
<body><div class="r-app"><div class="r-topbar">Crew</div>
<div class="r-canvas-view r-crew-view"><div class="r-content">
<div class="r-reader-with-attachments"><div class="r-body">
<div class="r-crew-surface"><div class="r-crew-heading"><h1>nova &middot; {card_count} runs</h1></div>
<div class="r-crew-list" aria-label="Live crew runs">{cards}</div>
</div></div></div></div></div></div></body></html>"""


def test_crew_view_scrolls_a_dozen_runs_within_the_viewport(tmp_path: Path) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip("an installed browser is required for rendered geometry")

    stylesheets = [
        ROOT / "docs/_shared/foundation.css",
        ROOT / "docs/_shared/dashboard.css",
        ROOT / "docs/ui/styles-base.css",
        ROOT / "docs/ui/styles.css",
        ROOT / "docs/ui/plans.css",
        ROOT / "docs/ui/crew.css",
    ]
    styles = "\n".join(path.read_text(encoding="utf-8") for path in stylesheets)
    document = _crew_fixture_document(styles, 12)
    page = tmp_path / "rendered-crew-geometry.html"
    page.write_text(document, encoding="utf-8")
    context = ServedSpa(browser=browser, url=page.resolve().as_uri(), tmp_path=tmp_path)

    with _skip_when_browser_is_unavailable():
        metrics = context.run_probe(
            """(() => {
              const view = document.querySelector('.r-crew-view');
              const flexAncestorMinHeights = [];
              let node = view.parentElement;
              while (node) {
                const style = getComputedStyle(node);
                if (style.display === 'flex' || style.display === 'inline-flex') {
                  flexAncestorMinHeights.push(style.minHeight);
                }
                node = node.parentElement;
              }
              return {
                viewScrollHeight: view.scrollHeight,
                viewClientHeight: view.clientHeight,
                docScrollHeight: document.documentElement.scrollHeight,
                flexAncestorMinHeights,
              };
            })()""",
            viewport=(1374, 900),
            ready_expression="Boolean(document.querySelector('.r-crew-list'))",
        )

    assert metrics["viewScrollHeight"] > metrics["viewClientHeight"], metrics
    assert metrics["docScrollHeight"] == 900, metrics
    assert metrics["flexAncestorMinHeights"], metrics
    assert all(value == "0px" for value in metrics["flexAncestorMinHeights"]), metrics
