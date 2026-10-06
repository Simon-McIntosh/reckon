"""The answer verb and the plan-review view read a review through the plan.

Two surfaces answer "which review belongs to this plan": ``crew review-plan
--answer`` and the MCP plan-review view. A review exists only as a delivered
sidecar until something stores it, and a plan may be covered section by section
rather than by one whole-document record, so both surfaces must store a
delivery before reading and must select the review through the plan's content.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
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


def _covering_record(path: Path) -> dict:
    """A review that covers the plan's current content section by section.

    Its section digests match the plan but its whole-document fingerprint
    matches no recognised form, so coverage is reached through the sections,
    not the whole document.
    """
    document = path.read_text(encoding="utf-8")
    return {
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


def _stale_record() -> dict:
    """A review of other content: it matches no section of the current plan."""
    return {
        "project": "sample",
        "plan_slug": "fixture",
        "plan_version": 4,
        "reviewed_blob_sha": "b" * 40,
        "plan_fingerprint": "1" * 64,
        "section_digests": {"s1": "deadbeef"},
        "rubric": "design",
        "findings": [],
        "responses": {},
    }


def _store_two_reviews(path: Path) -> tuple[dict, dict]:
    """Store an older covering review and a newer review that does not cover.

    The covering review is the older by version and by file mtime, so a reader
    that joins on the plan's content returns it while a reader that takes the
    newest stored record regardless of coverage returns the non-covering one.
    That difference is what the surface tests assert, so dropping the plan's
    content from a caller makes them fail rather than silently agreeing on the
    wrong record.
    """
    covering = _covering_record(path)
    stale = _stale_record()
    covering_path = plan_review.store_plan_review(covering)
    stale_path = plan_review.store_plan_review(stale)
    # Pin the mtimes so the non-covering review is unambiguously newest by file
    # mtime: the no-plan branch returns the newest by mtime, and the two writes
    # would otherwise be too close for a filesystem to order reliably.
    now = time.time()
    os.utime(covering_path, (now - 10, now - 10))
    os.utime(stale_path, (now, now))
    return covering, stale


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
    covering, stale = _store_two_reviews(path)
    document = path.read_text(encoding="utf-8")
    # The covering review is covered by the sections, not by the whole document:
    # its fingerprint matches none of the plan's recognised forms, so a reader
    # joining on the whole document would miss it, and the coverage predicate
    # reports every unit covered. A reader that takes the newest stored record
    # regardless of coverage would return the stale one instead, so the two
    # answers diverge and the surface must give the covering one.
    assert covering["plan_fingerprint"] not in plan_review._fingerprint_forms(document)
    assert plan_review.review_coverage("sample", "fixture", plan=document)[1] == set()
    assert plan_review.read_plan_review("sample", "fixture")["plan_version"] == int(
        stale["plan_version"]
    )

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
    assert stored["plan_version"] == int(covering["plan_version"])
    assert stored["responses"][_FINDING]["action"] == "acted"


def test_mcp_view_reads_a_section_covering_review(project):
    _, _, path = project
    covering, stale = _store_two_reviews(path)
    payload = mcp._crew(project="sample", plan="fixture", view="plan-review")
    assert payload["record"] is not None
    assert payload["record"]["plan_slug"] == "fixture"
    assert payload["record"]["plan_version"] == int(covering["plan_version"])
    assert payload["record"]["plan_fingerprint"] == "0" * 64
    assert payload["record"]["plan_fingerprint"] != stale["plan_fingerprint"]


def test_absent_covering_review_names_the_stale_one(project):
    _, _, path = project
    stale = _stale_record()
    # With nothing stored the stale-review note is absent, which is a different
    # fact from "a review exists but does not cover the plan".
    assert mcp._stale_plan_review("sample", "fixture") is None
    plan_review.store_plan_review(stale)
    # The note names the version the review module's own lookup returns, so the
    # two surfaces cannot drift from the one rule that selects a stored review.
    looked_up = plan_review.read_plan_review("sample", "fixture")
    assert mcp._stale_plan_review("sample", "fixture")[0] == int(
        looked_up["plan_version"]
    )
    # The only stored review is of other content, so no review covers the plan
    # now present. Reporting "no stored review" would hide the one that exists,
    # so both surfaces name it by version and point at the verb that composes a
    # review of the current content.
    document = path.read_text(encoding="utf-8")
    assert plan_review.read_plan_review("sample", "fixture", plan=document) is None

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
    assert result.exit_code != 0, result.output
    detail = json.loads(result.output)["detail"]
    assert f"version {int(stale['plan_version'])}" in detail
    assert "no longer covers" in detail
    assert "crew review-plan" in detail

    payload = mcp._crew(project="sample", plan="fixture", view="plan-review")
    assert payload["record"] is None
    assert payload["stale_review"]["plan_version"] == int(stale["plan_version"])
    assert "no longer covers" in payload["stale_review"]["detail"]
