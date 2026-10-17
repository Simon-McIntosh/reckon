"""One discovery pass writes the persisted metadata store once, not per document.

Discovery re-reads every file whose digest moved and records the parse it made
back into the persisted index so the next process reuses it. The store is one
flat JSON blob holding every entry, so a per-document write re-serialises the
whole blob each time: a first read over an empty store encoded and rewrote the
store once per discovered document. This module pins the write count to one
flush of the recorded discoveries plus the covered-shape write from the tree
walk, and pins the store's content to what the per-document writes produced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import file_memo, metadata_index, serve

_PLAN_COUNT = 25


def _plan_html_text(project: str, slug: str, status: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{project}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="{status}">
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _clear_caches() -> None:
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    serve._GIT_LAST_MODIFIED_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()
    file_memo.clear()
    metadata_index.clear()


@pytest.fixture(autouse=True)
def _isolated_caches(monkeypatch):
    """No discovery, signature or index state survives in or out of a test."""

    _clear_caches()
    monkeypatch.setattr(serve, "_SIGNATURE_TTL_S", 0.0)
    yield
    _clear_caches()


@pytest.fixture()
def docs_tree(tmp_path, monkeypatch):
    """A 25-plan temporary docs tree reached through a temporary mounts file."""

    project = "sample"
    docs_dir = tmp_path / project / "docs"
    plans_dir = docs_dir / "plans"
    plans_dir.mkdir(parents=True)
    for index in range(_PLAN_COUNT):
        slug = f"plan-{index:02d}"
        (plans_dir / f"{slug}.html").write_text(
            _plan_html_text(project, slug, "active")
        )

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    return docs_dir, project


@pytest.fixture()
def store_writes(monkeypatch):
    """Record every call to the persisted-store writer for one read."""

    original = metadata_index._store_persisted
    calls: list[tuple[str, str, int]] = []

    def counted(docs_dir, project, entries, dir_stamps=None):
        calls.append((str(docs_dir), project, len(entries)))
        return original(docs_dir, project, entries, dir_stamps)

    monkeypatch.setattr(metadata_index, "_store_persisted", counted)
    return calls


def _empty_store(docs_dir: Path, project: str) -> None:
    """Drop the persisted store and every in-process cache, for a cold read."""

    index_path = metadata_index._index_path(docs_dir, project)
    index_path.unlink(missing_ok=True)
    _clear_caches()


def test_one_first_read_writes_the_store_at_most_twice(docs_tree, store_writes):
    docs_dir, project = docs_tree

    result = serve.discover_plans(docs_dir, project, None)

    plan_rows = [item for item in result["inventory"] if item.get("type") == "plan"]
    # Positive control: the read discovered the whole tree, so a low write count
    # is distinguishable from a read that returned nothing.
    assert len(plan_rows) == _PLAN_COUNT
    # Positive control on the instrument: the counter saw the writes it exists to
    # count, so a count of two is distinguishable from one that never fired.
    assert store_writes, "the persisted-store writer never ran during the read"
    assert len(store_writes) <= 2, f"{len(store_writes)} store writes in one read"

    persisted = metadata_index._load_persisted(docs_dir, project)
    for index in range(_PLAN_COUNT):
        relative = f"plans/plan-{index:02d}.html"
        assert relative in persisted, relative


def test_persisted_store_equals_the_per_document_writes(
    docs_tree, store_writes, monkeypatch
):
    docs_dir, project = docs_tree

    serve.discover_plans(docs_dir, project, None)
    batched = metadata_index._load_persisted(docs_dir, project)

    # Rebuild over an empty store with the batch disabled, so record_discovery
    # writes the store once per document exactly as it did before the batch.
    _empty_store(docs_dir, project)
    store_writes.clear()
    monkeypatch.setattr(metadata_index, "begin_discovery", lambda *a, **k: None)
    monkeypatch.setattr(metadata_index, "flush_discovery", lambda *a, **k: None)

    serve.discover_plans(docs_dir, project, None)
    per_document = metadata_index._load_persisted(docs_dir, project)

    assert per_document == batched
    # The unbatched path is the defect the batch removes: one write per document.
    assert len(store_writes) > 2, len(store_writes)
