"""A plan write names the units it uncovered, not only the plan's whole debt.

``review_owed`` is the plan's whole review debt: a stored review leaves a unit
uncovered for as long as the unit's prose has moved, so a metadata write that
changed nothing in the reviewed digest still reports that debt. A coordinator
reading it as its own doing composes a review the plan did not need — the
project paid 98 plan reviews against 143 implementation runs in four days.

So every plan write also answers with ``uncovered_by_write``: the units this
write moved from covered to uncovered, found by comparing coverage before and
after it. A metadata or lifecycle write uncovers nothing; a prose edit names
the section it changed; a create, which has no content before it, names every
unit it wrote. These cases drive the tool surface against a synthesised mounted
project under ``RECKON_HOME``, so nothing here reads or writes the operator's
crew home.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import _plan_html, flight, mcp
from reckon.crew import plan_review

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
    "fences": {"time_budget": "20m", "needs_help_after_fixes": 2},
}

SNAPSHOT_RUN_ID = "a-write-names-what-it-uncovered"
DONE_WHEN = "delta ships and nothing regresses"


def _words(prefix: str) -> str:
    return " ".join(f"{prefix}{index:02d}" for index in range(12))


def _base_html(status: str = "active") -> str:
    return (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="Fixture">'
        f'<meta name="plan-status" content="{status}">'
        '<meta name="plan-version" content="3">'
        "</head><body>"
        f'<h2 id="s2">Bravo</h2><p>{_words("bravo")}</p>'
        f'<h2 id="s3">Charlie</h2><p>{_words("charlie")}</p>'
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
    state["section_declarations"] = {"s2": "implementable", "s3": "implementable"}
    state["sections"] = _section_records(("s2", "s3"))
    path.write_text(_plan_html.write_state(text, state))
    return home, repo, path


def _store_review(document: str) -> dict:
    """Store a review of ``document`` with its snapshot, under ``RECKON_HOME``."""
    record = {
        "project": "sample",
        "plan_slug": "fixture",
        "plan_version": 3,
        "reviewed_blob_sha": "a" * 40,
        "plan_fingerprint": plan_review.plan_fingerprint(document),
        "section_digests": plan_review._section_digests(document),
        "findings": [],
        "responses": {},
        "status": "ready",
        "review_run_id": SNAPSHOT_RUN_ID,
    }
    plan_review.store_plan_review(record)
    directory = plan_review.review_report_directory(
        "sample", "fixture", SNAPSHOT_RUN_ID
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / plan_review._REVIEW_SNAPSHOT_NAME).write_text(
        document, encoding="utf-8"
    )
    return record


def _stale_debt(store) -> Path:
    """The fixture with s2's prose already moved past the stored review.

    The review is stored against the plan as it stands, and only then is s2's
    prose rewritten outside the tool, so s2 is the plan's pre-existing debt
    before any write under test runs.
    """
    _, _repo, path = store
    text = path.read_text()
    _store_review(text)
    path.write_text(text.replace(_words("bravo"), _words("zulu")), encoding="utf-8")
    return path


def _version(path: Path) -> int:
    return int(_plan_html.read_state(path.read_text())["version"])


def _units(entries) -> set[str]:
    return {entry["unit"] for entry in entries}


def test_a_sprint_write_names_the_whole_debt_and_uncovers_nothing(sectioned):
    _, repo, path = sectioned
    _stale_debt(sectioned)
    result = mcp._edit_plan_tool(
        "sample",
        "fixture",
        expected_version=_version(path),
        ops=[{"op": "set", "path": "sprint", "value": "S99"}],
        checkout_path=str(repo),
    )
    assert result["ok"], result
    assert "s2" in _units(result["review_owed"]), result
    assert result["uncovered_by_write"] == []


@pytest.fixture
def closable(project):
    home, repo, path = project
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["section_declarations"] = {"s2": "implementable", "s3": "implementable"}
    state["sections"] = _section_records(("s2", "s3"))
    state["status"] = "done"
    path.write_text(_plan_html.write_state(text, state))
    evidence = repo / "docs" / "evidence" / "archive" / "fixture-landed.html"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="evidence">'
        '<meta name="plan-evidence-for" content="fixture">',
        encoding="utf-8",
    )
    return home, repo, path


def test_a_close_names_the_whole_debt_and_uncovers_nothing(closable):
    _, repo, path = closable
    _stale_debt(closable)
    result = mcp._edit_plan_tool(
        "sample",
        "fixture",
        expected_version=_version(path),
        ops=[
            {
                "op": "close",
                "status": "done",
                "outcome": "fixture landed — no followup",
                "by": "coordinator",
            }
        ],
        checkout_path=str(repo),
    )
    assert result["ok"], result
    assert "s2" in _units(result["review_owed"]), result
    assert result["uncovered_by_write"] == []


def test_a_prose_edit_names_only_the_unit_it_uncovered(sectioned):
    _, repo, path = sectioned
    _stale_debt(sectioned)
    result = mcp._edit_plan_tool(
        "sample",
        "fixture",
        expected_version=_version(path),
        mode="text",
        old_html=f"<p><strong>Done when</strong>: {DONE_WHEN}.</p>",
        new_html="<p><strong>Done when</strong>: delta ships and nothing breaks.</p>",
        checkout_path=str(repo),
    )
    assert result["ok"], result
    assert _units(result["review_owed"]) == {"s2", "s3"}, result
    # s2 is the plan's existing debt; only s3 is this write's doing.
    assert _units(result["uncovered_by_write"]) == {"s3"}, result


def test_a_create_names_every_unit_it_wrote(project):
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
                "value": "A plan created with no content before it.",
            }
        ],
        checkout_path=str(repo),
    )
    assert result["ok"], result
    owed = _units(result["review_owed"])
    assert owed, result
    assert _units(result["uncovered_by_write"]) == owed, result


def test_the_edit_skill_says_which_list_to_act_on():
    text = (
        Path(__file__).parents[1] / "skills" / "reckon-edit" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "uncovered_by_write" in text
    # The write's own list is what its author acts on; the whole debt is the
    # dispatch-time question, answered before a build rather than after a write.
    after_write = text.split("uncovered_by_write", 1)[1]
    assert "review_owed" in after_write
