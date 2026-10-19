"""A figure revision is fetched once per browser.

A figure or thumbnail URL carries the file's revision — the token the index row
already holds. The server answers a URL naming the file's *current* revision
with ``Cache-Control: max-age=31536000, immutable``, so the browser fetches it
once and never revalidates. A revision that no longer matches (a rewritten
figure) falls back to ``no-cache``, so the stale URL is refetched under the new
one the surface now builds. The list and reader image sources carry that
revision, so both sides agree on the URL.

The negative control, armed with ``RECKON_TEST_DROP_FIGURE_REVISION=1``, makes
the server ignore the URL's revision so both the figure and the thumbnail fall
back to ``no-cache``: the immutable-header assertions below fail.
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
_NEGATIVE_CONTROL_ENV = "RECKON_TEST_DROP_FIGURE_REVISION"
_IMMUTABLE = "max-age=31536000, immutable"
ROOT = Path(__file__).resolve().parents[1]


def _solid_png(width: int, height: int, colour: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (width, height), colour)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


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
    """The temporary project the figure routes serve, and its one figure."""

    def __init__(self, docs_dir: Path):
        self.docs_dir = docs_dir
        self.figure = docs_dir / "figures" / "demo" / "wide.png"

    def revision(self) -> str:
        return serve._file_revision(self.figure.stat())


@pytest.fixture()
def project(tmp_path, monkeypatch) -> _Project:
    config_home = tmp_path / "config"
    config_home.mkdir()
    repository = tmp_path / "repository"
    figures_dir = repository / "docs" / "figures" / "demo"
    figures_dir.mkdir(parents=True)
    (figures_dir / "wide.png").write_bytes(_solid_png(400, 300, (10, 40, 90)))

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(repository / "docs")}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_CLIENT_CACHE", str(tmp_path / "client-cache"))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    return _Project(repository / "docs")


@pytest.fixture(autouse=True)
def _negative_control_when_armed(monkeypatch):
    """Arm the declared mutation: the server ignores the URL's revision."""

    if os.environ.get(_NEGATIVE_CONTROL_ENV) != "1":
        return

    monkeypatch.setattr(serve, "_revision_matches", lambda requested, identity: False)


def test_the_figure_row_carries_the_revision_of_its_bytes(project, monkeypatch):
    """The surface builds its URLs from the row, so the row must carry the token."""

    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(serve.subprocess, "run", run)
    serve._DISC_CACHE.clear()
    try:
        result = serve.discover_plans(project.docs_dir, _PROJECT, None)
    finally:
        serve._DISC_CACHE.clear()

    row = next(
        item
        for item in result["inventory"]
        if item.get("type") == "figure" and item.get("slug") == "demo/wide.png"
    )
    assert row["revision"] == project.revision()
    assert row["revision"] != ""


def test_a_figure_url_that_carries_its_revision_is_immutable(project):
    source = project.figure.read_bytes()
    with _served() as port:
        status, headers, body = _get(
            port, f"/{_PROJECT}/figures/demo/wide.png?rev={project.revision()}"
        )

    assert status == 200
    assert headers["Cache-Control"] == _IMMUTABLE
    assert body == source


def test_a_figure_url_without_a_revision_is_still_revalidated(project):
    with _served() as port:
        status, headers, _body = _get(port, f"/{_PROJECT}/figures/demo/wide.png")

    assert status == 200
    assert headers["Cache-Control"] == "no-cache"


def test_a_rewritten_figure_is_not_immutable_at_its_old_revision(project):
    previous = project.revision()
    with _served() as port:
        status, headers, _body = _get(
            port, f"/{_PROJECT}/figures/demo/wide.png?rev={previous}"
        )
        assert status == 200
        assert headers["Cache-Control"] == _IMMUTABLE

        project.figure.write_bytes(_solid_png(400, 300, (90, 40, 10)))
        stat = project.figure.stat()
        os.utime(project.figure, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        current = project.revision()
        assert current != previous

        status, stale_headers, stale_body = _get(
            port, f"/{_PROJECT}/figures/demo/wide.png?rev={previous}"
        )
        _status, fresh_headers, _body = _get(
            port, f"/{_PROJECT}/figures/demo/wide.png?rev={current}"
        )

    assert status == 200
    assert stale_body == project.figure.read_bytes()
    assert stale_headers["Cache-Control"] == "no-cache"
    assert fresh_headers["Cache-Control"] == _IMMUTABLE


def test_a_thumbnail_url_that_carries_its_revision_is_immutable(project):
    with _served() as port:
        status, headers, body = _get(
            port, f"/_thumb/{_PROJECT}/figures/demo/wide.png?rev={project.revision()}"
        )

    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert headers["Cache-Control"] == _IMMUTABLE
    with Image.open(io.BytesIO(body)) as thumbnail:
        assert max(thumbnail.size) == serve.THUMB_MAX_EDGE


def test_a_thumbnail_url_without_a_revision_is_still_revalidated(project):
    with _served() as port:
        status, headers, _body = _get(port, f"/_thumb/{_PROJECT}/figures/demo/wide.png")

    assert status == 200
    assert headers["Cache-Control"] == "no-cache"


_SPA_PROBE = r"""
const fs = require('fs');
const vm = require('vm');
const React = {
  Fragment: Symbol('Fragment'),
  createElement: (type, props, ...children) => ({ type, props: props || {}, children }),
  useCallback: fn => fn,
  useEffect: () => {},
  useLayoutEffect: () => {},
  useMemo: fn => fn(),
  useRef: () => ({}),
  useState: value => [typeof value === 'function' ? value() : value, () => {}],
};
const window = {
  addEventListener() {},
  removeEventListener() {},
  location: { href: 'http://local/' },
};
window.window = window;
const context = vm.createContext({ React, window, console, URL });
vm.runInContext(fs.readFileSync(__SHELL__, 'utf8'), context, { filename: 'shell-plans.js' });
vm.runInContext(fs.readFileSync(__PLAN__, 'utf8'), context, { filename: 'plan.js' });
console.log(vm.runInContext(`JSON.stringify({
  thumbnail: window.ReckonShell.plans.artifactImageSrc({ href: '/sample/figures/demo/wide.png', type: 'figure', revision: 'abc123' }),
  thumbnailNoRevision: window.ReckonShell.plans.artifactImageSrc({ href: '/sample/figures/demo/wide.png', type: 'figure' }),
  inline: window.ReckonShell.plans.artifactImageSrc({ href: 'data:image/svg+xml;base64,PHN2Zy8+', type: 'figure', revision: 'abc123' }),
  reader: window.readerFigureSource({ href: '/sample/figures/demo/wide.png', revision: 'abc123' }, 'sample'),
  readerBySlug: window.readerFigureSource({ slug: 'demo/wide.png', revision: 'abc123' }, 'sample'),
  readerNoRevision: window.readerFigureSource({ href: '/sample/figures/demo/wide.png' }, 'sample'),
})`, context));
"""


def test_the_list_and_reader_sources_carry_the_revision(project, tmp_path):
    with _served() as port:
        shell_status, _h, shell_module = _get(port, "/_ui/shell-plans.js")
        plan_status, _h, plan_module = _get(port, "/_ui/plan.js")

    assert shell_status == 200
    assert plan_status == 200

    shell_path = tmp_path / "shell-plans.js"
    plan_path = tmp_path / "plan.js"
    shell_path.write_bytes(shell_module)
    plan_path.write_bytes(plan_module)

    script = _SPA_PROBE.replace("__SHELL__", json.dumps(str(shell_path))).replace(
        "__PLAN__", json.dumps(str(plan_path))
    )
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "thumbnail": "/_thumb/sample/figures/demo/wide.png?rev=abc123",
        "thumbnailNoRevision": "/_thumb/sample/figures/demo/wide.png",
        "inline": "data:image/svg+xml;base64,PHN2Zy8+",
        "reader": "/sample/figures/demo/wide.png?rev=abc123",
        "readerBySlug": "/sample/figures/demo/wide.png?rev=abc123",
        "readerNoRevision": "/sample/figures/demo/wide.png",
    }
