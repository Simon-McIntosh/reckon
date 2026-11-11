"""A design review and a content review of one plan can run together.

A plan review dispatches under the plan's node id, ``plan-review-of-<slug>``,
and the in-flight check in ``reckon/crew/recovery_review_dispatch.py`` keyed on
that id alone. So a design review could not be composed while a content review
of the same plan was running, although the two rubrics read the plan
independently: one round of content findings held the design review behind it.

The check is now keyed on the plan and the rubric, so one review per rubric may
be in flight for a plan. Two reviews of the same rubric still exclude each
other, so a second content review is refused while the first is running.

Every case drives the real compose, not a helper: a helper-level assertion is
how a check that never consulted the rubric would stay green. The first case
composes a content review through the same path a coordinator's ``crew review``
uses, then composes a design review of that plan against the live pointer it
left behind.

Declared negative control: keying the check on the plan alone again (dropping
the rubric narrowing inside ``_standing_plan_review``) makes the design case
refuse, and the first case fails.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import recovery, runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "sample"
PLAN_SLUG = "fixture"
PLAN_VERSION = 3
PLAN_REVIEW_NODE = f"plan-review-of-{PLAN_SLUG}"

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
    "roles": {
        "implement": {},
        "review": {"backend": "worker", "execution_capable": True},
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN_SLUG}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN_SLUG}">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-version" content="{PLAN_VERSION}">'
        '</head><body><h2 id="delivery">Delivery</h2>'
        "<p>Extend the existing mechanism.</p></body></html>",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", f"docs/plans/{PLAN_SLUG}.html"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a committed plan."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    monkeypatch.setattr(flight, "resolve", lambda **kw: SimpleNamespace(config=CONFIG))
    return home, repo


def _prepare_worktree(tmp_path: Path, head: str):
    def prepare(*_args):
        path = tmp_path / "worktree"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": "HEAD", "base_sha": head}

    return prepare


def _compose(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rubric: str
) -> dict:
    """Compose one plan review of ``rubric`` through the dispatch a coordinator runs."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _prepare_worktree(tmp_path, head)
    )
    subject = recovery.plan_review_subject(
        PROJECT, PLAN_SLUG, "coordinator", rubric=rubric
    )
    return recovery.dispatch_review_for_run(subject, config=CONFIG)


def _live_plan_reviews() -> list[dict]:
    return [
        pointer
        for pointer in runs.list_live(project=PROJECT)
        if str((pointer.get("node") or {}).get("id") or "") == PLAN_REVIEW_NODE
    ]


# ── a design review composes beside a running content review ─────────────────


def test_a_design_review_composes_while_a_content_review_works(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home, repo = project

    content = _compose(repo, monkeypatch, tmp_path, "content")
    assert content["dispatched"] is True, content.get("reason")

    # The content review is in flight, and its goal is what names its rubric.
    live = _live_plan_reviews()
    assert [str(pointer["run_id"]) for pointer in live] == [content["review_run_id"]]
    assert "review the content of plan" in str(live[0]["node"]["goal"])

    design = _compose(repo, monkeypatch, tmp_path, "design")

    assert design["dispatched"] is True, design.get("reason")
    assert design["review_run_id"] != content["review_run_id"]
    goals = sorted(str(pointer["node"]["goal"]) for pointer in _live_plan_reviews())
    assert goals == [
        f"review the content of plan {PLAN_SLUG}",
        f"review the design of plan {PLAN_SLUG}",
    ], goals


# ── a second content review of the same plan is still refused ────────────────


def test_a_second_content_review_is_refused_while_one_is_working(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home, repo = project

    content = _compose(repo, monkeypatch, tmp_path, "content")
    assert content["dispatched"] is True, content.get("reason")

    second = _compose(repo, monkeypatch, tmp_path, "content")

    assert second["dispatched"] is False, second
    # The refusal names the review that stands in the way, so a reader sees
    # which rubric is running rather than only that something is.
    assert second["review_run_id"] == content["review_run_id"], second
    assert "already in flight" in str(second["reason"]), second["reason"]
    assert len(_live_plan_reviews()) == 1, _live_plan_reviews()


# ── a pointer whose rubric cannot be read keeps standing ─────────────────────


def test_a_review_whose_rubric_cannot_be_read_still_stands(
    project: tuple[Path, Path],
) -> None:
    """An unclassifiable pointer refuses rather than admits a duplicate.

    A record written before a goal named a rubric, or one truncated past it,
    answers no rubric. The reader must not read that as "the other rubric" and
    compose a review the plan may already have running.
    """
    runs._write_json(
        runs.pointer_path("r-unreadable-rubric-review"),
        {
            "run_id": "r-unreadable-rubric-review",
            "project": PROJECT,
            "session": "coordinator-typo",
            "phase": "running",
            "node": {"id": PLAN_REVIEW_NODE, "plan": "", "section": ""},
        },
    )

    found = recovery._standing_plan_review(PROJECT, PLAN_SLUG, rubric="design")

    assert found == "r-unreadable-rubric-review", found
