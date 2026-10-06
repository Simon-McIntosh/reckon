"""A plan's comments do not earn it a review; a decision edit does.

A promotion appends one comment per promoted run to the section the run landed
against, under an id derived from the run id (``c-run-...``). Comments and
followups left the document unit, so neither a run comment nor an authored one
moves the fingerprint a review keys on: the design a review reads is the
sections, decisions and dependencies. These cases hold the boundary from both
sides: a run comment and an authored comment each leave the digest unchanged,
while an edit to a decision's text still moves it.

The cases drive the machinery itself — the promotion comment writer and the
plan store — against a synthesised mounted project under ``RECKON_HOME``, so
nothing here reads or writes the real crew home.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import _plan_html, _store, flight
from reckon.crew import plan_review as module
from reckon.crew import promotion

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

PLAN_HTML = (
    "<!doctype html><html><head>"
    '<meta name="docs-project" content="sample">'
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-slug" content="fixture">'
    '<meta name="plan-title" content="Fixture">'
    '<meta name="plan-status" content="active">'
    '<meta name="plan-version" content="3">'
    "</head><body>"
    '<h2 id="s1">Section one</h2><p>Extend the existing mechanism.</p>'
    "</body></html>"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    path = plans / "fixture.html"
    path.write_text(PLAN_HTML, encoding="utf-8")
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


def _append_authored_comment(path: Path, repo: Path, body: str) -> str:
    """Append a comment the way the plan surface does, and return its id."""
    state, version = _store.read_plan("sample", "fixture", repo, artifact_type="plan")
    comments = {
        key: list(items) for key, items in (state.get("comments") or {}).items()
    }
    comment_id = "c-20261004T214500000000"
    comments.setdefault("s1", []).append(
        {
            "id": comment_id,
            "who": "Simon McIntosh",
            "when": "2026-10-04T21:45:00Z",
            "body": body,
        }
    )
    _store.write_plan(
        "sample",
        "fixture",
        {**state, "comments": comments},
        version,
        repo,
        artifact_type="plan",
    )
    return comment_id


def _record_landing(path: Path, repo: Path, run_id: str) -> dict:
    """Record a landing comment through the promotion's own writer."""
    return promotion._record_landing_comment(
        project="sample",
        plan="fixture",
        section="s1",
        run_id=run_id,
        narrative="",
        author="promotion",
        when="2026-10-04T21:30:00Z",
        root=repo,
        landing="The node landed and its guard now fires.",
    )


def _edit_a_decision(path: Path, rationale: str) -> None:
    """Rewrite a decision's text, the way the plan surface does."""
    text = path.read_text(encoding="utf-8")
    state = _plan_html.read_state(text)
    state.setdefault("decisions", {})["choice"] = {
        "question": "Which owner?",
        "choice": "shared",
        "rationale": rationale,
    }
    path.write_text(_plan_html.write_state(text, state), encoding="utf-8")


def test_comments_leave_the_fingerprint_and_a_decision_edit_moves_it(project):
    _home, repo, path = project
    before = module.plan_fingerprint(path)

    recorded = _record_landing(path, repo, "r-20261004T213000-a-landing-run")
    assert recorded["recorded"] is True, recorded
    # The comment really landed, under the run-derived id the exclusion keys on.
    document = path.read_text(encoding="utf-8")
    assert f'data-id="{recorded["comment_id"]}"' in document
    assert recorded["comment_id"].startswith(module.RUN_COMMENT_PREFIX)
    assert module.plan_fingerprint(path) == before, (
        "a promotion's landing comment must not move the fingerprint"
    )

    authored_id = _append_authored_comment(path, repo, "<p>An authored remark.</p>")
    authored = path.read_text(encoding="utf-8")
    assert f'data-id="{authored_id}"' in authored
    assert module.plan_fingerprint(path) == before, (
        "an authored comment must not move the fingerprint"
    )

    _edit_a_decision(path, "A different owner.")
    assert module.plan_fingerprint(path) != before, (
        "an edit to a decision's text must move the fingerprint"
    )
