"""A portfolio review composes one disposition per live plan.

The verb's ``--portfolio`` subject reads every non-shipped plan of a project,
composes a brief carrying each plan's derived implementation fraction, commit
age and sprint, and reads the reviewer's one-DISPOSITION-per-plan output into a
disposition round that refuses an output omitting a plan in scope.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, doccheck, flight
from reckon.crew import plan_review

# The declared mutation: make the plan filter include shipped plans, so the
# shipped plan is listed and the omission assertion fails. Set the environment
# variable to run the red arm.
NEGATIVE_CONTROL_MUTATION = (
    "make the plan filter include shipped plans so the shipped plan is listed"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_PORTFOLIO_NEGATIVE") == "1"

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "30m",
        }
    },
    "roles": {"review": {"backend": "worker", "execution_capable": True}},
    "fences": {"time_budget": "30m", "needs_help_after_failures": 2},
}

PLAN_TEMPLATE = """<!doctype html><html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<meta name="plan-status" content="{status}">
<meta name="plan-version" content="1">
<meta name="plan-section-declarations" content="{declarations}">
</head><body>
<h2 id="s1">First section</h2>
<p>Deliverable. Extend module <code>a</code>. Done when the suite passes.</p>
<h2 id="s2">Second section</h2>
<p>Deliverable. Extend module <code>b</code>. Done when the suite passes.</p>
</body></html>
"""

# One plan per lifecycle shape: a shipped plan (omitted), an active plan whose
# every section is done (impl 1.0), an untouched draft (impl 0), and an active
# plan with one of two sections done (impl 0.5).
PLANS = {
    "shipped": ("Shipped", "shipped", ""),
    "allsections": ("All Done", "active", "{&quot;s1&quot;:&quot;done&quot;}"),
    "draft": ("Draft", "draft", ""),
    "progress": (
        "Progress",
        "active",
        "{&quot;s1&quot;:&quot;done&quot;,&quot;s2&quot;:&quot;implementable&quot;}",
    ),
}

IN_SCOPE = ("allsections", "draft", "progress")

REVIEWER_OUTPUT = """DISPOSITION allsections: close — every section is already in the tree
DISPOSITION draft: retire — never started, subject dropped
DISPOSITION progress: re-sprint next — still wanted, wrong sprint
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    for slug, (title, status, declarations) in PLANS.items():
        (plans / f"{slug}.html").write_text(
            PLAN_TEMPLATE.format(
                slug=slug, title=title, status=status, declarations=declarations
            ),
            encoding="utf-8",
        )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "docs"],
        [
            "commit",
            "-q",
            "-m",
            "chore: seed fixture",
            "-m",
            "Supply four plans of distinct lifecycle shapes.",
        ],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"sample": str(repo / "docs")}))
    monkeypatch.setattr(flight, "resolve", lambda **kw: SimpleNamespace(config=CONFIG))
    return home, repo, plans


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


def _plan_line(brief: str, slug: str) -> str:
    for line in brief.splitlines():
        if line.startswith(f"PLAN {slug}:"):
            return line
    raise AssertionError(f"brief carries no line for plan {slug!r}:\n{brief}")


def test_portfolio_lists_the_non_shipped_plans_with_their_figures(project, monkeypatch):
    _home, _repo, _plans = project
    if NEGATIVE_CONTROL:
        # The declared mutation: the shipped filter is emptied, so the shipped
        # plan is listed and the omission assertion below fails.
        monkeypatch.setattr(doccheck, "SHIPPED_PLAN_STATUSES", frozenset())
    payload, brief = _brief(
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--portfolio",
            "--session",
            "coordinator",
            "--dry-run",
        ]
    )
    assert payload["dry_run"] is True
    for slug in IN_SCOPE:
        line = _plan_line(brief, slug)
        # The derived implementation fraction and the commit age are both
        # carried, each with the reader that supplied it.
        assert f"impl={_expected_impl(slug):g}" in line, line
        assert "age=" in line and "d (" in line, line
    assert "PLAN shipped" not in brief
    assert "PLAN shipped:" not in brief


def _expected_impl(slug: str) -> float:
    """The derived implementation fraction each fixture plan should carry."""
    return {"allsections": 1.0, "draft": 0.0, "progress": 0.5}[slug]


def test_a_fixed_reviewer_output_parses_to_one_disposition_per_plan(project):
    _home, _repo, _plans = project
    parsed = plan_review.parse_portfolio_review(REVIEWER_OUTPUT, plans=IN_SCOPE)
    assert [d["plan_slug"] for d in parsed["dispositions"]] == list(IN_SCOPE)
    assert [d["disposition"] for d in parsed["dispositions"]] == [
        "close",
        "retire",
        "re-sprint",
    ]
    assert parsed["dispositions"][2]["target"] == "next"
    assert parsed["rubric"] == plan_review.PORTFOLIO_REVIEW_RUBRIC


def test_a_reviewer_output_omitting_a_plan_is_refused_by_name(project):
    _home, _repo, _plans = project
    omitted = "".join(
        line
        for line in REVIEWER_OUTPUT.splitlines(keepends=True)
        if not line.startswith("DISPOSITION progress")
    )
    with pytest.raises(plan_review.PortfolioReviewError) as caught:
        plan_review.parse_portfolio_review(omitted, plans=IN_SCOPE)
    assert caught.value.missing == ["progress"]
    assert "progress" in str(caught.value)


def test_the_verb_refuses_a_portfolio_named_with_another_subject(project):
    _home, _repo, _plans = project
    result = _runner().invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "draft",
            "--portfolio",
            "--session",
            "coordinator",
            "--dry-run",
        ],
    )
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["error"] == "crew_error"
    assert len(payload["detail"].splitlines()) == 1
    assert "--portfolio" in payload["detail"] or "--plan" in payload["detail"]
