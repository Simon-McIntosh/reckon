import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
READER_CSS = (ROOT / "docs" / "ui" / "reader.css").read_text()
PLAN_SOURCE = (ROOT / "docs" / "ui" / "plan.jsx").read_text()


def _declarations(selector: str) -> dict[str, str]:
    pattern = rf"(?:^|}})\s*{re.escape(selector)}\s*\{{([^}}]+)\}}"
    match = re.search(pattern, READER_CSS, flags=re.MULTILINE)
    assert match, f"missing CSS rule for {selector}"
    return {
        name.strip(): value.strip()
        for declaration in match.group(1).split(";")
        if ":" in declaration
        for name, value in [declaration.split(":", 1)]
    }


def test_reader_column_uses_the_canvas_flex_geometry() -> None:
    rule = _declarations(".r-reader-with-attachments > .r-body")

    assert rule["flex"] == "1"
    assert rule["min-width"] == "300px"
    assert rule["overflow"] == "auto"
    assert rule["padding"] == "22px 26px 34px"


def test_reader_headings_and_metadata_use_the_declared_type_scale() -> None:
    headings = _declarations(".r-reading:not(.is-focus-mode) h2,\n.r-reading:not(.is-focus-mode) .r-plan-html h2")
    metadata = _declarations(".r-reading-controls")
    buttons = _declarations(".r-reading-controls button")

    assert headings["font-weight"] == "600"
    assert headings["font-size"] == "23px"
    assert headings["letter-spacing"] == "-0.015em"
    assert metadata["font-family"] == "var(--mono)"
    assert metadata["font-size"] == "11.5px"
    assert buttons["font-size"] == "11.5px"
    assert buttons["padding"] == "2px 8px"
    assert buttons["flex"] == "none"


def test_focus_mode_is_a_full_viewport_surface_with_canvas_reading_measure() -> None:
    surface = _declarations(".r-reading.is-focus-mode")
    header = _declarations(".r-reading.is-focus-mode .r-reading-controls")
    viewport = _declarations(".r-reading.is-focus-mode .r-reading-viewport")
    content = _declarations(".r-reading.is-focus-mode .r-reading-content")
    title = _declarations(".r-reading-focus-title h1")

    assert surface["position"] == "fixed"
    assert surface["inset"] == "0"
    assert surface["display"] == "flex"
    assert surface["flex-direction"] == "column"
    assert header["padding"] == "10px 20px"
    assert header["border-bottom"] == "1px solid var(--line)"
    assert header["font-size"] == "11.5px"
    assert viewport["flex"] == "1"
    assert viewport["overflow"] == "auto"
    assert content["max-width"] == "720px"
    assert content["padding"] == "56px 24px 120px"
    assert title["font-size"] == "32px"
    assert title["letter-spacing"] == "-0.022em"


def test_focus_header_paging_and_reads_list_keep_existing_navigation_paths() -> None:
    assert 'className="r-reading-position" role="status"' in PLAN_SOURCE
    assert 'className="r-reading-paging"' in PLAN_SOURCE
    assert "onPage?.(-1)" in PLAN_SOURCE
    assert "onPage?.(1)" in PLAN_SOURCE
    assert 'className="r-reading-reads"' in PLAN_SOURCE
    assert 'onNav?.({ view: "plan", slug: key })' in PLAN_SOURCE

    reads = _declarations(".r-reading-reads")
    cards = _declarations(".r-reading-reads-list > button")
    assert reads["margin-top"] == "44px"
    assert reads["padding-top"] == "18px"
    assert cards["padding"] == "12px 14px"
    assert cards["gap"] == "9px"
