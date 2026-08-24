from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module


@pytest.fixture()
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    project = "tagged-project"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    project_state = state_root / project
    project_state.mkdir(parents=True)
    (project_state / "index.json").write_text(
        json.dumps(
            {
                "updated": "2026-08-24T00:00:00",
                "project": project,
                "doc": "index",
                "data": {"_version": 0, "sprints": [], "milestones": []},
            }
        ),
        encoding="utf-8",
    )
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts_file
    serve_module._STATE_ROOT = state_root
    importlib.reload(store_module)
    importlib.reload(mcp_module)
    return docs_dir, project


def _write_resource(
    docs_dir: Path,
    project: str,
    resource_type: str,
    slug: str,
    tags: list[str],
) -> None:
    from reckon._plan_html import write_state

    root = {"plan": "plans", "research": "research", "evidence": "evidence"}[
        resource_type
    ]
    target = docs_dir / root / f"{slug}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    state = {
        "type": resource_type,
        "slug": slug,
        "title": slug,
        "tags": tags,
    }
    if resource_type == "plan":
        state["status"] = "active"
    target.write_text(write_state(bare, state), encoding="utf-8")


def test_discovery_derives_tag_inventory_from_every_resource_type(setup) -> None:
    docs_dir, project = setup
    _write_resource(
        docs_dir,
        project,
        "plan",
        "tagged-plan",
        ["standard-names", "shared-topic"],
    )
    _write_resource(
        docs_dir,
        project,
        "research",
        "tagged-research",
        ["shared-topic", "diagnostics"],
    )
    _write_resource(
        docs_dir,
        project,
        "evidence",
        "tagged-evidence",
        ["verification"],
    )

    initial = mcp_module._read_plan(project=project)

    assert initial["tag_inventory"] == [
        {"tag": "diagnostics", "count": 1},
        {"tag": "shared-topic", "count": 2},
        {"tag": "standard-names", "count": 1},
        {"tag": "verification", "count": 1},
    ]
    resources = {item["slug"]: item for item in initial["plans"]}
    assert resources["tagged-plan"]["tags"] == ["standard-names", "shared-topic"]
    assert resources["tagged-research"]["tags"] == [
        "shared-topic",
        "diagnostics",
    ]
    assert resources["tagged-evidence"]["tags"] == ["verification"]

    _write_resource(
        docs_dir,
        project,
        "evidence",
        "newly-authored",
        ["invented-during-test"],
    )
    refreshed = mcp_module._read_plan(project=project)
    refreshed_counts = {
        item["tag"]: item["count"] for item in refreshed["tag_inventory"]
    }

    assert refreshed_counts["invented-during-test"] == 1
    assert refreshed_counts["shared-topic"] == 2
