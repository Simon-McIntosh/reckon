"""Queue placements reuse roadmap priorities until a filesystem input changes."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from reckon import _store, file_memo, roadmap
from reckon.crew import queue_admission, queue_order
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(queue_order, "_PRIORITY_KEYS", {})
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


def _placements(*, ranked: bool = True) -> dict[str, dict[str, Any]]:
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
    if ranked:
        assert placed["queued-work"]["reason"] == "roadmap priority"
    return placed


def _touch(path: Path) -> None:
    observed = path.stat()
    os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))


def test_unchanged_placements_and_plan_touch_reuse_the_build(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _count_builds(monkeypatch)
    first = _placements()
    assert builds == [fixtures.PROJECT]
    assert _placements() == first
    assert builds == [fixtures.PROJECT]
    _touch(project / "docs" / "plans" / f"{fixtures.PLAN}.html")
    assert _placements() == first
    assert builds == [fixtures.PROJECT]
    _placements()
    assert len(builds) == 1


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


def test_an_unqueued_plan_edit_keeps_the_build(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project / "docs" / "plans" / "other-work.html"
    path.write_text(
        (project / "docs" / "plans" / f"{fixtures.PLAN}.html")
        .read_text(encoding="utf-8")
        .replace(fixtures.PLAN, "other-work"),
        encoding="utf-8",
    )
    builds = _count_builds(monkeypatch)
    first = _placements()
    assert builds == [fixtures.PROJECT]
    path.write_text(
        path.read_text(encoding="utf-8") + "<p>More explanation.</p>", encoding="utf-8"
    )
    assert _placements() == first
    assert builds == [fixtures.PROJECT]


def test_unqueued_plan_arrival_and_removal_keep_the_build(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _count_builds(monkeypatch)
    _placements()
    path = project / "docs" / "plans" / "arriving-work.html"
    path.write_text("<!doctype html>", encoding="utf-8")
    _placements()
    assert len(builds) == 1
    path.unlink()
    _placements()
    assert len(builds) == 1


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


def test_directory_listing_shares_memo_clearing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = project / "docs" / "sprints"
    directory.mkdir()
    original = Path.glob
    listings: list[Path] = []

    def counted(path: Path, pattern: str, **kwargs: Any) -> Any:
        if path == directory:
            listings.append(path)
        return original(path, pattern, **kwargs)

    monkeypatch.setattr(Path, "glob", counted)
    builds = _count_builds(monkeypatch)
    _placements()
    assert listings == [directory]
    listings.clear()
    _placements()
    assert not listings
    file_memo.clear()
    _placements()
    assert listings == [directory]
    assert builds == [fixtures.PROJECT]


def test_queued_plan_prose_and_bookkeeping_keep_the_build(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project / "docs" / "plans" / f"{fixtures.PLAN}.html"
    builds = _count_builds(monkeypatch)
    first = _placements()
    path.write_text(
        path.read_text(encoding="utf-8")
        + "<p>A clearer explanation of the same work.</p>"
        + '<meta name="plan-version" content="2">'
        + '<meta name="plan-modified" content="2026-11-11">',
        encoding="utf-8",
    )
    assert _placements() == first
    assert builds == [fixtures.PROJECT]


@pytest.mark.parametrize("change", ["state", "wait", "sprint", "roi", "effort"])
def test_priority_state_change_rebuilds(
    project: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = project / "docs" / "plans" / f"{fixtures.PLAN}.html"
    builds = _count_builds(monkeypatch)
    _placements()
    text = path.read_text(encoding="utf-8")
    changed = {
        "state": lambda: text.replace('content="active"', 'content="blocked"'),
        "wait": lambda: text.replace(f"{fixtures.PLAN}#s1", f"{fixtures.PLAN}#s2"),
        "sprint": lambda: text + '<meta name="plan-sprint" content="iteration">',
        "roi": lambda: text + '<meta name="plan-roi" content="high">',
        "effort": lambda: text + '<meta name="plan-effort-hours" content="12">',
    }[change]()
    assert changed != text
    path.write_text(changed, encoding="utf-8")
    result = _placements(ranked=False)
    assert builds == [fixtures.PROJECT, fixtures.PROJECT]
    queue_order._PRIORITY_KEYS.clear()
    assert _placements(ranked=False) == result


def test_queued_plan_removal_and_arrival_rebuild(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project / "docs" / "plans" / f"{fixtures.PLAN}.html"
    absent = project / "held-plan.html"
    builds = _count_builds(monkeypatch)
    first = _placements()
    path.rename(absent)
    _placements(ranked=False)
    assert len(builds) == 2
    absent.rename(path)
    assert _placements() == first
    assert len(builds) == 3


@pytest.mark.parametrize(
    "change", ["followup-title", "followup-body", "decision-rationale"]
)
def test_editorial_workflow_edit_keeps_the_build(
    project: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = project / "docs" / "plans" / f"{fixtures.PLAN}.html"
    text = path.read_text(encoding="utf-8") + (
        '<section data-reckon="followups"><article class="r-fu" '
        'data-id="next-work" data-status="open">'
        '<h4 class="r-fu-title">Carry the work</h4>'
        '<div class="r-fu-body"><p>Explain the work.</p></div>'
        "</article></section>"
        '<section data-reckon="decisions"><div class="r-dec" '
        'data-key="approach" data-choice="chosen" data-sections="s1">'
        '<p class="r-dec-q">Choose the approach</p>'
        '<p class="r-dec-rat">Reason for the choice.</p>'
        "</div></section>"
    )
    path.write_text(text, encoding="utf-8")
    builds = _count_builds(monkeypatch)
    first = _placements()
    before, after = {
        "followup-title": ("Carry the work", "Continue the work"),
        "followup-body": ("Explain the work.", "Explain the same work more clearly."),
        "decision-rationale": (
            "Reason for the choice.",
            "A clearer reason for the same choice.",
        ),
    }[change]
    assert before in text
    path.write_text(text.replace(before, after), encoding="utf-8")
    assert _placements() == first
    assert builds == [fixtures.PROJECT]


def test_decision_state_change_rebuilds(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project / "docs" / "plans" / f"{fixtures.PLAN}.html"
    text = path.read_text(encoding="utf-8") + (
        '<section data-reckon="decisions"><div class="r-dec" '
        'data-key="approach" data-choice="chosen" data-sections="s1">'
        '<p class="r-dec-q">Choose the approach</p>'
        "</div></section>"
    )
    path.write_text(text, encoding="utf-8")
    builds = _count_builds(monkeypatch)
    first = _placements()
    path.write_text(
        text.replace('data-choice="chosen"', 'data-choice=""'), encoding="utf-8"
    )
    changed = _placements(ranked=False)
    assert len(builds) == 2
    assert changed != first
    queue_order._PRIORITY_KEYS.clear()
    assert _placements(ranked=False) == changed
