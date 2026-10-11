"""The health-review parser reads its rubric's emitted form.

A health review reads a neighbourhood of code rather than one diff, and its
parser is the only reader of what it emits: the rubric prompt asks for six item
verdicts, classed findings and exactly one treatment, and this test holds the
parser to that form and the prompt to the parser. The two cannot drift — an item
added to the parser and not to the prompt is a check nobody is told to make, and
the reverse is a check no reader can enumerate — so the prompt's own
``RUBRIC_ITEMS`` line is compared against the parser's item constant.

The one-treatment rule is the review's point: a neighbourhood produces at most
one repair, and that is what keeps a health review from becoming busy work. A
review that emits two treatments is refused by name, and so is a treatment that
names no write path, because a repair that cannot be dispatched is not a repair.
"""

from __future__ import annotations

import pytest

from reckon.crew import review as review_module
from reckon.crew.review import (
    HEALTH_MULTIPLE_TREATMENTS,
    HEALTH_REVIEW_ITEMS,
    HEALTH_TREATMENT_WITHOUT_WRITE_PATHS,
    HealthReviewError,
    parse_health_review,
)

# A fixed reviewer output: six item verdicts, two classed findings (one citing a
# findings-reader row, one new) and one warranted treatment.
FIXED_REVIEW = """\
RUBRIC known_findings: a finding — two open rows name reckon/a.py:41, one defect class recurring across two reviews.
RUBRIC duplicate_capability: a finding — path normalisation is implemented twice, at reckon/c.py:12 and reckon/d.py:57.
RUBRIC unreached: pass — every module the brief lists is reached by an entry point.
RUBRIC interface_growth: pass — nothing was added in the window without a retirement.
RUBRIC width: pass — no module in the neighbourhood is over the worker-window bound.
RUBRIC test_debt: a finding — a test asserts the file's source text rather than its behaviour.
FINDING duplicate_capability reckon/c.py:12 — CLASS: duplicated_capability — normalise_path is re-implemented at reckon/d.py:57, so a fix to one leaves the other wrong — CITES: new
FINDING test_debt tests/test_c.py:20 — CLASS: source_text_assertion — the test asserts the file's text contains a token — CITES: reckon/a.py:41
TREATMENT — GOAL: delete the second implementation and route callers to the first — WRITE_PATHS: reckon/c.py, reckon/d.py — DONE_WHEN: reckon/d.py no longer defines normalise_path and the suite passes; negative control: removing the survivor turns tests/test_c.py::test_normalise red — EXPECTED_DELTA: -24
"""

NONE_TREATMENT = """\
RUBRIC known_findings: pass — no open finding is named for the neighbourhood.
RUBRIC duplicate_capability: pass — one implementation per capability.
RUBRIC unreached: pass — every module is reached.
RUBRIC interface_growth: pass — no surface was added in the window.
RUBRIC width: pass — no module is over the bound.
RUBRIC test_debt: pass — no duplicated helper or source-text assertion.
TREATMENT: none — every item passes, so no repair is owed.
"""


def _items_from_prompt(text: str) -> list[str]:
    """Read the ``RUBRIC_ITEMS:`` line the prompt carries as its own list."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("RUBRIC_ITEMS:"):
            payload = stripped.split(":", 1)[1]
            return [part.strip() for part in payload.split(",") if part.strip()]
    raise AssertionError(
        "prompt carries no RUBRIC_ITEMS line, so its items cannot be enumerated"
    )


def test_fixed_review_parses_six_verdicts() -> None:
    record = parse_health_review(FIXED_REVIEW)
    assert record["rubric"] == review_module.HEALTH_REVIEW_RUBRIC
    assert set(record["rubric_items"]) == set(HEALTH_REVIEW_ITEMS)
    assert len(record["rubric_items"]) == 6
    assert record["absent_items"] == []


def test_findings_carry_their_class_and_anchor() -> None:
    record = parse_health_review(FIXED_REVIEW)
    findings = record["findings"]
    assert [finding["class"] for finding in findings] == [
        "duplicated_capability",
        "source_text_assertion",
    ]
    first = findings[0]
    assert first["item"] == "duplicate_capability"
    assert first["anchor"] == "reckon/c.py:12"
    assert first["cites"] == "new"
    assert first["id"] == "duplicate_capability-1"
    # A finding already in the brief cites the findings-reader row it came from.
    assert findings[1]["cites"] == "reckon/a.py:41"


def test_one_treatment_carries_write_paths_and_a_done_when() -> None:
    treatment = parse_health_review(FIXED_REVIEW)["treatment"]
    assert treatment["write_paths"] == ["reckon/c.py", "reckon/d.py"]
    assert treatment["done_when"]
    assert "negative control" in treatment["done_when"]
    assert treatment["goal"]
    assert treatment["expected_delta"] == "-24"


def test_treatment_none_form_parses() -> None:
    treatment = parse_health_review(NONE_TREATMENT)["treatment"]
    assert "none" in treatment
    assert treatment["none"]
    assert "write_paths" not in treatment


def test_two_treatment_lines_are_refused_by_name() -> None:
    doubled = FIXED_REVIEW + (
        "TREATMENT — GOAL: another repair — WRITE_PATHS: reckon/e.py — "
        "DONE_WHEN: the repair holds; negative control: removing it fails — "
        "EXPECTED_DELTA: -3\n"
    )
    with pytest.raises(HealthReviewError) as raised:
        parse_health_review(doubled)
    assert raised.value.reason == HEALTH_MULTIPLE_TREATMENTS


def test_treatment_naming_no_write_path_is_refused_by_name() -> None:
    without_paths = """\
RUBRIC known_findings: pass — nothing open for the neighbourhood.
TREATMENT — GOAL: delete the second implementation — DONE_WHEN: the duplicate is gone and the suite passes — EXPECTED_DELTA: -24
"""
    with pytest.raises(HealthReviewError) as raised:
        parse_health_review(without_paths)
    assert raised.value.reason == HEALTH_TREATMENT_WITHOUT_WRITE_PATHS


def test_prompt_names_exactly_the_parsers_six_items() -> None:
    text = review_module.load_health_review_prompt()
    # Direction 1: every item the parser enumerates is named in the prompt.
    for item in HEALTH_REVIEW_ITEMS:
        assert item in text, f"rubric item {item} missing from the prompt"
    # Direction 2: the prompt's own list is exactly the parser's, so an item
    # added to one and not the other is caught.
    assert _items_from_prompt(text) == list(HEALTH_REVIEW_ITEMS)


def test_prompt_states_the_one_treatment_rule() -> None:
    text = review_module.load_health_review_prompt().lower()
    assert "exactly one `treatment` line" in text
    assert "none" in text


def test_removing_an_item_from_the_prompt_fails_the_mirror() -> None:
    """The mirror is a check that can fail: drop one item and it no longer holds.

    This exercises the same comparison ``test_prompt_names_exactly_the_parsers_six_items``
    makes, on a prompt text with one item deleted, so the direction-2 half is
    shown to reject a drifted prompt rather than merely to pass on the real one.
    """
    text = review_module.load_health_review_prompt()
    drifted = text.replace("known_findings, ", "", 1)
    assert drifted != text
    assert _items_from_prompt(drifted) != list(HEALTH_REVIEW_ITEMS)
