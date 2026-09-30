"""A finding may state whether it blocks the node's landing.

A review's findings are read by a machine, and the judgement that decides
whether a node lands — this defect has to be repaired first, that one does not —
was legible only to a human reading the finding's free text. The vocabulary is
declared once in the schema and mirrored in the emission form the reviewer
reads, so the parser and the prompt cannot drift; a finding that states none of
the declared words carries no severity field rather than a default, because a
default is a judgement nobody made and a gate reading it would act on one.

The falsifiers here parse the emitted forms, read the prompt as text, and check
that a severity survives the durable store to the production reader, so a value
that exists only in memory is caught. The text of a finding is asserted
to be unchanged by a stated severity: a finding's identity is derived from its
file, line and text, so rewriting the text would re-identify every finding a
reviewer marked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import review as review_module

PROJECT = "proj"
RUN_ID = "r-20260101T000000000000-reviewed-run"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40

# One finding, as the parsed form the store carries.
PLAIN_FINDING = {"file": "tests/test_x.py", "line": "12", "text": "the guard never fires"}


def _review(*finding_lines: str) -> str:
    """A review text that parses, carrying the finding lines it is given."""
    return "\n".join(
        (
            *finding_lines,
            *(f"SCORE {dimension}: 15" for dimension in review_module.REVIEW_DIMENSIONS),
        )
    )


def _only_finding(*finding_lines: str) -> dict[str, str]:
    findings = review_module.parse_review(_review(*finding_lines))["findings"]
    assert len(findings) == 1
    return findings[0]


def test_every_declared_severity_parses_to_its_own_value() -> None:
    # A parser that recorded one constant, or ignored severity altogether,
    # satisfies at most one of these iterations.
    for severity in review_module.FINDING_SEVERITIES:
        finding = _only_finding(
            f"FINDING tests/test_x.py:12 {severity}: the guard never fires"
        )
        assert finding["severity"] == severity


def test_the_blocking_value_is_a_declared_value_and_parses_to_itself() -> None:
    # The blocking case: the value the node's goal names explicitly, recorded
    # as declared rather than as the word the reviewer happened to type.
    assert review_module.BLOCKING_FINDING_SEVERITY in review_module.FINDING_SEVERITIES

    finding = _only_finding(
        f"FINDING tests/test_x.py:12 "
        f"{review_module.BLOCKING_FINDING_SEVERITY}: the guard never fires"
    )

    assert finding["severity"] == review_module.BLOCKING_FINDING_SEVERITY


def test_a_finding_that_states_no_severity_carries_no_severity_key() -> None:
    finding = _only_finding("FINDING tests/test_x.py:12 the guard never fires")

    assert "severity" not in finding
    assert finding == PLAIN_FINDING


def test_a_stated_severity_leaves_file_line_and_text_unchanged() -> None:
    # The shape a review carried before the severity slot existed, now with a
    # severity word: the finding still parses to the same file, line and text,
    # so a finding's id is the one the same text yielded before.
    finding = _only_finding("FINDING tests/test_x.py:12 blocking: the guard never fires")

    assert finding == {
        **PLAIN_FINDING,
        "text": "blocking: the guard never fires",
        "severity": "blocking",
    }


def test_an_undeclared_word_stays_in_the_text_with_no_severity() -> None:
    finding = _only_finding("FINDING tests/test_x.py:12 critical: the guard never fires")

    assert "severity" not in finding
    assert finding["text"] == "critical: the guard never fires"


def test_a_declared_word_is_recorded_in_its_declared_spelling() -> None:
    # Two records of one judgement must compare equal to a gate that reads the
    # declared spelling, whatever case the reviewer wrote it in.
    finding = _only_finding("FINDING tests/test_x.py:12 Blocking: the guard never fires")

    assert finding["severity"] == "blocking"
    assert finding["text"] == "Blocking: the guard never fires"


def test_a_colon_later_in_a_finding_is_not_a_severity() -> None:
    finding = _only_finding(
        "FINDING tests/test_x.py:12 the guard never fires: it returns early"
    )

    assert "severity" not in finding
    assert finding["text"] == "the guard never fires: it returns early"


def test_a_finding_existing_readers_parsed_is_unchanged() -> None:
    # The shapes the store carries today: a finding with no severity word at
    # all, and the capitalised keyword every reader has always accepted.
    assert _only_finding("FINDING tests/test_x.py:12 the guard never fires") == (
        PLAIN_FINDING
    )
    assert review_module.parse_review(
        _review("finding tests/test_x.py:12 the guard never fires")
    )["findings"] == [PLAIN_FINDING]


def test_the_prompt_names_every_declared_severity() -> None:
    prompt = review_module.load_review_prompt()
    collapsed = " ".join(prompt.split())

    assert len(review_module.FINDING_SEVERITIES) <= 3
    assert len(set(review_module.FINDING_SEVERITIES)) == len(
        review_module.FINDING_SEVERITIES
    )
    for severity in review_module.FINDING_SEVERITIES:
        assert severity in collapsed, (
            f"the prompt does not teach the declared severity {severity!r}, so a "
            "reviewer cannot emit it and the parser never reads it"
        )
    # The emission form the prompt asks for carries the slot, not only the words.
    assert "FINDING <file>:<line> <severity>:" in collapsed


def test_a_stored_review_round_trips_its_severity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    record = review_module.parse_review(
        _review("FINDING tests/test_x.py:12 blocking: the guard never fires")
    )
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "reviewed_base_sha": BASE_SHA,
            "reviewed_head_sha": HEAD_SHA,
        }
    )
    base_dir = str(tmp_path / "reviews")

    written = review_module.store_review(record, base_dir=base_dir)
    assert written.is_file()

    # Both production reads: the file's own content, and the annotated view a
    # caller gets, which must not drop the field on its way out.
    _, stored = review_module.stored_record(
        PROJECT, RUN_ID, base_dir=base_dir, reviewed_head_sha=HEAD_SHA
    )
    assert stored is not None
    assert stored["findings"] == [
        {
            "file": "tests/test_x.py",
            "line": "12",
            "text": "blocking: the guard never fires",
            "severity": "blocking",
        }
    ]

    read = review_module.read_review(
        PROJECT, RUN_ID, base_dir=base_dir, reviewed_head_sha=HEAD_SHA
    )
    assert read is not None
    assert [finding["severity"] for finding in read["findings"]] == ["blocking"]