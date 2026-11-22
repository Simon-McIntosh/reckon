"""One obligations sweep builds a project's roadmap at most once.

The sweep examines every review item the fleet holds for a project, so the
roadmap build is counted at its entry point: a sweep that composes the roadmap
once per item cannot hide behind a cache that only its first call populates.

The counter is proved live by a positive control before a zero is reported.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from reckon import roadmap as roadmap_module
from reckon.crew import obligation_snapshot, runs
from tests.helpers.git import git

PROJECTS = ("sweep-one", "sweep-two")
SESSION = "sweep-session"
ITEMS_PER_PROJECT = 20


def _now() -> datetime:
    return datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


def _plan_html(slug: str) -> str:
    return (
        "<html><head>"
        '<meta name="docs-project" content="sweep">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0">'
        "</head><body>"
        '<section data-reckon="section" data-id="s1" data-status="implementable">'
        "</section></body></html>"
    )


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    home = tmp_path / "config"
    (home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    roots: dict[str, Any] = {}
    mounts: dict[str, str] = {}
    item = 0
    for project in PROJECTS:
        root = tmp_path / project
        plans = root / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "sweep-plan.html").write_text(_plan_html("sweep-plan"), encoding="utf-8")
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
            run_id = f"{project}-run-{index:03d}"
            head = f"{item + 1:040x}"
            manifest = home / "manifests" / f"{run_id}.md"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
                encoding="utf-8",
            )
            runs._write_json(
                runs.pointer_path(run_id),
                {
                    "run_id": run_id,
                    "project": project,
                    "repo": str(root),
                    "worktree": str(root),
                    "session": SESSION,
                    "role": "implement",
                    "process_alive": False,
                    "launcher_host": "another-host",
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


def _sweep(project: str) -> list[dict[str, Any]]:
    obligation_snapshot.sweep(
        project,
        sessions=[SESSION],
        producer={
            "pid": 1,
            "pid_start_time": "1",
            "session": SESSION,
            "started_at": None,
            "code_stamp": "sweep-test",
        },
        stream_offset=0,
        transition_fired=True,
        now=_now(),
    )
    state = obligation_snapshot.fleet_state(project, now=_now())
    payload = obligation_snapshot.payload_for(state, SESSION)
    return payload.get("obligations", [])


@pytest.fixture()
def builds(monkeypatch):
    counted: list[str] = []
    original_build = roadmap_module.build_roadmap
    original_low = roadmap_module._build_roadmap

    def counting(project, *a, **k):
        counted.append(project)
        return original_build(project, *a, **k)

    def counting_low(project, *a, **k):
        counted.append(project)
        return original_low(project, *a, **k)

    monkeypatch.setattr(roadmap_module, "build_roadmap", counting)
    monkeypatch.setattr(roadmap_module, "_build_roadmap", counting_low)
    return counted


def test_the_counter_sees_a_build(fleet, builds):
    """The instrument fires on a build it was handed, so a zero means zero."""
    docs_dir = fleet["roots"][PROJECTS[0]] / "docs"
    with roadmap_module.roadmap_build_scope(
        resolve_coordinators=False, classify_runs=False, audit_landings=False
    ):
        roadmap_module.build_roadmap(
            PROJECTS[0], [], [], docs_dir=docs_dir, now=_now(), include_attempts=False
        )
    assert builds, "the counting instrument saw no build it was handed"


def test_a_sweep_builds_each_project_roadmap_at_most_once(fleet, builds):
    reported = {project: _sweep(project) for project in PROJECTS}
    counts = {project: builds.count(project) for project in PROJECTS}
    assert all(count <= 1 for count in counts.values()), counts
    assert all(reported[project] for project in PROJECTS), {
        project: len(reported[project]) for project in PROJECTS
    }


def _identity(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return sorted(
        (str(item.get("kind")), str(item.get("run_id") or item.get("id") or ""))
        for item in items
    )


def test_a_second_sweep_reports_the_same_obligations(fleet, builds):
    """Whatever the sweep reports is the same obligations a base sweep does."""
    first = {project: _identity(_sweep(project)) for project in PROJECTS}
    second = {project: _identity(_sweep(project)) for project in PROJECTS}
    assert second == first