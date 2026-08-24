from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _store
from reckon._plan_html import read_state, write_state
from reckon.project_state import read_resource, write_resource
from reckon.tags import rename_project_tag


def _write_html_resource(
    docs_dir: Path,
    project: str,
    resource_type: str,
    slug: str,
    tags: list[str],
) -> Path:
    path = (
        docs_dir
        / {
            "plan": "plans",
            "research": "research",
            "evidence": "evidence",
        }[resource_type]
        / f"{slug}.html"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {
        "type": resource_type,
        "slug": slug,
        "title": slug,
        "tags": tags,
    }
    if resource_type == "plan":
        state["status"] = "active"
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture()
def tagged_project(tmp_path: Path) -> tuple[Path, str, dict[str, Path]]:
    project = "tagged-project"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    marker = docs_dir / ".reckon" / "project-state-migration.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"status": "complete", "format": "distributed"}),
        encoding="utf-8",
    )
    paths = {
        resource_type: _write_html_resource(
            docs_dir,
            project,
            resource_type,
            f"tagged-{resource_type}",
            ["standrd-names", "shared-topic"],
        )
        for resource_type in ("plan", "research", "evidence")
    }
    write_resource(
        docs_dir,
        project,
        "sprint",
        "tagged-sprint",
        {
            "id": "tagged-sprint",
            "theme": "Tagged work",
            "tags": ["standrd-names", "shared-topic"],
        },
        0,
        create=True,
    )
    paths["sprint"] = docs_dir / "sprints" / "tagged-sprint.html"
    return docs_dir, project, paths


def test_rename_rewrites_every_tag_carrier(tagged_project) -> None:
    docs_dir, project, paths = tagged_project

    result = rename_project_tag(docs_dir, project, "standrd names", "standard names")

    assert result["changed"] == 4
    assert {item["type"] for item in result["resources"]} == {
        "plan",
        "research",
        "evidence",
        "sprint",
    }
    for resource_type in ("plan", "research", "evidence"):
        assert read_state(paths[resource_type].read_text(encoding="utf-8"))["tags"] == [
            "standard-names",
            "shared-topic",
        ]
    sprint, version = read_resource(docs_dir, project, "sprint", "tagged-sprint")
    assert sprint["tags"] == ["standard-names", "shared-topic"]
    assert version == 2


def test_rename_merges_when_target_identity_already_exists(tagged_project) -> None:
    docs_dir, project, paths = tagged_project
    research = paths["research"]
    state = read_state(research.read_text(encoding="utf-8"))
    state["tags"] = ["standrd-names", "standard-names", "shared-topic"]
    research.write_text(
        write_state(research.read_text(encoding="utf-8"), state),
        encoding="utf-8",
    )

    rename_project_tag(docs_dir, project, "standrd-names", "standard-names")

    assert read_state(research.read_text(encoding="utf-8"))["tags"] == [
        "standard-names",
        "shared-topic",
    ]


def test_rename_between_equivalent_spellings_is_a_no_op(tagged_project) -> None:
    docs_dir, project, paths = tagged_project
    before = {kind: path.read_bytes() for kind, path in paths.items()}

    result = rename_project_tag(docs_dir, project, "Standrd_Names", "standrd names")

    assert result["changed"] == 0
    assert result["resources"] == []
    assert {kind: path.read_bytes() for kind, path in paths.items()} == before


def test_dry_run_lists_resources_and_writes_nothing(tagged_project) -> None:
    docs_dir, project, paths = tagged_project
    before = {kind: path.read_bytes() for kind, path in paths.items()}

    result = rename_project_tag(
        docs_dir,
        project,
        "standrd-names",
        "standard-names",
        dry_run=True,
    )

    assert result["changed"] == 0
    assert result["dry_run"] is True
    assert len(result["resources"]) == 4
    assert {item["resource"] for item in result["resources"]} == {
        f"{project}:plan:tagged-plan",
        f"{project}:research:tagged-research",
        f"{project}:evidence:tagged-evidence",
        f"{project}:sprint:tagged-sprint",
    }
    assert {kind: path.read_bytes() for kind, path in paths.items()} == before


def test_rename_refuses_to_clobber_a_concurrent_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = "tagged-project"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    path = _write_html_resource(
        docs_dir,
        project,
        "research",
        "contended-resource",
        ["standrd-names"],
    )
    original_write = _store.write_plan
    raced = False

    def racing_write(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            state, version = _store.read_plan(
                project,
                "contended-resource",
                root=tmp_path,
                artifact_type="research",
            )
            original_write(
                project,
                "contended-resource",
                {**state, "summary": "concurrent writer was here"},
                version,
                root=tmp_path,
                artifact_type="research",
            )
        return original_write(*args, **kwargs)

    monkeypatch.setattr(_store, "write_plan", racing_write)

    with pytest.raises(_store.VersionConflict):
        rename_project_tag(docs_dir, project, "standrd-names", "standard-names")

    final = read_state(path.read_text(encoding="utf-8"))
    assert final["summary"] == "concurrent writer was here"
    assert final["tags"] == ["standrd-names"]
