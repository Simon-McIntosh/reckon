"""A plan write tells its author the review it owes.

Every successful write to a plan through the edit tool returns ``review_owed``:
the outstanding units the coverage predicate reports uncovered, each with its
measured change where one exists, and the one-line ``review_invocation`` that
composes a review of them. The gate that refuses a build and the edit tool that
reports the debt both call the one public predicate ``review_coverage``, so the
units they name cannot disagree. These cases drive the tool surface itself
against a synthesised mounted project under ``RECKON_HOME``, so nothing here
reads or writes the operator's crew home.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import _plan_html, flight, mcp
from reckon.crew import plan_review, recovery, routing
from reckon.crew.node import PlanReviewMissingError, TaskNode

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"review": {"backend": "worker", "execution_capable": True}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

SNAPSHOT_RUN_ID = "an-edit-tells-its-author-a-review-is-owed"
DONE_WHEN = "charlie ships and nothing regresses"
_INVOCATION = "reckon crew review-plan --project sample --plan fixture --local"


def _words(prefix: str, count: int = 12) -> str:
    return " ".join(f"{prefix}{index:02d}" for index in range(count))


def _base_html() -> str:
    return (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="3">'
        "</head><body>"
        f'<h2 id="a">Alpha</h2><p>{_words("alpha")}</p>'
        f'<h2 id="b">Beta</h2><p>{_words("bravo")}</p>'
        f'<h2 id="c">Charlie</h2><p>{_words("charlie")}</p>'
        f"<p><strong>Done when</strong>: {DONE_WHEN}.</p>"
        "</body></html>"
    )


def _section_records(names):
    return [
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
        for name in names
    ]


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
    path.write_text(_base_html(), encoding="utf-8")
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


@pytest.fixture
def sectioned(project):
    home, repo, path = project
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["section_declarations"] = {
        "a": "implementable",
        "b": "implementable",
        "c": "implementable",
    }
    state["sections"] = _section_records(("a", "b", "c"))
    path.write_text(_plan_html.write_state(text, state))
    return home, repo, path


def _subject(path, *, run_id=SNAPSHOT_RUN_ID):
    return {
        "subject": "plan",
        "project": "sample",
        "plan_slug": "fixture",
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": "coordinator",
        "rubric": "content",
        "local": True,
        "run_id": run_id,
    }


def _review(path, *, run_id=SNAPSHOT_RUN_ID, version=3):
    """Store a review of the plan as it stands, with its snapshot to measure against."""
    fields = recovery._review_dispatch_fields(_subject(path, run_id=run_id))
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    sidecar["plan_version"] = version
    Path(sidecar["report_path"]).write_text("RUBRIC wiring: pass\n")
    plan_review.store_plan_review(
        {**sidecar, "findings": [], "responses": {}, "review_run_id": run_id}
    )
    return sidecar


def _version(path: Path) -> int:
    return int(_plan_html.read_state(path.read_text())["version"])


def _owed_units(result) -> set[str]:
    return {entry["unit"] for entry in result["review_owed"]}


def _gate(repo):
    return routing.require_plan_reviewed(
        node=TaskNode(
            id="build-c",
            goal="Build charlie",
            plan="fixture",
            section="c",
            role="implement",
        ),
        project="sample",
        repo=repo,
        authority={"plan": {"docs": str(repo / "docs"), "source": "repository"}},
        enforce=True,
    )


def test_a_done_when_edit_owes_a_review_of_its_section(sectioned):
    _, repo, path = sectioned
    _review(path)
    result = mcp._edit_plan_tool(
        "sample",
        "fixture",
        expected_version=_version(path),
        mode="text",
        old_html=f"<p><strong>Done when</strong>: {DONE_WHEN}.</p>",
        new_html="<p><strong>Done when</strong>: charlie ships and nothing breaks.</p>",
        checkout_path=str(repo),
    )
    assert result["ok"], result
    assert _owed_units(result) == {"c"}
    assert result["review_invocation"] == _INVOCATION


def test_the_gate_and_the_edit_name_the_same_units(sectioned):
    _, repo, path = sectioned
    _review(path)
    result = mcp._edit_plan_tool(
        "sample",
        "fixture",
        expected_version=_version(path),
        mode="text",
        old_html=f"<p><strong>Done when</strong>: {DONE_WHEN}.</p>",
        new_html="<p><strong>Done when</strong>: charlie ships and nothing breaks.</p>",
        checkout_path=str(repo),
    )
    assert result["ok"], result
    with pytest.raises(PlanReviewMissingError, match=r"uncovered units: c;"):
        _gate(repo)
    # One predicate, so the tool's report and the gate's refusal name one set.
    assert _owed_units(result) == {"c"}


def test_a_comment_only_edit_owes_no_review(sectioned):
    _, repo, path = sectioned
    _review(path)
    result = mcp._edit_plan_tool(
        "sample",
        "fixture",
        expected_version=_version(path),
        ops=[
            {
                "op": "append",
                "target": "comments",
                "section": "a",
                "item": {
                    "id": "c-author-note",
                    "who": "author",
                    "when": "2026-10-06T00:00:00Z",
                    "body": "A note that records nothing the design reads.",
                },
            }
        ],
        checkout_path=str(repo),
    )
    assert result["ok"], result
    assert result["review_owed"] == []


def test_a_new_plan_owes_at_least_the_document_unit(project):
    _, repo, _ = project
    result = mcp._edit_plan_tool(
        "sample",
        "brand-new",
        expected_version=0,
        create=True,
        ops=[
            {
                "op": "set",
                "path": "standalone",
                "value": "A synthetic plan with no relations, for this check.",
            }
        ],
        checkout_path=str(repo),
    )
    assert result["ok"], result
    assert result["review_owed"], result
    assert "_document" in _owed_units(result)


def test_both_skills_name_the_review_and_its_invocation():
    root = Path(__file__).parents[1] / "skills"
    for skill in ("reckon-create", "reckon-edit"):
        text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "review_owed" in text, skill
        assert "reckon crew review-plan --project" in text, skill
