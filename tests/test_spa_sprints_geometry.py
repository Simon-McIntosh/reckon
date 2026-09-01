import re
from pathlib import Path

import pytest

from tests import spa_module_eval
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
        if (".r-sprint-view" in selector or ".r-sprint-surface" in selector) and (
            "overflow: auto" in body or "overflow-y: auto" in body
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


def test_fixed_horizon_retires_calendar_controls_and_sprint_geometry() -> None:
    for retired in ('"4w"', '"8w"', '"6m"', "SPRINT_HORIZONS", "sprintAxis"):
        assert retired not in JSX
    assert "r-sprint-mark" not in JSX
    assert "r-sprint-mark" not in CSS
    assert (
        "sprint.starts"
        not in JSX[
            JSX.index("function horizonStrip") : JSX.index("function openGateCount")
        ]
    )


def test_horizon_strip_is_local_fixed_and_places_timestamped_events() -> None:
    result = spa_module_eval.evaluate_jsx_module(
        ROOT / "docs/ui/sprint.jsx",
        "(() => {"
        "const now = new Date(2026, 8, 1, 6, 0, 0).getTime();"
        "const completed = [{run_id: 'done', completed_at: new Date(2026, 8, 1, 2, 0, 0).toISOString()}];"
        "const live = [{run_id: 'live', dispatched_at: new Date(2026, 8, 1, 4, 0, 0).toISOString()}];"
        "const first = horizonStrip(now, completed, live);"
        "const later = horizonStrip(now + HOUR_MS, completed, live);"
        "return {"
        "startHour: new Date(first.start).getHours(),"
        "durationHours: (Date.parse(first.end) - Date.parse(first.start)) / HOUR_MS,"
        "tomorrowPosition: first.tomorrowPosition,"
        "nowPosition: first.nowPosition, laterPosition: later.nowPosition,"
        "tickCount: first.ticks.length,"
        "events: first.events.map(event => ({kind: event.kind, id: event.run.run_id, left: event.left}))"
        "};"
        "})()",
    )

    assert result["startHour"] == 0
    assert result["durationHours"] == 48
    assert result["tomorrowPosition"] == 50
    assert result["tickCount"] == 9
    assert result["laterPosition"] - result["nowPosition"] == pytest.approx(100 / 48)
    assert result["events"] == [
        {"kind": "completed", "id": "done", "left": pytest.approx(100 / 24)},
        {"kind": "live", "id": "live", "left": pytest.approx(100 / 12)},
    ]


def test_now_line_uses_the_current_instant_and_advances_on_a_timer() -> None:
    assert (
        "const [currentInstant, setCurrentInstant] = useState(() => Date.now())" in JSX
    )
    assert "setInterval(() => setCurrentInstant(Date.now()), HORIZON_REFRESH_MS)" in JSX
    assert "window.clearInterval(timer)" in JSX
    assert "horizonStrip(currentInstant, finishedRuns, liveRuns)" in JSX
    assert (
        "M.today"
        not in JSX[
            JSX.index("function horizonStrip") : JSX.index("function openGateCount")
        ]
    )


def test_horizon_strip_has_compact_two_day_geometry() -> None:
    assert_declares(
        ".r-horizon-strip",
        "margin-bottom: 14px",
        "padding: 10px 14px 8px",
    )
    assert_declares(
        ".r-horizon-strip > header", "grid-template-columns: repeat(2, 1fr)"
    )
    assert_declares(".r-horizon-track", "position: relative", "height: 44px")
    assert_declares(".r-now-line", "position: absolute", "width: 2px")
    assert_declares(".r-horizon-event", "position: absolute", "border-radius: 50%")
    assert 'className="r-tomorrow-line"' in JSX
    assert 'className={`r-horizon-event ${event.kind} ${run.gate || ""}`}' in JSX


@pytest.mark.parametrize("viewport_width", [1374, 1920])
def test_rendered_horizon_uses_available_width_and_equal_day_halves(
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
<section class="r-horizon-strip"><header><span>Today</span><span>Tomorrow</span></header>
<div class="r-horizon-track"><i class="r-tomorrow-line" style="left:50%"></i><i class="r-now-line" style="left:25%"></i><a class="r-horizon-event completed" style="left:12.5%"></a></div>
<footer><span><i class="completed"></i> completed</span></footer></section>
</section></div></div></div></div></div></div></body></html>"""
    metrics = run_browser_probe(
        tmp_path,
        browser,
        document,
        """(() => {
          const surface = document.querySelector('.r-sprint-surface');
          const overview = document.querySelector('.r-sprint-overview');
          const track = document.querySelector('.r-horizon-track');
          const division = document.querySelector('.r-tomorrow-line');
          const now = document.querySelector('.r-now-line');
          const trackRect = track.getBoundingClientRect();
          return {
            surfaceWidth: surface.clientWidth, overviewWidth: overview.clientWidth,
            trackWidth: track.clientWidth, viewportWidth: innerWidth,
            divisionOffset: division.getBoundingClientRect().left - trackRect.left,
            nowOffset: now.getBoundingClientRect().left - trackRect.left,
          };
        })()""",
        viewport=(viewport_width, 900),
        ready_expression="Boolean(document.querySelector('.r-now-line'))",
    )

    assert metrics["overviewWidth"] == metrics["surfaceWidth"] - 52, metrics
    assert metrics["divisionOffset"] == pytest.approx(metrics["trackWidth"] / 2, abs=1)
    assert metrics["nowOffset"] == pytest.approx(metrics["trackWidth"] / 4, abs=1)
    if viewport_width == 1920:
        assert metrics["surfaceWidth"] > 1216, metrics
        assert metrics["trackWidth"] > 1216, metrics


def test_undated_sprint_is_never_filtered_from_the_surface() -> None:
    result = spa_module_eval.evaluate_jsx_module(
        ROOT / "docs/ui/sprint.jsx",
        "sprintStateRows([{id: 'undated', status: 'active', starts: '', ends: '', metrics: {}}], '2026-09-01').map(row => row.sprint.id)",
    )

    assert result == ["undated"]
    assert "stateRows.map(row =>" in JSX


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
