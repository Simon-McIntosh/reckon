"""The focused sprint follows the live crew; the stored marker decides otherwise.

The fixture is a synthetic config home and docs tree: two plans in two sprints,
each optionally served by a live pointer, under a stored sprint status that is
set to ``open`` on both so the live pointer alone can decide the focus. The
composition must name the live sprint with the most recent stream activity, list
the live sprints newest first, and fall back to the stored ``active`` marker when
no crew is working the project — and the read must never reach the workstation's
real crew pointer directory.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from reckon.project_state import (
    compose_project_state,
    create_project_state,
    write_resource,
)
from reckon.roadmap import build_roadmap

# A project name no test elsewhere uses, so the live-pointer reader cannot pick
# up a peer's fleet state through the registry.
PROJECT = "focus-sample"

RUN_IDS = ("r-20260925T000000000001-focus-s1", "r-20260925T000000000002-focus-s2")


def _write_plan(docs: Path, slug: str, sprint: str) -> Path:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-sprint" content="{sprint}">'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    return path


def _write_sprint(docs: Path, sprint_id: str, status: str, items: list[str]) -> None:
    write_resource(
        docs,
        PROJECT,
        "sprint",
        sprint_id,
        {"status": status, "items": items},
        0,
        create=True,
    )


def _write_pointer(
    home: Path,
    run_id: str,
    plan: str,
    *,
    age_seconds: float,
) -> None:
    """One working, alive pointer whose stream was last written ``age_seconds`` ago."""
    run_dir = home / "crew" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    stream = run_dir / "stdin.jsonl"
    stream.write_text('{"type":"thread.started"}\n', encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(stream, (stamp, stamp))
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": f"s-{run_id}",
        "phase": "working",
        "process_alive": True,
        "node": {"plan": plan, "section": "s4"},
        "log_path": str(stream),
    }
    live_dir = home / "crew" / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _inventory() -> list[dict]:
    return [
        {
            "slug": "plan-one",
            "title": "One",
            "type": "plan",
            "status": "pending",
            "impl": 0.0,
        },
        {
            "slug": "plan-two",
            "title": "Two",
            "type": "plan",
            "status": "pending",
            "impl": 0.0,
        },
    ]


def _sprints(s1_status: str, s2_status: str) -> list[dict]:
    return [
        {"id": "S1", "status": s1_status, "items": ["plan-one"]},
        {"id": "S2", "status": s2_status, "items": ["plan-two"]},
    ]


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "config-home"
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    create_project_state(docs, PROJECT)
    _write_plan(docs, "plan-one", "S1")
    _write_plan(docs, "plan-two", "S2")
    return home, docs


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture pointer may appear under the workstation's real crew home."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    real_pointers = [real_live / f"{run_id}.json" for run_id in RUN_IDS]
    assert not any(path.exists() for path in real_pointers)
    yield
    assert not any(path.exists() for path in real_pointers)


def test_focus_follows_the_live_crew_on_every_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, docs = _tree(tmp_path)
    monkeypatch.setenv("RECKON_HOME", str(home))
    _write_sprint(docs, "S1", "open", ["plan-one"])
    _write_sprint(docs, "S2", "open", ["plan-two"])
    # S2's stream was written more recently than S1's, so S2 leads. Both stored
    # statuses are `open`, so only the pointers can decide the focus.
    _write_pointer(home, RUN_IDS[0], "plan-one", age_seconds=600)
    _write_pointer(home, RUN_IDS[1], "plan-two", age_seconds=60)

    composed = compose_project_state(docs, PROJECT)
    assert composed["active_sprint_id"] == "S2"
    assert composed["live_sprint_ids"] == ["S2", "S1"]

    report = build_roadmap(
        PROJECT, _inventory(), _sprints("open", "open"), docs_dir=docs
    )
    assert report["active_sprint_id"] == "S2"
    assert report["live_sprint_ids"] == ["S2", "S1"]


def test_focus_falls_back_to_the_stored_marker_when_no_crew_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, docs = _tree(tmp_path)
    monkeypatch.setenv("RECKON_HOME", str(home))
    _write_sprint(docs, "S1", "active", ["plan-one"])
    _write_sprint(docs, "S2", "open", ["plan-two"])

    composed = compose_project_state(docs, PROJECT)
    assert composed["active_sprint_id"] == "S1"
    assert composed["live_sprint_ids"] == []

    report = build_roadmap(
        PROJECT, _inventory(), _sprints("active", "open"), docs_dir=docs
    )
    assert report["active_sprint_id"] == "S1"
    assert report["live_sprint_ids"] == []
