"""A server refuses requests after its imported package changes on disk."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


def _request(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


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
        "import reckon.crew.recovery\n"
        "from reckon import serve\n"
        "assert 'reckon.roadmap' not in sys.modules\n"
        "print(f'module={serve.__file__}', flush=True)\n"
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
                try:
                    ready_status, _ = _request(port, "/favicon.ico")
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AssertionError(output.read_text()) from None
                    time.sleep(0.05)
            assert ready_status == 204
            assert f"module={package / 'serve.py'}" in output.read_text()

            # recovery is already in memory. The new roadmap imports a name
            # that appears only in the replacement source on disk.
            with (package / "crew" / "recovery.py").open("a") as source:
                source.write("\nstale_code_probe = True\n")
            with (package / "roadmap.py").open("a") as source:
                source.write("\nfrom reckon.crew.recovery import stale_code_probe\n")

            status, body = _request(port, "/_discover/sample")
            payload = json.loads(body)
            assert status == 503, (status, payload)
            assert payload["error"] == "stale-code"
            assert payload["running_code_stamp"] != payload["disk_code_stamp"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
