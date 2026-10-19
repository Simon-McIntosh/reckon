"""Text files sent by ``_send_file`` are gzipped above the size threshold.

JSON responses were already gzipped, but the full SVG figures in the reader,
the stylesheets and the scripts went out uncompressed. Gzip at level 5 shrinks
power_convertor's 257 SVGs from 28.6 MB to 7.9 MB; nova's largest figure drops
from 3.2 MB to 0.32 MB. The reader reaches the server through an SSH tunnel,
where bytes are the cost. The route now compresses a compressible text response
above ``_GZIP_MIN_BYTES`` when the client accepts gzip, and caches the
compressed bytes per revision so a file is compressed once per revision rather
than once per view.

The negative control, armed with ``RECKON_TEST_TEXT_COMPRESSION_OFF=1``,
removes the compression branch: ``_is_compressible_text`` answers False, so the
route serves the file as it did before and the content-encoding assertion
fails.
"""

from __future__ import annotations

import gzip
import http.client
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import serve

_PROJECT = "sample"
_NEGATIVE_CONTROL_ENV = "RECKON_TEST_TEXT_COMPRESSION_OFF"


def _large_svg(shapes: int = 6000) -> str:
    """An SVG whose source is well over the compression threshold."""

    rows = []
    for index in range(shapes):
        x = (index * 7) % 640
        y = (index * 13) % 480
        fill = f"#{index % 256:02x}{(index * 3) % 256:02x}{(index * 7) % 256:02x}"
        rows.append(f'<rect x="{x}" y="{y}" width="6" height="4" fill="{fill}"/>')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 480">'
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
    """The temporary project the file route serves, and its config home."""

    def __init__(self, docs_dir: Path, config_home: Path):
        self.docs_dir = docs_dir
        self.config_home = config_home
        self.plot_svg = docs_dir / "figures" / "demo" / "plot.svg"

    def gzip_cache_files(self) -> list[Path]:
        cache = self.config_home / "cache" / "gzip"
        return sorted(cache.glob("*.gz")) if cache.is_dir() else []


@pytest.fixture()
def project(tmp_path, monkeypatch) -> _Project:
    """A temporary project with one SVG figure over a temporary config home."""

    config_home = tmp_path / "config"
    config_home.mkdir()
    repository = tmp_path / "repository"
    figures_dir = repository / "docs" / "figures" / "demo"
    figures_dir.mkdir(parents=True)

    (figures_dir / "plot.svg").write_text(_large_svg())

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(repository / "docs")}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_FILE", str(mounts_file))
    monkeypatch.setenv("RECKON_CLIENT_CACHE", str(tmp_path / "client-cache"))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    return _Project(repository / "docs", config_home)


@pytest.fixture(autouse=True)
def _negative_control_when_armed(monkeypatch):
    """Arm the declared mutation: the compression branch is removed."""

    if os.environ.get(_NEGATIVE_CONTROL_ENV) != "1":
        return

    monkeypatch.setattr(serve, "_is_compressible_text", lambda _ctype: False)


@pytest.fixture()
def compress_counter(monkeypatch) -> dict[str, int]:
    """Count the gzip compressions the server performs."""

    original = gzip.compress
    counts = {"compressions": 0}

    def counted(data, *args, **kwargs):
        counts["compressions"] += 1
        return original(data, *args, **kwargs)

    monkeypatch.setattr(serve.gzip, "compress", counted)
    return counts


def _figure_path() -> str:
    return f"/{_PROJECT}/figures/demo/plot.svg"


def test_a_large_svg_is_gzipped_when_the_client_accepts_it(project):
    source_bytes = project.plot_svg.read_bytes()
    assert len(source_bytes) >= serve._GZIP_MIN_BYTES

    with _served() as port:
        status, headers, body = _get(port, _figure_path(), {"Accept-Encoding": "gzip"})

    assert status == 200
    assert headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(body) == source_bytes
    assert len(body) < len(source_bytes)


def test_the_same_file_is_sent_as_is_when_gzip_is_not_accepted(project):
    source_bytes = project.plot_svg.read_bytes()

    with _served() as port:
        status, headers, body = _get(port, _figure_path())

    assert status == 200
    assert "Content-Encoding" not in headers
    assert body == source_bytes


def test_a_matching_if_none_match_still_answers_304(project):
    with _served() as port:
        status, headers, _body = _get(port, _figure_path(), {"Accept-Encoding": "gzip"})
        assert status == 200
        etag = headers["ETag"]

        status, reread_headers, body = _get(
            port,
            _figure_path(),
            {"Accept-Encoding": "gzip", "If-None-Match": etag},
        )

    assert status == 304
    assert body == b""
    assert reread_headers["ETag"] == etag


def test_a_second_request_does_not_compress_again(project, compress_counter):
    with _served() as port:
        first = _get(port, _figure_path(), {"Accept-Encoding": "gzip"})
        second = _get(port, _figure_path(), {"Accept-Encoding": "gzip"})

    assert first[0] == 200 and second[0] == 200
    assert first[2] == second[2]
    assert compress_counter["compressions"] == 1
    cached = project.gzip_cache_files()
    assert len(cached) == 1
    assert cached[0].read_bytes() == first[2]
    assert serve._config_home() == project.config_home.resolve()


def test_a_below_threshold_text_file_is_not_compressed(project, tmp_path):
    small = project.docs_dir / "figures" / "demo" / "small.svg"
    small.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    assert small.stat().st_size < serve._GZIP_MIN_BYTES

    with _served() as port:
        status, headers, body = _get(
            port,
            f"/{_PROJECT}/figures/demo/small.svg",
            {"Accept-Encoding": "gzip"},
        )

    assert status == 200
    assert "Content-Encoding" not in headers
    assert body == small.read_bytes()
