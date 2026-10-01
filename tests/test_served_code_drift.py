"""The server reports when the code on disk has moved past the code it runs.

The served process imports reckon's Python once, at start, while the client is
compiled per request from the working tree. A process left running across
landed changes serves current client code against an older server, and the
only symptom is a client falling back past a route the old server lacks.
These tests pin the comparison the server makes between the source it started
with and the source on disk, and pin that only source the server has actually
run counts: most commits to the package change code the server never executes.
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
    assert "1 package file changed since it started at" in report["summary"]
    assert "(serve.py)" in report["summary"]


def test_added_and_removed_modules_are_reported(tmp_path: Path):
    package = _package(tmp_path)
    snapshot = served_code.take_snapshot(package)

    (package / "sub" / "helper.py").unlink()
    (package / "new_route.py").write_text("X = 1\n")
    report = served_code.drift(snapshot)

    assert report["stale"] is True
    assert report["added"] == ["new_route.py"]
    assert report["removed"] == ["sub/helper.py"]
    assert "2 package files changed" in report["summary"]


def test_a_change_to_code_the_server_never_ran_is_not_drift(tmp_path: Path):
    package = _package(tmp_path)
    snapshot = served_code.take_snapshot(package)

    (package / "sub" / "helper.py").write_text("def helper():\n    return 2\n")
    _bump_mtime(package / "sub" / "helper.py")
    (package / "new_route.py").write_text("X = 1\n")
    report = served_code.drift(snapshot, executed={"serve.py"})

    assert report["stale"] is False
    assert report["scope"] == "executed"
    assert report["summary"] is None


def test_a_change_to_code_the_server_ran_names_the_file(tmp_path: Path):
    package = _package(tmp_path)
    snapshot = served_code.take_snapshot(package)

    (package / "serve.py").write_text("ROUTES = ['/a', '/_index']\n")
    _bump_mtime(package / "serve.py")
    (package / "sub" / "helper.py").write_text("def helper():\n    return 2\n")
    _bump_mtime(package / "sub" / "helper.py")
    report = served_code.drift(snapshot, executed={"serve.py"})

    assert report["stale"] is True
    assert report["changed"] == ["serve.py"]
    assert "code it runs changed in 1 file since it started at" in report["summary"]
    assert "(serve.py)" in report["summary"]
    assert "helper" not in report["summary"]


def test_a_removed_module_counts_only_when_the_server_ran_it(tmp_path: Path):
    package = _package(tmp_path)
    snapshot = served_code.take_snapshot(package)

    (package / "sub" / "helper.py").unlink()

    assert served_code.drift(snapshot, executed={"serve.py"})["stale"] is False
    ran_it = served_code.drift(snapshot, executed={"serve.py", "sub/helper.py"})
    assert ran_it["removed"] == ["sub/helper.py"]


def test_the_tracker_records_package_code_run_on_any_thread(tmp_path: Path):
    import importlib
    import sys

    package = _package(tmp_path)
    (package / "sub" / "__init__.py").write_text("")
    (package / "sub" / "worker.py").write_text("def work():\n    return 'done'\n")
    sys.path.insert(0, str(tmp_path))
    tracker = served_code.ExecutedSource(package)
    try:
        assert tracker.start() is True
        worker = importlib.import_module("pkg.sub.worker")
        thread = threading.Thread(target=worker.work)
        thread.start()
        thread.join()
        recorded = tracker.relative_paths()
    finally:
        tracker.stop()
        sys.path.remove(str(tmp_path))
        for name in [n for n in sys.modules if n == "pkg" or n.startswith("pkg.")]:
            del sys.modules[name]

    assert "sub/worker.py" in recorded
    assert "sub/helper.py" not in recorded
    assert not any(path.startswith("..") for path in recorded)


def test_a_stopped_tracker_releases_its_monitoring_tool(tmp_path: Path):
    import sys

    package = _package(tmp_path)
    tracker = served_code.ExecutedSource(package)
    assert tracker.start() is True
    tool = tracker.tool
    tracker.stop()

    assert sys.monitoring.get_tool(tool) is None


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


class _Ran:
    """A stand-in tracker: the server has run exactly these files."""

    def __init__(self, *paths: str) -> None:
        self.paths = set(paths)

    def relative_paths(self) -> set[str]:
        return set(self.paths)


@pytest.fixture()
def recorded_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = _package(tmp_path)
    monkeypatch.setattr(serve, "_SOURCE_SNAPSHOT", served_code.take_snapshot(package))
    monkeypatch.setattr(serve, "_EXECUTED_SOURCE", _Ran("serve.py"))
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


def test_server_route_ignores_a_change_to_code_it_never_ran(recorded_snapshot: Path):
    helper = recorded_snapshot / "sub" / "helper.py"
    helper.write_text("def helper():\n    return 2\n")
    _bump_mtime(helper)

    with _served() as port:
        status, body = _get(port, "/_server")

    assert status == 200
    assert body["code"]["stale"] is False
    assert body["code"]["scope"] == "executed"


def test_main_releases_the_served_process_state_when_serving_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A caller that runs main() in a thread and then stops it must get the
    # library defaults back: a module still acting as the served process would
    # let a later handler start real refresh children and keep a monitoring
    # tool registered for the rest of the interpreter.
    import socket
    import sys
    import time

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text("{}")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(serve, "start_fleet_change_watch", lambda mounts: None)
    servers: list = []

    class _Recording(serve.ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(serve, "ThreadingHTTPServer", _Recording)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    thread = threading.Thread(
        target=serve.main,
        kwargs={"port": port, "host": "127.0.0.1", "mounts_file": mounts_file},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not servers and time.monotonic() < deadline:
        time.sleep(0.05)
    assert servers, "main() never bound its server"
    while serve._EXECUTED_SOURCE is None and time.monotonic() < deadline:
        time.sleep(0.05)
    tool = serve._EXECUTED_SOURCE.tool if serve._EXECUTED_SOURCE else None
    assert serve._CHECK_REFRESH_ENABLED is True

    servers[0].shutdown()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert serve._CHECK_REFRESH_ENABLED is False
    assert serve._EXECUTED_SOURCE is None
    assert tool is None or sys.monitoring.get_tool(tool) is None


def _modules(root: Path) -> Path:
    """A package whose server module reads a constant and a class from others."""

    package = root / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "states.py").write_text(
        "BASE = frozenset({'done'})\n"
        "TERMINAL = BASE | {'archived'}\n"
        "\n"
        "\n"
        "def unused_helper():\n"
        "    return 1\n"
    )
    (package / "kinds.py").write_text(
        "class Hold(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def dispatch():\n"
        "    return 'dispatched'\n"
    )
    (package / "serve.py").write_text(
        "from pkg.states import TERMINAL\n"
        "from pkg import kinds\n"
        "\n"
        "LIMIT = 3\n"
        "\n"
        "\n"
        "def route(status):\n"
        '    """Answer one request."""\n'
        "    try:\n"
        "        return status in TERMINAL and LIMIT\n"
        "    except kinds.Hold:\n"
        "        return None\n"
        "\n"
        "\n"
        "def never_called():\n"
        "    return 0\n"
    )
    return package


def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))
    _bump_mtime(path)


_RAN = {"serve.py": {"route"}}


@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        ("serve.py", "and LIMIT", "and LIMIT + 1"),
        ("serve.py", "LIMIT = 3", "LIMIT = 4"),
        ("states.py", "frozenset({'done'})", "frozenset({'done', 'shipped'})"),
        ("kinds.py", "    pass", "    code = 7"),
    ],
    ids=[
        "the-function-it-ran",
        "a-constant-it-reads",
        "a-constant-behind-an-import",
        "a-class-it-catches",
    ],
)
def test_a_change_to_what_the_server_runs_or_reads_is_drift(
    tmp_path: Path, relative: str, old: str, new: str
):
    package = _modules(tmp_path)
    snapshot = served_code.take_snapshot(package)

    _rewrite(package / relative, old, new)
    report = served_code.drift(snapshot, executed=_RAN)

    assert report["stale"] is True, report
    assert report["changed"] == [relative]


@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        ("serve.py", "return 0", "return 1"),
        ("states.py", "return 1", "return 2"),
        ("kinds.py", "'dispatched'", "'sent'"),
        ("serve.py", '"""Answer one request."""', '"""Answer one request, quickly."""'),
        ("serve.py", "def route(status):", "# a comment\n\n\ndef route(status):"),
    ],
    ids=[
        "a-function-it-never-ran",
        "an-unused-helper-beside-a-constant",
        "a-function-beside-a-class",
        "a-docstring",
        "a-comment-and-moved-lines",
    ],
)
def test_a_change_the_server_cannot_see_is_not_drift(
    tmp_path: Path, relative: str, old: str, new: str
):
    package = _modules(tmp_path)
    snapshot = served_code.take_snapshot(package)

    _rewrite(package / relative, old, new)
    report = served_code.drift(snapshot, executed=_RAN)

    assert report["stale"] is False, report


def test_a_report_names_the_definitions_that_changed(tmp_path: Path):
    package = _modules(tmp_path)
    snapshot = served_code.take_snapshot(package)

    _rewrite(
        package / "states.py", "frozenset({'done'})", "frozenset({'done', 'shipped'})"
    )
    report = served_code.drift(snapshot, executed=_RAN)

    assert report["definitions"] == ["states.py:BASE"]
    assert "(states.py)" in report["summary"]


def test_the_tracker_records_the_functions_it_saw_run(tmp_path: Path):
    import importlib
    import sys

    package = _modules(tmp_path)
    sys.path.insert(0, str(tmp_path))
    tracker = served_code.ExecutedSource(package)
    try:
        assert tracker.start() is True
        server_module = importlib.import_module("pkg.serve")
        server_module.route("done")
        recorded = tracker.definitions()
    finally:
        tracker.stop()
        sys.path.remove(str(tmp_path))
        for name in [n for n in sys.modules if n == "pkg" or n.startswith("pkg.")]:
            del sys.modules[name]

    assert "route" in recorded["serve.py"]
    assert "never_called" not in recorded["serve.py"]


def test_main_releases_the_served_process_state_when_it_cannot_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import socket
    import sys

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text("{}")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    seen_tools: list[int | None] = []
    original_start = served_code.ExecutedSource.start

    def recording_start(self):
        started = original_start(self)
        seen_tools.append(self.tool)
        return started

    monkeypatch.setattr(served_code.ExecutedSource, "start", recording_start)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        with pytest.raises(OSError, match="in use"):
            serve.main(port=port, host="127.0.0.1", mounts_file=mounts_file)

    assert serve._CHECK_REFRESH_ENABLED is False
    assert serve._EXECUTED_SOURCE is None
    assert serve._SOURCE_SNAPSHOT is None
    assert seen_tools and all(
        tool is None or sys.monitoring.get_tool(tool) is None for tool in seen_tools
    )
