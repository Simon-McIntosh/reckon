"""Stored results follow the definitions run by their refresh child."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import route_results, serve, served_code
from tests.test_the_slow_routes_answer_from_stored_results import _project


def _rewrite_function(path: Path, name: str) -> None:
    source = path.read_text(encoding="utf-8")
    functions = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(functions) == 1
    lines = source.splitlines(keepends=True)
    lines.insert(functions[0].body[0].end_lineno, "    marker = 'changed'\n")
    path.write_text("".join(lines), encoding="utf-8")


def test_refresh_result_tracks_only_executed_definitions(tmp_path, monkeypatch):
    package = tmp_path / "reckon"
    shutil.copytree(Path(route_results.__file__).parent, package)
    docs = _project(tmp_path, "sample", ("alpha",))
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"sample": str(docs)}), encoding="utf-8")
    home = tmp_path / "home"
    env = dict(os.environ, PYTHONPATH=str(tmp_path), RECKON_HOME=str(home))
    command = [
        sys.executable,
        "-m",
        "reckon.route_results",
        "--key",
        "discover:sample",
        "--mounts",
        str(mounts),
    ]
    subprocess.run(command, env=env, cwd=tmp_path, check=True, capture_output=True)

    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts)
    monkeypatch.setattr(
        serve, "_SOURCE_SNAPSHOT", SimpleNamespace(revision="new-process")
    )
    monkeypatch.setattr(served_code, "package_root", lambda: package)
    monkeypatch.setattr(route_results, "_REFRESH_ENABLED", True)
    spawned = []
    monkeypatch.setattr(
        route_results, "_start_refresh", lambda key: spawned.append(key) or True
    )
    key = ("discover", "sample")
    stored = route_results._refresh_result(key)
    assert stored is not None
    payload = json.loads(route_results._refresh_store_path(key).read_text())
    assert stored[0] == payload["result"]
    assert "route_results.py" in payload["source_fingerprints"]
    assert (
        "_compute_refresh"
        in payload["source_fingerprints"]["route_results.py"]["definitions"]
    )

    source = package / "route_results.py"
    _rewrite_function(source, "velocity_key")
    original_read = Path.read_text
    source_reads = []

    def counted_read(path, *args, **kwargs):
        if path == source:
            source_reads.append(path)
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as count:
        count.setattr(Path, "read_text", counted_read)
        assert route_results._stored_discovery("sample", docs) is not None
        assert route_results._stored_discovery("sample", docs) is not None
    assert len(source_reads) == 1
    assert spawned == []

    unrelated = package / "served_code.py"
    assert (
        "code_fingerprints"
        not in payload["source_fingerprints"]["served_code.py"]["definitions"]
    )
    _rewrite_function(unrelated, "code_fingerprints")
    assert route_results._stored_discovery("sample", docs) is not None
    assert spawned == []

    _rewrite_function(source, "_compute_refresh")
    assert route_results._stored_discovery("sample", docs) is None
    assert spawned == [key]


def test_empty_fingerprints_are_a_miss(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", tmp_path / "mounts.json")
    monkeypatch.setattr(serve, "_SOURCE_SNAPSHOT", SimpleNamespace(revision="same"))
    monkeypatch.setattr(route_results, "_REFRESH_ENABLED", True)
    key = ("discover", "sample")
    route_results._store_refresh_result(
        key, {"marker": "unverifiable"}, source_fingerprints={}
    )
    started = []
    monkeypatch.setattr(
        route_results, "_start_refresh", lambda target: started.append(target) or True
    )
    with monkeypatch.context() as bypass:
        bypass.setattr(served_code, "definitions_unchanged", lambda *_: True)
        assert route_results._stored_discovery("sample", tmp_path) is None
    assert started == [key]
    assert not served_code.definitions_unchanged(tmp_path, {})


@pytest.mark.parametrize(
    "fingerprints",
    ["invalid", {"route_results.py": {"revision": [0] * 5, "definitions": {}}}],
)
def test_malformed_fingerprints_are_a_miss(tmp_path, monkeypatch, fingerprints):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", tmp_path / "mounts.json")
    monkeypatch.setattr(serve, "_SOURCE_SNAPSHOT", SimpleNamespace(revision="same"))
    key = ("discover", "sample")
    route_results._store_refresh_result(
        key, {"marker": "unverifiable"}, source_fingerprints=fingerprints
    )
    assert route_results._refresh_result(key) is None


def test_refresh_child_skips_unverifiable_result(tmp_path, monkeypatch):
    mounts = tmp_path / "mounts.json"
    mounts.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setattr(route_results, "_compute_refresh", lambda _key: {"ok": True})
    monkeypatch.setattr(served_code, "definition_fingerprints", lambda *_: {})
    paths = (serve._MOUNTS_FILE, serve._STATE_ROOT, serve._SHARED_ROOT)
    try:
        with pytest.raises(RuntimeError, match="no package definitions"):
            route_results._refresh_main(
                ["--key", "discover:sample", "--mounts", str(mounts)]
            )
        assert not route_results._refresh_store_path(("discover", "sample")).exists()
    finally:
        serve._MOUNTS_FILE, serve._STATE_ROOT, serve._SHARED_ROOT = paths
