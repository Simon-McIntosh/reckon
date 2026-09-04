from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from reckon.figures import figure_rows
from tests.spa_browser_harness import file_spa, installed_browser_or_skip


@pytest.fixture(scope="module")
def rendered_browser() -> str:
    return installed_browser_or_skip()


def _svg_data_url(width: int = 1920, height: int = 1080) -> str:
    image = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="#dce6f2"/>'
        "</svg>"
    )
    encoded = base64.b64encode(image.encode()).decode()
    return f"data:image/svg+xml;base64,{encoded}"


def _figure_state(caption: str) -> dict[str, object]:
    figure = {
        "nav_key": "figure:work/capture.svg",
        "slug": "work/capture.svg",
        "href": _svg_data_url(),
        "title": "Reader capture",
        "caption": caption,
        "type": "figure",
        "status": "done",
        "for_plan": "work",
        "dims": "1920 \u00d7 1080",
    }
    plan = {
        "slug": "work",
        "title": "Work plan",
        "type": "plan",
        "status": "active",
        "sprint": "current",
    }
    inventory = [plan, figure]
    return {
        "project": "reckon",
        "projects": [{"project": "reckon", "plans_count": len(inventory)}],
        "inventory": inventory,
        "plans": {item.get("nav_key", item["slug"]): item for item in inventory},
        "sprints": [{"id": "current", "status": "active", "items": ["work"]}],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "attachment_relations": [],
    }


def test_figure_rows_take_caption_only_from_the_capture_index(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    figures = docs / "figures" / "work"
    figures.mkdir(parents=True)
    for name in ("described.svg", "undescribed.svg"):
        (figures / name).write_text('<svg viewBox="0 0 1920 1080"></svg>')
    (figures / "capture-index.json").write_text(
        json.dumps(
            {
                "captures": [
                    {
                        "capture": "described",
                        "image": "described.svg",
                        "description": "Control layout at reading width",
                    },
                    {"capture": "undescribed", "image": "undescribed.svg"},
                ]
            }
        )
    )

    rows = {row["slug"]: row for row in figure_rows(docs, "sample", set())}

    assert rows["work/described.svg"]["caption"] == ("Control layout at reading width")
    assert rows["work/undescribed.svg"]["caption"] == ""
    assert rows["work/undescribed.svg"]["caption"] != "undescribed"


def test_reader_renders_only_recorded_captions(tmp_path: Path, rendered_browser: str):
    probe = """(() => ({
      captionCount: document.querySelectorAll('.r-reader-figure-caption').length,
      caption: document.querySelector('.r-reader-figure-caption')?.textContent.trim() || '',
      sourcePath: document.querySelector('.r-reader-figure figcaption code')?.textContent.trim() || '',
    }))()"""
    measurements = []
    for caption in ("Control layout at reading width", ""):
        with file_spa(
            tmp_path,
            rendered_browser,
            _figure_state(caption),
            route="#figure/work%2Fcapture.svg",
        ) as spa:
            measurements.append(
                spa.run_probe(
                    probe,
                    ready_expression=(
                        "Boolean(document.querySelector('.r-reader-figure img')?.complete)"
                    ),
                )
            )

    assert measurements == [
        {
            "captionCount": 1,
            "caption": "Control layout at reading width",
            "sourcePath": "docs/figures/work/capture.svg",
        },
        {
            "captionCount": 0,
            "caption": "",
            "sourcePath": "docs/figures/work/capture.svg",
        },
    ]


def test_figure_zooms_pans_inside_its_viewport_and_resets(
    tmp_path: Path, rendered_browser: str
) -> None:
    probe = """(async () => {
      const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
      const settle = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const viewport = document.querySelector('.r-reader-figure-viewport');
      const image = viewport.querySelector('img');
      const initialWidth = image.getBoundingClientRect().width;
      image.click();
      await settle();
      const naturalWidth = image.getBoundingClientRect().width;
      document.querySelector('.r-figure-zoom-in').click();
      await settle();
      const furtherWidth = image.getBoundingClientRect().width;
      const beforePan = { left: viewport.scrollLeft, top: viewport.scrollTop };
      const rect = viewport.getBoundingClientRect();
      viewport.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, pointerId: 7, clientX: rect.left + 300, clientY: rect.top + 250,
      }));
      viewport.dispatchEvent(new PointerEvent('pointermove', {
        bubbles: true, pointerId: 7, buttons: 1, clientX: rect.left + 120, clientY: rect.top + 100,
      }));
      viewport.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true, pointerId: 7, clientX: rect.left + 120, clientY: rect.top + 100,
      }));
      await settle();
      const afterPan = { left: viewport.scrollLeft, top: viewport.scrollTop };
      const figureRect = document.querySelector('.r-reader-figure').getBoundingClientRect();
      const viewportRect = viewport.getBoundingClientRect();
      document.querySelector('.r-figure-zoom-reset').click();
      await delay(50);
      await settle();
      return {
        initialWidth,
        naturalWidth,
        furtherWidth,
        beforePan,
        afterPan,
        clipped: getComputedStyle(viewport).overflowX === 'auto',
        viewportInsideFigure: viewportRect.left >= figureRect.left
          && viewportRect.right <= figureRect.right
          && viewportRect.top >= figureRect.top
          && viewportRect.bottom <= figureRect.bottom,
        resetWidth: image.getBoundingClientRect().width,
        resetLeft: viewport.scrollLeft,
        resetTop: viewport.scrollTop,
      };
    })()"""
    with file_spa(
        tmp_path,
        rendered_browser,
        _figure_state("Control layout at reading width"),
        route="#figure/work%2Fcapture.svg",
    ) as spa:
        measurement = spa.run_probe(
            probe,
            viewport=(1374, 900),
            ready_expression=(
                "Boolean(document.querySelector('.r-reader-figure img')?.naturalWidth)"
            ),
        )

    assert measurement["naturalWidth"] > measurement["initialWidth"]
    assert measurement["furtherWidth"] > measurement["naturalWidth"]
    assert measurement["afterPan"]["left"] > measurement["beforePan"]["left"]
    assert measurement["afterPan"]["top"] > measurement["beforePan"]["top"]
    assert measurement["clipped"] is True
    assert measurement["viewportInsideFigure"] is True
    assert measurement["resetWidth"] == pytest.approx(
        measurement["initialWidth"], abs=1
    )
    assert measurement["resetLeft"] == 0
    assert measurement["resetTop"] == 0
