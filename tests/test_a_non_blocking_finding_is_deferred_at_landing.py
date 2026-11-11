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
from reckon.crew import (
    queue_landing,
    recovery_review_acceptance,
    review_dispositions,
)
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
        PROJECT, RUN, reviewed_head_sha=HEAD
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


def test_a_claimless_entry_ahead_of_it_does_not_shift_the_deferral(stored) -> None:
    """The deferral answers the finding's own index, not a filtered count.

    A plan review keeps an entry that states no claim, so its findings sit at
    the positions the findings surface numbers them by; an entry a reader drops
    would renumber every finding after it. The guard and the writer both count
    the record's own findings, so the deferral answers the row the coordinator
    was shown rather than the one before it.
    """
    record = stored(
        {
            "project": PROJECT,
            "reviewed_run_id": "",
            "review_run_id": "review-of-the-plan",
            "status": "delivered",
            "findings": [
                {"anchor": "docs/plans/work-flows-to-the-lane.html#1", "text": ""},
                {
                    "anchor": "docs/plans/work-flows-to-the-lane.html#2",
                    "text": "the section body repeats the deliverable",
                    "type": "non-blocking",
                },
            ],
        }
    )

    assert queue_landing._undisposed_finding_guard(record) == ""

    assert review_dispositions.record_landed_deferrals(
        PROJECT, RUN, reviewed_head_sha=HEAD
    ) == [1]
    assert set(record["finding_dispositions"]) == {"1"}, record["finding_dispositions"]
    answered = [row for row in _open_rows(record) if row["text"]]
    assert [row["disposition"] for row in answered] == [f"deferred:{RUN}"], answered


def test_the_recovery_acceptance_path_defers_what_the_landing_path_defers(
    stored, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run promoted by recovery keeps its findings open, as a landed one does.

    Recovery promotes a reviewed run without a coordinator and records the
    findings the reviewer left unanswered. That record used to be an exemption,
    a judgement, which closed the finding — so the same run closed its findings
    when recovery promoted it and left them open when the landing pass did. The
    path a run arrives by must not decide whether its unreviewed findings are
    still owed, so both write the deferral through one writer.
    """
    from reckon.crew import promotion as promotion_module

    record = stored(
        _review(
            _finding(
                "reckon/crew/queue_landing.py:210",
                "the refusal names the run it could not read",
                "non-blocking",
            )
        )
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    # Recovery reads a review's completeness before it promotes, so the record
    # takes the shape a parsed one has: every dimension scored and their total.
    record["status"] = "parsed"
    record["total"] = sum(record["scores"].values())

    monkeypatch.setattr(
        recovery_review_acceptance, "_review_head_and_tree", lambda _p: (HEAD, None)
    )
    monkeypatch.setattr(
        recovery_review_acceptance,
        "select_review_for_head",
        lambda *_a, **_k: (record, HEAD),
    )
    monkeypatch.setattr(
        recovery_review_acceptance,
        "_recorded_gate_passed",
        lambda _p: (
            {"command": "true", "exit_status": 0},
            (),
        ),
    )
    monkeypatch.setattr(
        recovery_review_acceptance, "_commits_missing_from", lambda *_a: ([], None)
    )
    monkeypatch.setattr(
        promotion_module,
        "rerun_gate_at_integrated_revision",
        lambda **_k: {"integrated_verdict": "passed"},
    )
    monkeypatch.setattr(promotion_module, "complete", lambda *_a, **_k: {"record": {}})

    accepted = recovery_review_acceptance.accept_clean_review(
        {"run_id": RUN, "project": PROJECT, "repo": str(checkout)}
    )

    assert accepted is not None, accepted
    assert accepted["accepted"] is True, accepted
    assert accepted["review"]["deferred_findings"] == 1, accepted
    assert "deferral_refusal" not in accepted, accepted
    assert findings_module.finding_disposition(record, 0) == f"deferred:{RUN}", record[
        "finding_dispositions"
    ]
