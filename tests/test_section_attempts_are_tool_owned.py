"""Crew launch accounting on a plan section's typed record."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from reckon import _plan_html, _store, crew
from reckon.crew.node import TaskNode


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).parents[1] / "skills/reckon-build/scripts/worktree_fleet.py",
        scripts / "worktree_fleet.py",
    )
    plan = root / "docs/plans/fixture.html"
    plan.parent.mkdir(parents=True)
    authored = (
        '<!doctype html><html><head><meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan"><title>Fixture</title>'
        '</head><body><main class="plan-doc"><h2 id="work">Work</h2>'
        '</main></body></html>'
    )
    plan.write_text(
        _plan_html.write_state(
            authored,
            {
                "project": "sample",
                "type": "plan",
                "slug": "fixture",
                "title": "Fixture",
                "status": "active",
                "section_declarations": {"work": "implementable"},
                "sections": [
                    {
                        "id": "work",
                        "effort_hours": 1.0,
                        "capability": {
                            "version": "1.0",
                            "class": "general",
                            "requirements": {
                                "reasoning": "standard",
                                "verification": "strict",
                                "risk": "low",
                            },
                        },
                        "attempts": 0,
                        "status": "implementable",
                        "links": [],
                    }
                ],
            },
        )
    )
    (root / "seed.txt").write_text("seed\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "seed.txt", "docs/plans/fixture.html", "skills/reckon-build/scripts/worktree_fleet.py")
    _git(root, "commit", "-q", "-m", "test: seed repository", "-m", "Provide a typed section for launch accounting.")
    (home / "mounts.json").write_text(json.dumps({"sample": str(root / "docs")}))
    return root, plan


def _dispatch(root: Path, node_id: str) -> dict:
    return crew.dispatch(
        node=TaskNode(
            id=node_id,
            goal="exercise section accounting",
            plan="fixture",
            section="work",
            role="implement",
            spec_level="exact",
            done_when="pytest reports one passing section accounting case",
            write_paths=[f"src/{node_id}.py"],
            time_budget="20m",
            negative_control="none: fixture does not write a test",
        ),
        project="sample",
        repo=root,
        config={
            "default_backend": "native",
            "backends": {"native": {"launch": "in-harness", "sandbox": "worktree-full", "time_budget": "20m"}},
            "roles": {"implement": {}},
            "fences": {"time_budget": "20m"},
        },
        session=f"session-{node_id}",
        unreviewed_plan_override=True,
        check_budget=False,
    )


def test_two_dispatches_increment_the_section_counter(tmp_path: Path, monkeypatch) -> None:
    root, _plan = _repository(tmp_path, monkeypatch)
    _dispatch(root, "first")
    _dispatch(root, "second")

    state, _version = _store.read_plan("sample", "fixture", root, artifact_type="plan")
    assert state["sections"][0]["attempts"] == 2
