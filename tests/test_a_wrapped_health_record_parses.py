"""A health review whose records wrap across lines still parses.

A reviewer writes its emitted records by hand, so a long record wraps wherever
the writer's line broke. The parser reads each record as one source line, which
drops every field label that wrapped past the first line — a warranted treatment
whose ``WRITE_PATHS:`` began on the next line parses as naming no write path and
the store refuses a report whose content is sound. The records are reassembled
before they are read, so a wrapped record carries all of its text.

The fixture is the delivered report of a real review whose treatment wrapped,
kept verbatim so the parse is held to the text that defeated the old reader.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew.review import parse_health_review

FIXTURE = Path(__file__).parent / "fixtures" / "wrapped-health-review.md"

# One line per record: no record wraps, so this parses to the same shape it did
# before the records were joined — the join must not disturb a line it does not
# need to continue.
ONE_LINE_PER_RECORD = """\
RUBRIC known_findings: pass — no open finding is named for the neighbourhood.
RUBRIC duplicate_capability: a finding — path normalisation is implemented twice.
RUBRIC unreached: pass — every module is reached.
RUBRIC interface_growth: pass — nothing was added in the window.
RUBRIC width: pass — no module is over the worker-window bound.
RUBRIC test_debt: pass — no duplicated helper.
FINDING duplicate_capability reckon/c.py:12 — CLASS: duplicated_capability — normalise_path is re-implemented at reckon/d.py:57, so a fix to one leaves the other wrong — CITES: new
TREATMENT — GOAL: delete the second implementation — WRITE_PATHS: reckon/c.py, reckon/d.py — DONE_WHEN: the duplicate is gone and the suite passes; negative control: removing the survivor fails — EXPECTED_DELTA: -24
"""


def test_delivered_report_treatment_names_its_write_paths_and_delta() -> None:
    """The report whose wrap defeated the parser parses with its whole treatment."""
    treatment = parse_health_review(FIXTURE.read_text())["treatment"]
    assert treatment["write_paths"] == [
        "reckon/crew/obligation_snapshot.py",
        "reckon/crew/obligations.py",
    ]
    assert treatment["expected_delta"] == "-18"
    assert "single owner of the unreadable-row build" in treatment["goal"]


def test_wrapped_finding_keeps_its_class_and_whole_text() -> None:
    """A finding wrapped after ``CLASS:`` keeps both halves the wrap separated."""
    findings = parse_health_review(FIXTURE.read_text())["findings"]
    first = findings[0]
    assert first["item"] == "duplicate_capability"
    assert first["anchor"] == "reckon/crew/obligation_snapshot.py:107"
    assert first["class"] == "duplicated_capability"
    assert first["text"] == (
        "`_config_home` is re-implemented at "
        "`reckon/hooks/coordinator_obligations.py:242` (docstring at :242 states "
        "the copy is intentional to avoid importing the store on the prompt "
        "path), so the two can drift"
    )
    assert first["cites"].startswith("new (cited by the clone detector")


def test_one_line_per_record_parses_to_the_same_shape() -> None:
    """A report with no wrapped record parses exactly as it did before the join."""
    record = parse_health_review(ONE_LINE_PER_RECORD)
    assert record["absent_items"] == []
    assert record["findings"] == [
        {
            "class": "duplicated_capability",
            "text": (
                "normalise_path is re-implemented at reckon/d.py:57, so a fix to "
                "one leaves the other wrong"
            ),
            "cites": "new",
            "id": "duplicate_capability-1",
            "item": "duplicate_capability",
            "anchor": "reckon/c.py:12",
        }
    ]
    assert record["treatment"] == {
        "goal": "delete the second implementation",
        "write_paths": ["reckon/c.py", "reckon/d.py"],
        "done_when": (
            "the duplicate is gone and the suite passes; negative control: "
            "removing the survivor fails"
        ),
        "expected_delta": "-24",
    }


def test_a_heading_ends_a_record_rather_than_joining_it() -> None:
    """A heading after a record is not swallowed as a continuation line."""
    text = """\
FINDING known_findings reckon/a.py:41 — CLASS: a_class — the text — CITES: new
## Next section
prose that follows the heading
TREATMENT: none — nothing owed.
"""
    finding = parse_health_review(text)["findings"][0]
    assert finding["class"] == "a_class"
    assert finding["text"] == "the text"
    assert "prose that follows" not in finding["text"]


def test_a_blank_line_ends_a_record_rather_than_joining_it() -> None:
    """The prose paragraph after a blank line is not joined into the record."""
    text = """\
FINDING known_findings reckon/a.py:41 — CLASS: a_class — the text — CITES: new

A paragraph explaining the finding, which is not part of it.
TREATMENT: none — nothing owed.
"""
    finding = parse_health_review(text)["findings"][0]
    assert finding["text"] == "the text"


def test_a_bare_treatment_keyword_opens_a_record_its_lines_complete() -> None:
    """A record may wrap immediately after ``TREATMENT``, before any label."""
    text = """\
RUBRIC known_findings: pass — nothing open for the neighbourhood.
TREATMENT
— GOAL: delete the second implementation — WRITE_PATHS: reckon/c.py
— DONE_WHEN: the duplicate is gone and the suite passes
— EXPECTED_DELTA: -12
"""
    treatment = parse_health_review(text)["treatment"]
    assert treatment["write_paths"] == ["reckon/c.py"]
    assert treatment["goal"] == "delete the second implementation"
    assert treatment["done_when"] == "the duplicate is gone and the suite passes"
    assert treatment["expected_delta"] == "-12"


def test_a_treatment_keyword_and_separator_alone_opens_a_record() -> None:
    """``TREATMENT —`` with the labelled fields on the lines below opens one."""
    text = """\
RUBRIC known_findings: pass — nothing open for the neighbourhood.
TREATMENT —
GOAL: delete the second implementation
WRITE_PATHS: reckon/c.py, reckon/d.py
DONE_WHEN: the duplicate is gone and the suite passes
EXPECTED_DELTA: -12
"""
    treatment = parse_health_review(text)["treatment"]
    assert treatment["write_paths"] == ["reckon/c.py", "reckon/d.py"]
    assert treatment["expected_delta"] == "-12"
