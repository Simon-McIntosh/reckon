"""A figure review's record contract states the shape of a finding.

A figure reviewer reads two texts: the rubric prompt ``figure_review.md`` and
the composed brief ``_review_brief_text`` composes beside it. Neither said what
a finding is or that it declares a severity, so a reviewer improvised and the
store read a follow-on left unlabelled as a defect that holds the node, which
refused a promotion the reviewed run had earned. The prompt now names the four
fields a finding carries — ``file``, ``line``, ``severity`` and ``summary`` —
and the two words :data:`reckon.crew.review.FINDING_SEVERITIES` declares, and
the brief's record contract for the figure rubric lists the figure record's own
fields with no text scores.

The falsifiers read the prompt as text, read the composed brief's contract
block for both the figure and the text rubrics, and drive the two findings a
record carries through :func:`declared_severity` so the severities a reviewer
writes are the ones a gate reads.
"""

from __future__ import annotations

from reckon.crew import review as review_module
from reckon.crew.recovery_review_subject import _review_brief_text

PROJECT = "proj"
RUN_ID = "r-20260101T000000000000-reviewed-run"


def _contract_block(text: str) -> str:
    """The record contract a brief carries, split from the rubric above it."""
    contract = text.partition("Review record contract")[2]
    return contract.partition("\n\n")[0]


def test_the_prompt_names_the_four_finding_fields() -> None:
    prompt = review_module.load_figure_review_prompt()
    collapsed = " ".join(prompt.split())

    for field in ("file", "line", "severity", "summary"):
        assert field in collapsed, (
            f"the figure prompt does not name the finding field {field!r}, so a "
            "reviewer cannot record one in the shape the store reads"
        )


def test_the_prompt_names_every_declared_severity() -> None:
    prompt = review_module.load_figure_review_prompt()
    collapsed = " ".join(prompt.split())

    for severity in review_module.FINDING_SEVERITIES:
        assert severity in collapsed, (
            f"the figure prompt does not teach the declared severity "
            f"{severity!r}, so a reviewer cannot declare it"
        )


def test_the_prompt_defines_blocking_by_the_reader_stopping_defects() -> None:
    collapsed = " ".join(review_module.load_figure_review_prompt().split())

    for defect in (
        "clipped or overlapping label",
        "unreadable text",
        "does not let a reader see",
    ):
        assert defect in collapsed, (
            f"the figure prompt does not name the reader-stopping defect "
            f"{defect!r}, so 'blocking' is left for a reviewer to guess"
        )


def test_the_prompt_tells_the_reviewer_to_omit_scores() -> None:
    prompt = review_module.load_figure_review_prompt()
    collapsed = " ".join(prompt.split())

    assert "SCORE" not in prompt, (
        "the figure prompt asks for SCORE lines, which a figure record does not carry"
    )
    assert "omit them" in collapsed
    assert "scores" in collapsed


def test_the_figure_brief_requires_no_scores_and_names_the_finding_fields() -> None:
    text = _review_brief_text({"run_id": RUN_ID}, "2" * 40, rubric="figure")
    contract = _contract_block(text)

    assert "scores" not in contract, (
        "a figure brief still lists scores among its required fields, which is "
        "why reviewers write the six text dimensions as null"
    )
    for field in ("file", "line", "severity", "summary"):
        assert field in contract, (
            f"the figure brief's contract does not name the finding field {field!r}"
        )


def test_the_text_brief_still_requires_scores_with_all_six_dimensions() -> None:
    text = _review_brief_text({"run_id": RUN_ID}, "2" * 40, rubric="design")
    contract = _contract_block(text)

    assert "scores" in contract
    for dimension in review_module.REVIEW_DIMENSIONS:
        assert dimension in contract, (
            f"the text brief no longer requires the dimension {dimension!r}"
        )


def test_the_record_a_reviewer_writes_declares_each_finding_severity() -> None:
    # The shape the figure prompt asks for: two findings, one of each severity.
    blocking, follow_on = review_module.FINDING_SEVERITIES
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "findings": [
            {
                "file": "docs/figures/proj/review/plot.svg",
                "line": 12,
                "severity": blocking,
                "summary": "the y-axis unit is clipped at the canvas edge",
            },
            {
                "file": "docs/figures/proj/review/plot.svg",
                "line": None,
                "severity": follow_on,
                "summary": "a caption repeats the figure title",
            },
        ],
    }

    severities = [
        review_module.declared_severity(finding)
        for finding in record["findings"]
        if isinstance(finding, dict)
    ]
    assert severities == [blocking, follow_on]
    assert [
        word for word in severities if word == review_module.BLOCKING_FINDING_SEVERITY
    ] == [blocking]
