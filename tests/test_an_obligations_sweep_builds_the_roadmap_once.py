"""One obligations sweep builds a project's roadmap at most once.

The sweep asks the admission ordering where each queued request stands, once
for every session's payload. Each of those asks carries the same pointers at
the same instant, and ordering them again derives the project's roadmap again
— the queued requests' section priorities come from it. This measurement
drives that path: forty queued review items across two projects and four
sessions, with the build counted where the ordering composes it.

The counter is proved live by the fixture's own footfall: the sweep must
derive at least one roadmap per project, so a zero count would be a fixture
that never reached the path rather than a sweep that composed nothing.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon import roadmap as roadmap_module
from reckon.crew import obligation_snapshot, queue_order, runs
from tests.helpers.git import git

PROJECTS = ("sweep-one", "sweep-two")
SESSIONS = ("session-a", "session-b", "session-c", "session-d")
ITEMS_PER_PROJECT = 20


def _now() -> datetime:
    return datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


def _plan_html(project: str) -> str:
    declarations = json.dumps({"s1": "implementable", "s9": "implementable"}).replace(
        '"', "&quot;"
    )
    return (
        "<html><head>"
        f'<meta name="docs-project" content="{project}">'
        '<meta name="plan-slug" content="sweep-plan">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-section-declarations" content="{declarations}">'
        "</head><body>"
        '<h2 id="s1">S1</h2>'
        '<section data-reckon="section" data-id="s1" data-effort-hours="0.5" '
        'data-capability-version="1.0" data-capability-class="general" '
        'data-capability-reasoning="standard" data-capability-verification="standard" '
        'data-capability-risk="low" data-status="implementable"></section>'
        '<h2 id="s9">S9</h2>'
        '<section data-reckon="section" data-id="s9" data-effort-hours="0.5" '
        'data-capability-version="1.0" data-capability-class="general" '
        'data-capability-reasoning="standard" data-capability-verification="standard" '
        'data-capability-risk="low" data-status="implementable"></section>'
        '<section data-reckon="gates"><div class="r-gate" data-id="ship" '
        'data-status="open" data-verdict="" data-gated-sections="s1">'
        '<h4 class="r-gate-measure">approve the release</h4></div></section>'
        "</body></html>"
    )


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    home = tmp_path / "config"
    (home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    roots: dict[str, Path] = {}
    mounts: dict[str, str] = {}
    item = 0
    for project in PROJECTS:
        root = tmp_path / project
        plans = root / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "sweep-plan.html").write_text(_plan_html(project), encoding="utf-8")
        (root / "seed.txt").write_text("seed\n", encoding="utf-8")
        for args in (
            ("init", "-q", "-b", "main"),
            ("config", "user.email", "w@example.invalid"),
            ("config", "user.name", "W"),
            ("add", "seed.txt"),
            ("add", "docs"),
            ("commit", "-q", "-m", "seed"),
        ):
            git(root, *args)
        roots[project] = root
        mounts[project] = str(root / "docs")
        for index in range(ITEMS_PER_PROJECT):
            run_id = f"{project}-{index:03d}"
            head = f"{item + 1:040x}"
            manifest = home / "manifests" / f"{run_id}.md"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
                encoding="utf-8",
            )
            session = SESSIONS[index % len(SESSIONS)]
            runs._write_json(
                runs.pointer_path(run_id),
                {
                    "run_id": run_id,
                    "project": project,
                    "repo": str(root),
                    "worktree": str(root),
                    "session": session,
                    "role": "implement",
                    "phase": "queued",
                    "queued_at": "2026-11-01T11:00:00Z",
                    "plan": "sweep-plan",
                    "section": "s1",
                    "backend": "clive",
                    "launcher_host": "another-host",
                    "process_alive": False,
                    "base_sha": head,
                    "manifest_path": str(manifest),
                    "node": {
                        "id": run_id,
                        "plan": "sweep-plan",
                        "section": "s1",
                        "write_paths": ["sweep-module.py"],
                    },
                },
            )
            item += 1
    (home / "mounts.json").write_text(json.dumps(mounts), encoding="utf-8")
    return {"home": home, "roots": roots}


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every roadmap composition, with the ordering's input cache always missing.

    A sweep writes while it reads, so the inputs the ordering keys on move
    between one session's payload and the next; the cache therefore misses and
    the ordering composes the roadmap again unless the sweep's own read is
    reused. Forcing a fresh input signature each time is that shape, so this
    fixture measures the sweep rather than the ordering's cache.
    """
    counted: list[str] = []
    original = roadmap_module.build_roadmap
    original_signature = queue_order._roadmap_input_signature
    seen = itertools.count()

    def counting(project: str, *args: Any, **kwargs: Any) -> Any:
        counted.append(project)
        return original(project, *args, **kwargs)

    def moving(docs_dir: Path, slugs: set[str], project: str) -> tuple[Any, ...]:
        return (original_signature(docs_dir, slugs, project), next(seen))

    monkeypatch.setattr(roadmap_module, "build_roadmap", counting)
    monkeypatch.setattr(queue_order, "_roadmap_input_signature", moving)
    return counted


def _sweep(project: str):
    state = obligation_snapshot.fleet_state(project, now=_now())
    reported: dict[str, list[dict[str, Any]]] = {}
    for session in SESSIONS:
        payload = obligation_snapshot.payload_for(state, session)
        reported[session] = payload.get("obligations", [])
    return state, reported


def _identity(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return sorted(
        (str(item.get("kind")), str(item.get("run_id") or item.get("id") or ""))
        for item in items
    )


def test_the_sweep_reaches_the_roadmap_it_counts(
    fleet: dict[str, Any], builds: list[str]
) -> None:
    """The fixture's footfall proves the counter sees this path's builds."""
    for project in PROJECTS:
        _sweep(project)
    assert {project: builds.count(project) for project in PROJECTS} == dict.fromkeys(
        PROJECTS, 1
    ), builds


def test_a_sweep_builds_each_project_roadmap_at_most_once(
    fleet: dict[str, Any], builds: list[str]
) -> None:
    for project in PROJECTS:
        _sweep(project)
    counts = {project: builds.count(project) for project in PROJECTS}
    assert all(count <= 1 for count in counts.values()), counts


def _reported(project: str) -> dict[str, list[tuple[str, str]]]:
    _, reported = _sweep(project)
    return {session: _identity(items) for session, items in reported.items()}


def test_a_second_sweep_reports_the_same_obligations(
    fleet: dict[str, Any], builds: list[str]
) -> None:
    first = {project: _reported(project) for project in PROJECTS}
    second = {project: _reported(project) for project in PROJECTS}
    assert second == first
    assert all(items for project in first.values() for items in project.values())
