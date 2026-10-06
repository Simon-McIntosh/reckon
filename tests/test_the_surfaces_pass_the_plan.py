"""The answer verb and the plan-review view read a review through the plan.

Two surfaces answer "which review belongs to this plan": ``crew review-plan
--answer`` and the MCP plan-review view. A review exists only as a delivered
sidecar until something stores it, and a plan may be covered section by section
rather than by one whole-document record, so both surfaces must store a
delivery before reading and must select the review through the plan's content.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, flight, mcp
from reckon.crew import plan_review, recovery

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "mock",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"review": {"backend": "worker", "execution_capable": True}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

_FINDING = "reuse_search-1"
_REPORT_LINE = (
    "FINDING reuse_search plan#s1 — Name the owner of the composition. "
    "— WOULD_CHANGE_THE_PLAN: yes — REASON: the core already exists"
)


def _section(identity: str) -> str:
    return (
        f'<section data-reckon="section" data-id="{identity}" data-effort-hours="0.5" '
        'data-capability-version="1.0" data-capability-class="general" '
        'data-capability-reasoning="standard" data-capability-verification="strict" '
        'data-capability-risk="low" data-status="implementable" data-links=""></section>'
    )


def _plan_html() -> str:
    declarations = json.dumps({"s1": "implementable", "s2": "implementable"})
    return (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="3">'
        f"<meta name=\"plan-section-declarations\" content='{declarations}'>"
        "</head><body><h1>Fixture</h1>"
        '<h2 id="s1">First section</h2>'
        + _section("s1")
        + "<p>Authored prose for the first section.</p>"
        '<h2 id="s2">Second section</h2>'
        + _section("s2")
        + "<p>Authored prose for the second section.</p>"
        "</body></html>"
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    path = plans / "fixture.html"
    path.write_text(_plan_html(), encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a committed plan."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"sample": str(repo / "docs")}))
    monkeypatch.setattr(flight, "resolve", lambda **kw: SimpleNamespace(config=CONFIG))
    return home, repo, path


def _delivered_report(project) -> Path:
    """Compose a delivered (unstored) review and return its report path."""
    subject = recovery.plan_review_subject("sample", "fixture", "coordinator")
    fields = recovery._review_dispatch_fields(subject)
    directory = Path(fields["sidecar"]).parent
    report = directory / "report.md"
    report.write_text(_REPORT_LINE + "\n", encoding="utf-8")
    return report


def _store_section_covering_review(path: Path) -> dict:
    """Store a review whose section digests match but whose whole digest does not.

    This is the subject of the second surface question: a plan covered section
    by section carries a stored review whose whole-document fingerprint matches
    the plan in no recognised form, so a reader that joins on the whole document
    reports it unreviewed while a reader that joins on the sections does not.
    """
    document = path.read_text(encoding="utf-8")
    record = {
        "project": "sample",
        "plan_slug": "fixture",
        "plan_version": 3,
        "reviewed_blob_sha": "a" * 40,
        "plan_fingerprint": "0" * 64,
        "section_digests": plan_review._section_digests(document),
        "rubric": "design",
        "findings": [
            {"id": _FINDING, "type": "reuse_search", "text": "Name the owner."}
        ],
        "responses": {},
    }
    plan_review.store_plan_review(record)
    return record


def test_answer_stores_an_unstored_delivery_before_answering(project):
    _, _, path = project
    _delivered_report(project)
    document = path.read_text(encoding="utf-8")
    # The delivery exists only as a sidecar; nothing has stored it, so a reader
    # that did not store first would report no review to answer.
    assert plan_review.list_plan_reviews(project="sample") == []

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            _FINDING,
            "--acted",
        ],
    )
    assert result.exit_code == 0, result.output
    stored = plan_review.read_plan_review("sample", "fixture", plan=document)
    assert stored is not None
    assert stored["responses"][_FINDING]["action"] == "acted"


def test_answer_surface_reads_a_section_covering_review(project):
    _, _, path = project
    _store_section_covering_review(path)
    document = path.read_text(encoding="utf-8")
    # The stored review is covered by the sections, not by the whole document:
    # its fingerprint matches none of the plan's recognised forms, and the
    # coverage predicate reports every unit covered.
    stored_fingerprint = plan_review.read_plan_review("sample", "fixture")[
        "plan_fingerprint"
    ]
    assert stored_fingerprint not in plan_review._fingerprint_forms(document)
    assert plan_review._review_coverage("sample", "fixture", plan=document)[1] == set()
    assert plan_review.read_plan_review("sample", "fixture", plan=document) is not None

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            _FINDING,
            "--acted",
        ],
    )
    assert result.exit_code == 0, result.output
    stored = plan_review.read_plan_review("sample", "fixture", plan=document)
    assert stored["responses"][_FINDING]["action"] == "acted"


def test_mcp_view_reads_a_section_covering_review(project):
    _, _, path = project
    _store_section_covering_review(path)
    payload = mcp._crew(project="sample", plan="fixture", view="plan-review")
    assert payload["record"] is not None
    assert payload["record"]["plan_slug"] == "fixture"
    assert payload["record"]["plan_fingerprint"] == "0" * 64
