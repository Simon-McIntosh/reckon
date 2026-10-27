"""A carried plan-review finding reaches its section's node, and a unit is
reviewed at most twice for one edit burst.

A finding answered ``--carried`` is not acted on in the plan — editing the plan
reopens the review gate, whose next review raises the next finding, and the
section loops. Instead the answer is recorded against the unit the finding's
anchor names, closes the finding for the dispatch gate, and a later dispatch of
that section appends the finding's text to the node's done-when under a
``Carried review findings:`` heading. A third composed review of one unit for
one edit burst is refused, naming the open findings its author must carry or
decline; a fresh edit to the unit starts its count again.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew
from reckon.cli import main as cli_main
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

PLAN_HTML = """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
<meta name="plan-title" content="Fixture">
<meta name="plan-status" content="active">
<meta name="plan-impl" content="0.0">
<meta name="plan-modified" content="2026-09-25">
<meta name="plan-version" content="1">
</head><body>
<h2 id="alpha">Alpha</h2>
<section data-reckon="section" data-id="alpha" data-effort-hours="1.0" data-capability-version="1.0" data-capability-class="general" data-capability-reasoning="deep" data-capability-verification="standard" data-capability-risk="low" data-status="implementable" data-links=""></section>
<p>Alpha ships one measured change.</p>
<h2 id="beta">Beta</h2>
<section data-reckon="section" data-id="beta" data-effort-hours="1.0" data-capability-version="1.0" data-capability-class="general" data-capability-reasoning="deep" data-capability-verification="standard" data-capability-risk="low" data-status="implementable" data-links=""></section>
<p>Beta ships another measured change.</p>
</body></html>
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
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
    plan_path.write_text(PLAN_HTML, encoding="utf-8")
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


def _line_of(plan_path: Path, marker: str) -> int:
    """The 1-based line number of a plan line, for a finding anchor."""
    for number, line in enumerate(
        plan_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if marker in line:
            return number
    raise AssertionError(f"marker {marker!r} is not in the plan")


def _store_review(
    plan_path: Path,
    config_home: Path,
    *,
    review_run_id: str,
    findings: list[dict],
    responses: dict | None = None,
    section_digests: dict | None = None,
    plan_fingerprint: str | None = None,
) -> None:
    plan_review.store_plan_review(
        {
            "project": "sample",
            "plan_slug": "fixture",
            "plan_version": 1,
            "rubric": "plan_content_review",
            "reviewed_blob_sha": "a" * 40,
            "plan_fingerprint": plan_fingerprint
            or plan_review.plan_fingerprint(plan_path),
            "section_digests": section_digests
            if section_digests is not None
            else plan_review._section_digests(plan_path.read_text(encoding="utf-8")),
            "findings": findings,
            "responses": responses or {},
            "status": "ready",
            "review_run_id": review_run_id,
        },
        base_dir=config_home / "crew" / "reviews",
    )


def _node(config_home: Path, *, section: str, name: str | None = None) -> crew.TaskNode:
    return crew.TaskNode(
        id=name or f"{section}-node",
        goal=f"ship {section}",
        plan="fixture",
        section=section,
        role="implement",
        spec_level="exact",
        done_when="pytest reports one passing case for this section",
        write_paths=[f"src/{section}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{section}-node.md"),
    )


def test_a_carried_finding_reaches_here_and_not_another_section(
    project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = project
    alpha_line = _line_of(plan_path, "Alpha ships")
    beta_line = _line_of(plan_path, "Beta ships")
    findings = [
        {
            "id": "reasoning-1",
            "type": "reasoning",
            "anchor": f"{plan_path}:{alpha_line}",
            "text": "alpha's mechanism must name the store it repairs",
        },
        {
            "id": "reasoning-2",
            "type": "reasoning",
            "anchor": f"{plan_path}:{beta_line}",
            "text": "beta's mechanism must name its rollback step",
        },
    ]
    _store_review(plan_path, config_home, review_run_id="r-review", findings=findings)

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            "reasoning-1",
            "--carried",
        ],
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    response = record["record"]["responses"]["reasoning-1"]
    assert response["action"] == "carried"
    # The finding is carried to the unit its anchor sits in, and not to the
    # other section: a carried answer delivered to an unrelated node would give
    # that node work the reviewer never asked of it.
    assert response["section"] == "alpha"
    assert record["unanswered"] == ["reasoning-2"]

    alpha = crew.plan_dispatch(
        node=_node(config_home, section="alpha"),
        config=CONFIG,
        project="sample",
        repo=repo,
        base="HEAD",
    )
    beta = crew.plan_dispatch(
        node=_node(config_home, section="beta"),
        config=CONFIG,
        project="sample",
        repo=repo,
        base="HEAD",
    )

    assert plan_review.CARRIED_HEADING in alpha.node.done_when
    assert "alpha's mechanism must name the store it repairs" in alpha.node.done_when
    assert "beta's mechanism must name its rollback step" not in alpha.node.done_when
    assert plan_review.CARRIED_HEADING not in beta.node.done_when


def test_a_carried_answer_opens_no_review(
    project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = project
    alpha_line = _line_of(plan_path, "Alpha ships")
    findings = [
        {
            "id": "coverage-1",
            "type": "coverage",
            "anchor": f"{plan_path}:{alpha_line}",
            "text": "alpha's done-when names no negative control",
        }
    ]
    _store_review(plan_path, config_home, review_run_id="r-review", findings=findings)
    before = plan_review.plan_fingerprint(plan_path)

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            "coverage-1",
            "--carried",
        ],
    )
    assert result.exit_code == 0, result.output

    # The answer closes the finding for the dispatch gate without touching the
    # plan, so the plan's fingerprint is unchanged and no fresh review of the
    # edited content is owed.
    assert plan_review.plan_fingerprint(plan_path) == before
    stored = plan_review.read_plan_review(
        "sample", "fixture", base_dir=config_home / "crew" / "reviews"
    )
    assert stored is not None
    assert plan_review.unanswered_findings(stored) == []


def test_a_third_review_of_one_edit_burst_is_refused(
    project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = project
    digests = plan_review._section_digests(plan_path.read_text(encoding="utf-8"))
    open_finding = {
        "id": "reasoning-1",
        "type": "reasoning",
        "anchor": f"{plan_path}:1",
        "text": "alpha's mechanism must name the store it repairs",
    }
    for review_run_id in ("r-review-1", "r-review-2"):
        _store_review(
            plan_path,
            config_home,
            review_run_id=review_run_id,
            findings=[dict(open_finding, id=f"{review_run_id}-finding")],
            section_digests=digests,
        )

    refusal = plan_review.review_burst_refusal(
        "sample",
        "fixture",
        "alpha",
        base_dir=config_home / "crew" / "reviews",
    )

    assert refusal is not None
    assert "carry or decline" in refusal
    assert "r-review-1-finding" in refusal
    assert "r-review-2-finding" in refusal


def test_a_fresh_edit_resets_the_review_count(
    project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = project
    digests = plan_review._section_digests(plan_path.read_text(encoding="utf-8"))
    for review_run_id in ("r-review-1", "r-review-2"):
        _store_review(
            plan_path,
            config_home,
            review_run_id=review_run_id,
            findings=[
                {
                    "id": f"{review_run_id}-finding",
                    "type": "reasoning",
                    "anchor": f"{plan_path}:1",
                    "text": "alpha's mechanism must name the store it repairs",
                }
            ],
            section_digests=digests,
        )
    assert (
        plan_review.review_burst_refusal(
            "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
        )
        is not None
    )

    # A fresh edit is a new digest for the unit, so the two reviews of the
    # previous prose no longer count against this one and the unit may be
    # reviewed again.
    plan_path.write_text(
        PLAN_HTML.replace(
            "Alpha ships one measured change.", "Alpha ships two measured changes."
        ),
        encoding="utf-8",
    )

    assert (
        plan_review.review_burst_refusal(
            "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
        )
        is None
    )
