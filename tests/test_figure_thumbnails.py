"""A thumbnail is a thumbnail: 200 px on the long edge, cached and revalidated.

The figure list drew every row from the figure itself, so opening the figures
of a capture-heavy project transferred each image at the size it was stored.
The server now serves a downscaled copy at ``/_thumb/<project>/<figure path>``,
rendered once and kept under the configuration home keyed by the source file's
stat identity, so a second view pays a stat rather than a decode. An SVG scales
without cost and is passed through unchanged.

The negative control, armed with ``RECKON_TEST_THUMBNAIL_SOURCE=1``, makes the
thumbnail route serve the source file: the size assertion below fails.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from reckon import serve

_PROJECT = "sample"
_NEGATIVE_CONTROL_ENV = "RECKON_TEST_THUMBNAIL_SOURCE"


def _noise_png(width: int, height: int) -> bytes:
    """A PNG whose pixels do not compress, so a downscale is a real byte saving."""

    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _solid_png(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), (10, 40, 90))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _svg_text(width: int, height: int) -> str:
    return f'<svg viewBox="0 0 {width} {height}"><rect width="10" height="10"/></svg>'


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
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class _Project:
    """The temporary project the thumbnail route serves, and its configuration home."""

    def __init__(self, docs_dir: Path, config_home: Path):
        self.docs_dir = docs_dir
        self.config_home = config_home
        self.wide_png = docs_dir / "figures" / "demo" / "wide.png"
        self.tall_png = docs_dir / "figures" / "demo" / "tall.png"
        self.plot_svg = docs_dir / "figures" / "demo" / "plot.svg"
        self.broken_png = docs_dir / "figures" / "demo" / "broken.png"

    def thumb_pngs(self) -> list[Path]:
        thumbs = self.config_home / "cache" / "thumbs"
        return sorted(thumbs.glob("*.png")) if thumbs.is_dir() else []


@pytest.fixture()
def project(tmp_path, monkeypatch) -> _Project:
    """A temporary project of figures over a temporary configuration home."""

    config_home = tmp_path / "config"
    config_home.mkdir()
    repository = tmp_path / "repository"
    figures_dir = repository / "docs" / "figures" / "demo"
    figures_dir.mkdir(parents=True)

    (figures_dir / "wide.png").write_bytes(_noise_png(2000, 1500))
    (figures_dir / "tall.png").write_bytes(_solid_png(100, 600))
    (figures_dir / "plot.svg").write_text(_svg_text(640, 480))
    # A figure Pillow cannot decode: the extension promises an image, the
    # bytes do not deliver one.
    (figures_dir / "broken.png").write_bytes(b"not a png at all\n")

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(repository / "docs")}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_CLIENT_CACHE", str(tmp_path / "client-cache"))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    return _Project(repository / "docs", config_home)


@pytest.fixture(autouse=True)
def _negative_control_when_armed(monkeypatch):
    """Arm the declared mutation: the thumbnail route serves the source file."""

    if os.environ.get(_NEGATIVE_CONTROL_ENV) != "1":
        return

    monkeypatch.setattr(
        serve, "_thumbnail_bytes", lambda source, identity: source.read_bytes()
    )


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


def test_a_figure_is_served_as_a_thumbnail_not_its_source(project):
    source_bytes = project.wide_png.read_bytes()
    with _served() as port:
        status, headers, body = _get(port, f"/_thumb/{_PROJECT}/figures/demo/wide.png")

    assert status == 200
    assert len(body) * 10 < len(source_bytes), (
        f"thumbnail is {len(body)} bytes against a {len(source_bytes)}-byte source"
    )
    with Image.open(io.BytesIO(body)) as thumbnail:
        assert max(thumbnail.size) == serve.THUMB_MAX_EDGE
        assert thumbnail.size == (200, 150)
    assert headers["Content-Type"] == "image/png"


def test_a_tall_figure_is_bounded_by_its_long_edge(project):
    with _served() as port:
        status, _headers, body = _get(port, f"/_thumb/{_PROJECT}/figures/demo/tall.png")

    assert status == 200
    with Image.open(io.BytesIO(body)) as thumbnail:
        assert thumbnail.size == (33, 200)
        assert max(thumbnail.size) == serve.THUMB_MAX_EDGE


def test_a_second_request_revalidates_by_etag_without_regenerating(
    project, render_counter
):
    with _served() as port:
        status, headers, _body = _get(port, f"/_thumb/{_PROJECT}/figures/demo/wide.png")
        assert status == 200
        assert render_counter["renders"] == 1

        status, _headers, body = _get(
            port,
            f"/_thumb/{_PROJECT}/figures/demo/wide.png",
            headers={"If-None-Match": headers["ETag"]},
        )

    assert status == 304
    assert body == b""
    assert render_counter["renders"] == 1


def test_the_thumbnail_is_cached_on_disk_and_generated_once(project, render_counter):
    with _served() as port:
        first = _get(port, f"/_thumb/{_PROJECT}/figures/demo/wide.png")
        second = _get(port, f"/_thumb/{_PROJECT}/figures/demo/wide.png")

    assert first[0] == 200 and second[0] == 200
    assert first[2] == second[2]
    assert render_counter["renders"] == 1
    cached = project.thumb_pngs()
    assert len(cached) == 1
    assert cached[0].read_bytes() == first[2]
    # The cache lives under the configuration home the fixture installed, so
    # nothing lands in the operator's real one.
    assert serve._config_home() == project.config_home.resolve()


def test_a_rewritten_source_is_thumbnailed_again(project, render_counter):
    with _served() as port:
        status, first_headers, first = _get(
            port, f"/_thumb/{_PROJECT}/figures/demo/tall.png"
        )
        assert status == 200

        previous = project.tall_png.stat().st_mtime_ns
        project.tall_png.write_bytes(_solid_png(300, 300))
        stat = project.tall_png.stat()
        os.utime(
            project.tall_png, ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous + 1))
        )

        status, second_headers, second = _get(
            port, f"/_thumb/{_PROJECT}/figures/demo/tall.png"
        )

    assert status == 200
    assert second != first
    with Image.open(io.BytesIO(second)) as thumbnail:
        assert thumbnail.size == (200, 200)
    assert render_counter["renders"] == 2
    assert second_headers["ETag"] != first_headers["ETag"]
    # One cache file per source revision: the rewrite keys a new entry.
    assert len(project.thumb_pngs()) == 2


def test_svg_sources_pass_through_unchanged(project):
    source_bytes = project.plot_svg.read_bytes()
    with _served() as port:
        status, headers, body = _get(port, f"/_thumb/{_PROJECT}/figures/demo/plot.svg")

    assert status == 200
    assert body == source_bytes
    assert headers["Content-Type"] == "image/svg+xml"


def test_a_figure_that_cannot_be_thumbnailed_serves_its_source(project):
    source_bytes = project.broken_png.read_bytes()
    with _served() as port:
        status, headers, body = _get(
            port, f"/_thumb/{_PROJECT}/figures/demo/broken.png"
        )

    assert status == 200
    assert body == source_bytes
    assert headers["Content-Type"] == "image/png"


def test_an_unknown_figure_is_not_found(project):
    with _served() as port:
        status, _headers, _body = _get(
            port, f"/_thumb/{_PROJECT}/figures/demo/no-such.png"
        )

    assert status == 404


_SHELL_PROBE = """
const fs = require('fs');
const vm = require('vm');
const React = {
  useCallback: fn => fn,
  useEffect: () => {},
  useMemo: fn => fn(),
  useRef: () => ({}),
  useState: value => [value, () => {}],
};
const window = {};
const context = vm.createContext({ React, window, console });
vm.runInContext(fs.readFileSync(__MODULE__, 'utf8'), context, { filename: 'shell-plans.js' });
console.log(vm.runInContext(`JSON.stringify({
  figure: window.ReckonShell.plans.artifactImageSrc({ href: '/sample/figures/demo/wide.png', type: 'figure' }),
  inline: window.ReckonShell.plans.artifactImageSrc({ href: 'data:image/svg+xml;base64,PHN2Zy8+', type: 'figure' }),
  absent: window.ReckonShell.plans.artifactImageSrc({}),
})`, context));
"""


def test_the_figure_list_draws_its_rows_from_the_thumbnail_url(project, tmp_path):
    """The served shell names the thumbnail route, and resolves to it."""

    with _served() as port:
        status, _headers, module = _get(port, "/_ui/shell-plans.js")

    assert status == 200
    # The row's image is the helper's output, so the probe below covers the
    # bytes a row actually draws.
    assert "src: artifactImageSrc(item)" in module.decode()

    compiled = tmp_path / "shell-plans.js"
    compiled.write_bytes(module)
    script = _SHELL_PROBE.replace("__MODULE__", json.dumps(str(compiled)))
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == {
        "figure": "/_thumb/sample/figures/demo/wide.png",
        "inline": "data:image/svg+xml;base64,PHN2Zy8+",
        "absent": "",
    }
