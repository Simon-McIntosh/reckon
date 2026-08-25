from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CREW_CSS = ROOT / "docs" / "ui" / "crew.css"
CREW_JSX = ROOT / "docs" / "ui" / "crew.jsx"


def _declarations(selector: str) -> dict[str, str]:
    source = CREW_CSS.read_text(encoding="utf-8")
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]+)\}}", source)
    assert match is not None, f"missing CSS selector {selector}"
    return {
        name.strip(): value.strip()
        for declaration in match.group("body").split(";")
        if ":" in declaration
        for name, value in [declaration.split(":", 1)]
    }


def test_crew_view_and_header_match_canvas_geometry() -> None:
    view = _declarations(".r-crew-view")
    heading = _declarations(".r-crew-heading")
    title = _declarations(".r-crew-heading h1")
    metadata = _declarations(".r-crew-heading > span")

    assert view == {
        "flex": "1",
        "overflow": "auto",
        "padding": "20px 26px 40px",
    }
    assert heading["display"] == "flex"
    assert heading["gap"] == "12px"
    assert title["font-size"] == "17px"
    assert title["font-weight"] == "600"
    assert title["letter-spacing"] == "-0.012em"
    assert metadata["font"] == "11.5px var(--mono)"
    assert 'className="r-crew-view"' in CREW_JSX.read_text(encoding="utf-8")


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
