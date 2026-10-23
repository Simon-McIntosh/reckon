"""A finding is blocking or non-blocking, and an old record still reads.

The vocabulary a finding declares its severity in carries two values: the defect
holds the node, or it does not. The non-blocking value used to be spelled
``follow-on``, the retired word for a deferred-work bucket; a review stored
under that spelling is still a review someone made, so the reader maps the old
word to the current one on the way in and never writes the old word back.

The falsifiers here read the durable store, the repair reflex and the emitted
forms, so a value that lives only in a constant cannot pass. A word written
into a stored record is read through the store's own reader, so a mapping that
exists only where the parser runs is caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import recovery_repair_dispatch, repair, reports
from reckon.crew import review as review_module

PROJECT = "proj"
RUN_ID = "r-20260101T000000000000-reviewed-run"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40

# The spelling the non-blocking severity was written under before the rename.
OLD_WORD = "follow-on"

# A finding whose severity is the current non-blocking value.
NON_BLOCKING_FINDING = {
    "file": "tests/test_x.py",
    "line": "12",
    "text": "the naming would read better",
    "severity": "non-blocking",
}


def _emitted_review(*finding_lines: str) -> str:
    """A review text that parses, carrying the finding lines it is given."""
    return "\n".join(
        (
            *finding_lines,
            *(
                f"SCORE {dimension}: 15"
                for dimension in review_module.REVIEW_DIMENSIONS
            ),
        )
    )


def _record(findings: list[dict[str, object]]) -> dict[str, object]:
    return {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "status": "parsed",
        "findings": findings,
    }


def test_a_non_blocking_report_is_stored_and_read_with_that_severity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emitted word survives the durable store and the store's reader."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    base_dir = str(tmp_path / "reviews")
    record = review_module.parse_review(
        _emitted_review(
            "FINDING tests/test_x.py:12 non-blocking: the naming would read better"
        )
    )
    record.update(
        {
            "project": PROJECT,
            "reviewed_base_sha": BASE_SHA,
            "reviewed_head_sha": HEAD_SHA,
        }
    )
    record["reviewed_run_id"] = RUN_ID

    written = review_module.store_review(record, base_dir=base_dir)
    assert written.is_file()

    read = review_module.read_review(
        PROJECT, RUN_ID, base_dir=base_dir, reviewed_head_sha=HEAD_SHA
    )
    assert read is not None
    assert [finding["severity"] for finding in read["findings"]] == ["non-blocking"]


def test_the_repair_reflex_composes_no_repair_from_a_non_blocking_finding() -> None:
    """A non-blocking finding is recorded, not repaired."""
    record = _record([dict(NON_BLOCKING_FINDING)])

    assert repair.blocking_findings(record) == []
    non_blocking = repair.non_blocking_findings(record)
    assert [finding["file"] for finding in non_blocking] == ["tests/test_x.py"]
    assert repair.unmarked_findings(record) == []


def test_a_stored_record_saying_the_old_word_reads_as_non_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record written under the retired spelling still reads non-blocking."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    base_dir = str(tmp_path / "reviews")
    old_finding = {**NON_BLOCKING_FINDING, "severity": OLD_WORD}
    record = _record([old_finding])
    record["review_run_id"] = "r-20260101T000000000000-current-review"

    written = review_module.store_review(record, base_dir=base_dir)
    assert written.is_file()
    # The retired word is what the fixture stored; the mapping is the reader's.
    assert OLD_WORD in written.read_text(encoding="utf-8")

    read = review_module.read_review(
        PROJECT, RUN_ID, base_dir=base_dir, reviewed_head_sha=HEAD_SHA
    )
    assert read is not None
    assert review_module.declared_severity(read["findings"][0]) == "non-blocking"

    # The reflex reads the same record the same way: recorded, not repaired.
    assert repair.blocking_findings(read) == []
    assert len(repair.non_blocking_findings(read)) == 1


def test_the_review_prompts_teach_the_current_word_and_no_other() -> None:
    for prompt in (
        review_module.load_review_prompt(),
        review_module.load_figure_review_prompt(),
    ):
        collapsed = " ".join(prompt.split())
        assert "non-blocking" in collapsed
        assert OLD_WORD not in collapsed, (
            "a review prompt still teaches the retired non-blocking word"
        )


def test_no_review_message_names_the_retired_word(tmp_path: Path) -> None:
    """The emitted messages that name a severity name the current one."""
    # The write-time audit's refusal, emitted for a finding that states none.
    offenders = reports._unmarked_findings_in_record(
        _record([{"file": "tests/test_x.py", "line": "12", "text": "unmarked"}]),
        str(tmp_path / "review.json"),
    )
    assert offenders, "the unmarked-finding refusal did not fire on its input"
    assert all(OLD_WORD not in message for message in offenders)

    # The recovery reflex's decline-only reason, naming the non-blocking
    # findings it leaves behind.
    review = _record(
        [
            {"id": "f1", "file": "reckon/crew/thing.py", "line": "1", "text": "b"},
            {
                "id": "f2",
                "file": "reckon/crew/other.py",
                "line": "2",
                "text": "n",
                "severity": "non-blocking",
            },
        ]
    )
    reason = recovery_repair_dispatch._decline_only_reason(
        review, [], fence=None, project=PROJECT, config=None
    )
    assert "non-blocking finding(s) cite repository path(s)" in reason
    assert OLD_WORD not in reason
