"""One review verb composes a code neighbourhood around a plan or a path.

The verb resolves the production modules a plan's implementable sections name
together with the modules one import away in either direction, composes the
health-review brief for that neighbourhood, and keeps ``review-plan`` working
as an alias that composes the same plan brief.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, flight
from reckon.crew import plan_review

# The declared mutation: drop the one-hop import expansion so the neighbour
# module is missing from the composed brief and the neighbourhood assertion
# fails. Set the environment variable to run the red arm.
NEGATIVE_CONTROL_MUTATION = (
    "remove the one-hop import expansion so module b is missing from the composed brief"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_CODE_SUBJECT_NEGATIVE") == "1"

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

PLAN_HTML = """<!doctype html><html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
<meta name="plan-title" content="Fixture">
<meta name="plan-status" content="active">
<meta name="plan-version" content="3">
<meta name="plan-section-declarations" content="{&quot;s1&quot;:&quot;implementable&quot;}">
</head><body>
<h2 id="s1">First section</h2>
<p>Deliverable. Extend module <code>a</code>. Write paths: <code>reckon/a.py</code>.
Done when the module is extended and the suite passes.</p>
</body></html>
"""

FINDING_TEXT = "a re-implements the capability b already owns"


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    package = repo / "reckon"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text(
        "import reckon.b\n\n\nVALUE = reckon.b.VALUE + 1\n", encoding="utf-8"
    )
    (package / "b.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "c.py").write_text("OTHER = 2\n", encoding="utf-8")
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "fixture.html"
    plan.write_text(PLAN_HTML, encoding="utf-8")
    committed = repo / "docs" / "state" / "sample" / "reviews" / "plan" / "fixture"
    committed.mkdir(parents=True)
    (committed / "seed.json").write_text(
        json.dumps(
            {
                "project": "sample",
                "plan_slug": "fixture",
                "plan_version": 1,
                "rubric": "content",
                "findings": [
                    {
                        "id": "f1",
                        "anchor": "reckon/a.py:3",
                        "text": FINDING_TEXT,
                        "type": "duplicate_capability",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "reckon", "docs"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a plan and code."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"sample": str(repo / "docs")}))
    monkeypatch.setattr(flight, "resolve", lambda **kw: SimpleNamespace(config=CONFIG))
    return home, repo, plan


def _runner() -> CliRunner:
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:  # click >= 8.2 always separates stderr
        return CliRunner()


def _brief(argv: list[str]) -> tuple[dict, str]:
    result = _runner().invoke(cli.main, argv)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout if hasattr(result, "stdout") else result.output)
    return payload, payload["brief_text"]


def _line(brief: str, prefix: str) -> str:
    for line in brief.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"brief carries no {prefix!r} line:\n{brief}")


def test_around_composes_the_neighbourhood_brief(project, monkeypatch):
    _, _repo, _plan = project
    if NEGATIVE_CONTROL:
        # The declared mutation: the one-hop import expansion is removed, so the
        # brief loses module b and names only what it was given.
        monkeypatch.setattr(plan_review, "neighbourhood", lambda *a, **k: frozenset())
    payload, brief = _brief(
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--around",
            "fixture",
            "--session",
            "coordinator",
            "--dry-run",
        ]
    )
    assert payload["dry_run"] is True
    named = _line(brief, "Named paths:")
    neighbourhood = _line(brief, "Neighbourhood modules")
    assert "reckon/a.py" in named
    assert "reckon.b" in neighbourhood
    assert "reckon.c" not in neighbourhood
    assert "reckon/c.py" not in named
    assert FINDING_TEXT in brief
    figures = _line(brief, "Interface figures:")
    for key in ("public_definitions", "cli_options", "mcp_views", "refusal_families"):
        assert key in figures, figures


def test_review_plan_alias_composes_the_same_plan_brief(project):
    _home, _repo, _plan = project
    _head_payload, head_brief = _brief(
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "coordinator",
            "--dry-run",
        ]
    )
    _alias_payload, alias_brief = _brief(
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "coordinator",
            "--dry-run",
        ]
    )
    assert head_brief
    assert _without_run_identity(head_brief) == _without_run_identity(alias_brief)


def _without_run_identity(brief: str) -> str:
    """The brief with its per-invocation run id and stamped instant folded out.

    Two invocations of the verb compose the same content but mint their own run
    id and record their own instant, so equality of composition is measured
    with those two values folded out rather than compared literally.
    """
    folded = re.sub(r"r-\d{8}T\d+", "r-<run>", brief)
    return re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z", "<instant>", folded)


def test_review_plan_alias_prints_a_deprecation_line(project):
    _home, _repo, _plan = project
    result = _runner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "coordinator",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    stderr = getattr(result, "stderr", "") or ""
    assert "deprecated" in stderr
