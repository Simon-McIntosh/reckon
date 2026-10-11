"""One review verb composes a code neighbourhood around a plan or a path.

The verb resolves the production modules a plan's implementable sections name
together with the modules one import away in either direction and composes the
health-review brief for that neighbourhood.
"""

from __future__ import annotations

import hashlib
import json
import os
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

# A delivered health review of the fixture neighbourhood, in the grammar the
# rubric's reader takes: one RUBRIC line per item, one FINDING line, and the
# single TREATMENT line the store refuses a report without.
HEALTH_REPORT = """RUBRIC known_findings: pass — the brief names no open finding.
RUBRIC duplicate_capability: a finding — `reckon/b.py:1` owns the constant `a` re-reads.
RUBRIC unreached: pass — every module is reached by an entry point.
RUBRIC interface_growth: pass — no public surface was added.
RUBRIC width: pass — one module changed.
RUBRIC test_debt: pass — no test asserts source text.
FINDING duplicate_capability reckon/a.py:3 — CLASS: duplicated_capability — `a` re-reads a constant `b` already owns — CITES: new
TREATMENT: none — the neighbourhood is healthy
"""


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


def _expected_neighbourhood_key(named: list[str], modules: list[str]) -> str:
    """The neighbourhood key from its documented form, computed independently.

    The write path keys a neighbourhood by digesting the sorted union of the
    named paths and the adjacent modules and keeping the first twelve hex
    characters. The stored key is checked against this expectation rather than
    against :func:`plan_review.code_review_key`, so the read side cannot move in
    step with the write side and agree vacuously.
    """
    blob = json.dumps(
        sorted({str(item) for item in named} | {str(module) for module in modules}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def test_a_delivered_code_report_is_stored_under_its_neighbourhood(project):
    """The plan-build delivery path stores a code review beside a plan's.

    A composed code review writes its brief and its sidecar into the report
    directory its neighbourhood keys, and the reviewer's report lands beside
    them. Storing the plan's deliveries must take that report through the health
    rubric's own reader and store it under the neighbourhood, so the stored half
    of the review exists without a store step of its own.
    """
    _home, _repo, _plan = project
    subject = plan_review.code_review_subject("sample", "coordinator", around="fixture")
    plan_review.code_review_fields(subject, write=True)
    directory = plan_review.code_review_report_directory(subject)
    (directory / "report.md").write_text(HEALTH_REPORT, encoding="utf-8")
    key = _expected_neighbourhood_key(subject["named_paths"], subject["modules"])
    assert plan_review.code_review_record("sample", key) is None

    refused = plan_review.store_delivered_reviews("sample", "fixture")

    assert refused == []
    record = plan_review.code_review_record("sample", key)
    assert record is not None, plan_review.code_review_path("sample", key)
    assert plan_review.code_review_path("sample", key).is_file()
    assert record["subject"] == "code"
    assert record["neighbourhood_key"] == key
    assert record["around"] == "fixture"
    assert record["review_run_id"] == subject["run_id"]
    assert [finding["anchor"] for finding in record["findings"]] == ["reckon/a.py:3"]
    assert record["treatment"] == {"none": "the neighbourhood is healthy"}
    # The delivery is stored once: a later pass finds the record already there.
    assert plan_review.store_delivered_reviews("sample", "fixture") == []
    assert plan_review.list_code_reviews("sample") != []


def test_a_path_only_delivered_code_report_is_stored(project):
    """A code review composed with ``--path`` alone is stored by the delivery path.

    A path-only subject names no plan, so its sidecar carries an empty
    ``around``. The plan-build delivery path must still read the reviewer's
    report into the store keyed by its neighbourhood, or a review the
    coordinator dispatched is composed and dispatched and then never stored.
    """
    _home, _repo, _plan = project
    subject = plan_review.code_review_subject(
        "sample", "coordinator", paths=["reckon/a.py"]
    )
    assert subject["around"] == ""
    plan_review.code_review_fields(subject, write=True)
    directory = plan_review.code_review_report_directory(subject)
    (directory / "report.md").write_text(HEALTH_REPORT, encoding="utf-8")
    key = _expected_neighbourhood_key(subject["named_paths"], subject["modules"])
    assert plan_review.code_review_record("sample", key) is None

    refused = plan_review.store_delivered_reviews("sample", "fixture")

    assert refused == []
    record = plan_review.code_review_record("sample", key)
    assert record is not None, plan_review.code_review_path("sample", key)
    assert record["subject"] == "code"
    assert record["neighbourhood_key"] == key
    assert record["around"] == ""
    assert [finding["anchor"] for finding in record["findings"]] == ["reckon/a.py:3"]
    assert plan_review.list_code_reviews("sample") != []


def test_the_review_verb_refuses_a_plan_given_with_a_code_subject(project):
    """Naming both subject kinds is refused rather than silently preferring one."""
    _home, _repo, _plan = project
    for extra in (["--around", "fixture"], ["--path", "reckon/a.py"]):
        result = _runner().invoke(
            cli.main,
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
                *extra,
            ],
        )
        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["error"] == "crew_error"
        assert len(payload["detail"].splitlines()) == 1
        assert "--plan" in payload["detail"]
        assert "--around" in payload["detail"] or "--path" in payload["detail"]
        assert "brief_text" not in payload
