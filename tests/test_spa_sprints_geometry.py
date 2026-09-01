import re
from pathlib import Path

import pytest

from tests.spa_browser_harness import installed_browser, run_browser_probe

ROOT = Path(__file__).parents[1]
CSS = (ROOT / "docs/ui/sprints.css").read_text()
BASE_CSS = (ROOT / "docs/ui/styles-base.css").read_text()
JSX = (ROOT / "docs/ui/sprint.jsx").read_text()


def declarations(selector: str) -> str:
    match = re.search(rf"(?:^|\n){re.escape(selector)}\s*\{{([^}}]+)\}}", CSS)
    assert match, f"missing CSS rule for {selector}"
    return re.sub(r"\s+", " ", match.group(1)).strip()


def assert_declares(selector: str, *properties: str) -> None:
    body = declarations(selector)
    for prop in properties:
        assert prop in body, f"{selector} must declare {prop!r}; got {body!r}"


def test_sprint_view_declares_one_vertical_scroll_owner() -> None:
    view_styles = f"{BASE_CSS}\n{CSS}"
    scroll_owners = []
    for match in re.finditer(
        r"(?m)^([^\n{]*(?:\.r-sprint-view|\.r-sprint-surface)[^\n{]*)\s*\{([^{}]+)\}",
        view_styles,
    ):
        selector = re.sub(r"\s+", " ", match.group(1)).strip()
        body = re.sub(r"\s+", " ", match.group(2)).strip()
        if (
            (".r-sprint-view" in selector or ".r-sprint-surface" in selector)
            and ("overflow: auto" in body or "overflow-y: auto" in body)
        ):
            scroll_owners.append(selector)

    assert scroll_owners == [".r-sprint-view .r-reader-with-attachments > .r-body"]
    owner_rule = re.search(
        r"\.r-sprint-view \.r-reader-with-attachments > \.r-body\s*\{([^}]+)\}",
        BASE_CSS,
    )
    assert owner_rule
    owner_declarations = re.sub(r"\s+", " ", owner_rule.group(1)).strip()
    assert "min-height: 0" in owner_declarations
    assert "overflow-y: auto" in owner_declarations


def test_sprint_surface_and_header_use_content_geometry() -> None:
    assert_declares(
        ".r-sprint-surface",
        "padding: 20px 26px 40px",
    )
    surface_rule = declarations(".r-sprint-surface")
    assert "flex:" not in surface_rule
    assert "overflow: auto" not in surface_rule
    assert "overflow-y: auto" not in surface_rule
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
        "grid-template-columns: minmax(320px, 24%) minmax(0, 1fr)",
        "gap: 0 16px",
    )
    assert_declares(".r-time-axis > div", "grid-template-columns: repeat(6, 1fr)")
    assert_declares(
        ".r-folded-band", "padding: 8px 0", "border-bottom: 1px solid var(--line)"
    )
    assert_declares(".r-folded-track", "height: 15px")
    assert_declares(
        ".r-timeline-row", "padding: 9px 0", "border-bottom: 1px solid var(--line)"
    )
    assert_declares(".r-timeline-track", "height: 21px")
    assert_declares(".r-sprint-mark", "top: 4px", "height: 13px", "border-radius: 3px")
    assert "const tickCount = 6;" in JSX
    assert 'className="r-folded-track"' in JSX


@pytest.mark.parametrize("viewport_width", [1374, 1920])
def test_rendered_timeline_labels_fit_and_use_available_width(
    tmp_path: Path, viewport_width: int
) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip("an installed browser is required for rendered geometry")

    stylesheets = [
        ROOT / "docs/_shared/foundation.css",
        ROOT / "docs/_shared/dashboard.css",
        ROOT / "docs/ui/styles-base.css",
        ROOT / "docs/ui/styles.css",
        ROOT / "docs/ui/sprints.css",
    ]
    styles = "\n".join(path.read_text() for path in stylesheets)
    document = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{styles}</style></head>
<body><div class="r-app"><div class="r-canvas-view r-sprint-view"><div class="r-content">
<div class="r-reader-with-attachments"><div class="r-body">
<div class="r-page wide r-sprint-surface"><section class="r-sprint-overview">
<div class="r-time-axis"><span></span><div>{"".join("<span>Aug 25</span>" for _ in range(6))}</div></div>
<div class="r-timeline-rows">
<div class="r-timeline-row"><a href="#"><strong>S9</strong><span class="r-sprint-title">Observation and orientation across the complete planning surface</span><em>active</em></a>
<div class="r-timeline-track"><a class="r-sprint-mark active" href="#" style="left: 20%; width: 1.5%"><span class="r-sprint-mark-label">S9</span></a></div></div>
</div></section></div></div></div></div></div></div></body></html>"""
    metrics = run_browser_probe(
        tmp_path,
        browser,
        document,
        """(() => {
          const title = document.querySelector('.r-sprint-title');
          const mark = document.querySelector('.r-sprint-mark-label');
          const surface = document.querySelector('.r-sprint-surface');
          const overview = document.querySelector('.r-sprint-overview');
          const track = document.querySelector('.r-timeline-track');
          return {
            titleClient: title.clientWidth, titleScroll: title.scrollWidth,
            markClient: mark.clientWidth, markScroll: mark.scrollWidth,
            surfaceWidth: surface.clientWidth, overviewWidth: overview.clientWidth,
            trackWidth: track.clientWidth, viewportWidth: innerWidth,
          };
        })()""",
        viewport=(viewport_width, 900),
        ready_expression="Boolean(document.querySelector('.r-sprint-mark-label'))",
    )

    assert metrics["titleScroll"] <= metrics["titleClient"], metrics
    assert metrics["markScroll"] <= metrics["markClient"], metrics
    # The overview fills the surface's content box; the 52px difference is the
    # surface's declared 26px horizontal padding on each side.
    assert metrics["overviewWidth"] == metrics["surfaceWidth"] - 52, metrics
    if viewport_width == 1920:
        assert metrics["surfaceWidth"] > 1216, metrics
        assert metrics["trackWidth"] > 1216, metrics


@pytest.mark.parametrize(
    ("surface_class", "last_class"),
    [("r-sprint-overview", "overview-last"), ("r-sprint-board", "board-last")],
)
def test_constrained_viewport_reaches_last_element_of_each_sprint_surface(
    tmp_path: Path, surface_class: str, last_class: str
) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip("an installed browser is required for rendered geometry")

    stylesheets = [
        ROOT / "docs/_shared/foundation.css",
        ROOT / "docs/_shared/dashboard.css",
        ROOT / "docs/ui/styles-base.css",
        ROOT / "docs/ui/styles.css",
        ROOT / "docs/ui/plans.css",
        ROOT / "docs/ui/reader.css",
        ROOT / "docs/ui/sprints.css",
    ]
    styles = "\n".join(path.read_text() for path in stylesheets)
    rows = "".join(f'<div class="capture-row">row {index}</div>' for index in range(8))
    document = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{styles}
.capture-row {{ height: 72px; border-bottom: 1px solid var(--line); }}
.capture-last {{ height: 36px; }}
</style></head><body><div class="r-app"><div class="r-topbar">Sprints</div>
<div class="r-canvas-view r-sprint-view"><div class="r-content">
<div class="r-reader-with-attachments"><div class="r-body">
<div class="r-page wide r-sprint-surface"><section class="{surface_class}">
{rows}<div class="capture-last {last_class}">last element</div>
</section></div></div></div></div></div></div></body></html>"""
    metrics = run_browser_probe(
        tmp_path,
        browser,
        document,
        f"""(() => {{
          const owner = document.querySelector('.r-sprint-view .r-reader-with-attachments > .r-body');
          const surface = document.querySelector('.r-sprint-surface');
          const last = document.querySelector('.{last_class}');
          owner.scrollTop = owner.scrollHeight;
          const ownerRect = owner.getBoundingClientRect();
          const lastRect = last.getBoundingClientRect();
          const scrollable = [...document.querySelectorAll('*')].filter(element => {{
            const overflow = getComputedStyle(element).overflowY;
            return (overflow === 'auto' || overflow === 'scroll') && element.scrollHeight > element.clientHeight;
          }});
          return {{
            ownerClientHeight: owner.clientHeight,
            ownerScrollHeight: owner.scrollHeight,
            ownerScrollTop: owner.scrollTop,
            surfaceClientHeight: surface.clientHeight,
            surfaceScrollHeight: surface.scrollHeight,
            lastTop: lastRect.top,
            lastBottom: lastRect.bottom,
            ownerTop: ownerRect.top,
            ownerBottom: ownerRect.bottom,
            documentScrollTop: document.scrollingElement.scrollTop,
            scrollableCount: scrollable.length,
            scrollableClass: scrollable.map(element => element.className).join(' '),
          }};
        }})()""",
        viewport=(900, 420),
        ready_expression=f"Boolean(document.querySelector('.{last_class}'))",
    )

    assert metrics["ownerScrollHeight"] > metrics["ownerClientHeight"], metrics
    assert metrics["ownerScrollTop"] > 0, metrics
    assert metrics["lastTop"] >= metrics["ownerTop"] - 1, metrics
    assert metrics["lastBottom"] <= metrics["ownerBottom"] + 1, metrics
    assert metrics["surfaceScrollHeight"] == metrics["surfaceClientHeight"], metrics
    assert metrics["documentScrollTop"] == 0, metrics
    assert metrics["scrollableCount"] == 1, metrics
    assert "r-body" in metrics["scrollableClass"], metrics


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
