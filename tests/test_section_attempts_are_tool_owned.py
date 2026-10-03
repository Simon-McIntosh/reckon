"""Crew launch accounting on a plan section's typed record."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew, mcp, mcp_views, roadmap
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
        "</main></body></html>"
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
    _git(
        root,
        "add",
        "seed.txt",
        "docs/plans/fixture.html",
        "skills/reckon-build/scripts/worktree_fleet.py",
    )
    _git(
        root,
        "commit",
        "-q",
        "-m",
        "test: seed repository",
        "-m",
        "Provide a typed section for launch accounting.",
    )
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
            "backends": {
                "native": {
                    "launch": "in-harness",
                    "sandbox": "worktree-full",
                    "time_budget": "20m",
                }
            },
            "roles": {"implement": {}},
            "fences": {"time_budget": "20m"},
        },
        session=f"session-{node_id}",
        unreviewed_plan_override=True,
        check_budget=False,
    )


def test_two_dispatches_increment_the_section_counter(
    tmp_path: Path, monkeypatch
) -> None:
    root, plan = _repository(tmp_path, monkeypatch)
    _git(root, "branch", "parallel")
    other = tmp_path / "parallel"
    _git(root, "worktree", "add", "-q", str(other), "parallel")
    (other / "parallel.txt").write_text("independent\n")
    _git(other, "add", "parallel.txt")
    _git(
        other,
        "commit",
        "-q",
        "-m",
        "test: add independent file",
        "-m",
        "Exercise a merge without changing the plan.",
    )

    first = _dispatch(root, "first")
    second = _dispatch(root, "second")

    state, _version = _store.read_plan("sample", "fixture", root, artifact_type="plan")
    assert state["sections"][0]["attempts"] == 2
    assert "Attempt 1 launched" in state["comments"]["work"][0]["body"]
    assert "Attempt 2 launched" in state["comments"]["work"][1]["body"]

    crew.record_resumption(
        second["run_id"],
        pid=999999,
        turn=1,
        log_path=tmp_path / "resume.jsonl",
        stderr_path=tmp_path / "resume.stderr.log",
    )
    state, _version = _store.read_plan("sample", "fixture", root, artifact_type="plan")
    assert state["sections"][0]["attempts"] == 2

    crew.complete(
        first["run_id"],
        gate="passed",
        outcome="landed",
        root=root,
        no_impl_change="The fixture checks attempt accounting only.",
    )
    crew.complete(second["run_id"], gate="not-run", outcome="superseded", root=root)
    state, version = _store.read_plan("sample", "fixture", root, artifact_type="plan")
    bodies = [row["body"] for row in state["comments"]["work"]]
    assert any("Attempt 1: promoted" in body for body in bodies)
    assert any("Attempt 2: superseded" in body for body in bodies)

    edited = mcp._edit_plan_tool(
        "sample",
        "fixture",
        expected_version=version,
        checkout_path=str(root),
        doc_type="plan",
        mode="state",
        ops=[{"op": "set", "path": "summary", "value": "Edited through the plan tool"}],
    )
    assert edited["ok"] is True, edited
    _git(root, "add", "docs/plans/fixture.html")
    _git(
        root,
        "commit",
        "-q",
        "-m",
        "test: record plan edit",
        "-m",
        "Keep the edited plan for the merge check.",
    )
    _git(root, "merge", "--no-ff", "--no-edit", "parallel")
    state, _version = _store.read_plan("sample", "fixture", root, artifact_type="plan")
    assert state["sections"][0]["attempts"] == 2

    inventory = [_plan_html.parse_meta(plan)]
    report = roadmap.build_roadmap(
        "sample", inventory, [], docs_dir=root / "docs", review={}
    )
    assert report["ready_now"][0]["section_attempts"] == {"work": 2}
    assert mcp_views.ready_set_view(report)["ready"][0]["section_attempts"] == {
        "work": 2
    }


def test_failed_attempt_carries_its_classification(tmp_path: Path, monkeypatch) -> None:
    root, _plan = _repository(tmp_path, monkeypatch)
    run = _dispatch(root, "failure")
    crew.complete(
        run["run_id"],
        gate="failed",
        failure_classification="negative-result",
        outcome="The stated check failed",
        root=root,
    )
    state, _version = _store.read_plan("sample", "fixture", root, artifact_type="plan")
    assert state["sections"][0]["attempts"] == 1
    assert "Attempt 1: failed (negative-result)" in state["comments"]["work"][0]["body"]


def test_plan_edit_cannot_author_an_attempt(tmp_path: Path, monkeypatch) -> None:
    root, _plan = _repository(tmp_path, monkeypatch)
    state, version = _store.read_plan("sample", "fixture", root, artifact_type="plan")
    state["sections"][0]["attempts"] = 1
    with pytest.raises(_store.OpError, match="owned by crew dispatch"):
        _store.write_plan(
            "sample", "fixture", state, version, root, artifact_type="plan"
        )
