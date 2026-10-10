"""A server refuses requests after its imported package changes on disk."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import serve, served_code


def _request(port: int, path: str) -> tuple[int, bytes]:
    status, body, _headers = _request_details(port, path)
    return status, body


def _request_details(port: int, path: str) -> tuple[int, bytes, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


class _Executed:
    def __init__(self, definitions: dict[str, set[str]]) -> None:
        self._definitions = definitions

    def definitions(self) -> dict[str, set[str]]:
        return self._definitions


@contextmanager
def _listening(
    server: serve.ThreadingHTTPServer, *, reload: serve._CodeReload | None = None
):
    def run() -> None:
        server.serve_forever()
        if reload is not None:
            reload.finish()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        yield server.server_port
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "reckon"
    package.mkdir()
    (package / "active.py").write_text("def active():\n    return 1\n")
    (package / "unused.py").write_text("def unused():\n    return 1\n")
    monkeypatch.setattr(serve, "_SOURCE_SNAPSHOT", served_code.take_snapshot(package))
    monkeypatch.setattr(serve, "_EXECUTED_SOURCE", _Executed({"active.py": {"active"}}))
    monkeypatch.setattr(serve, "_SERVER_CODE_STAMP", "running", raising=False)
    served_code.forget_reports()
    return package


def test_unexecuted_module_change_keeps_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _snapshot(tmp_path, monkeypatch)
    (package / "unused.py").write_text("def unused():\n    return 2\n")
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    with _listening(server) as port:
        status, _body = _request(port, "/favicon.ico")
    assert status == 204


def test_executed_module_change_reexecs_after_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _snapshot(tmp_path, monkeypatch)
    (package / "active.py").write_text("def active():\n    return 2\n")
    calls: list[tuple[str, list[str]]] = []
    invoked = threading.Event()

    def record_exec(program: str, argv: list[str]) -> None:
        calls.append((program, argv))
        invoked.set()

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    reload = serve._CodeReload(server, exec_=record_exec)
    with _listening(server, reload=reload) as port:
        status, body, headers = _request_details(port, "/favicon.ico")
        assert status == 503
        payload = json.loads(body)
        assert payload["error"] == "stale-code"
        assert payload["running_code_stamp"] != payload["disk_code_stamp"]
        assert payload["changed_files"] == ["active.py"]
        assert headers["Retry-After"] == "5"
        assert invoked.wait(5)
        assert server.socket.fileno() == -1
    assert calls == [(sys.executable, [sys.executable, *sys.orig_argv[1:]])]


def test_requests_inside_report_window_compare_package_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _snapshot(tmp_path, monkeypatch)
    compare = served_code._compare
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return compare(*args, **kwargs)

    monkeypatch.setattr(served_code, "_compare", counted)
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    with _listening(server) as port:
        assert [_request(port, "/favicon.ico")[0] for _ in range(6)] == [204] * 6
    assert calls == 1


def test_server_refuses_a_lazy_import_from_new_source(tmp_path: Path) -> None:
    package = tmp_path / "reckon"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "reckon",
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"sample": str(docs)}))

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    script = (
        "import sys\n"
        "import reckon.crew.recovery as recovery\n"
        "from reckon import serve\n"
        "assert 'reckon.roadmap' not in sys.modules\n"
        "print(f'module={serve.__file__}', flush=True)\n"
        "original_start = serve.start_fleet_change_watch\n"
        "def start_watch(mounts):\n"
        "    recovery.local_liveness({})\n"
        "    print('executed=local_liveness', flush=True)\n"
        "    return original_start(mounts)\n"
        "serve.start_fleet_change_watch = start_watch\n"
        "from pathlib import Path\n"
        f"serve.main(port={port}, host='127.0.0.1', mounts_file=Path({str(mounts)!r}))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(tmp_path),
        "RECKON_HOME": str(tmp_path / "home"),
    }
    output = tmp_path / "server.log"
    with output.open("wb") as stream:
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise AssertionError(output.read_text())
                if "executed=local_liveness" in output.read_text():
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            break
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    raise AssertionError(output.read_text())
                time.sleep(0.05)
            assert f"module={package / 'serve.py'}" in output.read_text()

            # recovery is already in memory. The new roadmap imports a name
            # that appears only in the replacement source on disk.
            recovery = package / "crew" / "recovery_liveness.py"
            source = recovery.read_text()
            needle = "    return alive, proven\n\n\ndef live_worker_pid"
            assert source.count(needle) == 1
            recovery.write_text(
                source.replace(
                    needle, "    return alive, bool(proven)\n\n\ndef live_worker_pid"
                )
                + "\nstale_code_probe = True\n"
            )
            with (package / "roadmap.py").open("a") as source:
                source.write("\nfrom reckon.crew.recovery_liveness import stale_code_probe\n")

            status, body, headers = _request_details(port, "/_discover/sample")
            payload = json.loads(body)
            assert status == 503, (status, payload)
            assert payload["error"] == "stale-code"
            assert payload["running_code_stamp"] != payload["disk_code_stamp"]
            assert "crew/recovery_liveness.py" in payload["changed_files"]
            assert headers["Retry-After"] == "5"
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
