"""A plan review its session cannot hear is refused before it writes anything.

A plan review composes its brief, plan snapshot and sidecar into the plan's
report directory, and only the dispatch that carries them away judges whether
the composing session can hear the run finish. So a session with no follower
was refused after the report directory existed: a caller that stopped before
the dispatch could discard it left a brief and a sidecar with no run behind
them, which is what the plan's next review attempt then read.

Every session-side refusal is answered before the composition, so a refused
review leaves no report directory. The positive case drives the same call with
a follower registered and shows the composition and the dispatch are reached,
so the guard refuses a session that would hear nothing rather than refusing
every session.

Declared negative control: with the delivery check moved after the composition,
the composed directory is written and then discarded by the dispatch refusal,
and the unchanged-report-directory assertion fails because composing creates
the plan's report root and its dispatch lock.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import flight
from reckon.crew import plan_review, runs

dispatch_commands = importlib.import_module("reckon.crew_dispatch_commands")
dispatch_module = importlib.import_module("reckon.crew.dispatch")
review_dispatch = importlib.import_module("reckon.crew.recovery_review_dispatch")

PROJECT = "sample"
PLAN_SLUG = "fixture"
SESSION = "coordinator"

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


PLAN_HTML = "".join(
    [
        "<!doctype html><html><head>",
        f'<meta name="docs-project" content="{PROJECT}">',
        '<meta name="reckon-type" content="plan">',
        f'<meta name="plan-slug" content="{PLAN_SLUG}">',
        '<meta name="plan-title" content="Fixture">',
        '<meta name="plan-status" content="active">',
        '<meta name="plan-version" content="3">',
        "</head><body>",
        '<h2 id="delivery">Delivery</h2>',
        "<p>Review this plan.</p>",
        "</body></html>",
    ]
)


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    monkeypatch.setattr(
        flight, "resolve", lambda **_kwargs: SimpleNamespace(config=CONFIG)
    )
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN_SLUG}.html").write_text(PLAN_HTML, encoding="utf-8")
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", f"docs/plans/{PLAN_SLUG}.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return home, repo


def _report_root() -> Path:
    """The plan's review-report directory, which a composition creates."""
    return plan_review._plan_review_report_root(PROJECT, PLAN_SLUG)


def _review(repo: Path):
    """Invoke the review verb as a coordinator would."""
    return CliRunner().invoke(
        dispatch_commands.crew,
        ["review", "--project", PROJECT, "--plan", PLAN_SLUG, "--session", SESSION],
    )


def test_a_review_without_a_follower_is_refused_before_it_writes(
    project, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, repo = project
    composed: list = []
    real_composer = review_dispatch._review_dispatch_fields

    def spy(*args, **kwargs):
        composed.append(args)
        return real_composer(*args, **kwargs)

    monkeypatch.setattr(review_dispatch, "_review_dispatch_fields", spy)
    assert not _report_root().exists(), "the fixture starts with no report directory"

    outcome = _review(repo)

    assert outcome.exit_code == 1, outcome.output
    assert "no registered follower" in outcome.output, outcome.output
    assert not _report_root().exists(), (
        "a refused review must leave the plan's report directory as it found it"
    )
    assert composed == [], "the brief composer ran before the session was judged"


def test_a_review_with_a_follower_composes_and_dispatches(
    project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home, repo = project
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare(*_args, **_kwargs):
        path = tmp_path / "worktree"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": "HEAD", "base_sha": head}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare)

    with runs.follower_claim(PROJECT, SESSION, delivery="stream") as claim:
        assert claim[0], f"the session's follower was not registered: {claim[1]}"
        outcome = _review(repo)

    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.stdout)
    assert payload.get("dispatched") is True, payload
    briefs = list(_report_root().glob("*/brief.md"))
    assert briefs, f"the composition wrote no brief under {_report_root()}"
