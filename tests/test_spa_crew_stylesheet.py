from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, serve

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


def _local_scripts(html: str, prefix: str) -> tuple[str, ...]:
    return tuple(
        reference.lstrip("/")
        for reference in re.findall(r'<script[^>]+src="([^"]+\.js)"', html)
        if reference.lstrip("/").startswith(prefix)
    )


def _stylesheets(html: str) -> tuple[str, ...]:
    return tuple(
        reference.lstrip("/")
        for reference in re.findall(r'<link[^>]+href="([^"]+\.css)"', html)
        if not reference.startswith(("https://", "http://"))
    )


def _assert_entry_point_assets_match(
    assets: dict[str, tuple[str, ...]], expected: tuple[str, ...]
) -> None:
    assert assets == dict.fromkeys(assets, expected)


@pytest.fixture(scope="module")
def spa_entry_points(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("entry-points")
    synced_docs = root / "synced"
    built_docs = root / "built"
    synced_docs.mkdir()
    built_docs.mkdir()

    sync_result = CliRunner().invoke(
        cli.main,
        [
            "sync",
            str(synced_docs),
            "--project",
            "fixture",
            "--mounts",
            str(root / "mounts.json"),
            "--state-root",
            str(root / "state"),
        ],
    )
    assert sync_result.exit_code == 0, sync_result.output
    build_result = CliRunner().invoke(
        cli.main, ["build", str(built_docs), "--project", "fixture"]
    )
    assert build_result.exit_code == 0, build_result.output

    return {
        "checked-in": (ROOT / "docs" / "index.html").read_text(encoding="utf-8"),
        "served": serve._render_spa_html("fixture"),
        "synced": (synced_docs / "index.html").read_text(encoding="utf-8"),
        "built": (built_docs / "index.html").read_text(encoding="utf-8"),
    }


def test_crew_stylesheet_registers_with_every_spa_entry_point(
    spa_entry_points: dict[str, str],
) -> None:
    registrations = {
        name: _stylesheets(html) for name, html in spa_entry_points.items()
    }
    expected = registrations["checked-in"]

    _assert_entry_point_assets_match(registrations, expected)
    assert expected.count("_ui/crew.css") == 1
    mutated = {
        **registrations,
        "served": tuple(
            asset for asset in registrations["served"] if asset != "_ui/crew.css"
        ),
    }
    with pytest.raises(AssertionError):
        _assert_entry_point_assets_match(mutated, expected)


def test_spa_entry_points_register_only_local_compiled_runtime_assets(
    spa_entry_points: dict[str, str],
) -> None:
    registrations = {
        name: _local_scripts(html, "_runtime/")
        for name, html in spa_entry_points.items()
    }
    expected = registrations["checked-in"]

    _assert_entry_point_assets_match(registrations, expected)
    assert expected == ("_runtime/react.js", "_runtime/react-dom.js")
    assert all("text/babel" not in html for html in spa_entry_points.values())
    assert all("@babel/standalone" not in html for html in spa_entry_points.values())
    assert all(
        not re.search(r'<script[^>]+src="(?:https?:)?//', html)
        for html in spa_entry_points.values()
    )
    mutated = {**registrations, "built": registrations["built"][:-1]}
    with pytest.raises(AssertionError):
        _assert_entry_point_assets_match(mutated, expected)


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
