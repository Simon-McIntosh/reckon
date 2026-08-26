"""Live-pointer listing under concurrent promotion."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import runs


@pytest.fixture()
def live_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep pointer and watcher state inside the test directory."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _record(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "project": "sample",
        "node": {"id": run_id, "plan": "delivery", "time_budget": "20m"},
        "phase": "working",
        "created_at": "2026-08-26T12:00:00Z",
    }


def test_live_pointer_removed_between_scan_and_read_is_skipped(
    live_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surviving = _record("r-a-surviving")
    removed = _record("r-z-removed")
    surviving_path = runs.pointer_path(surviving["run_id"])
    removed_path = runs.pointer_path(removed["run_id"])
    runs._write_json(surviving_path, surviving)
    runs._write_json(removed_path, removed)

    original_read_text = Path.read_text
    remove_on_survivor_read = True

    def read_text_after_concurrent_removal(path: Path, *args, **kwargs) -> str:
        nonlocal remove_on_survivor_read
        if path == surviving_path and remove_on_survivor_read:
            removed_path.unlink()
            remove_on_survivor_read = False
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_after_concurrent_removal)

    assert runs.list_live(project="sample") == [surviving]

    runs._write_json(removed_path, removed)
    remove_on_survivor_read = True
    visibility = runs.project_watch_visibility("sample")

    assert isinstance(visibility, dict)
    assert visibility["pointer_count"] == 1
    assert removed_path.exists() is False
