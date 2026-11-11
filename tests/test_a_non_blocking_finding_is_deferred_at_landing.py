"""A finding that does not block is deferred at landing, not held.

A run whose review raises only non-blocking observations used to wait for a
coordinator who had nothing to add, because the landing pass refused any
undisposed finding. It no longer does: the pass lands the run and records each
still-open non-blocking finding as ``deferred``, naming the run it deferred
over and the rule that deferred it. The reason the pass used to write an
exemption does not reach a deferral — an exemption is a judgement, and the pass
makes none — so the finding stays open work in the findings view until a fold
or an exemption answers it.

Only a finding that withholds the landing holds it: one that declares the
blocking severity, and one that declares no severity at all. A hand-written
``deferred`` entry is refused, so the kind always means the rule acted rather
than that somebody typed it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import findings as findings_module
from reckon.crew import queue_landing, review_dispositions
from reckon.crew import review as review_module

PROJECT = "deferral-fixture"
RUN = "r-deferred-implementation"
HEAD = "a" * 40


def _finding(anchor: str, text: str, severity: Any) -> dict[str, Any]:
    """One stored finding, shaped the way the store writes one."""
    path, _, line = anchor.partition(":")
    finding: dict[str, Any] = {"file": path, "line": line, "text": text}
    if severity is not None:
        finding["severity"] = severity
    return finding


def _review(*findings: dict[str, Any]) -> dict[str, Any]:
    return {
        "project": PROJECT,
        "reviewed_run_id": RUN,
        "review_run_id": "review-of-the-implementation",
        "reviewed_head_sha": HEAD,
        "status": "delivered",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "findings": list(findings),
    }


@pytest.fixture()
def stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the deferral writer at a record held in this process.

    The writer resolves its record through the project store, so the store's
    own selection is replaced with the record under test and the two writes
    that reach outside it — the delivered copies a filing re-reads and the
    commit of a tracked record — are stood down. Everything the test asserts
    is the behaviour of the deferral rule, the guard and the findings reader.
    """

    def install(record: dict[str, Any]) -> dict[str, Any]:
        path = tmp_path / "review-record.json"
        path.write_text("{}", encoding="utf-8")

        def _stored_record(_project, _run_id, **_kwargs):
            return path, record

        monkeypatch.setattr(review_dispositions, "stored_record", _stored_record)
        monkeypatch.setattr(
            review_dispositions, "_carry_answer_to_delivered_copies", lambda *a, **k: []
        )
        monkeypatch.setattr(
            review_dispositions, "commit_record_write", lambda *a, **k: None
        )
        return record

    return install


def _open_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The rows the findings view builds for this record, filtered to open work."""
    return [row for row in findings_module._record_rows(record, None) if row["open"]]


def test_two_non_blocking_and_one_follow_on_finding_land_deferred(stored) -> None:
    """The pass admits the run and records all three findings as deferred.

    The third finding is stored under the spelling a review written before the
    severity was renamed carries, so the rule is shown to read it as a
    non-blocking finding rather than refusing it as an unreadable word.
    """
    record = stored(
        _review(
            _finding(
                "reckon/crew/findings.py:210",
                "the helper re-derives a value it could cache",
                "non-blocking",
            ),
            _finding(
                "docs/plans/work-flows-to-the-lane.html:64",
                "the section body repeats the deliverable",
                "non-blocking",
            ),
            _finding(
                "tests/test_alpha.py:12",
                "the fixture could be shared with its sibling",
                "follow-on",
            ),
        )
    )

    assert queue_landing._red_suite_guard({}, record) == ""
    assert queue_landing._undisposed_finding_guard(record) == ""

    deferred = review_dispositions.record_landed_deferrals(
        PROJECT, RUN, record, reviewed_head_sha=HEAD
    )

    assert deferred == [0, 1, 2], deferred
    entries = record["finding_dispositions"]
    for index in (0, 1, 2):
        assert entries[str(index)]["run"] == RUN, entries
        assert (
            entries[str(index)]["rule"] == review_dispositions.DEFERRED_AT_LANDING_RULE
        ), entries
    rows = _open_rows(record)
    assert len(rows) == 3, rows
    for row in rows:
        assert row["disposition"] == f"deferred:{RUN}", row
        assert row["open"] is True, row


def test_one_open_blocking_finding_refuses_the_landing_naming_it(stored) -> None:
    """A blocking finding holds the run, and the refusal names the anchor."""
    record = stored(
        _review(
            _finding(
                "reckon/crew/queue_landing.py:410",
                "the guard is read after the measurement",
                "non-blocking",
            ),
            _finding(
                "reckon/crew/queue_landing.py:512",
                "the refusal drops the run id",
                "blocking",
            ),
        )
    )

    guard = queue_landing._red_suite_guard({}, record)

    assert guard.startswith("red-suite:"), guard
    assert "reckon/crew/queue_landing.py:512 (blocking)" in guard, guard
    assert "reckon/crew/queue_landing.py:410" not in guard, guard


def test_a_finding_with_no_stated_severity_refuses_the_landing(stored) -> None:
    """An unstated severity is a judgement nobody made, so it blocks."""
    record = stored(
        _review(
            _finding(
                "reckon/crew/queue_landing.py:700",
                "the loop reads a mapping it never validates",
                None,
            )
        )
    )

    guard = queue_landing._undisposed_finding_guard(record)

    assert guard.startswith("red-suite:"), guard
    assert "reckon/crew/queue_landing.py:700 (unstated severity)" in guard, guard


def test_a_hand_written_deferred_disposition_is_refused(tmp_path: Path) -> None:
    """Only the landing rule writes a deferral, so the verb refuses the kind."""
    with pytest.raises(ValueError, match="recorded only by the landing rule"):
        review_dispositions.record_finding_disposition(
            PROJECT,
            RUN,
            0,
            kind=review_dispositions.DEFERRED_DISPOSITION_KIND,
        )
