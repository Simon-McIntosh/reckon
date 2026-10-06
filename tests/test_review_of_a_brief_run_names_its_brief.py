"""A review of a brief-carried run names that run's stored brief as authority.

A node dispatched with ``--brief`` names no committed plan section: its
semantic authority is the stored brief its worker read. Composing the review of
such a run from the node's plan and section alone left the review carrying no
authority, and admission refused it as ``not-dispatchable`` — so the review of
a brief-carried run could not be dispatched at all. These cases compose that
review through :func:`recovery._review_dispatch_fields` and drive the composed
dispatch through the dry run, asserting the reviewed run's stored brief stands
in for the plan/section pair.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import recovery

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "worker",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"review": {"backend": "worker", "execution_capable": True}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

BRIEF_TEXT = (
    "BRIEF\n"
    "The goal is to carry a brief as the run's semantic authority.\n"
    "Done when the composed review names this brief as its authority.\n"
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A mountable project, an isolated crew home, and a stored brief file."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="3">'
        '</head><body><h2 id="delivery">Delivery</h2>'
        "<p>Extend the existing mechanism.</p></body></html>",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a committed plan."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"sample": str(repo / "docs")}))
    # The brief's stored copy is the durable authority a later reader opens, so
    # the reviewed run records the path dispatch stored beside its own record.
    brief = home / "crew" / "runs" / "r-brief-run" / "brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(BRIEF_TEXT, encoding="utf-8")
    monkeypatch.setattr(
        flight, "resolve", lambda **kwargs: SimpleNamespace(config=CONFIG)
    )
    return home, repo, brief


def _brief_carried_record(repo: Path, brief: Path) -> dict:
    """A live pointer for a run dispatched with ``--brief`` and no plan."""
    return {
        "run_id": "r-brief-run",
        "project": "sample",
        "repo": str(repo),
        "session": "coordinator",
        "node": {
            "id": "brief-worker",
            "plan": "",
            "section": "",
            "brief": str(brief),
            "brief_path": str(brief),
            "brief_sha256": hashlib.sha256(brief.read_bytes()).hexdigest(),
        },
    }


def _plan_carried_record(repo: Path, brief: Path) -> dict:
    """A node naming a plan section, with a brief as coordinator instructions."""
    return {
        "run_id": "r-plan-run",
        "project": "sample",
        "repo": str(repo),
        "session": "coordinator",
        "node": {
            "id": "plan-worker",
            "plan": "fixture",
            "section": "delivery",
            "brief": str(brief),
            "brief_path": str(brief),
            "brief_sha256": hashlib.sha256(brief.read_bytes()).hexdigest(),
        },
    }


def test_a_brief_run_composes_its_stored_brief_as_the_authority(project):
    _, repo, brief = project
    fields = recovery._review_dispatch_fields(_brief_carried_record(repo, brief))

    assert fields["brief"] == str(brief)
    assert fields["plan"] == ""
    assert fields["section"] == ""


def test_the_composed_argv_names_the_brief_and_not_the_plan(project):
    _, repo, brief = project
    record = _brief_carried_record(repo, brief)
    argv = recovery._review_dispatch_argv(record, config=CONFIG)

    assert argv[argv.index("--brief") + 1] == str(brief)
    assert "--plan" not in argv
    assert "--section" not in argv


def test_the_composed_dispatch_dry_run_validates(project):
    _, repo, brief = project
    record = _brief_carried_record(repo, brief)
    fields = recovery._review_dispatch_fields(record)

    result = recovery._dispatch_composed_review(
        record,
        fields,
        config=CONFIG,
        launcher=None,
        allow_unreconciled_runs=True,
        prefer_local=False,
        dry_run=True,
    )

    assert result["dry_run"] is True, result
    assert result["refused"] is False, result
    assert result["validation"]["ok"] is True
    argv = result["argv"]
    assert argv[argv.index("--brief")] == "--brief"
    assert argv[argv.index("--brief") + 1] == str(brief)
    assert "--plan" not in argv


def test_a_plan_carried_run_still_names_its_plan(project):
    _, repo, brief = project
    fields = recovery._review_dispatch_fields(_plan_carried_record(repo, brief))

    assert fields["brief"] == ""
    assert fields["plan"] == "fixture"
    assert fields["section"] == "delivery"

    argv = recovery._review_dispatch_argv(
        _plan_carried_record(repo, brief), config=CONFIG
    )
    assert argv[argv.index("--plan") + 1] == "fixture"
    assert "--brief" not in argv
