from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module


@pytest.fixture()
def tagged_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    project = "tagged-project"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts_file
    serve_module._STATE_ROOT = state_root
    importlib.reload(store_module)
    importlib.reload(mcp_module)

    from reckon._plan_html import write_state

    tagged = {
        "established-a": ["standard-names"],
        "established-b": ["standard-names"],
        "established-c": ["standard-names"],
        "suspected-typo": ["standrd-names"],
        "new-topic": ["geometry"],
    }
    for slug, tags in tagged.items():
        bare = (
            "<!doctype html><html><head>"
            f'<meta name="docs-project" content="{project}">'
            f"<title>{slug}</title></head><body><main></main></body></html>"
        )
        state = {
            "slug": slug,
            "title": slug,
            "status": "active",
            "tags": tags,
            "version": 0,
        }
        (docs_dir / f"{slug}.html").write_text(
            write_state(bare, state), encoding="utf-8"
        )
    return docs_dir, project


def test_audit_reports_tag_singletons_and_near_duplicates_without_merging(
    tagged_project: tuple[Path, str],
) -> None:
    docs_dir, project = tagged_project
    before = {path: path.read_bytes() for path in docs_dir.rglob("*.html")}

    result = mcp_module._audit(project)

    singletons = {
        item["extra"]["tag"]: item
        for item in result["findings"]
        if item["code"] == "tag-singleton"
    }
    assert singletons["geometry"]["extra"]["count"] == 1
    assert singletons["standrd-names"]["extra"]["count"] == 1

    pair = next(
        item
        for item in result["findings"]
        if item["code"] == "tag-near-duplicate"
    )
    assert pair["extra"]["tags"] == [
        {"tag": "standard-names", "count": 3},
        {"tag": "standrd-names", "count": 1},
    ]
    assert pair["extra"]["distance"] == 1
    assert pair["extra"]["rename_invocation"] == (
        "reckon tag rename --project tagged-project standrd-names standard-names"
    )
    assert pair["extra"]["rename_invocation"] in pair["message"]
    assert {path: path.read_bytes() for path in docs_dir.rglob("*.html")} == before
