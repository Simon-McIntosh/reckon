"""Happy-path tests for reckon._store.

These run against a temporary directory — no docs-server or live state files needed.
Set RECKON_STATE_ROOT to a tempdir before importing _store so all paths resolve there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture()
def state_root(tmp_path, monkeypatch):
    """Provide a clean temp state root and patch the env var."""
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path))
    return tmp_path


# Re-import _store after env is patched so _state_root() picks up the tempdir.
# Using importlib.reload is cleaner than module-level import.
import importlib
import reckon._store as _store_module


def get_store(state_root):  # noqa: ARG001
    """Reload _store so _state_root() sees the patched env var."""
    importlib.reload(_store_module)
    return _store_module


def test_read_missing_plan_returns_empty(state_root):
    """read_plan on a non-existent file returns ({}, 0)."""
    store = get_store(state_root)
    data, version = store.read_plan("my-project", "my-plan")
    assert data == {}
    assert version == 0


def test_write_then_read_roundtrip(state_root):
    """write_plan creates the file; read_plan recovers the data."""
    store = get_store(state_root)

    initial_data = {
        "status": "active",
        "title": "Test plan",
        "impl": 0.5,
    }
    # First write: expected_version=0 (file absent)
    new_version = store.write_plan("proj", "my-slug", initial_data, expected_version=0)
    assert new_version == 1

    data, version = store.read_plan("proj", "my-slug")
    assert version == 1
    assert data["status"] == "active"
    assert data["title"] == "Test plan"
    assert data["_version"] == 1

    # Verify the file envelope is well-formed
    path = store.state_path("proj", "my-slug")
    assert path.exists()
    envelope = json.loads(path.read_text())
    assert envelope["project"] == "proj"
    assert envelope["doc"] == "my-slug"
    assert "updated" in envelope


def test_version_conflict_raises(state_root):
    """write_plan raises VersionConflict when expected_version is stale."""
    store = get_store(state_root)

    store.write_plan("proj", "plan-a", {"status": "pending"}, expected_version=0)
    # version is now 1; passing 0 again should conflict
    with pytest.raises(store.VersionConflict) as exc_info:
        store.write_plan("proj", "plan-a", {"status": "active"}, expected_version=0)

    exc = exc_info.value
    assert exc.expected == 0
    assert exc.current == 1


def test_patch_plan_merges_top_level(state_root):
    """patch_plan merges patch keys into existing data without clobbering others."""
    store = get_store(state_root)

    store.write_plan("proj", "plan-b", {"status": "pending", "impl": 0.0, "roi": "high"}, 0)

    new_version = store.patch_plan("proj", "plan-b", {"status": "active", "impl": 0.3}, expected_version=1)
    assert new_version == 2

    data, version = store.read_plan("proj", "plan-b")
    assert version == 2
    assert data["status"] == "active"
    assert data["impl"] == pytest.approx(0.3)
    # Original key preserved
    assert data["roi"] == "high"


def test_append_to_list(state_root):
    """append_to_list adds items to a list field without overwriting others."""
    store = get_store(state_root)

    store.write_plan("proj", "plan-c", {"status": "active", "followups": []}, 0)

    followup = {"id": "f1", "title": "Do the thing", "body": "..."}
    new_version = store.append_to_list("proj", "plan-c", "followups", followup, expected_version=1)
    assert new_version == 2

    data, _ = store.read_plan("proj", "plan-c")
    assert len(data["followups"]) == 1
    assert data["followups"][0]["id"] == "f1"

    # Append a second item
    store.append_to_list("proj", "plan-c", "followups", {"id": "f2", "title": "Next"}, expected_version=2)
    data, _ = store.read_plan("proj", "plan-c")
    assert len(data["followups"]) == 2


def test_set_nested(state_root):
    """set_nested writes data[field][key] = value."""
    store = get_store(state_root)

    store.write_plan("proj", "plan-d", {"status": "active", "decisions": {}}, 0)

    new_version = store.set_nested(
        "proj", "plan-d", "decisions", "transport",
        {"choice": "stdio", "rationale": "default for Claude Code", "when": "2026-05-26", "by": "Simon"},
        expected_version=1,
    )
    assert new_version == 2

    data, _ = store.read_plan("proj", "plan-d")
    assert data["decisions"]["transport"]["choice"] == "stdio"


def test_resolve_in_list(state_root):
    """resolve_in_list updates matching item by id."""
    store = get_store(state_root)

    followups = [
        {"id": "f1", "title": "Open followup"},
        {"id": "f2", "title": "Other followup"},
    ]
    store.write_plan("proj", "plan-e", {"followups": followups}, 0)

    new_version = store.resolve_in_list(
        "proj", "plan-e", "followups", "f1",
        {"resolved_at": "2026-05-26T12:00:00", "outcome": "done"},
        expected_version=1,
    )
    assert new_version == 2

    data, _ = store.read_plan("proj", "plan-e")
    resolved = next(f for f in data["followups"] if f["id"] == "f1")
    assert resolved["outcome"] == "done"
    assert resolved["title"] == "Open followup"  # preserved

    # f2 untouched
    other = next(f for f in data["followups"] if f["id"] == "f2")
    assert "outcome" not in other


def test_resolve_in_list_missing_id_raises(state_root):
    """resolve_in_list raises KeyError for an unknown followup id."""
    store = get_store(state_root)

    store.write_plan("proj", "plan-f", {"followups": [{"id": "f1"}]}, 0)

    with pytest.raises(KeyError):
        store.resolve_in_list("proj", "plan-f", "followups", "NOPE", {"outcome": "x"}, expected_version=1)
