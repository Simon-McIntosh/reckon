"""A sprint move writes the plan's own sprint in the same call.

A sprint item and the plan it names each record the membership: the sprint
lists the plan, and the plan carries a ``plan-sprint`` naming its sprint. A move
that writes only the sprints and reports success leaves those two disagreeing,
which the lifecycle audit reports afterwards as a defect nobody reading the
write would have guessed. These cases drive the move through both entry points
— the index batch and the standalone item mover — and read the plan back.

Hermetic fixture mirrors tests/test_edit_plan.py: a temp docs dir, a temp state
root and a mounts file, with the store-backed MCP modules reloaded against them.
A second mounted project supplies the plan a move cannot write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
from reckon._plan_html import write_state
from reckon.doccheck import SPRINT_MEMBERSHIP, audit_lifecycle
from tests.mcp_family_reload import reload_mcp_family

# ── Fixture ────────────────────────────────────────────────────────────────


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """Hermetic temp docs dirs + mounts + state root.

    Returns ``(docs, foreign_docs, state, project)``. ``foreign_docs`` is a
    second mounted project, whose plans the moved project's caller cannot write.
    """
    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    foreign_docs = tmp_path / "foreign-docs"
    foreign_docs.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(
        json.dumps({project: str(docs_dir), "other": str(foreign_docs)})
    )

    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root

    reload_mcp_family()

    return docs_dir, foreign_docs, state_root, project


def _write_plan(docs_dir: Path, project: str, slug: str, *, sprint: str) -> Path:
    """A live plan carrying the sprint the sprint list names it under."""
    path = docs_dir / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{project}">'
        f"<title>{slug}</title></head><body>"
        '<main class="plan-doc"><h2 id="s1">&sect;1 &mdash; One</h2></main>'
        "</body></html>"
    )
    path.write_text(
        write_state(
            html,
            {
                "project": project,
                "type": "plan",
                "slug": slug,
                "title": slug.title(),
                "status": "active",
                "impl": 0.0,
                "sprint": sprint,
            },
        ),
        encoding="utf-8",
    )
    return path


def _seed_index(state_root: Path, project: str, data: dict) -> None:
    proj_dir = state_root / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    envelope = {
        "updated": "2026-01-01T00:00:00",
        "project": project,
        "doc": "index",
        "data": {"_version": 0, **data},
    }
    (proj_dir / "index.json").write_text(json.dumps(envelope, indent=2))


def _two_sprints(state_root: Path, project: str, from_items: list) -> None:
    _seed_index(
        state_root,
        project,
        {
            "active_sprint_id": "S1",
            "sprints": [
                {"id": "S1", "status": "active", "items": list(from_items)},
                {"id": "S2", "status": "planned", "items": []},
            ],
        },
    )


def _sprint_items(project: str, sprint_id: str) -> list:
    data, _ = _store_module.read_plan(project, "index")
    for sprint in data.get("sprints", []):
        if sprint.get("id") == sprint_id:
            names = [
                x if isinstance(x, str) else x.get("slug") for x in sprint["items"]
            ]
            return sorted(names)
    raise AssertionError(f"sprint {sprint_id!r} not found")


def _membership_findings(docs_dir: Path, project: str) -> list:
    return [
        finding
        for finding in audit_lifecycle(project=project, docs_dir=docs_dir)
        if getattr(finding, "flag", None) == SPRINT_MEMBERSHIP
    ]


def _move_ops(*slugs: str) -> list[dict]:
    return [
        {"op": "move", "target": "sprint_item", "slug": s, "from": "S1", "to": "S2"}
        for s in slugs
    ]


# ── The move writes the plan's sprint ──────────────────────────────────────


def test_index_move_writes_the_plan_sprint(setup):
    docs_dir, _, state_root, project = setup
    _write_plan(docs_dir, project, "a", sprint="S1")
    _two_sprints(state_root, project, ["a"])

    r = mcp_module._edit_plan_tool(project, "index", 0, ops=_move_ops("a"))

    assert r["ok"] is True
    plan, _ = _store_module.read_plan(project, "a")
    assert plan["sprint"] == "S2"
    assert "a" not in _sprint_items(project, "S1")
    assert _sprint_items(project, "S2") == ["a"]
    assert _membership_findings(docs_dir, project) == []


def test_index_batch_move_writes_every_moved_plan(setup):
    docs_dir, _, state_root, project = setup
    _write_plan(docs_dir, project, "a", sprint="S1")
    _write_plan(docs_dir, project, "b", sprint="S1")
    _two_sprints(state_root, project, ["a", "b"])

    r = mcp_module._edit_plan_tool(project, "index", 0, ops=_move_ops("a", "b"))

    assert r["ok"] is True
    assert {w["slug"] for w in r["plan_writes"]} == {"a", "b"}
    for slug in ("a", "b"):
        plan, _ = _store_module.read_plan(project, slug)
        assert plan["sprint"] == "S2"
    assert _sprint_items(project, "S1") == []
    assert _sprint_items(project, "S2") == ["a", "b"]
    assert _membership_findings(docs_dir, project) == []


def test_standalone_item_mover_writes_the_plan_sprint(setup):
    docs_dir, _, state_root, project = setup
    _write_plan(docs_dir, project, "a", sprint="S1")
    _two_sprints(state_root, project, ["a"])

    r = mcp_module._move_sprint_item(project, "a", "S1", "S2", 0)

    assert r["ok"] is True
    plan, _ = _store_module.read_plan(project, "a")
    assert plan["sprint"] == "S2"
    assert _sprint_items(project, "S1") == []
    assert _sprint_items(project, "S2") == ["a"]
    assert _membership_findings(docs_dir, project) == []


def test_move_of_an_absent_plan_still_moves_the_sprints(setup):
    """A member the project has no plan file for owes no plan write."""
    _, _, state_root, project = setup
    _two_sprints(state_root, project, ["a"])

    r = mcp_module._edit_plan_tool(project, "index", 0, ops=_move_ops("a"))

    assert r["ok"] is True
    assert _sprint_items(project, "S1") == []
    assert _sprint_items(project, "S2") == ["a"]


# ── A move naming a plan that cannot be written is refused ─────────────────


def test_move_naming_an_unwritable_plan_is_refused(setup):
    _, foreign_docs, state_root, project = setup
    # "other:plan-a" is another project's plan; this call cannot write it.
    foreign_plan = _write_plan(foreign_docs, "other", "plan-a", sprint="S1")
    foreign_before = foreign_plan.read_bytes()
    _two_sprints(state_root, project, ["other:plan-a"])
    index_before, _ = _store_module.read_plan(project, "index")

    r = mcp_module._edit_plan_tool(project, "index", 0, ops=_move_ops("other:plan-a"))

    assert r["ok"] is False
    assert r["error"] == "sprint_move_plan_unwritable"
    assert r["from_sprint"] == "S1"
    assert r["to_sprint"] == "S2"
    index_after, index_version = _store_module.read_plan(project, "index")
    assert index_after == index_before
    assert index_version == 0
    assert _sprint_items(project, "S1") == ["other:plan-a"]
    assert _sprint_items(project, "S2") == []
    assert foreign_plan.read_bytes() == foreign_before


def test_standalone_mover_refuses_an_unwritable_plan(setup):
    _, foreign_docs, state_root, project = setup
    _write_plan(foreign_docs, "other", "plan-a", sprint="S1")
    _two_sprints(state_root, project, ["other:plan-a"])

    r = mcp_module._move_sprint_item(project, "other:plan-a", "S1", "S2", 0)

    assert r["ok"] is False
    assert r["error"] == "sprint_move_plan_unwritable"
    assert r["from_sprint"] == "S1"
    assert r["to_sprint"] == "S2"
    assert _sprint_items(project, "S1") == ["other:plan-a"]
    assert _sprint_items(project, "S2") == []
