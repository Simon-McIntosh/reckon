"""Node interpreter resolution for the served client and service unit."""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path
from urllib.request import urlopen

import pytest

from reckon import serve, service


def _node_without_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    node = tmp_path / ".local" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write(sys.stdin.read())\n"
    )
    node.chmod(0o755)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv(
        "PATH", os.pathsep.join(["/nonexistent", str(tmp_path / "empty")])
    )
    monkeypatch.delenv("RECKON_NODE", raising=False)
    return node


def _compiler_module(tmp_path: Path) -> Path:
    compiler = tmp_path / "babel.js"
    compiler.write_text(
        "module.exports = { transform(source) { return { code: source }; } };\n"
    )
    return compiler


def test_compiler_resolves_node_when_the_bare_name_is_absent_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    node = _node_without_path(tmp_path, monkeypatch)
    compiler = _compiler_module(tmp_path)
    monkeypatch.setenv("RECKON_CLIENT_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(serve, "_client_asset", lambda name: compiler)

    compiled = serve.compile_jsx("window.answer = 42;", filename="answer.jsx")

    assert service.node_executable() == node
    assert b"window.answer = 42" in compiled


def test_written_unit_search_path_contains_the_node_interpreter_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    node = _node_without_path(tmp_path, monkeypatch)
    executable = tmp_path / "reckon-bin" / "reckon"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service, "server_executable", lambda: executable)

    unit_path, changed = service.write_unit()

    unit = unit_path.read_text()
    search_path = re.search(r'Environment="PATH=([^"]+)"', unit)
    assert changed
    assert search_path
    assert str(node.parent) in search_path.group(1).split(os.pathsep)


def test_all_served_jsx_modules_compile_without_node_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _node_without_path(tmp_path, monkeypatch)
    compiler = _compiler_module(tmp_path)
    monkeypatch.setenv("RECKON_CLIENT_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(serve, "_client_asset", lambda name: compiler)

    module_paths = re.findall(
        r'<script src="(/_ui/[^\"]+\.js)">', serve._render_spa_html("sample")
    )
    module_paths = [
        path
        for path in module_paths
        if (serve._ui_root() / Path(path).name).with_suffix(".jsx").is_file()
    ]
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        statuses = [
            urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=10).status
            for path in module_paths
        ]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert len(module_paths) == 10
    assert statuses == [200] * len(module_paths)
