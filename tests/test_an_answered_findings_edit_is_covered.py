"""An edit a finding asked for is covered by the review that asked for it.

A review that raises a finding and is answered ``acted`` has already said what
the plan must change. The edit that follows is the review's own subject, so it
does not owe a second review while it stays under the word-share threshold —
even when the review-need judge would ask for one, which is what the gate did to
a 1 % correction a finding had asked for. An edit of the same size to a section
no finding named is still uncovered, and so is an edit past the threshold. The
answer names the commit it answers for, so the covering record carries the
evidence for its verdict.

The cases drive ``plan_review.review_coverage`` and the threshold the flight
layer declares; the fixture synthesises a mounted project under ``RECKON_HOME``,
so nothing here reads the operator's crew home.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import plan_review, recovery, review_need
from tests.test_review_plan_command import project as project  # noqa: PLC0414

RUN_ID = "answered-findings-edit"
FINDING_ID = "fix-five"
ANCHOR = "docs/plans/fixture.html#s5"
THRESHOLD = 0.30


def _plain(prefix: str, count: int = 100) -> str:
    """Distinct, single-token words, so one substitution moves a known share."""
    return " ".join(f"{prefix}{index:03d}" for index in range(count))


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
        "<p>Shared introduction text.</p>"
        f'<h2 id="s4">Four</h2><p>{_plain("four")}</p>'
        f'<h2 id="s5">Five</h2><p>{_plain("five")}</p>'
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
def sectioned(project):
    home, repo, path = project
    path.write_text(_base_html(), encoding="utf-8")
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["section_declarations"] = {"s4": "implementable", "s5": "implementable"}
    state["sections"] = _section_records(("s4", "s5"))
    path.write_text(_plan_html.write_state(text, state))
    return home, repo, path


def _subject(path, *, run_id=RUN_ID):
    return {
        "subject": "plan",
        "project": "sample",
        "plan_slug": "fixture",
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": "coordinator",
        "rubric": "design",
        "local": True,
        "run_id": run_id,
    }


def _finding():
    return {
        "id": FINDING_ID,
        "anchor": ANCHOR,
        "type": "correctness",
        "text": "Replace the stale word in this section.",
    }


def _store_review(path, *, findings=()):
    """Store the review the fixture plan's snapshot is read for."""
    fields = recovery._review_dispatch_fields(_subject(path))
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    sidecar["plan_version"] = 3
    Path(sidecar["report_path"]).write_text("RUBRIC wiring: pass\n")
    plan_review.store_plan_review(
        {
            **sidecar,
            "findings": list(findings),
            "responses": {},
            "review_run_id": RUN_ID,
        }
    )


def _requires_a_review(monkeypatch):
    """Stand in for a judge that asks for a review of every change.

    A real judge answers ``required`` for a correction a finding asked for —
    that is the refusal the incident recorded — so the cases state that answer
    outright rather than depending on a model call.
    """

    def judge(changes, *, subject, goals, threshold, caller=None):
        return {
            change.identity: review_need.Verdict(True, 0.9, "jev") for change in changes
        }

    monkeypatch.setattr(plan_review.review_need, "judge", judge)


def _coverage(path):
    return plan_review.review_coverage("sample", "fixture", plan=path)


def _section_paragraph_match(path, section):
    """Return the document text and the section paragraph's match.

    The state writer injects a ``<section>`` marker between a heading and its
    paragraph, so the paragraph is the first one after the heading rather than
    its immediate sibling.
    """
    text = path.read_text()
    heading = re.search(rf'<h2 id="{re.escape(section)}">[^<]*</h2>', text)
    assert heading, f"section {section} heading not found"
    paragraph = re.compile(r"<p>([^<]*)</p>").search(text, heading.end())
    assert paragraph, f"section {section} paragraph not found"
    return text, paragraph


def _edit_words(path, section, count: int):
    """Replace the section's first ``count`` words, and return the share moved."""
    before = plan_review._prose_texts(path)[section]
    text, paragraph = _section_paragraph_match(path, section)
    words = paragraph.group(1).split()
    for index in range(count):
        words[index] = f"edited{index:03d}"
    path.write_text(
        text[: paragraph.start(1)] + " ".join(words) + text[paragraph.end(1) :]
    )
    after = plan_review._prose_texts(path)[section]
    return plan_review._edit_share(before, after)


def _commit(repo: Path, message: str) -> str:
    for args in (["add", "docs/plans/fixture.html"], ["commit", "-q", "-m", message]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode().strip()


def _answer(repo: Path, path: Path, *, count: int, message: str):
    """Store the review, edit the plan, commit it, and answer the finding."""
    _store_review(path, findings=[_finding()])
    share = _edit_words(path, "s5", count)
    commit = _commit(repo, message)
    stored = plan_review.read_plan_review("sample", "fixture")
    assert stored is not None
    answered_path = plan_review.record_response(
        stored, FINDING_ID, action="acted", by="coordinator"
    )
    answered = json.loads(Path(answered_path).read_text())
    return share, commit, answered


def test_an_edit_a_finding_asked_for_is_covered(sectioned, monkeypatch):
    _, repo, path = sectioned
    _requires_a_review(monkeypatch)
    share, commit, answered = _answer(
        repo, path, count=1, message="fix the stale word the finding named"
    )

    assert share < THRESHOLD, f"the edit is 1 % of the section's words, not {share}"

    records, uncovered, _ = _coverage(path)
    assert "s5" not in uncovered, "the edit a finding asked for is covered"
    covering = [record for record in records if record.get("review_run_id") == RUN_ID]
    assert covering, "the answering review is the one that covers the edit"
    assert answered["responses"][FINDING_ID]["commit"] == commit, (
        "the answer records the commit of the edit it answers for"
    )
    assert covering[0]["responses"][FINDING_ID]["commit"] == commit, (
        "the covering record carries the edit's commit as its evidence"
    )


def test_a_same_size_edit_elsewhere_is_uncovered(sectioned, monkeypatch):
    _, repo, path = sectioned
    _requires_a_review(monkeypatch)
    _store_review(path, findings=[_finding()])
    _edit_words(path, "s4", 1)
    _commit(repo, "edit a section no finding named")
    stored = plan_review.read_plan_review("sample", "fixture")
    assert stored is not None
    plan_review.record_response(stored, FINDING_ID, action="acted", by="coordinator")

    _, uncovered, _ = _coverage(path)
    assert "s4" in uncovered, "a same-size edit outside the finding's unit is owed"
    assert "s5" not in uncovered


def test_an_edit_past_the_threshold_is_uncovered(sectioned, monkeypatch):
    _, repo, path = sectioned
    _requires_a_review(monkeypatch)
    _store_review(path, findings=[_finding()])
    total = len(plan_review._prose_texts(path)["s5"].split())
    share = _edit_words(path, "s5", int(total * THRESHOLD) + 1)
    _commit(repo, "rewrite a third of the section")
    stored = plan_review.read_plan_review("sample", "fixture")
    assert stored is not None
    plan_review.record_response(stored, FINDING_ID, action="acted", by="coordinator")

    assert share >= THRESHOLD, f"the edit is 30 % of the section's words, not {share}"
    assert share < 0.40, "but not much past it"
    _, uncovered, _ = _coverage(path)
    assert "s5" in uncovered, "an edit past the threshold still owes a review"


def test_an_unanswered_finding_earns_no_cover(sectioned, monkeypatch):
    _, _repo, path = sectioned
    _requires_a_review(monkeypatch)
    _store_review(path, findings=[_finding()])
    _edit_words(path, "s5", 1)

    _, uncovered, _ = _coverage(path)
    assert "s5" in uncovered, "the finding has asked for nothing until it is answered"
