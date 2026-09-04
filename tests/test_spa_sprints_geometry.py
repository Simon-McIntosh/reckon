import re
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    BrowserProbeError,
    ServedSpa,
    file_spa,
    installed_browser,
)

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
    for retired in (
        '"4w"',
        '"8w"',
        '"6m"',
        "SPRINT_HORIZONS",
        "sprintAxis",
        "HORIZON_HOURS",
        "horizonStrip",
    ):
        assert retired not in JSX
    assert "r-sprint-mark" not in JSX
    assert "r-sprint-mark" not in CSS
    assert "r-horizon-strip" not in JSX
    assert "r-horizon-strip" not in CSS
    assert "r-completed-work" not in JSX
    assert "r-completed-work" not in CSS


def test_derived_state_table_row_is_source_of_state_not_the_file() -> None:
    assert "derivedSprintState(" not in JSX
    assert 'const state = sprint.derived_state || "unknown";' in JSX
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
    assert_declares(".r-sprint-dag-column-label", "position: absolute", "top: 0")
    assert_declares(
        ".r-sprint-dag-card strong",
        "max-height: 2.5em",
        "white-space: normal",
    )
    assert "window.ReckonGraph" in JSX
    assert "sprintDagPlans(" in JSX


def test_sprint_detail_cards_render_the_layout_values_and_depth_labels(
    tmp_path: Path,
) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip("an installed browser is required for rendered geometry")

    inventory = [
        {
            "slug": "root",
            "title": "Root",
            "status": "shipped",
            "impl": 1.0,
            "effort_hours": 2,
            "depends_on": [],
            "decisions": [],
        },
        {
            "slug": "working",
            "title": "Working",
            "status": "active",
            "impl": 0.64,
            "effort_hours": 5,
            "depends_on": [],
            "decisions": [],
        },
        {
            "slug": "blocked",
            "title": "Carry all computed card values forward",
            "status": "blocked",
            "impl": 0.37,
            "effort_hours": 7,
            "depends_on": ["root"],
            "decisions": [],
        },
    ]
    sprint = {
        "id": "focus",
        "theme": "Rendered detail",
        "status": "active",
        "derived_state": "active",
        "implementation_pct": 67,
        "blocked": 1,
        "items": [{"slug": plan["slug"]} for plan in inventory],
    }
    state = {
        "project": "reckon",
        "today": "2026-09-04",
        "loaded_at": "2026-09-04T12:00:00Z",
        "source_format": "distributed",
        "active_sprint_id": "focus",
        "active_sprints": [sprint],
        "active_sprint_conflict": False,
        "inventory": inventory,
        "plans": {plan["slug"]: plan for plan in inventory},
        "projects": [{"project": "reckon", "plans_count": len(inventory)}],
        "sprints": [sprint],
        "milestones": [],
        "blockers": [],
        "timeline": [],
        "north_stars": [],
        "ready_set": {"ready": []},
    }

    probe = r"""(async () => {
      const detailLink = document.querySelector('.r-sprint-table a[href="#sprint/focus"]');
      detailLink.click();
      for (let attempt = 0; attempt < 40; attempt += 1) {
        if (document.querySelectorAll('.r-sprint-dag-card').length === window.STATE.inventory.length) break;
        await new Promise(resolve => setTimeout(resolve, 25));
      }
      const layout = window.ReckonGraph.layout(window.STATE.inventory, 'expected');
      const expected = Object.fromEntries(layout.nodes.map(node => [node.slug, node]));
      const cards = [...document.querySelectorAll('.r-sprint-dag-card')].map(card => {
        const slug = card.dataset.planSlug;
        const node = expected[slug];
        const track = card.querySelector('.r-sprint-dag-progress');
        const fill = track.querySelector('b');
        return {
          slug,
          hours: card.querySelector('.r-sprint-dag-meta span:last-child').textContent.trim(),
          expectedHours: node.hours,
          percent: 100 * fill.getBoundingClientRect().width / track.getBoundingClientRect().width,
          expectedPercent: node.percent,
          border: getComputedStyle(card).borderTopColor,
          background: getComputedStyle(card).backgroundColor,
          blocked: node.blocked,
        };
      });
      const tokenProbe = document.createElement('span');
      tokenProbe.style.cssText = 'position:absolute;border:1px solid var(--bad);background:var(--bg)';
      document.body.appendChild(tokenProbe);
      const tokenStyle = getComputedStyle(tokenProbe);
      const bad = tokenStyle.borderTopColor;
      const ground = tokenStyle.backgroundColor;
      tokenProbe.remove();
      const singleTitle = document.querySelector('[data-plan-slug="root"] strong');
      const wrappedTitle = document.querySelector('[data-plan-slug="blocked"] strong');
      return {
        cards,
        labels: [...document.querySelectorAll('.r-sprint-dag-column-label')]
          .map(label => label.textContent.trim()),
        expectedLabels: layout.columns.map(column => column.label),
        bad,
        ground,
        singleTitleHeight: singleTitle.getBoundingClientRect().height,
        wrappedTitleHeight: wrappedTitle.getBoundingClientRect().height,
        singleTitleLineHeight: parseFloat(getComputedStyle(singleTitle).lineHeight),
      };
    })()"""

    with file_spa(
        tmp_path,
        browser,
        state,
        project="reckon",
        route="#sprint/focus",
    ) as context:
        result = context.run_probe(
            probe,
            viewport=(1374, 900),
            ready_expression='Boolean(document.querySelector(".r-sprint-table"))',
        )

    assert len(result["cards"]) == len(inventory)
    for card in result["cards"]:
        assert card["hours"] == card["expectedHours"], card
        assert card["percent"] == pytest.approx(card["expectedPercent"], abs=0.75), card
        assert (card["border"] == result["bad"]) is card["blocked"], card
        assert card["background"] == result["ground"], card
    assert (
        result["labels"]
        == result["expectedLabels"]
        == [
            "no prerequisites",
            "depth 1",
        ]
    )
    assert result["singleTitleHeight"] == pytest.approx(
        result["singleTitleLineHeight"], abs=1
    )
    assert result["wrappedTitleHeight"] == pytest.approx(
        2 * result["singleTitleLineHeight"], abs=1
    )


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
