"""A warm roadmap read answers from the index's recorded tree shape, not a walk.

The discovery change signature needs the covered file set on every read, and
the index already records each visited directory's identity. A read whose tree
shape is unchanged must answer without walking; a tree that gained a document
must still walk so the new document is reported.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.serve as serve_module
from reckon import metadata_index
from reckon._plan_html import write_state
from tests.mcp_family_reload import reload_mcp_family


@pytest.fixture()
def walks(monkeypatch) -> list[int]:
    """Count every entry into ``_walk_covered`` for the duration of a test."""

    count = [0]
    original = metadata_index._walk_covered

    def counting(docs_dir, *args, **kwargs):
        count[0] += 1
        return original(docs_dir, *args, **kwargs)

    monkeypatch.setattr(metadata_index, "_walk_covered", counting)
    return count


@pytest.fixture()
def mounted(tmp_path, monkeypatch):
    project = "sample"
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    serve_module._SIGNATURE_MEMO.clear()
    metadata_index.clear()
    reload_mcp_family()
    return project, docs


def _write_plan(docs: Path, slug: str) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        write_state(bare, {"slug": slug, "title": slug, "status": "active"}),
        encoding="utf-8",
    )
    return path


def _roadmap(project: str) -> dict:
    import reckon.mcp as mcp_module

    return mcp_module._roadmap(project, view=None)


def test_warm_roadmap_read_reuses_the_index_shape(mounted, walks) -> None:
    project, docs = mounted
    _write_plan(docs, "alpha")
    _write_plan(docs, "beta")

    first = _roadmap(project)
    assert first["completion"]["plans"] == 2

    walks[0] = 0
    second = _roadmap(project)

    assert walks[0] == 0, "the warm read must not re-walk the covered tree"
    assert second["completion"]["plans"] == 2


def test_roadmap_walks_and_reports_a_new_plan(mounted, walks) -> None:
    project, docs = mounted
    _write_plan(docs, "alpha")
    _write_plan(docs, "beta")
    _roadmap(project)

    _write_plan(docs, "gamma")
    walks[0] = 0
    third = _roadmap(project)

    assert walks[0] >= 1, "a changed tree shape must be walked"
    assert third["completion"]["plans"] == 3
