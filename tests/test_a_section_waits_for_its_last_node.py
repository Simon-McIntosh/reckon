"""A section closes with its last landing, not with its first.

A section split into several dispatch nodes is finished by the section's last
landing. A landing that declared the section done while a sibling was still
queued made that sibling's request read as a dispatch of finished work, and the
request was refused until the section was reopened by hand.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import queue_landing, runs

PROJECT = "landing-fixture"
PLAN = "delivery-plan"
SECTION = "s1"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_plan(repo: Path) -> Path:
    path = repo / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    authored = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<title>Delivery plan</title></head><body><h2 id="s1">One section</h2>'
        "<p>The section body.</p></body></html>"
    )
    path.write_text(
        _plan_html.write_state(
            authored,
            {
                "project": PROJECT,
                "type": "plan",
                "slug": PLAN,
                "title": "Delivery plan",
                "status": "active",
                "modified": "2026-11-22",
                "version": 0,
                "section_declarations": {SECTION: "implementable"},
                "sections": [
                    {
                        "id": SECTION,
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
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def landing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    plan_path = _write_plan(repo)
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}), encoding="utf-8"
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "chore: seed the plan fixture")
    return {"repo": repo, "plan": plan_path}


def _queue_node(run_id: str, node_id: str, phase: str, repo: Path) -> None:
    """Publish one live pointer for a node of the fixture's section."""
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "phase": phase,
            "repo": str(repo),
            "node": {
                "id": node_id,
                "plan": PLAN,
                "section": SECTION,
                "role": "implement",
            },
        },
    )


def _declaration(plan_path: Path) -> str:
    state = _plan_html.read_state(plan_path.read_text(encoding="utf-8"))
    return str((state.get("section_declarations") or {}).get(SECTION) or "")


def _comment_ids(plan_path: Path) -> list[str]:
    state = _plan_html.read_state(plan_path.read_text(encoding="utf-8"))
    return [
        str(comment.get("id") or "")
        for comments in (state.get("comments") or {}).values()
        for comment in comments
    ]


def _beat(run_id: str) -> str:
    return queue_landing._landing_beat(runs.read_pointer(run_id), run_id)


def test_a_landing_leaves_the_section_open_for_its_other_node(landing) -> None:
    repo, plan_path = landing["repo"], landing["plan"]
    _queue_node("r-alpha", "alpha", "delivered", repo)
    _queue_node("r-beta", "beta", "queued", repo)

    refused = _beat("r-alpha")

    assert refused == ""
    assert _declaration(plan_path) == "implementable"
    assert "c-landed-r-alpha" in _comment_ids(plan_path)
    assert "c-close-s1" not in _comment_ids(plan_path)
    record = repo / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"
    assert record.is_file(), "the waiting landing wrote no evidence record"
    assert 'id="alpha"' in record.read_text(encoding="utf-8")


def test_the_last_landing_declares_the_section_done(landing) -> None:
    repo, plan_path = landing["repo"], landing["plan"]
    # The first node has already landed: its pointer left the live set.
    _queue_node("r-beta", "beta", "delivered", repo)

    refused = _beat("r-beta")

    assert refused == ""
    assert _declaration(plan_path) == "done"
    assert "c-close-s1" in _comment_ids(plan_path)


def test_a_single_node_section_is_declared_done_by_its_landing(landing) -> None:
    repo, plan_path = landing["repo"], landing["plan"]
    _queue_node("r-only", "only", "delivered", repo)

    refused = _beat("r-only")

    assert refused == ""
    assert _declaration(plan_path) == "done"
