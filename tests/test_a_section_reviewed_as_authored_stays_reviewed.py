"""Review coverage follows authored units while completed sections leave the gate."""

import json
from pathlib import Path

import pytest

from reckon import _plan_html, mcp
from reckon.crew import plan_review, recovery, routing
from reckon.crew.node import TaskNode
from tests.test_review_plan_command import project as project  # noqa: PLC0414


@pytest.fixture
def section_plan(project):
    home, repo, path = project
    text = path.read_text().replace(
        '<h2 id="delivery">Delivery</h2><p>Extend the existing mechanism.</p>',
        '<p>Shared introduction.</p><h2 id="a">Alpha</h2><p>Build alpha.</p>'
        '<h2 id="b">Beta</h2><p>Build beta.</p>',
    )
    state = _plan_html.read_state(text)
    state["section_declarations"] = {"a": "implementable", "b": "implementable"}
    state["sections"] = [
        {
            "id": name,
            "effort_hours": 1.0,
            "status": "implementable",
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
            "links": [],
        }
        for name in ("a", "b")
    ]
    state["decisions"] = {
        "choice": {
            "question": "Which owner?",
            "choice": "shared",
            "rationale": "Reuse the owner.",
        }
    }
    path.write_text(_plan_html.write_state(text, state))
    return home, repo, path


def _subject(path):
    return {
        "subject": "plan",
        "project": "sample",
        "plan_slug": "fixture",
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": "coordinator",
        "rubric": "content",
        "local": True,
        "run_id": "review-authored-content",
    }


def _review(path, *, store=True, version=None, findings=()):
    fields = recovery._review_dispatch_fields(_subject(path))
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    if version is not None:
        sidecar["plan_version"] = version
    Path(sidecar["report_path"]).write_text("RUBRIC wiring: pass\n")
    if store:
        plan_review.store_plan_review(
            {**sidecar, "findings": list(findings), "responses": {}}
        )
    return sidecar


def _gate(repo):
    return routing.require_plan_reviewed(
        node=TaskNode(
            id="build-beta",
            goal="Build beta",
            plan="fixture",
            section="b",
            role="implement",
        ),
        project="sample",
        repo=repo,
        authority={"plan": {"docs": str(repo / "docs"), "source": "repository"}},
        enforce=True,
    )


def _collapse(repo, path):
    result = mcp._edit_plan_tool(
        "sample",
        "fixture",
        checkout_path=str(repo),
        doc_type="plan",
        mode="state",
        expected_version=_plan_html.read_state(path.read_text())["version"],
        ops=[
            {
                "op": "collapse_section",
                "section": "a",
                "summary": "Alpha built.",
                "evidence_anchor": "/sample/evidence/archive/fixture#alpha",
            }
        ],
    )
    assert result.get("ok"), result
    assert 'class="section-landed"' in path.read_text()


def test_collapse_keeps_coverage(section_plan):
    _, repo, path = section_plan
    _review(path)
    assert _gate(repo) is None
    _collapse(repo, path)
    assert _gate(repo) is None
