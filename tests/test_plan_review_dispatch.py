"""A delivered plan-review report is parsed, stored, and read by the gate.

A plan review runs as its own dispatch and leaves a report plus a sidecar on a
durable path. The coordinator never runs a store step: the build gate finds no
stored review for the plan's content, stores the delivered report whose sidecar
carries that content's fingerprint, and judges the result. These cases hold the
report parser to the prompt grammar (one finding per line, every item a
verdict), the store to the fingerprint key, and the gate to the store-at-read
behaviour — a delivered report is stored, its unanswered finding refuses the
build by name, and answering it admits the build.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import node as node_module
from reckon.crew import plan_review
from reckon.crew import review as review_module

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

PLAN_ANCHOR = "<plan>#<node>"

REPORT_TEXT = (
    "RUBRIC wiring: a finding — the plan declares a dependency that resolves to no node.\n"
    "RUBRIC done_when: pass — each node's done-when names a control.\n"
    "RUBRIC single_goal: pass — each node names one deliverable.\n"
    "RUBRIC evidence_paths: pass — gates write under the evidence directory.\n"
    "RUBRIC anchors_resolve: pass — every cited anchor resolves.\n"
    "RUBRIC naming: pass — no plan labels reach the new symbols.\n"
    "RUBRIC reasoning: a finding — the stated cause is not the one the evidence shows.\n"
    f"FINDING wiring {PLAN_ANCHOR} — the declared dependency resolves to no node — "
    "WOULD_CHANGE_THE_PLAN: yes — REASON: a dangling dependency sends the node to "
    "a plan that does not exist.\n"
    f"FINDING reasoning {PLAN_ANCHOR} — the stated cause is not supported by the "
    "cited measurement — WOULD_CHANGE_THE_PLAN: no — REASON: the mechanism stands "
    "even if the cause is reworded.\n"
)

WIRING_FINDING = "wiring-1"
REASONING_FINDING = "reasoning-1"


@pytest.fixture
def reviewed_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
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
        ["add", "seed.txt", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo, plan_path


def _node(config_home: Path, *, name: str = "delivery") -> crew.TaskNode:
    return crew.TaskNode(
        id=name,
        goal="ship one measured change",
        plan="fixture",
        section="delivery",
        role="implement",
        spec_level="exact",
        done_when="pytest reports one passing plan review dispatch case",
        write_paths=["src/change.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _plan(node: crew.TaskNode, repo: Path, *, mode: str | None = None):
    config = CONFIG if mode is None else {**CONFIG, "plan_review_gate": mode}
    return crew.plan_dispatch(
        node=node, config=config, project="sample", repo=repo, base="HEAD"
    )


def _deliver_report(plan_path: Path, *, run_id: str = "r-delivered-report") -> Path:
    """Write a delivered report and its sidecar where the gate looks for them."""
    directory = plan_review.review_report_directory("sample", "fixture", run_id)
    report_path = directory / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(REPORT_TEXT, encoding="utf-8")
    plan_review.write_review_sidecar(
        directory,
        project="sample",
        plan_slug="fixture",
        plan_version=1,
        reviewed_blob_sha="a" * 40,
        document=plan_path,
        rubric="plan_review",
        report_path=report_path,
    )
    return report_path


def test_a_report_with_two_findings_parses_to_two_findings(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    _config_home, _repo, _plan_path = reviewed_project

    parsed = plan_review.parse_review_report(REPORT_TEXT, rubric="plan_review")

    assert parsed["rubric"] == "plan_review"
    assert parsed["absent_items"] == []
    findings = {finding["id"]: finding for finding in parsed["findings"]}
    assert set(findings) == {WIRING_FINDING, REASONING_FINDING}
    assert findings[WIRING_FINDING]["type"] == "wiring"
    assert findings[WIRING_FINDING]["anchor"] == PLAN_ANCHOR
    assert findings[WIRING_FINDING]["would_change"] is True
    assert "dangling dependency" in findings[WIRING_FINDING]["reason"]
    assert findings[REASONING_FINDING]["type"] == "reasoning"
    assert findings[REASONING_FINDING]["would_change"] is False
    assert parsed["rubric_items"]["wiring"].startswith("a finding")


def test_a_report_missing_a_rubric_line_names_that_item_absent(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    _config_home, _repo, _plan_path = reviewed_project
    without_reasoning = "\n".join(
        line
        for line in REPORT_TEXT.splitlines()
        if not line.startswith("RUBRIC reasoning:")
    )

    parsed = plan_review.parse_review_report(without_reasoning, rubric="plan_review")

    assert parsed["absent_items"] == ["reasoning"]
    assert "reasoning" not in parsed["rubric_items"]


def test_the_report_grammar_has_one_owner() -> None:
    source = Path(plan_review.__file__).read_text(encoding="utf-8")
    grammar_regexes = [
        line
        for line in source.splitlines()
        if "re.compile" in line and ("RUBRIC" in line or "FINDING" in line)
    ]
    assert grammar_regexes == []
    for name in ("_RUBRIC_LINE_RE", "_FINDING_LINE_RE", "_FINDING_TAIL_RE"):
        assert not hasattr(plan_review, name)
    assert plan_review.parse_review_report is review_module.parse_plan_review_report


def test_interface_budget_finding_parses_as_a_design_item() -> None:
    parsed = plan_review.parse_review_report(
        "RUBRIC interface_budget: a finding — public_definitions 2, cli_options 1, "
        "mcp_views 2, refusal_families 1; the section adds an option without "
        "retiring or merging anything or saying why nothing can be.\n"
        f"FINDING interface_budget {PLAN_ANCHOR} — the section adds an option "
        "without accounting for its interface cost — WOULD_CHANGE_THE_PLAN: yes "
        "— REASON: name what it retires or merges, or why nothing can be.\n",
        rubric="plan_design_review",
    )
    assert "interface_budget" in parsed["rubric_items"]
    assert "interface_budget" not in parsed["absent_items"]
    assert len(parsed["findings"]) == 1
    finding = parsed["findings"][0]
    assert finding["type"] == "interface_budget"
    assert finding["id"] == "interface_budget-1"
    assert finding["anchor"] == PLAN_ANCHOR
    assert finding["would_change"] is True


def test_store_delivered_reviews_writes_a_record_the_store_finds_by_fingerprint(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = reviewed_project
    report_path = _deliver_report(plan_path)
    fingerprint = plan_review.plan_fingerprint(plan_path)

    delivered = plan_review.delivered_reports(
        "sample", "fixture", plan_fingerprint=fingerprint
    )
    assert len(delivered) == 1
    assert delivered[0]["stored"] is False
    assert Path(delivered[0]["report_path"]) == report_path

    refusals = plan_review.store_delivered_reviews("sample", "fixture")

    assert refusals == []
    record = plan_review.read_plan_review(
        "sample", "fixture", plan_fingerprint=fingerprint
    )
    assert record is not None
    assert plan_review.finding_ids(record) == [WIRING_FINDING, REASONING_FINDING]
    stored = sorted(
        (config_home / "crew" / "reviews" / "sample").glob("plan-fixture.v*.json")
    )
    assert len(stored) == 1
    assert config_home in stored[0].parents


def test_enforce_mode_stores_a_delivered_report_then_refuses_by_the_finding(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = reviewed_project
    fingerprint = plan_review.plan_fingerprint(plan_path)
    _deliver_report(plan_path)

    expected_error = getattr(node_module, "PlanReviewMissingError", crew.CrewError)
    with pytest.raises(expected_error) as excinfo:
        _plan(_node(config_home), repo, mode="enforce")

    refusal = str(excinfo.value)
    assert WIRING_FINDING in refusal and REASONING_FINDING in refusal
    assert "unanswered" in refusal

    # The gate stored the delivered report at read: the record is now on disk,
    # keyed by the content fingerprint, independent of the dispatch that read it.
    record = plan_review.read_plan_review(
        "sample", "fixture", plan_fingerprint=fingerprint
    )
    assert record is not None
    base_dir = config_home / "crew" / "reviews"
    for finding_id in (WIRING_FINDING, REASONING_FINDING):
        plan_review.record_response(
            record, finding_id, action="acted", base_dir=base_dir
        )
        record = plan_review.read_plan_review(
            "sample", "fixture", plan_fingerprint=fingerprint
        )

    resolution = _plan(_node(config_home), repo, mode="enforce")
    assert resolution.validation.ok is True
