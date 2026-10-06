from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


@contextmanager
def _figure_source(dropped: int | None) -> Iterator[tuple[str, list[str]]]:
    """Serve one SVG whose first ``dropped`` requests close without a response.

    A connection closed before any status line is what the browser sees when a
    tunnel between it and the server resets mid-flight; ``None`` drops every
    request. Yields the image URL and the list of request paths received.
    """

    image = b'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480"></svg>'
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(self.path)
            if dropped is None or len(received) <= dropped:
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(image)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(image)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/capture.svg", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _figure_state(caption: str, href: str | None = None) -> dict[str, object]:
    figure = {
        "nav_key": "figure:work/capture.svg",
        "slug": "work/capture.svg",
        "href": href or _svg_data_url(),
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


def test_figure_reloads_after_a_dropped_connection(
    tmp_path: Path, rendered_browser: str
) -> None:
    probe = """(async () => {
      const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
      const deadline = Date.now() + 10000;
      let image = document.querySelector('.r-reader-figure img');
      while (Date.now() < deadline && !(image?.naturalWidth > 0)) {
        await delay(100);
        image = document.querySelector('.r-reader-figure img');
      }
      return {
        naturalWidth: image?.naturalWidth || 0,
        notice: document.querySelector('.r-reader-figure-load')?.textContent.trim() || '',
      };
    })()"""
    with (
        _figure_source(dropped=1) as (href, received),
        file_spa(
            tmp_path,
            rendered_browser,
            _figure_state("", href=href),
            route="#figure/work%2Fcapture.svg",
        ) as spa,
    ):
        measurement = spa.run_probe(
            probe,
            ready_expression="Boolean(document.querySelector('.r-reader-figure img'))",
        )

    assert measurement == {"naturalWidth": 640, "notice": ""}
    assert received == ["/capture.svg", "/capture.svg"]


def test_figure_that_keeps_failing_says_so_and_retries_on_request(
    tmp_path: Path, rendered_browser: str
) -> None:
    probe = """(async () => {
      const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
      const deadline = Date.now() + 5000;
      while (Date.now() < deadline && !document.querySelector('.r-reader-figure-load')) {
        await delay(50);
      }
      const notice = document.querySelector('.r-reader-figure-load');
      const before = document.querySelector('.r-reader-figure img');
      notice?.querySelector('button')?.click();
      await delay(50);
      return {
        notice: notice?.textContent.trim() || '',
        button: notice?.querySelector('button')?.textContent.trim() || '',
        reloaded: document.querySelector('.r-reader-figure img') !== before,
      };
    })()"""
    with (
        _figure_source(dropped=None) as (href, received),
        file_spa(
            tmp_path,
            rendered_browser,
            _figure_state("", href=href),
            route="#figure/work%2Fcapture.svg",
        ) as spa,
    ):
        measurement = spa.run_probe(
            probe,
            ready_expression="Boolean(document.querySelector('.r-reader-figure img'))",
        )

    assert measurement["notice"].startswith("Image did not load")
    assert measurement["button"] == "Retry"
    assert measurement["reloaded"] is True
    assert len(received) >= 2
