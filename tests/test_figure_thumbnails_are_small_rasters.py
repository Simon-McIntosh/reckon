"""An SVG figure's thumbnail is a small raster, not the whole drawing.

The thumbnail route passed an SVG through unchanged, so a 50 by 34 pixel list
icon cost the browser the full vector: nova's SVGs have a 70 KB median and a
3.2 MB maximum, and a first screen of sixty rows moved megabytes to draw icons.
The route now rasterises an SVG to a PNG bounded by ``THUMB_MAX_EDGE``, cached
per source revision exactly as a PNG source already was.

The negative control, armed with ``RECKON_TEST_THUMBNAIL_SVG_PASSTHROUGH=1``,
restores the SVG passthrough on the thumbnail route by making the rasteriser
fail, which is the route's own fallback: the served body is the SVG and the
content-type assertion fails.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from reckon import serve

_PROJECT = "sample"
_NEGATIVE_CONTROL_ENV = "RECKON_TEST_THUMBNAIL_SVG_PASSTHROUGH"


def _dense_svg(width: int, height: int, shapes: int = 3000) -> str:
    """An SVG whose source is far larger than a 200-pixel raster of it."""

    rows = []
    for index in range(shapes):
        x = (index * 7) % width
        y = (index * 13) % height
        fill = f"#{index % 256:02x}{(index * 3) % 256:02x}{(index * 7) % 256:02x}"
        rows.append(f'<rect x="{x}" y="{y}" width="6" height="4" fill="{fill}"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">'
        + "".join(rows)
        + "</svg>"
    )


@contextmanager
def _served():
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(port: int, path: str, headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class _Project:
    """The temporary project the thumbnail route serves, and its config home."""

    def __init__(self, docs_dir: Path, config_home: Path):
        self.docs_dir = docs_dir
        self.config_home = config_home
        self.plot_svg = docs_dir / "figures" / "demo" / "plot.svg"
        self.tall_svg = docs_dir / "figures" / "demo" / "tall.svg"
        self.broken_svg = docs_dir / "figures" / "demo" / "broken.svg"

    def thumb_pngs(self) -> list[Path]:
        thumbs = self.config_home / "cache" / "thumbs"
        return sorted(thumbs.glob("*.png")) if thumbs.is_dir() else []


@pytest.fixture()
def project(tmp_path, monkeypatch) -> _Project:
    """A temporary project of SVG figures over a temporary configuration home."""

    config_home = tmp_path / "config"
    config_home.mkdir()
    repository = tmp_path / "repository"
    figures_dir = repository / "docs" / "figures" / "demo"
    figures_dir.mkdir(parents=True)

    (figures_dir / "plot.svg").write_text(_dense_svg(640, 480))
    (figures_dir / "tall.svg").write_text(_dense_svg(300, 900))
    # A vector the rasteriser cannot parse: the extension promises an SVG, the
    # bytes do not deliver one.
    (figures_dir / "broken.svg").write_text("not an svg at all\n")

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(repository / "docs")}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_FILE", str(mounts_file))
    monkeypatch.setenv("RECKON_CLIENT_CACHE", str(tmp_path / "client-cache"))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    return _Project(repository / "docs", config_home)


@pytest.fixture(autouse=True)
def _negative_control_when_armed(monkeypatch):
    """Arm the declared mutation: the route serves the SVG as it did before."""

    if os.environ.get(_NEGATIVE_CONTROL_ENV) != "1":
        return

    def passthrough(_svg_text: str) -> bytes:
        raise RuntimeError("negative control: the SVG passthrough is restored")

    monkeypatch.setattr(serve, "_rasterise_svg", passthrough)


@pytest.fixture()
def render_counter(monkeypatch) -> dict[str, int]:
    """Count the thumbnail renders the server performs."""

    original = serve._render_thumbnail
    counts = {"renders": 0}

    def counted(source: Path) -> bytes:
        counts["renders"] += 1
        return original(source)

    monkeypatch.setattr(serve, "_render_thumbnail", counted)
    return counts


def test_an_svg_figure_is_served_as_a_bounded_png(project):
    source_bytes = project.plot_svg.read_bytes()
    with _served() as port:
        status, headers, body = _get(port, f"/_thumb/{_PROJECT}/figures/demo/plot.svg")

    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert len(body) < len(source_bytes), (
        f"thumbnail is {len(body)} bytes against a {len(source_bytes)}-byte source"
    )
    with Image.open(io.BytesIO(body)) as thumbnail:
        assert max(thumbnail.size) <= serve.THUMB_MAX_EDGE, thumbnail.size
        assert thumbnail.size == (200, 150), thumbnail.size


def test_a_tall_svg_is_bounded_by_its_long_edge(project):
    with _served() as port:
        status, headers, body = _get(port, f"/_thumb/{_PROJECT}/figures/demo/tall.svg")

    assert status == 200
    assert headers["Content-Type"] == "image/png"
    with Image.open(io.BytesIO(body)) as thumbnail:
        assert thumbnail.size == (67, 200), thumbnail.size
        assert max(thumbnail.size) <= serve.THUMB_MAX_EDGE


def test_a_second_request_reuses_the_cache_without_rendering_again(
    project, render_counter
):
    with _served() as port:
        first = _get(port, f"/_thumb/{_PROJECT}/figures/demo/plot.svg")
        second = _get(port, f"/_thumb/{_PROJECT}/figures/demo/plot.svg")

    assert first[0] == 200 and second[0] == 200
    assert first[2] == second[2]
    assert render_counter["renders"] == 1
    cached = project.thumb_pngs()
    assert len(cached) == 1
    assert cached[0].read_bytes() == first[2]
    assert serve._config_home() == project.config_home.resolve()


def test_a_rewritten_svg_renders_afresh(project, render_counter):
    with _served() as port:
        status, first_headers, first = _get(
            port, f"/_thumb/{_PROJECT}/figures/demo/plot.svg"
        )
        assert status == 200

        previous = project.plot_svg.stat().st_mtime_ns
        project.plot_svg.write_text(_dense_svg(480, 480))
        stat = project.plot_svg.stat()
        os.utime(
            project.plot_svg, ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous + 1))
        )

        status, second_headers, second = _get(
            port, f"/_thumb/{_PROJECT}/figures/demo/plot.svg"
        )

    assert status == 200
    assert second != first
    with Image.open(io.BytesIO(second)) as thumbnail:
        assert thumbnail.size == (200, 200)
        assert max(thumbnail.size) <= serve.THUMB_MAX_EDGE
    assert render_counter["renders"] == 2
    assert second_headers["ETag"] != first_headers["ETag"]
    assert len(project.thumb_pngs()) == 2


def test_an_svg_that_cannot_be_rasterised_falls_back_to_the_svg(project):
    source_bytes = project.broken_svg.read_bytes()
    with _served() as port:
        status, headers, body = _get(
            port, f"/_thumb/{_PROJECT}/figures/demo/broken.svg"
        )

    assert status == 200
    assert body == source_bytes
    assert headers["Content-Type"] == "image/svg+xml"


def test_the_rasteriser_is_declared_as_a_project_dependency():
    """The wheel is present wherever the server and its tests run."""

    pyproject = (
        Path(serve.__file__).resolve().parents[1] / "pyproject.toml"
    ).read_text()
    assert "resvg-py" in pyproject
    lock = (Path(serve.__file__).resolve().parents[1] / "uv.lock").read_text()
    assert 'name = "resvg-py"' in lock
    import resvg_py

    assert resvg_py.__file__
