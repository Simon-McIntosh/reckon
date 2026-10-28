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
<meta name="plan-section-declarations" content='{"alpha": "implementable", "beta": "implementable"}'>
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
    timestamp: str | None = None,
    rubric: str = "plan_content_review",
    status: str = "ready",
) -> None:
    plan_review.store_plan_review(
        {
            **({"timestamp": timestamp} if timestamp else {}),
            "project": "sample",
            "plan_slug": "fixture",
            "plan_version": 1,
            "rubric": rubric,
            "reviewed_blob_sha": "a" * 40,
            "plan_fingerprint": plan_fingerprint
            or plan_review.plan_fingerprint(plan_path),
            "section_digests": section_digests
            if section_digests is not None
            else plan_review._section_digests(plan_path.read_text(encoding="utf-8")),
            "findings": findings,
            "responses": responses or {},
            "status": status,
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
    # Two reviews that each left out an item of their own rubric. An incomplete
    # review covers nothing, so the plan still owes these units a review and a
    # third composition is the review that would re-raise the same findings
    # against prose that has already been read twice.
    for review_run_id in ("r-review-1", "r-review-2"):
        _store_review(
            plan_path,
            config_home,
            review_run_id=review_run_id,
            findings=[dict(open_finding, id=f"{review_run_id}-finding")],
            section_digests=digests,
            status=plan_review.INCOMPLETE_STATUS,
        )

    # Composing the review is what the cap governs, so the refusal is shown at
    # the command that composes one rather than by calling the helper.
    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "s-test",
        ],
    )

    assert result.exit_code != 0, result.output
    assert "carry or decline" in result.output
    assert "r-review-1-finding" in result.output
    assert "r-review-2-finding" in result.output


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
            status=plan_review.INCOMPLETE_STATUS,
        )
    assert (
        plan_review.review_burst_refusal(
            "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
        )
        is not None
    )

    # A fresh edit is a new digest for the unit, so the two reviews of the
    # previous prose no longer count against this one and the unit may be
    # reviewed again. Both units are edited, so the composition reads both of
    # them and neither is at its limit.
    plan_path.write_text(
        PLAN_HTML.replace(
            "Alpha ships one measured change.", "Alpha ships two measured changes."
        ).replace(
            "Beta ships another measured change.",
            "Beta ships a third measured change.",
        ),
        encoding="utf-8",
    )

    assert (
        plan_review.review_burst_refusal(
            "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
        )
        is None
    )
    # The composition is still refused, but for the document unit: an
    # incomplete review covers no unit at all, so the document unit is left
    # uncovered while the edited sections are not at their limit.
    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "s-test",
        ],
    )
    assert result.exit_code != 0, result.output
    assert "unit alpha " not in result.output
    assert f"unit {plan_review.DOCUMENT_UNIT} " in result.output


def test_a_covered_section_keeps_its_reviews_off_another_sections_cap(
    project: tuple[Path, Path, Path],
) -> None:
    """A composition is judged on the units it reads, and on one rubric.

    Alpha is unchanged and covered by two stored reviews — one content, one
    design — and beta has been edited since. The composition the plan owes
    reads beta and the document unit only, so alpha's two stored reviews are
    neither a third review of anything nor spendable against the one content
    review beta still owes.
    """
    config_home, _repo, plan_path = project
    digests = plan_review._section_digests(plan_path.read_text(encoding="utf-8"))
    _store_review(
        plan_path,
        config_home,
        review_run_id="r-content",
        findings=[
            {
                "id": "reasoning-1",
                "type": "reasoning",
                "anchor": f"{plan_path}:1",
                "text": "a finding about alpha's mechanism",
            }
        ],
        section_digests=digests,
    )
    _store_review(
        plan_path,
        config_home,
        review_run_id="r-design",
        findings=[
            {
                "id": "prior-art-1",
                "type": "prior_art",
                "anchor": f"{plan_path}:1",
                "text": "a finding about alpha's prior art",
            }
        ],
        section_digests=digests,
        rubric="plan_design_review",
    )

    # Alpha carries two reviews at its present digest, one per rubric, so a cap
    # that counted either way would refuse here.
    assert (
        len(
            plan_review.reviews_for_unit_burst(
                "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
            )
        )
        == 1
    )
    assert (
        len(
            plan_review.reviews_for_unit_burst(
                "sample",
                "fixture",
                "alpha",
                rubric=plan_review.DESIGN_RUBRIC,
                base_dir=config_home / "crew" / "reviews",
            )
        )
        == 1
    )

    plan_path.write_text(
        PLAN_HTML.replace(
            "Beta ships another measured change.",
            "Beta replaces the per-session depth clamp with one project bound, "
            "deduplicates a request on its plan, section and node, and never "
            "refuses the review of a delivered run. <strong>Done when</strong> "
            "a duplicate dispatch returns the existing request and a review "
            "launches past the bound.",
        ),
        encoding="utf-8",
    )
    _contributing, uncovered, _change = plan_review.review_coverage(
        "sample", "fixture", plan=plan_path
    )
    assert uncovered == {"beta"}

    assert (
        plan_review.compose_burst_refusal(
            "sample", "fixture", base_dir=config_home / "crew" / "reviews"
        )
        is None
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "s-test",
        ],
    )
    assert "carry or decline" not in result.output


def test_a_design_review_does_not_spend_the_content_budget(
    project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = project
    digests = plan_review._section_digests(plan_path.read_text(encoding="utf-8"))
    finding = {
        "id": "reasoning-1",
        "type": "reasoning",
        "anchor": f"{plan_path}:1",
        "text": "a finding about the reviewed prose",
    }
    _store_review(
        plan_path,
        config_home,
        review_run_id="r-content",
        findings=[finding],
        section_digests=digests,
    )
    _store_review(
        plan_path,
        config_home,
        review_run_id="r-design",
        findings=[dict(finding, id="prior-art-1", type="prior_art")],
        section_digests=digests,
        rubric="plan_design_review",
    )

    # The design review is a different read of the same prose, so the unit has
    # had one content review and is still owed its second.
    assert (
        plan_review.review_burst_refusal(
            "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
        )
        is None
    )

    _store_review(
        plan_path,
        config_home,
        review_run_id="r-content-2",
        findings=[dict(finding, id="coverage-1", type="coverage")],
        section_digests=digests,
    )
    refusal = plan_review.review_burst_refusal(
        "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
    )
    assert refusal is not None
    # Only the two content reviews are counted in what the refusal reports.
    assert "has had 2 reviews" in refusal
    assert "coverage-1" in refusal
    assert "prior-art-1" not in refusal


def test_an_unchanged_unit_does_not_hold_the_review_cap(
    project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = project
    digests = plan_review._section_digests(plan_path.read_text(encoding="utf-8"))
    # Three reviews of three different plan revisions, each carrying alpha's
    # present digest because the edit that moved the plan touched other units.
    # Alpha itself was not edited between them, so no review after the first
    # read it: counting one review per plan revision would refuse every
    # composition of a long-lived plan, which is what this arm forbids.
    for number, review_run_id in enumerate(("r-review-1", "r-review-2", "r-review-3")):
        _store_review(
            plan_path,
            config_home,
            review_run_id=review_run_id,
            findings=[
                {
                    "id": f"{review_run_id}-finding",
                    "type": "reasoning",
                    "anchor": f"{plan_path}:1",
                    "text": "a finding about a unit that has not moved",
                }
            ],
            section_digests=digests,
            plan_fingerprint=f"{number + 1:064x}",
        )

    assert (
        plan_review.review_burst_refusal(
            "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
        )
        is None
    )
    assert (
        plan_review.compose_burst_refusal(
            "sample", "fixture", base_dir=config_home / "crew" / "reviews"
        )
        is None
    )


def test_the_burst_counts_at_the_newest_revision_whatever_the_listing_order(
    project: tuple[Path, Path, Path],
    monkeypatch,
) -> None:
    config_home, _repo, plan_path = project
    digests = plan_review._section_digests(plan_path.read_text(encoding="utf-8"))
    # One review at an old plan revision and two at the revision the plan
    # carries now, each carrying alpha's present digest. The listing is then
    # handed back newest first, the reverse of what the store produces: a
    # bound that sorts on a field the store never writes leaves every key
    # equal, so the last element reads as "newest" and the cap counts at the
    # old revision instead, letting a third review of the current edit through.
    _store_review(
        plan_path,
        config_home,
        review_run_id="r-old",
        findings=[],
        section_digests=digests,
        plan_fingerprint=f"{1:064x}",
        timestamp="2026-01-01T00:00:00+00:00",
    )
    for number, review_run_id in enumerate(("r-new-1", "r-new-2"), start=2):
        _store_review(
            plan_path,
            config_home,
            review_run_id=review_run_id,
            findings=[
                {
                    "id": f"{review_run_id}-finding",
                    "type": "reasoning",
                    "anchor": f"{plan_path}:1",
                    "text": "a finding about the edit the plan carries now",
                }
            ],
            section_digests=digests,
            plan_fingerprint=f"{2:064x}",
            timestamp=f"2026-02-0{number}T00:00:00+00:00",
        )

    stored = plan_review.list_plan_reviews
    monkeypatch.setattr(
        plan_review,
        "list_plan_reviews",
        lambda project=None, *, base_dir=None: list(
            reversed(stored(project, base_dir=base_dir))
        ),
    )

    counted = plan_review.reviews_for_unit_burst(
        "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
    )
    assert [record.get("review_run_id") for record in counted] == ["r-new-1", "r-new-2"]

    refused = plan_review.review_burst_refusal(
        "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
    )
    assert refused is not None
    assert "r-new-1-finding" in refused
    assert "r-new-2-finding" in refused


def test_a_finding_answered_on_a_stored_record_is_not_named_open(
    project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = project
    digests = plan_review._section_digests(plan_path.read_text(encoding="utf-8"))
    # Both reviews read the one plan revision and both raise reasoning-1, so
    # both are counted for the burst. The answer is written on the review it
    # was resolved against, which leaves the other record's copy of the
    # finding unanswered; the refusal must read the answer from the record the
    # command wrote rather than from the record it counted.
    for review_run_id in ("r-review-1", "r-review-2"):
        _store_review(
            plan_path,
            config_home,
            review_run_id=review_run_id,
            findings=[
                {
                    "id": "reasoning-1",
                    "type": "reasoning",
                    "anchor": f"{plan_path}:1",
                    "text": "a finding on the reviewed prose",
                },
                {
                    "id": "coverage-1",
                    "type": "coverage",
                    "anchor": f"{plan_path}:1",
                    "text": "another finding, left unanswered",
                },
            ],
            section_digests=digests,
        )

    answered = CliRunner().invoke(
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
            "--acted",
        ],
    )
    assert answered.exit_code == 0, answered.output

    # reasoning-1 is answered on a stored record, so the refusal must not name
    # it again: a finding named open after it was answered can never be
    # cleared by another answer.
    refusal = plan_review.review_burst_refusal(
        "sample", "fixture", "alpha", base_dir=config_home / "crew" / "reviews"
    )
    assert refusal is not None
    assert "reasoning-1" not in refusal
    assert "coverage-1" in refusal


def test_a_carried_answer_keeps_an_earlier_answer_that_names_an_action(
    project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = project
    alpha_line = _line_of(plan_path, "Alpha ships")
    _store_review(
        plan_path,
        config_home,
        review_run_id="r-review",
        findings=[
            {
                "id": "reasoning-1",
                "type": "reasoning",
                "anchor": f"{plan_path}:{alpha_line}",
                "text": "alpha's mechanism must name the store it repairs",
            }
        ],
    )

    def answer(*flags: str) -> None:
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
                *flags,
            ],
        )
        assert result.exit_code == 0, result.output

    answer("--acted")
    # A carry hands the finding to a node; it is not a later judgement about
    # the prose, so it must leave the answer already on the record alone. A
    # carry that replaced an acted answer would erase a decision the author
    # made in the plan and the gate would read it as carried instead.
    answer("--carried")

    stored = plan_review.read_plan_review(
        "sample", "fixture", base_dir=config_home / "crew" / "reviews"
    )
    assert stored is not None
    assert stored["responses"]["reasoning-1"]["action"] == "acted"


def test_two_findings_carry_across_two_dispatches(
    project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = project
    alpha_line = _line_of(plan_path, "Alpha ships")
    findings = [
        {
            "id": "reasoning-1",
            "type": "reasoning",
            "anchor": f"{plan_path}:{alpha_line}",
            "text": "alpha's mechanism must name the store it repairs",
        },
        {
            "id": "coverage-1",
            "type": "coverage",
            "anchor": f"{plan_path}#alpha",
            "text": "alpha's done-when names no negative control",
        },
    ]
    _store_review(plan_path, config_home, review_run_id="r-review", findings=findings)

    def carry(finding_id: str) -> None:
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
                finding_id,
                "--carried",
            ],
        )
        assert result.exit_code == 0, result.output

    carry("reasoning-1")
    first = crew.plan_dispatch(
        node=_node(config_home, section="alpha"),
        config=CONFIG,
        project="sample",
        repo=_repo,
        base="HEAD",
    )
    assert "alpha's mechanism must name the store it repairs" in first.node.done_when
    assert "alpha's done-when names no negative control" not in first.node.done_when

    carry("coverage-1")
    # The second dispatch starts from the done-when the first one produced, as
    # a queue edit or a re-dispatch does. The second finding joins the heading
    # the first created instead of being dropped because the heading exists.
    again = _node(config_home, section="alpha", name="alpha-node-again")
    again.done_when = first.node.done_when
    second = crew.plan_dispatch(
        node=again,
        config=CONFIG,
        project="sample",
        repo=_repo,
        base="HEAD",
    )
    assert second.node.done_when.count(plan_review.CARRIED_HEADING) == 1
    assert "alpha's mechanism must name the store it repairs" in second.node.done_when
    assert "alpha's done-when names no negative control" in second.node.done_when
