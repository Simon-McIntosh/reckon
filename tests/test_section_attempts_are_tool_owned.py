"""A section's attempt history comes from crew runs, including live runs."""

from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew, mcp, mcp_views, roadmap
from reckon.crew.node import TaskNode


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _record(section: str) -> dict:
    return {
        "id": section,
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
        '<h2 id="other">Other</h2></main></body></html>'
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
                "section_declarations": {
                    "work": "implementable",
                    "other": "implementable",
                },
                "sections": [_record("work"), _record("other")],
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
        "Provide typed sections for crew accounting.",
    )
    (home / "mounts.json").write_text(json.dumps({"sample": str(root / "docs")}))
    return root, plan


def _dispatch(
    root: Path,
    node_id: str,
    *,
    role: str = "implement",
    section: str = "work",
    launcher=None,
) -> dict:
    backend = (
        {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
        if launcher is not None
        else {"launch": "in-harness", "sandbox": "worktree-full", "time_budget": "20m"}
    )
    return crew.dispatch(
        node=TaskNode(
            id=node_id,
            goal="exercise section accounting",
            plan="fixture",
            section=section,
            role=role,
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
            "backends": {"native": backend},
            "roles": {"implement": {}, "test": {}, "review": {}},
            "fences": {"time_budget": "20m"},
        },
        session=f"session-{node_id}",
        unreviewed_plan_override=True,
        check_budget=False,
        launcher=launcher,
    )


def _section(root: Path, section: str = "work") -> dict:
    state, _version = _store.read_plan("sample", "fixture", root, artifact_type="plan")
    return next(row for row in state["sections"] if row["id"] == section)


def test_attempts_derive_from_executable_runs_and_their_outcomes(
    tmp_path: Path, monkeypatch
) -> None:
    root, plan = _repository(tmp_path, monkeypatch)
    before = plan.read_bytes()
    base = _git(root, "rev-parse", "HEAD")
    first = _dispatch(root, "first")
    second = _dispatch(root, "second")
    review = _dispatch(root, "review-of-first", role="review")
    assert review["role"] == "review"

    active = _section(root)
    assert active["attempts"] == 2
    assert {row["status"] for row in active["attempt_outcomes"]} == {"in_flight"}
    assert {row["run_id"] for row in active["attempt_outcomes"]} == {
        first["run_id"],
        second["run_id"],
    }
    assert plan.read_bytes() == before
    assert _git(root, "rev-parse", "HEAD") == base

    crew.record_resumption(
        second["run_id"],
        pid=999999,
        turn=1,
        log_path=tmp_path / "resume.jsonl",
        stderr_path=tmp_path / "resume.stderr.log",
    )
    assert _section(root)["attempts"] == 2

    crew.complete(
        first["run_id"],
        gate="passed",
        outcome="landed",
        root=root,
        no_impl_change="The fixture checks attempt accounting only.",
    )
    crew.complete(
        second["run_id"],
        gate="failed",
        failure_classification="negative-result",
        outcome="The stated check failed",
        root=root,
    )
    finished = _section(root)
    assert finished["attempts"] == 2
    outcomes = {row["run_id"]: row for row in finished["attempt_outcomes"]}
    assert outcomes[first["run_id"]]["status"] == "promoted"
    assert outcomes[second["run_id"]] == {
        "run_id": second["run_id"],
        "status": "failed",
        "failure_classification": "negative-result",
    }
    assert review["run_id"] not in outcomes
    assert _plan_html.read_state(plan.read_text())["sections"][0]["attempts"] == 0

    raw = mcp._read_plan(
        project="sample", slug="fixture", checkout_path=str(root), view="raw"
    )
    assert raw["data"]["sections"][0]["attempts"] == 2
    inventory = [_plan_html.parse_meta(plan)]
    report = roadmap.build_roadmap(
        "sample", inventory, [], docs_dir=root / "docs", review={}
    )
    assert report["ready_now"][0]["section_attempts"] == {"work": 2, "other": 0}
    assert mcp_views.ready_set_view(report)["ready"][0]["section_attempts"] == {
        "work": 2,
        "other": 0,
    }


def test_concurrent_dispatches_leave_a_plan_edit_uncommitted(
    tmp_path: Path, monkeypatch
) -> None:
    root, plan = _repository(tmp_path, monkeypatch)
    base = _git(root, "rev-parse", "HEAD")
    plan.write_text(plan.read_text().replace("Work</h2>", "Updated work</h2>"))
    dirty = plan.read_bytes()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_dispatch, root, "parallel-work", section="work")
        second = pool.submit(_dispatch, root, "parallel-other", section="other")
        assert first.result()["run_id"] != second.result()["run_id"]
    assert plan.read_bytes() == dirty
    assert _git(root, "rev-parse", "HEAD") == base
    assert _git(root, "status", "--short", "--", "docs/plans/fixture.html") == (
        " M docs/plans/fixture.html"
    )
    assert _section(root, "work")["attempts"] == 1
    assert _section(root, "other")["attempts"] == 1


def test_state_write_preserves_non_authoritative_attribute(
    tmp_path: Path, monkeypatch
) -> None:
    root, plan = _repository(tmp_path, monkeypatch)
    _dispatch(root, "one")
    state, version = _store.read_plan("sample", "fixture", root, artifact_type="plan")
    assert state["sections"][0]["attempts"] == 1
    state["sections"][0]["attempts"] = 99
    state["summary"] = "An ordinary plan edit"
    _store.write_plan("sample", "fixture", state, version, root, artifact_type="plan")
    assert _plan_html.read_state(plan.read_text())["sections"][0]["attempts"] == 0
    assert _section(root)["attempts"] == 1


def test_refused_worker_start_does_not_count(tmp_path: Path, monkeypatch) -> None:
    root, plan = _repository(tmp_path, monkeypatch)
    before = plan.read_bytes()

    def refuse(*_args, **_kwargs):
        raise OSError("unavailable")

    with pytest.raises(crew.CrewError, match="could not start"):
        _dispatch(root, "refused", launcher=refuse)
    assert _section(root)["attempts"] == 0
    assert plan.read_bytes() == before
