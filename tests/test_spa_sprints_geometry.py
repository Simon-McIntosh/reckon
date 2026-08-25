import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
CSS = (ROOT / "docs/ui/sprints.css").read_text()
JSX = (ROOT / "docs/ui/sprint.jsx").read_text()


def declarations(selector: str) -> str:
    match = re.search(rf"(?:^|\n){re.escape(selector)}\s*\{{([^}}]+)\}}", CSS)
    assert match, f"missing CSS rule for {selector}"
    return re.sub(r"\s+", " ", match.group(1)).strip()


def assert_declares(selector: str, *properties: str) -> None:
    body = declarations(selector)
    for prop in properties:
        assert prop in body, f"{selector} must declare {prop!r}; got {body!r}"


def test_sprint_surface_and_header_use_canvas_geometry() -> None:
    assert_declares(
        ".r-sprint-surface",
        "flex: 1",
        "overflow: auto",
        "padding: 20px 26px 40px",
    )
    assert_declares(
        ".r-sprint-surface .r-sp-head",
        "display: flex",
        "align-items: center",
        "gap: 12px",
        "margin-bottom: 16px",
    )
    assert_declares(
        ".r-sprint-surface .r-sp-head h1",
        "font-size: 17px",
        "font-weight: 600",
        "letter-spacing: -0.012em",
    )


def test_gantt_uses_the_declared_shared_axis_and_row_dimensions() -> None:
    assert_declares(
        ".r-time-axis, .r-folded-band, .r-timeline-row",
        "grid-template-columns: 210px minmax(0, 1fr)",
        "gap: 0 16px",
    )
    assert_declares(".r-time-axis > div", "grid-template-columns: repeat(6, 1fr)")
    assert_declares(".r-folded-band", "padding: 8px 0", "border-bottom: 1px solid var(--line)")
    assert_declares(".r-folded-track", "height: 15px")
    assert_declares(".r-timeline-row", "padding: 9px 0", "border-bottom: 1px solid var(--line)")
    assert_declares(".r-timeline-track", "height: 21px")
    assert_declares(".r-sprint-mark", "top: 4px", "height: 13px", "border-radius: 3px")
    assert "const tickCount = 6;" in JSX
    assert 'className="r-folded-track"' in JSX


def test_board_columns_and_cards_use_canvas_geometry() -> None:
    assert_declares(
        ".r-sprint-surface .r-kanban",
        "grid-template-columns: repeat(3, 1fr)",
        "gap: 14px",
    )
    assert_declares(
        ".r-sprint-surface .r-col",
        "border-radius: 8px",
        "padding: 11px",
    )
    assert_declares(
        ".r-sprint-surface .r-kcard",
        "margin-bottom: 8px",
        "border-radius: 8px",
        "padding: 10px 11px",
    )
    assert_declares(
        ".r-card-title",
        "font-size: 13.5px",
        "font-weight: 600",
        "line-height: 1.3",
    )
    assert_declares(".r-card-description", "font-size: 12.5px", "line-height: 1.45")
    assert_declares(".r-card-progress .bar", "height: 3px", "border-radius: 2px")
