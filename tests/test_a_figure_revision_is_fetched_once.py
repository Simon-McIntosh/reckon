"""A figure revision is fetched once per browser.

A figure or thumbnail URL carries the file's revision. The server answers a URL
naming the file's *current* revision with ``Cache-Control: max-age=31536000,
immutable``, so the browser fetches it once and never revalidates; a revision
that no longer matches (a rewritten figure) falls back to ``no-cache``, so the
stale URL is refetched under the new one the surface builds.

The revision the page's first paint builds its URLs from is the one carried on
the row ``/_index/<project>`` serves, so the browser never has to ask for a URL
before it can name it. The row and the response are addressed by one token: the
value the index row carries is exactly what the file routes match against.

The negative control, armed with ``RECKON_TEST_DROP_FIGURE_REVISION=1``, makes
the index row drop its revision, so the first-paint case finds no revision to
build a URL from and fails.
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

from reckon import file_memo, metadata_index, serve

_PROJECT = "sample"
_NEGATIVE_CONTROL_ENV = "RECKON_TEST_DROP_FIGURE_REVISION"
_IMMUTABLE = "max-age=31536000, immutable"
ROOT = Path(__file__).resolve().parents[1]


def _revision_of(path: Path) -> str:
    """The revision token the server answers, derived from ``os.stat``.

    Computed here rather than by calling the servant's own helper, so the
    assertion fails if the helper and the served value ever disagree.
    """

    stat = os.stat(path)
    return f"{stat.st_ino:x}-{stat.st_size:x}-{stat.st_mtime_ns:x}"


def _cache_control(headers: list[tuple[str, str]]) -> list[str]:
    """Every ``Cache-Control`` value, so a duplicated header is visible."""

    return [value for name, value in headers if name.lower() == "cache-control"]


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
    """Return (status, header list, body); the list keeps a repeated header."""

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


class _Project:
    """The temporary project the figure routes serve, and its one figure."""

    def __init__(self, docs_dir: Path):
        self.docs_dir = docs_dir
        self.figure = docs_dir / "figures" / "demo" / "wide.png"
        self.relative = "figures/demo/wide.png"

    def revision(self) -> str:
        return _revision_of(self.figure)


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
    metadata_index.clear()
    yield _Project(repository / "docs")
    metadata_index.clear()


@pytest.fixture(autouse=True)
def _negative_control_when_armed(monkeypatch):
    """Arm the declared mutation: the index row drops the revision."""

    if os.environ.get(_NEGATIVE_CONTROL_ENV) != "1":
        return

    # The step that adds the revision to an index row, replaced so the row
    # carries none and the first-paint cases have no token to build a URL from.
    monkeypatch.setattr(metadata_index, "_figure_revision", lambda path: "")


def _figure_row(rows: list[dict]) -> dict:
    return next(
        row
        for row in rows
        if row.get("type") == "figure" and row.get("slug") == "demo/wide.png"
    )


def test_the_index_row_carries_the_revision_the_first_paint_uses(project, monkeypatch):
    """The row the page paints from carries the token its figure URLs name."""

    monkeypatch.setattr(serve, "_git_first_committed", lambda *a, **k: {})
    monkeypatch.setattr(serve, "_git_last_committed", lambda *a, **k: {})

    with _served() as port:
        index_status, _h, index_body = _get(port, f"/_index/{_PROJECT}")
        assert index_status == 200
        row = _figure_row(json.loads(index_body))

        assert row["revision"] == project.revision()
        assert row["revision"] != ""

        figure_url = f"{row['href']}?rev={row['revision']}"
        thumb_url = f"/_thumb{row['href']}?rev={row['revision']}"
        figure_status, figure_headers, figure_body = _get(port, figure_url)
        thumb_status, thumb_headers, _thumb_body = _get(port, thumb_url)

    assert figure_status == 200
    assert _cache_control(figure_headers) == [_IMMUTABLE]
    assert figure_body == project.figure.read_bytes()
    assert thumb_status == 200
    assert _cache_control(thumb_headers) == [_IMMUTABLE]


def test_an_index_entry_that_predates_the_field_is_refreshed(project, monkeypatch):
    """A figure entry written before the field is rebuilt, not served bare."""

    monkeypatch.setattr(serve, "_git_first_committed", lambda *a, **k: {})
    monkeypatch.setattr(serve, "_git_last_committed", lambda *a, **k: {})

    path = project.docs_dir / project.relative
    stale = {
        "slug": "demo/wide.png",
        "href": f"/{_PROJECT}/figures/demo/wide.png",
        "type": "figure",
        "title": "Wide",
        "status": "",
        "sprint": None,
        "archived": "",
        "created": 0,
        "edited": "",
        "width": 400,
        "height": 300,
    }
    assert "revision" not in stale
    metadata_index._store_persisted(
        project.docs_dir,
        _PROJECT,
        [
            {
                "path": project.relative,
                "digest": file_memo.content_digest(path),
                "fields": stale,
            }
        ],
        {},
    )
    metadata_index.clear()

    rows = serve._served_index_rows(project.docs_dir, _PROJECT)
    row = _figure_row(rows)
    assert row["revision"] == _revision_of(path)


def test_a_thumbnail_that_cannot_be_rendered_sends_one_cache_control(project):
    """A non-renderable figure falls back to its bytes under one header only."""

    broken = project.docs_dir / "figures" / "demo" / "broken.png"
    source = b"this figure cannot be decoded as an image"
    broken.write_bytes(source)

    with _served() as port:
        status, headers, body = _get(
            port,
            f"/_thumb/{_PROJECT}/figures/demo/broken.png?rev={_revision_of(broken)}",
        )

    assert status == 200
    assert _cache_control(headers) == [_IMMUTABLE]
    assert body == source


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

    row = _figure_row(result["inventory"])
    assert row["revision"] == project.revision()
    assert row["revision"] != ""


def test_a_figure_url_that_carries_its_revision_is_immutable(project):
    source = project.figure.read_bytes()
    with _served() as port:
        status, headers, body = _get(
            port, f"/{_PROJECT}/figures/demo/wide.png?rev={project.revision()}"
        )

    assert status == 200
    assert _cache_control(headers) == [_IMMUTABLE]
    assert body == source


def test_a_figure_url_without_a_revision_is_still_revalidated(project):
    with _served() as port:
        status, headers, _body = _get(port, f"/{_PROJECT}/figures/demo/wide.png")

    assert status == 200
    assert _cache_control(headers) == ["no-cache"]


def test_a_rewritten_figure_is_not_immutable_at_its_old_revision(project):
    previous = project.revision()
    with _served() as port:
        status, headers, _body = _get(
            port, f"/{_PROJECT}/figures/demo/wide.png?rev={previous}"
        )
        assert status == 200
        assert _cache_control(headers) == [_IMMUTABLE]

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
    assert _cache_control(stale_headers) == ["no-cache"]
    assert _cache_control(fresh_headers) == [_IMMUTABLE]


def test_a_thumbnail_url_that_carries_its_revision_is_immutable(project):
    with _served() as port:
        status, headers, body = _get(
            port, f"/_thumb/{_PROJECT}/figures/demo/wide.png?rev={project.revision()}"
        )

    assert status == 200
    assert _cache_control(headers) == [_IMMUTABLE]
    with Image.open(io.BytesIO(body)) as thumbnail:
        assert max(thumbnail.size) == serve.THUMB_MAX_EDGE


def test_a_thumbnail_url_without_a_revision_is_still_revalidated(project):
    with _served() as port:
        status, headers, _body = _get(port, f"/_thumb/{_PROJECT}/figures/demo/wide.png")

    assert status == 200
    assert _cache_control(headers) == ["no-cache"]


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
