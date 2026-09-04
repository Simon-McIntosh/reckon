import re
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.spa_browser_harness import BrowserProbeError, ServedSpa, installed_browser

ROOT = Path(__file__).parents[1]
CSS = (ROOT / "docs/ui/sprints.css").read_text()
BASE_CSS = (ROOT / "docs/ui/styles-base.css").read_text()
JSX = (ROOT / "docs/ui/sprint.jsx").read_text()


@contextmanager
def _skip_when_browser_is_unavailable():
    try:
        yield
    except BrowserProbeError as error:
        pytest.skip(f"browser unavailable ({error.classification}): {error}")


def _run_file_document_probe(
    tmp_path: Path,
    browser: str,
    document: str,
    expression: str,
    *,
    viewport: tuple[int, int],
    ready_expression: str,
) -> object:
    page = tmp_path / "rendered-geometry.html"
    page.write_text(document, encoding="utf-8")
    context = ServedSpa(browser=browser, url=page.resolve().as_uri(), tmp_path=tmp_path)
    with _skip_when_browser_is_unavailable():
        return context.run_probe(
            expression,
            viewport=viewport,
            ready_expression=ready_expression,
        )


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


def test_fixed_horizon_and_recorded_work_are_fully_retired() -> None:
    for retired in ('"4w"', '"8w"', '"6m"', "SPRINT_HORIZONS", "sprintAxis", "HORIZON_HOURS", "horizonStrip"):
        assert retired not in JSX
    assert "r-sprint-mark" not in JSX
    assert "r-sprint-mark" not in CSS
    assert "r-horizon-strip" not in JSX
    assert "r-horizon-strip" not in CSS
    assert "r-completed-work" not in JSX
    assert "r-completed-work" not in CSS


def test_derived_state_table_row_is_source_of_state_not_the_file() -> None:
    assert "derivedSprintState(" in JSX
    assert "sprintStateRows(" in JSX
    assert 'row.flag.startsWith("was ")' in JSX
    assert '<th scope="col">State</th>' in JSX
    assert '<th scope="col">Hours</th>' in JSX


def test_sprint_detail_geometry_declares_stage_and_stats() -> None:
    assert_declares(
        ".r-sprint-detail-stats",
        "display: grid",
        "grid-template-columns: repeat(6, minmax(0, 1fr))",
    )
    assert_declares(".r-sprint-dag-stage", "position: relative")
    assert_declares(".r-sprint-dag-card", "position: absolute")
    assert "window.ReckonGraph" in JSX
    assert "sprintDagPlans(" in JSX


def test_undated_sprint_is_never_filtered_from_the_table() -> None:
    assert "stateRows.map(row =>" in JSX
    assert "hidden={foldClosed && row.closed}" in JSX


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
    metrics = _run_file_document_probe(
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


def test_ready_lane_cards_use_canvas_geometry() -> None:
    assert_declares(
        ".r-ready-lane-list",
        "grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr))",
        "gap: 10px",
    )
    assert_declares(
        ".r-ready-lane",
        "display: grid",
        "min-width: 0",
        "overflow-wrap: anywhere",
        "border-radius: 8px",
        "padding: 11px 12px",
    )
    assert_declares(
        ".r-ready-lane-title a",
        "font-size: 13px",
        "font-weight: 600",
        "text-decoration: none",
    )
    assert_declares(
        ".r-ready-lane-description",
        "overflow: hidden",
        "text-overflow: ellipsis",
        "white-space: nowrap",
    )
