"""An implementation dispatch requires an answered review of plan content."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import node as node_module
from reckon.crew import plan_review

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        role: {"backend": "worker", "execution_capable": True}
        for role in ("implement", "investigate", "review", "test")
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture
def reviewed_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    plan_path = plans / "fixture.html"
    plan_path.write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
<meta name="plan-title" content="Fixture">
<meta name="plan-status" content="active">
<meta name="plan-impl" content="0.0">
<meta name="plan-modified" content="2026-09-25">
<meta name="plan-version" content="1">
</head><body><h2 id="delivery">Delivery</h2><p>Ship one measured change.</p></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo, plan_path


def _node(config_home: Path, *, role: str = "implement", name: str = "delivery"):
    return crew.TaskNode(
        id=name,
        goal="ship one measured change",
        plan="fixture",
        section="delivery",
        role=role,
        spec_level="exact",
        done_when="pytest reports one passing plan review gate case",
        write_paths=["src/change.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _plan(node: crew.TaskNode, repo: Path, **kwargs):
    return crew.plan_dispatch(
        node=node,
        config=CONFIG,
        project="sample",
        repo=repo,
        base="HEAD",
        **kwargs,
    )


def _store_answered_review(plan_path: Path, config_home: Path) -> None:
    plan_review.store_plan_review(
        {
            "project": "sample",
            "plan_slug": "fixture",
            "plan_version": 1,
            "rubric": "plan_review",
            "reviewed_blob_sha": "a" * 40,
            "plan_fingerprint": plan_review.plan_fingerprint(plan_path),
            "findings": [
                {
                    "id": "reuse-owner",
                    "type": "reuse",
                    "text": "name the existing owner",
                }
            ],
            "responses": {
                "reuse-owner": {
                    "action": "declined",
                    "reason": "the named owner is already the module being extended",
                }
            },
            "status": "declined",
            "review_run_id": "r-plan-review",
        },
        base_dir=config_home / "reviews",
    )


def test_unreviewed_implementation_is_refused_with_a_composed_remedy(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, _plan_path = reviewed_project

    expected_error = getattr(node_module, "PlanReviewMissingError", crew.CrewError)
    with pytest.raises(expected_error) as excinfo:
        _plan(_node(config_home), repo)

    refusal = str(excinfo.value)
    assert "fixture" in refusal
    assert "composed plan-review dispatch" in refusal


def test_answered_advisory_findings_admit_the_implementation(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = reviewed_project
    _store_answered_review(plan_path, config_home)

    resolution = _plan(_node(config_home), repo)

    assert resolution.validation.ok is True


@pytest.mark.parametrize("role", ["review", "investigate", "test"])
def test_non_implementation_roles_are_exempt(
    reviewed_project: tuple[Path, Path, Path], role: str
) -> None:
    config_home, repo, _plan_path = reviewed_project

    resolution = _plan(_node(config_home, role=role, name=f"{role}-fixture"), repo)

    assert resolution.validation.ok is True


def test_composed_review_identity_uses_the_same_exemption(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, _plan_path = reviewed_project

    resolution = _plan(
        _node(config_home, role="implement", name="review-of-fixture"), repo
    )

    assert resolution.validation.ok is True


def test_metadata_only_edit_keeps_the_review_current(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = reviewed_project
    original_fingerprint = plan_review.plan_fingerprint(plan_path)
    _store_answered_review(plan_path, config_home)
    edited = plan_path.read_text(encoding="utf-8")
    edited = edited.replace('content="0.0"', 'content="0.5"')
    edited = edited.replace('content="2026-09-25"', 'content="2026-09-26"')
    edited = edited.replace('content="1"', 'content="2"')
    plan_path.write_text(edited, encoding="utf-8")
    subprocess.run(
        ["git", "add", "docs/plans/fixture.html"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "docs: update plan metadata"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    assert plan_review.plan_fingerprint(plan_path) == original_fingerprint
    assert _plan(_node(config_home), repo).validation.ok is True


def test_unreviewed_waiver_is_recorded_on_the_run_pointer(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, _plan_path = reviewed_project
    node = _node(config_home, name="waived-delivery")

    record = crew.dispatch(
        node=node,
        project="sample",
        repo=repo,
        config=CONFIG,
        session="waiver-session",
        unreviewed_plan_override=True,
    )

    waiver = record["unreviewed_plan_override"]
    assert waiver == {"requested": True, "plan": "fixture"}
    assert crew.read_pointer(record["run_id"])["unreviewed_plan_override"] == waiver
