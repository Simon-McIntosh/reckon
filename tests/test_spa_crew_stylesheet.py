from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLESHEET = ROOT / "docs" / "ui" / "crew.css"


def _declarations(source: str, selector: str) -> set[str]:
    for match in re.finditer(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]+)\}", source):
        selectors = {item.strip() for item in match.group("selectors").split(",")}
        if selector in selectors:
            return {
                declaration.split(":", 1)[0].strip()
                for declaration in match.group("body").split(";")
                if ":" in declaration
            }
    raise AssertionError(f"missing rule for {selector}")


def test_crew_stylesheet_registers_with_every_spa_entry_point() -> None:
    registrations = {
        "checked-in": (ROOT / "docs" / "index.html").read_text(encoding="utf-8"),
        "served": (ROOT / "reckon" / "serve.py").read_text(encoding="utf-8"),
        "synced-and-built": (ROOT / "reckon" / "cli.py").read_text(encoding="utf-8"),
    }

    assert registrations["checked-in"].count("/_ui/crew.css") == 1
    assert registrations["served"].count("/_ui/crew.css") == 1
    assert registrations["synced-and-built"].count("_ui/crew.css") == 2


def test_spa_entry_points_register_only_local_compiled_runtime_assets() -> None:
    registrations = {
        "checked-in": (ROOT / "docs" / "index.html").read_text(encoding="utf-8"),
        "served": (ROOT / "reckon" / "serve.py").read_text(encoding="utf-8"),
        "synced-and-built": (ROOT / "reckon" / "cli.py").read_text(
            encoding="utf-8"
        ),
    }

    assert registrations["checked-in"].count("/_runtime/react.js") == 1
    assert registrations["checked-in"].count("/_runtime/react-dom.js") == 1
    assert registrations["served"].count("/_runtime/react.js") == 1
    assert registrations["served"].count("/_runtime/react-dom.js") == 1
    assert registrations["synced-and-built"].count("_runtime/react.js") == 2
    assert registrations["synced-and-built"].count("_runtime/react-dom.js") == 2
    for source in registrations.values():
        assert "text/babel" not in source


def test_crew_stylesheet_defines_the_three_zone_card_layout() -> None:
    source = STYLESHEET.read_text(encoding="utf-8")

    assert source.strip()
    assert {"display", "grid-template-columns", "gap"} <= _declarations(
        source, ".r-crew-card"
    )
    assert {"display", "align-items", "gap"} <= _declarations(
        source, ".r-crew-identity"
    )
    assert {"display", "align-items", "gap"} <= _declarations(
        source, ".r-crew-location"
    )


def test_budget_bar_and_gate_marks_have_tracks_and_measured_fills() -> None:
    source = STYLESHEET.read_text(encoding="utf-8")

    assert {"height", "overflow", "background"} <= _declarations(
        source, ".r-crew-meter"
    )
    assert {"display", "height", "background"} <= _declarations(
        source, ".r-crew-meter i"
    )
    assert {"display", "gap"} <= _declarations(source, ".r-crew-gate-marks")
    assert {"height", "flex", "background"} <= _declarations(
        source, ".r-crew-gate-marks i"
    )
    assert "background" in _declarations(source, ".r-crew-gate-marks i.measured")


def test_connection_expansion_keeps_session_host_and_attach_command_legible() -> None:
    source = STYLESHEET.read_text(encoding="utf-8")

    assert {"grid-column", "border-top"} <= _declarations(source, ".r-crew-connect")
    assert {"display", "gap"} <= _declarations(source, ".r-crew-connect-grid")
    assert {"display", "align-items", "background"} <= _declarations(
        source, ".r-crew-attach"
    )
    assert {"overflow", "text-overflow", "white-space"} <= _declarations(
        source, ".r-crew-attach code"
    )
