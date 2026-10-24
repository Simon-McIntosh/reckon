"""A retried partial sprint move finishes its plan writes instead of replaying.

A sprint move writes the index and then each moved plan's own sprint. When the
plan write fails after the index has moved, the call reports a partial write —
but the receipt it records used to read as a plain success, so a retry carrying
the same ``request_id`` was answered ``replayed`` with nothing written, and the
plan kept the sprint the move had taken it out of.

The index is the authority for a plan's sprint, so a retry reads the index's
current membership for each plan the receipt still names instead of re-applying
a batch whose index move has already landed. These cases drive the first
failure, a second failure on the first retry, and the retry that finishes the
work, and they carry a batch, an unrelated intervening edit, a second index
write, a removal from every sprint, and a plan whose sprint was already written.

Hermetic fixture mirrors tests/test_a_sprint_move_writes_the_plans_sprint.py: a
temp docs dir, a temp state root and a mounts file, with the store-backed MCP
modules reloaded against them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
import reckon.mcp_edit_plan as _edit_module
from reckon._plan_html import write_state
from tests.mcp_family_reload import reload_mcp_family

# ── Fixture ────────────────────────────────────────────────────────────────


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """Hermetic temp docs dir + mounts + state root.

    Returns ``(docs_dir, state_root, project)``.
    """
    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}))

    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    # A write with no checkout_path belongs to a live run's worktree; these
    # cases address a temporary project, so no run may be in scope.
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)

    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root

    reload_mcp_family()

    return docs_dir, state_root, project


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
            return sorted(
                _store_module.sprint_item_slug(x) for x in sprint.get("items", [])
            )
    raise AssertionError(f"sprint {sprint_id!r} not found")


def _move_ops(*slugs: str) -> list[dict]:
    return [
        {"op": "move", "target": "sprint_item", "slug": s, "from": "S1", "to": "S2"}
        for s in slugs
    ]


def _edit(project: str, ops: list[dict], expected_version: int = 0, request_id=None):
    return mcp_module._edit_plan_tool(
        project, "index", expected_version, ops=ops, request_id=request_id
    )


def _receipts_file(state_root: Path, project: str) -> Path:
    files = sorted((state_root / "_receipts" / project).glob("*.json"))
    assert len(files) == 1, files
    return files[0]


def _receipt(state_root: Path, project: str, request_id: str) -> dict:
    return json.loads(_receipts_file(state_root, project).read_text())[request_id]


def _fail_plan_writes(monkeypatch, slug: str, times: int) -> dict:
    """Make a plan's own write raise for its next ``times`` calls."""
    real = _edit_module.write_plan
    remaining = {"n": times}

    def wrapper(project, target_slug, data, version, root=None, **kwargs):
        if (
            target_slug == slug
            and kwargs.get("artifact_type") == "plan"
            and remaining["n"] > 0
        ):
            remaining["n"] -= 1
            raise OSError("the plan write was refused by this fixture")
        return real(project, target_slug, data, version, root, **kwargs)

    monkeypatch.setattr(_edit_module, "write_plan", wrapper)
    return remaining


# ── The partial write is reported, and the retry finishes it ────────────────


def test_a_partial_move_is_reported_and_finished_on_a_retry(setup, monkeypatch):
    docs_dir, state_root, project = setup
    _write_plan(docs_dir, project, "a", sprint="S1")
    _two_sprints(state_root, project, ["a"])
    # The first call and the first retry each fail after the index has moved.
    _fail_plan_writes(monkeypatch, "a", times=2)

    first = _edit(project, _move_ops("a"), request_id="req-move")
    assert first["ok"] is False, first
    assert first["error"] == "partial_write"
    assert [entry["slug"] for entry in first["partial_write"]] == ["a"]

    _, index_version = _store_module.read_plan(project, "index")
    assert _sprint_items(project, "S2") == ["a"]
    plan, plan_version = _store_module.read_plan(project, "a")
    assert plan["sprint"] == "S1", "the plan write failed, so its sprint is old"

    receipt = _receipt(state_root, project, "req-move")
    assert receipt["pending"] == [{"slug": "a", "sprint": "S2"}]
    assert receipt["version"] == index_version
    assert receipt["digest"]

    # First retry: the plan write still fails, so the partial write is reported
    # again rather than replayed as success.
    retry = _edit(project, _move_ops("a"), request_id="req-move")
    assert retry["ok"] is False, retry
    assert retry["error"] == "partial_write"
    assert [entry["slug"] for entry in retry["partial_write"]] == ["a"]
    assert _store_module.read_plan(project, "index")[1] == index_version

    # Second retry: the plan is writable, so its sprint is written and the
    # index is not moved again.
    finished = _edit(project, _move_ops("a"), request_id="req-move")
    assert finished["ok"] is True, finished
    assert finished["plan_writes"] == [
        {
            "slug": "a",
            "sprint": "S2",
            "outcome": "written",
            "new_version": plan_version + 1,
        }
    ]
    plan, _ = _store_module.read_plan(project, "a")
    assert plan["sprint"] == "S2"
    assert _store_module.read_plan(project, "index")[1] == index_version
    assert _receipt(state_root, project, "req-move")["pending"] == []

    # A further retry replays as success with nothing left to write.
    again = _edit(project, _move_ops("a"), request_id="req-move")
    assert again["ok"] is True, again
    assert again.get("replayed") is True
    assert _store_module.read_plan(project, "a")[1] == plan_version + 1


def test_a_batch_records_only_the_failed_plan_and_retries_only_it(setup, monkeypatch):
    docs_dir, state_root, project = setup
    _write_plan(docs_dir, project, "a", sprint="S1")
    _write_plan(docs_dir, project, "b", sprint="S1")
    _two_sprints(state_root, project, ["a", "b"])
    _fail_plan_writes(monkeypatch, "b", times=1)

    first = _edit(project, _move_ops("a", "b"), request_id="req-batch")
    assert first["ok"] is False, first
    assert first["error"] == "partial_write"
    assert [entry["slug"] for entry in first["partial_write"]] == ["b"]
    assert {entry["slug"] for entry in first["plan_writes"]} == {"a"}

    _, a_version = _store_module.read_plan(project, "a")
    _, b_version = _store_module.read_plan(project, "b")
    assert _store_module.read_plan(project, "a")[0]["sprint"] == "S2"
    assert _store_module.read_plan(project, "b")[0]["sprint"] == "S1"
    assert _receipt(state_root, project, "req-batch")["pending"] == [
        {"slug": "b", "sprint": "S2"}
    ]

    retry = _edit(project, _move_ops("a", "b"), request_id="req-batch")
    assert retry["ok"] is True, retry
    assert retry["plan_writes"] == [
        {
            "slug": "b",
            "sprint": "S2",
            "outcome": "written",
            "new_version": b_version + 1,
        }
    ]
    assert _store_module.read_plan(project, "a")[1] == a_version
    assert _store_module.read_plan(project, "b")[0]["sprint"] == "S2"


def test_a_pending_plan_with_an_unrelated_edit_still_gets_its_sprint(
    setup, monkeypatch
):
    docs_dir, state_root, project = setup
    _write_plan(docs_dir, project, "a", sprint="S1")
    _two_sprints(state_root, project, ["a"])
    _fail_plan_writes(monkeypatch, "a", times=1)

    assert _edit(project, _move_ops("a"), request_id="req-edit")["ok"] is False

    # Another caller edits the plan; its version moves on from the one the
    # failed move read.
    edited = mcp_module._edit_plan_tool(
        project,
        "a",
        0,
        ops=[{"op": "set", "path": "impl", "value": 0.5}],
    )
    assert edited["ok"] is True, edited
    edited_version = edited["new_version"]

    retry = _edit(project, _move_ops("a"), request_id="req-edit")
    assert retry["ok"] is True, retry
    plan, version = _store_module.read_plan(project, "a")
    assert plan["sprint"] == "S2"
    assert plan["impl"] == 0.5
    assert version == edited_version + 1


def test_a_pending_plan_another_call_moved_is_superseded(setup, monkeypatch):
    docs_dir, state_root, project = setup
    _write_plan(docs_dir, project, "a", sprint="S1")
    _two_sprints(state_root, project, ["a"])
    _fail_plan_writes(monkeypatch, "a", times=1)

    assert _edit(project, _move_ops("a"), request_id="req-super")["ok"] is False
    _, index_version = _store_module.read_plan(project, "index")

    # Another call moves the plan back, so the index no longer holds it where
    # this request sent it.
    other = _edit(
        project,
        [
            {
                "op": "move",
                "target": "sprint_item",
                "slug": "a",
                "from": "S2",
                "to": "S1",
            }
        ],
        expected_version=index_version,
    )
    assert other["ok"] is True, other
    plan_before, plan_version = _store_module.read_plan(project, "a")
    assert plan_before["sprint"] == "S1"

    retry = _edit(project, _move_ops("a"), request_id="req-super")
    assert retry["ok"] is True, retry
    assert retry["plan_writes"] == [
        {"slug": "a", "sprint": "S2", "outcome": "superseded", "listed_in": "S1"}
    ]
    plan_after, version_after = _store_module.read_plan(project, "a")
    assert plan_after["sprint"] == "S1"
    assert version_after == plan_version
    assert _sprint_items(project, "S1") == ["a"]
    assert _receipt(state_root, project, "req-super")["pending"] == []


def test_a_pending_plan_removed_from_every_sprint_is_superseded(setup, monkeypatch):
    docs_dir, state_root, project = setup
    _write_plan(docs_dir, project, "a", sprint="S1")
    _two_sprints(state_root, project, ["a"])
    _fail_plan_writes(monkeypatch, "a", times=1)

    assert _edit(project, _move_ops("a"), request_id="req-retired")["ok"] is False

    # Another call retires the plan from every sprint.
    index_data, index_version = _store_module.read_plan(project, "index")
    for sprint in index_data["sprints"]:
        sprint["items"] = [
            item
            for item in sprint.get("items", [])
            if _store_module.sprint_item_slug(item) != "a"
        ]
    _store_module.write_plan(project, "index", index_data, index_version)

    plan_before, plan_version = _store_module.read_plan(project, "a")
    retry = _edit(project, _move_ops("a"), request_id="req-retired")
    assert retry["ok"] is True, retry
    assert retry["plan_writes"] == [
        {"slug": "a", "sprint": "S2", "outcome": "superseded", "listed_in": None}
    ]
    plan_after, version_after = _store_module.read_plan(project, "a")
    assert plan_after == plan_before
    assert version_after == plan_version


def test_a_pending_plan_already_written_retires_as_settled(setup):
    docs_dir, state_root, project = setup
    _write_plan(docs_dir, project, "a", sprint="S1")
    _two_sprints(state_root, project, ["a"])

    landed = _edit(project, _move_ops("a"), request_id="req-settled")
    assert landed["ok"] is True, landed
    _, plan_version = _store_module.read_plan(project, "a")

    # A crash after the plan write and before the receipt update leaves the
    # receipt still owing a write the plan already carries.
    path = _receipts_file(state_root, project)
    receipts = json.loads(path.read_text())
    receipts["req-settled"]["pending"] = [{"slug": "a", "sprint": "S2"}]
    path.write_text(json.dumps(receipts, sort_keys=True))

    retry = _edit(project, _move_ops("a"), request_id="req-settled")
    assert retry["ok"] is True, retry
    assert retry["plan_writes"] == [{"slug": "a", "sprint": "S2", "outcome": "settled"}]
    plan, version = _store_module.read_plan(project, "a")
    assert plan["sprint"] == "S2"
    assert version == plan_version, "a settled entry writes nothing"
    assert _receipt(state_root, project, "req-settled")["pending"] == []
