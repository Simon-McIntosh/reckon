"""Queue placements reuse roadmap priorities until a filesystem input changes."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from reckon import _store, roadmap
from reckon.crew import queue_admission, queue_order
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(queue_order, "_PRIORITY_KEYS", {})
    monkeypatch.setattr(queue_order, "_DIRECTORY_FILES", {}, raising=False)
    monkeypatch.setattr("reckon.crew.list_live", lambda *args, **kwargs: [])
    return repository


def _count_builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    original = roadmap.build_roadmap
    builds: list[str] = []

    def counted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        builds.append(str(args[0]))
        return original(*args, **kwargs)

    # Existing imports bind the same callable under several names; later imports
    # read the patched definition from the owning module.
    for module in list(sys.modules.values()):
        if isinstance(module, ModuleType):
            for name, value in list(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, name, counted)
    return builds


def _placements() -> dict[str, dict[str, Any]]:
    pointer = {
        "run_id": "queued-work",
        "project": fixtures.PROJECT,
        "plan": fixtures.PLAN,
        "section": "s1",
        "session": fixtures.SESSION,
        "backend": "local",
        "phase": "queued",
        "queued_at": NOW.isoformat(),
        "node": {"role": "implement"},
    }
    placed = queue_admission.queued_placements(
        [pointer], now=NOW, config_for=lambda _pointer: {}
    )
    assert placed["queued-work"]["position"] == 1
    assert placed["queued-work"]["reason"] == "roadmap priority"
    return placed


def _touch(path: Path) -> None:
    observed = path.stat()
    os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))


def test_unchanged_placements_build_once_and_plan_touch_rebuilds(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _count_builds(monkeypatch)
    first = _placements()
    assert builds == [fixtures.PROJECT]
    assert _placements() == first
    assert builds == [fixtures.PROJECT]
    _touch(project / "docs" / "plans" / f"{fixtures.PLAN}.html")
    assert _placements() == first
    assert builds == [fixtures.PROJECT, fixtures.PROJECT]
    _placements()
    assert len(builds) == 2


@pytest.mark.parametrize("location", ["checkout", "configured"])
def test_sprint_index_change_rebuilds(
    project: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    index = _store.state_path(
        fixtures.PROJECT, "index", root=project if location == "checkout" else None
    )
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text('{"data": {"sprints": []}}', encoding="utf-8")
    builds = _count_builds(monkeypatch)
    first = _placements()
    assert builds == [fixtures.PROJECT]
    _touch(index)
    assert _placements() == first
    assert builds == [fixtures.PROJECT, fixtures.PROJECT]
    _placements()
    assert len(builds) == 2


def test_another_plan_change_rebuilds(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project / "docs" / "plans" / "other-work.html"
    path.write_text("<!doctype html>", encoding="utf-8")
    builds = _count_builds(monkeypatch)
    _placements()
    assert builds == [fixtures.PROJECT]
    _touch(path)
    _placements()
    assert builds == [fixtures.PROJECT, fixtures.PROJECT]


def test_plan_arrival_and_removal_rebuild(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _count_builds(monkeypatch)
    _placements()
    path = project / "docs" / "plans" / "arriving-work.html"
    path.write_text("<!doctype html>", encoding="utf-8")
    _placements()
    assert len(builds) == 2
    path.unlink()
    _placements()
    assert len(builds) == 3


def test_unrelated_file_change_keeps_the_build(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _count_builds(monkeypatch)
    first = _placements()
    figures = project / "docs" / "figures"
    figures.mkdir()
    (figures / "diagram.svg").write_text("<svg/>", encoding="utf-8")
    assert _placements() == first
    assert builds == [fixtures.PROJECT]


def test_unchanged_directory_is_not_listed_again(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.glob
    listings: list[Path] = []

    def counted(path: Path, pattern: str, **kwargs: Any) -> Any:
        if path in (project / "docs" / "plans", project / "docs" / "sprints"):
            listings.append(path)
        return original(path, pattern, **kwargs)

    monkeypatch.setattr(Path, "glob", counted)
    _placements()
    assert project / "docs" / "plans" in listings
    listings.clear()
    _placements()
    assert not listings
