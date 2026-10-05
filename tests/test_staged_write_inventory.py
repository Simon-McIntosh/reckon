"""Every locally staged file replacement reaches the shared writer."""

import ast
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from reckon import interface_counts

ROOT = Path(__file__).resolve().parents[1]
CENSUS_DIR = ROOT / "docs/research/data/crew-pattern-review/code-depth"


def _inventory(trees):
    return {
        (path, name)
        for path, tree in trees.items()
        for name in interface_counts._staged_writes(interface_counts.definitions(tree))
    }


@pytest.fixture(scope="module")
def trees():
    revision = os.environ.get("RECKON_INVENTORY_REVISION")
    if revision:
        return interface_counts.read_trees(ROOT, revision)
    # Use the existing reader's module set and overlay working bytes so an
    # uncommitted private writer is caught by the same assertion as a commit.
    result = interface_counts.read_trees(ROOT, "HEAD")
    return {
        path: ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
        for path in result
    }


def test_every_staged_write_reaches_the_owner(trees):
    observed = _inventory(trees)
    print("staged-write inventory:", len(observed), sorted(observed))
    assert observed == {
        ("reckon/_store.py", "write_atomically"),
        ("reckon/resources.py", "migrate_typed_layout"),
        ("reckon/ledger.py", "_indexed_data"),
    }, sorted(observed)


def test_claim_move_aside_is_not_a_staged_write(trees):
    observed = _inventory(trees)
    assert ("reckon/_store.py", "write_atomically") in observed
    assert not {name for path, name in observed if path == "reckon/crew/dispatch.py"}


def test_runtime_inventory_includes_the_census_atomic_json_replacements(trees):
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(CENSUS_DIR))
        spec = importlib.util.spec_from_file_location(
            "staged_write_census", CENSUS_DIR / "census.py"
        )
        census = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(census)
    finally:
        sys.path[:] = previous
    historical = {
        (path, name)
        for path, tree in trees.items()
        for node, name, _public, _nested in interface_counts.definitions(tree)
        if isinstance(node, interface_counts.FUNCTIONS)
        and "Atomic JSON file replacement" in census.primitive_concepts(node)
    }
    control = ast.parse(
        "def publish():\n tmp.write_text(json.dumps({}))\n os.replace(tmp, path)\n"
    ).body[0]
    assert "Atomic JSON file replacement" in census.primitive_concepts(control)
    assert historical <= _inventory(trees)


@pytest.mark.parametrize(
    "binding",
    [
        "temporary = destination.with_suffix('.tmp')",
        "descriptor, temporary = mkstemp()",
        "with temporary_file() as temporary: pass",
        "temporary: Path = Path(handle.name)",
    ],
)
@pytest.mark.parametrize(
    "rename",
    [
        "os.replace(str(temporary), destination)",
        "os.rename(Path(temporary), destination)",
        "temporary.replace(destination)",
        "temporary.rename(destination)",
        "replace(str(temporary), str(destination))",
        "rename(temporary, destination)",
        "os.replace(temporary / relative, destination)",
        "publish(temporary, destination)",
        "publish(source=temporary, destination=destination)",
    ],
)
def test_local_binding_forms_and_delegated_renames_are_reported(binding, rename):
    tree = ast.parse(
        "def publish(source, destination):\n os.replace(source, destination)\n"
        f"def writer(destination):\n {binding}\n {rename}\n"
    )
    assert interface_counts._staged_writes(interface_counts.definitions(tree)) == {
        "writer"
    }


def test_parameter_global_and_nested_sources_are_not_owned_by_the_caller():
    tree = ast.parse("""
def move(source, destination):
    os.rename(source, destination)
def caller(source, destination):
    move(source, destination)
def global_move(destination):
    os.replace(global_source, destination)
def enclosing(destination):
    def nested():
        temporary = make_temporary()
        temporary.replace(destination)
""")
    assert interface_counts._staged_writes(interface_counts.definitions(tree)) == {
        "enclosing.nested"
    }


def test_reclaim_after_failed_exclusive_create_is_not_a_staged_write():
    tree = ast.parse("""
def move(source, destination):
    os.rename(source, destination)
def claim(destination):
    source = claim_path()
    try:
        descriptor = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        move(source, destination)
def writer(destination):
    source = temporary_path()
    descriptor = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    move(source, destination)
""")
    assert interface_counts._staged_writes(interface_counts.definitions(tree)) == {
        "writer"
    }


@pytest.mark.parametrize("publisher", ["client", "compiled", "thumbnail"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_byte_publishers_use_binary_handles_and_clean_failed_temporaries(
    tmp_path, monkeypatch, publisher, interrupt
):
    import hashlib
    import io
    from contextlib import closing
    from types import SimpleNamespace

    from reckon import _store, serve

    payload = b"\x89PNG\x00\xff"
    monkeypatch.setenv("RECKON_CLIENT_CACHE", str(tmp_path / "client"))
    monkeypatch.setattr(
        serve, "_thumbnail_cache_path", lambda *args: tmp_path / "image.png"
    )
    monkeypatch.setattr(serve, "_render_thumbnail", lambda source: payload)
    monkeypatch.setattr(
        serve,
        "CLIENT_ASSETS",
        {
            "fixture.js": (
                "https://invalid.test/fixture",
                hashlib.sha256(payload).hexdigest(),
            )
        },
    )
    monkeypatch.setattr(
        serve, "urlopen", lambda *args, **kwargs: closing(io.BytesIO(payload))
    )
    if publisher == "compiled":
        monkeypatch.setattr(
            serve, "_client_asset", lambda name: tmp_path / "compiler.js"
        )
        monkeypatch.setattr(serve, "node_executable", lambda: tmp_path / "node")
        monkeypatch.setattr(
            serve.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stdout="window.label = 'é';", stderr=""
            ),
        )
    calls = []

    def publish(path, render, **kwargs):
        assert kwargs["binary"] is True
        calls.append(path)
        return _store.write_atomically(path, render, **kwargs)

    def refuse(source, destination):
        assert Path(source).is_file()
        assert Path(source).read_bytes()
        raise OSError("rename interrupted")

    monkeypatch.setattr(serve, "write_atomically", publish)
    if interrupt:
        monkeypatch.setattr(os, "replace", refuse)
    def action():
        if publisher == "client":
            return serve._client_asset("fixture.js").read_bytes()
        if publisher == "compiled":
            return serve.compile_jsx("source", filename="fixture.jsx")
        return serve._thumbnail_bytes(tmp_path / "source.png", "identity")

    if interrupt and publisher != "thumbnail":
        with pytest.raises(OSError, match="rename interrupted"):
            action()
    else:
        body = action()
        if publisher == "compiled":
            assert "é".encode() in body
        else:
            assert body == payload
    assert len(calls) == 1
    target = calls[0]
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))
    if interrupt:
        assert not target.exists()
    else:
        assert target.read_bytes() == body
