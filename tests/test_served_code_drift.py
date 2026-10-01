"""The server reports when the code on disk has moved past the code it runs.

The served process imports reckon's Python once, at start, while the client is
compiled per request from the working tree. A process left running across
landed changes serves current client code against an older server, and the
only symptom is a client falling back past a route the old server lacks.
These tests pin the comparison the server makes between the source it started
with and the source on disk.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import serve, served_code


def _package(root: Path) -> Path:
    package = root / "pkg"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "serve.py").write_text("ROUTES = ['/a']\n")
    (package / "sub" / "helper.py").write_text("def helper():\n    return 1\n")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "serve.cpython-314.pyc").write_bytes(b"\0")
    return package


def _bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def test_unchanged_source_is_current(tmp_path: Path):
    package = _package(tmp_path)
    snapshot = served_code.take_snapshot(package)

    report = served_code.drift(snapshot)

    assert report["stale"] is False
    assert report["changed"] == report["added"] == report["removed"] == []
    assert report["summary"] is None


def test_an_edited_module_makes_the_server_stale(tmp_path: Path):
    package = _package(tmp_path)
    snapshot = served_code.take_snapshot(package)

    (package / "serve.py").write_text("ROUTES = ['/a', '/_index']\n")
    _bump_mtime(package / "serve.py")
    report = served_code.drift(snapshot)

    assert report["stale"] is True
    assert report["changed"] == ["serve.py"]
    assert "1 file changed" in report["summary"]
    assert "since it started at" in report["summary"]


def test_added_and_removed_modules_are_reported(tmp_path: Path):
    package = _package(tmp_path)
    snapshot = served_code.take_snapshot(package)

    (package / "sub" / "helper.py").unlink()
    (package / "new_route.py").write_text("X = 1\n")
    report = served_code.drift(snapshot)

    assert report["stale"] is True
    assert report["added"] == ["new_route.py"]
    assert report["removed"] == ["sub/helper.py"]
    assert "2 files changed" in report["summary"]


def test_a_touch_without_a_content_change_is_not_drift(tmp_path: Path):
    package = _package(tmp_path)
    snapshot = served_code.take_snapshot(package)

    _bump_mtime(package / "serve.py")
    report = served_code.drift(snapshot)

    assert report["stale"] is False


def test_bytecode_caches_are_not_source(tmp_path: Path):
    package = _package(tmp_path)
    snapshot = served_code.take_snapshot(package)

    (package / "__pycache__" / "serve.cpython-314.pyc").write_bytes(b"\1\2")
    assert "__pycache__/serve.cpython-314.pyc" not in snapshot.files
    assert served_code.drift(snapshot)["stale"] is False


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


def _get(port: int, path: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture()
def recorded_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = _package(tmp_path)
    monkeypatch.setattr(serve, "_SOURCE_SNAPSHOT", served_code.take_snapshot(package))
    served_code.forget_reports()
    yield package
    served_code.forget_reports()


def test_server_route_reports_current_code(recorded_snapshot: Path):
    with _served() as port:
        status, body = _get(port, "/_server")

    assert status == 200
    assert body["code"]["stale"] is False
    assert body["pid"] == os.getpid()
    assert body["host"]


def test_server_route_reports_stale_code_and_the_restart_command(
    recorded_snapshot: Path,
):
    (recorded_snapshot / "serve.py").write_text("ROUTES = ['/a', '/_index']\n")
    _bump_mtime(recorded_snapshot / "serve.py")

    with _served() as port:
        status, body = _get(port, "/_server")

    assert status == 200
    assert body["code"]["stale"] is True
    assert body["code"]["changed"] == ["serve.py"]
    assert body["code"]["restart_command"] == "reckon service restart"
