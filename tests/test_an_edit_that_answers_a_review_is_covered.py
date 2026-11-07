"""An edit that only answers a review owes no new review.

A review raises findings and every one is answered; the edit that follows
answers them and changes nothing else. Before an acceptance the section is
uncovered, because its prose moved since the review read it. Once
``--accept-edits`` records the acceptance against that review, the units the
edits changed are covered and the record names them, the session and the
reason. A later edit moves a unit's digest and the section is uncovered again,
so an acceptance covers exactly the content it accepted.

The cases drive ``plan_review.review_coverage``,
``plan_review.accept_edits`` and the review verb's option; the fixture
synthesises a mounted project under ``RECKON_HOME``, so nothing here reads the
operator's crew home.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, cli
from reckon.crew import plan_review, recovery, review_need
from tests.test_review_plan_command import project as project  # noqa: PLC0414

RUN_ID = "edit-answers-a-review"
FINDING_ID = "answer-the-review"
ANCHOR = "docs/plans/fixture.html#s5"
THRESHOLD = 0.30
REASON = "the edit answers the finding; nothing else changed"
SESSION = "coordinator-session"
DECLINE = "the plan already carries the requested wording"


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

    An acceptance is the only thing under test here, so the judge states the
    stricter answer outright rather than depending on a model call: without the
    acceptance an edited unit stays uncovered.
    """

    def judge(changes, *, subject, goals, threshold, caller=None):
        return {
            change.identity: review_need.Verdict(True, 0.9, "jev") for change in changes
        }

    monkeypatch_judge = judge
    monkeypatch.setattr(plan_review.review_need, "judge", monkeypatch_judge)


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


def _edited(repo, path, *, answer=True):
    """Store a review, answer its finding, and edit the section it named."""
    _store_review(path, findings=[_finding()])
    share = _edit_words(path, "s5", 1)
    assert share < THRESHOLD, f"the edit is 1 % of the section's words, not {share}"
    _commit(repo, "answer the finding the review raised")
    if not answer:
        return share
    stored = plan_review.read_plan_review("sample", "fixture")
    assert stored is not None
    plan_review.record_response(
        stored, FINDING_ID, action="declined", reason=DECLINE, by="coordinator"
    )
    return share


def _accept(path, *, by=SESSION, reason=REASON):
    stored = plan_review.read_plan_review("sample", "fixture")
    assert stored is not None
    return plan_review.accept_edits(stored, plan=path, reason=reason, by=by)


def _last_acceptance(path):
    record = json.loads(Path(path).read_text())
    return record[plan_review.ACCEPTED_EDITS_KEY][-1]


def _accept_through_the_verb():
    return CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            SESSION,
            "--accept-edits",
            REASON,
        ],
    )


def test_an_answered_edit_is_uncovered_until_it_is_accepted(sectioned, monkeypatch):
    """The edit answers every finding, and still the section owes a review."""
    _, repo, path = sectioned
    _requires_a_review(monkeypatch)
    _edited(repo, path)

    records, uncovered, _ = _coverage(path)
    assert "s5" in uncovered, "an edit since the review is uncovered until accepted"
    covering = [record for record in records if record.get("review_run_id") == RUN_ID]
    assert covering, "the reviewed section stays covered"
    assert plan_review.unanswered_findings(covering[0]) == [], (
        "every finding is answered, so the debt is the missing acceptance"
    )
    assert plan_review.ACCEPTED_EDITS_KEY not in covering[0], (
        "nothing has been accepted yet"
    )


def test_accepting_the_edit_covers_the_units_it_names(sectioned, monkeypatch):
    _, repo, path = sectioned
    _requires_a_review(monkeypatch)
    _edited(repo, path)

    accepted_path = _accept(path)
    entry = _last_acceptance(accepted_path)

    assert entry["units"] == ["s5"], "the record names the unit the edit changed"
    assert entry["by"] == SESSION, "the record names the accepting session"
    assert entry["reason"] == REASON

    records, uncovered, _ = _coverage(path)
    assert "s5" not in uncovered, "the accepted edit is covered"
    covering = [record for record in records if record.get("review_run_id") == RUN_ID]
    assert covering, "the accepting review is the one that covers the edit"


def test_a_later_edit_uncovered_again(sectioned, monkeypatch):
    _, repo, path = sectioned
    _requires_a_review(monkeypatch)
    _edited(repo, path)
    _accept(path)
    assert "s5" not in _coverage(path)[1]

    _edit_words(path, "s5", 2)

    _, uncovered, _ = _coverage(path)
    assert "s5" in uncovered, "an edit after the acceptance moves the unit it covered"


def test_ignoring_the_acceptance_leaves_the_section_uncovered(sectioned, monkeypatch):
    """Negative control: a reader that never consults the record sees the debt."""
    _, repo, path = sectioned
    _requires_a_review(monkeypatch)
    _edited(repo, path)
    _accept(path)
    assert "s5" not in _coverage(path)[1], "the acceptance covers the edit"

    monkeypatch.setattr(
        plan_review, "_accepted_edit_units", lambda record, digests: set()
    )

    _, uncovered, _ = _coverage(path)
    assert "s5" in uncovered, (
        "without reading the acceptance record the acceptance has no effect"
    )


def test_accept_edits_refuses_an_unanswered_finding(sectioned, monkeypatch):
    _, repo, path = sectioned
    _requires_a_review(monkeypatch)
    _edited(repo, path, answer=False)

    stored = plan_review.list_plan_reviews("sample", "fixture")[0]
    with pytest.raises(ValueError, match="must be answered"):
        plan_review.accept_edits(stored, plan=path, reason=REASON, by=SESSION)

    persisted = _stored_review()
    assert plan_review.ACCEPTED_EDITS_KEY not in persisted, "the refusal writes nothing"


def _stored_review():
    """The single stored review the fixture wrote."""
    return plan_review.list_plan_reviews("sample", "fixture")[0]


def test_the_review_verb_records_the_acceptance(sectioned, monkeypatch):
    _, repo, path = sectioned
    _requires_a_review(monkeypatch)
    _edited(repo, path)

    result = _accept_through_the_verb()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["accepted_edits"]["units"] == ["s5"]
    assert payload["accepted_edits"]["by"] == SESSION
    assert payload["unanswered"] == []
    assert plan_review.review_coverage("sample", "fixture", plan=path)[1] == set()


def test_the_review_verb_refuses_an_unanswered_finding(sectioned, monkeypatch):
    _, repo, path = sectioned
    _requires_a_review(monkeypatch)
    _edited(repo, path, answer=False)

    result = _accept_through_the_verb()

    assert result.exit_code != 0
    assert "must be answered" in result.stdout
    stored = _stored_review()
    assert plan_review.ACCEPTED_EDITS_KEY not in stored
