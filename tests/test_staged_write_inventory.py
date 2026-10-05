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
