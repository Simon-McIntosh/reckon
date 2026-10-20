"""The served shell addresses scripts and stylesheets by their revisions."""

from __future__ import annotations

import gzip
import http.client
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from reckon import serve, shell_assets
from reckon.file_memo import file_revision


@contextmanager
def _server():
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
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _links(html: str) -> tuple[list[str], list[str]]:
    scripts = re.findall(r'<script src="(/_(?:ui|runtime)/[^"]+)"', html)
    styles = re.findall(r'<link rel="stylesheet" href="(/_(?:shared|ui)/[^"]+)"', html)
    return scripts, styles


def test_shell_assets_are_revisioned_and_revalidated(tmp_path: Path, monkeypatch):
    docs = tmp_path / "docs"
    shared = docs / "_shared"
    ui = docs / "ui"
    shared.mkdir(parents=True)
    ui.mkdir()
    (shared / "foundation.css").write_text("body { color: navy; }\n" * 100)
    (ui / "shell.css").write_text("main { color: teal; }\n" * 100)
    source = ui / "shell.jsx"
    source.write_text("window.answer = 42;\n" * 100)
    runtime = tmp_path / "react.js"
    runtime.write_bytes(b"window.React = {};\n" * 100)
    index = docs / "index.html"
    index.write_text(
        '<html><head><meta name="docs-project" content="old">'
        "<title>reckon · old</title>"
        '<link rel="stylesheet" href="/_shared/foundation.css">'
        '<link rel="stylesheet" href="/_ui/shell.css">'
        '</head><body><script src="/_runtime/react.js"></script>'
        '<script src="/_ui/shell.js"></script></body></html>'
    )
    template_render = serve._render_spa_html("sample", index_path=index)
    monkeypatch.setattr(serve, "_spa_index_path", lambda: index)
    monkeypatch.setattr(serve, "_ui_root", lambda: ui)
    monkeypatch.setattr(serve, "_SHARED_ROOT", shared)
    monkeypatch.setattr(serve, "_client_asset", lambda name: runtime)
    monkeypatch.setattr(
        serve, "compile_jsx", lambda text, filename: f"// {filename}\n{text}".encode()
    )
    monkeypatch.setattr(serve, "load_mounts", lambda: {"sample": docs})
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))

    with _server() as port:
        status, _, body = _get(port, "/sample/")
        assert status == 200
        html = body.decode()
        if os.environ.get("RECKON_TEST_SHELL_DROP_REV"):
            html = re.sub(r'(<script src="[^"?]+)\?rev=[^"&]+', r"\1", html)
        scripts, styles = _links(html)
        assert len(scripts) == 2 and len(styles) == 2
        assert all(parse_qs(urlsplit(url).query).get("rev") for url in scripts + styles)
        assert serve._render_spa_html("sample", index_path=index) == template_render

        compiled_url = next(url for url in scripts if url.startswith("/_ui/"))
        expected_revision = file_revision(source.stat())
        assert parse_qs(urlsplit(compiled_url).query)["rev"] == [expected_revision]
        status, headers, encoded = _get(port, compiled_url, {"Accept-Encoding": "gzip"})
        assert status == 200
        assert headers["Cache-Control"] == "max-age=31536000, immutable"
        assert headers["Content-Encoding"] == "gzip"
        assert gzip.decompress(encoded) == serve.compile_jsx(
            source.read_text(), filename=source.name
        )
        cached_gzip = list((tmp_path / "config" / "cache" / "gzip").glob("*.gz"))
        assert len(cached_gzip) == 1
        cached_time = cached_gzip[0].stat().st_mtime_ns
        assert _get(port, compiled_url, {"Accept-Encoding": "gzip"})[2] == encoded
        assert cached_gzip[0].stat().st_mtime_ns == cached_time
        status, style_headers, _ = _get(port, styles[0], {"Accept-Encoding": "gzip"})
        assert status == 200
        assert style_headers["Cache-Control"] == "max-age=31536000, immutable"
        assert style_headers["Content-Encoding"] == "gzip"

        runtime_url = next(url for url in scripts if url.startswith("/_runtime/"))
        status, runtime_headers, _ = _get(port, runtime_url)
        assert status == 200
        assert runtime_headers["Cache-Control"] == "max-age=31536000, immutable"
        assert runtime_headers["Vary"] == "Accept-Encoding"
        assert parse_qs(urlsplit(runtime_url).query)["rev"] == [
            serve.CLIENT_ASSETS["react.js"][1]
        ]
        status, runtime_headers, runtime_body = _get(
            port, runtime_url, {"Accept-Encoding": "gzip"}
        )
        assert status == 200
        assert runtime_headers["Content-Encoding"] == "gzip"
        assert gzip.decompress(runtime_body) == runtime.read_bytes()

        bare_url = urlsplit(compiled_url).path
        status, bare_headers, _ = _get(port, bare_url)
        assert status == 200
        assert bare_headers["Cache-Control"] == "no-cache"
        assert "ETag" in bare_headers
        assert bare_headers["Vary"] == "Accept-Encoding"
        status, _, _ = _get(port, bare_url, {"If-None-Match": bare_headers["ETag"]})
        assert status == 304

        source.write_text("window.answer = 99;\n" * 100)
        _, _, changed_shell = _get(port, "/sample/")
        changed_scripts, _ = _links(changed_shell.decode())
        changed_url = next(url for url in changed_scripts if url.startswith("/_ui/"))
        assert changed_url != compiled_url
        status, old_headers, _ = _get(port, compiled_url)
        assert status == 200
        assert old_headers["Cache-Control"] == "no-cache"


def test_authored_shell_links_resolve_to_revisioned_assets():
    root = Path(__file__).resolve().parents[1]
    html = serve._render_spa_html("sample")
    revisions = {
        name: serve.CLIENT_ASSETS[name][1] for name in ("react.js", "react-dom.js")
    }
    served = shell_assets.revisioned_html(
        html, root / "docs" / "_shared", root / "docs" / "ui", revisions
    )
    scripts, styles = _links(served)
    assert len(scripts) == 22 and len(styles) == 12
    assert all(parse_qs(urlsplit(url).query).get("rev") for url in scripts + styles)
    shell_script = next(url for url in scripts if url.startswith("/_ui/shell.js"))
    assert parse_qs(urlsplit(shell_script).query)["rev"] == [
        file_revision((root / "docs" / "ui" / "shell.jsx").stat())
    ]


def test_missing_link_keeps_its_bare_url_and_the_shell_answers(
    tmp_path: Path, monkeypatch
):
    shared = tmp_path / "_shared"
    ui = tmp_path / "ui"
    shared.mkdir()
    ui.mkdir()
    (shared / "present.css").write_text("body { color: navy; }\n")
    (ui / "present.js").write_text("window.ready = true;\n")
    index = tmp_path / "index.html"
    index.write_text(
        '<html><head><meta name="docs-project" content="old">'
        '<link rel="stylesheet" href="/_shared/missing.css">'
        '<link rel="stylesheet" href="/_shared/present.css">'
        '</head><body><script src="/_ui/missing.js"></script>'
        '<script src="/_ui/present.js"></script>'
        '<script src="/_runtime/react.js"></script></body></html>'
    )
    monkeypatch.setattr(serve, "_spa_index_path", lambda: index)
    monkeypatch.setattr(serve, "_ui_root", lambda: ui)
    monkeypatch.setattr(serve, "_SHARED_ROOT", shared)
    monkeypatch.setattr(serve, "load_mounts", lambda: {"sample": tmp_path})

    with _server() as port:
        status, _, body = _get(port, "/sample/")
        assert status == 200
        scripts, styles = _links(body.decode())
        assert len(scripts) == 3 and len(styles) == 2
        assert "/_shared/missing.css" in styles
        assert "/_ui/missing.js" in scripts
        assert all(
            parse_qs(urlsplit(url).query).get("rev")
            for url in scripts + styles
            if "missing" not in url
        )
        assert _get(port, "/_shared/missing.css")[0] == 404
        assert _get(port, "/_ui/missing.js")[0] == 404
