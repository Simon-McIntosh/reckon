"""A stored finding that declares no severity is refused at the write.

A review run's whole deliverable is the record it stores, and a finding in that
record says whether it blocks the reviewed node's landing by opening with one
of the declared severities. A finding that states none is recorded without the
key rather than defaulted — a defaulted severity is a judgement nobody made —
which leaves the gate that reads the record unable to tell a blocking defect
from a review's follow-on. The record is judged while the reviewer still holds
its turn, so the reviewer can restate the severities rather than the defect
surfacing hours later as a corrective node. The records here are written
through the production store so the test reads back what the store really
writes, and each refusal is paired with an acceptance so the check is shown to
be about the unstated severity rather than about a record existing at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew.node import TaskNode
from reckon.crew.reports import audit_manifest

PROJECT = "proj"
REVIEWED_RUN_ID = "r-20260101T000000000000-reviewed-run"
REVIEW_RUN_ID = "r-20260101T000100000000-review-of-the-reviewed-run"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40

# A manifest whose own audit is clean: the review role owes no commit and a
# test result is recorded, so the only findings in the cases below are the ones
# a stored record produces.
MANIFEST = (
    "node: review-of-the-reviewed-run\n"
    "status: complete\n"
    "tests: the write-time audit of this run's own stored record\n"
)


def _review_text(*finding_lines: str) -> str:
    """Emitted review text carrying the finding lines it is given."""
    return "\n".join(
        (
            *finding_lines,
            *(
                f"SCORE {dimension}: 15"
                for dimension in review_module.REVIEW_DIMENSIONS
            ),
        )
    )


def _store_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *finding_lines: str,
    with_head: bool = True,
) -> Path:
    """A review record written the way the production store writes one."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    record = review_module.parse_review(_review_text(*finding_lines))
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": REVIEWED_RUN_ID,
            "reviewer_run_id": REVIEW_RUN_ID,
        }
    )
    if with_head:
        record.update({"reviewed_base_sha": BASE_SHA, "reviewed_head_sha": HEAD_SHA})
    return review_module.store_review(record)


def _node(role: str) -> TaskNode:
    """The node the audit receives, granting the record paths in its role.

    A review's dispatch grants the store's record paths, and the implement
    node here carries the same declarations so the cases differ in the role
    alone: the role is what decides whether the record is read.
    """
    record_paths = [
        str(review_module.review_path(PROJECT, REVIEWED_RUN_ID)),
        str(
            review_module.review_path(
                PROJECT, REVIEWED_RUN_ID, reviewed_head_sha=HEAD_SHA
            )
        ),
    ]
    if role == "review":
        return TaskNode(
            id=f"review-of-{REVIEWED_RUN_ID}",
            goal="attach an independent review to the reviewed run",
            plan="write-time-fixture",
            role=role,
            write_paths=record_paths,
        )
    return TaskNode(
        id="an-implement-node",
        goal="land repository work",
        plan="write-time-fixture",
        role=role,
        write_paths=record_paths,
    )


def _findings(role: str) -> list[str]:
    return audit_manifest(MANIFEST, _node(role))["findings"]


def test_a_record_with_an_unmarked_finding_is_refused_at_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_record(
        tmp_path,
        monkeypatch,
        "FINDING tests/test_x.py:12 the guard never fires",
        "FINDING reckon/crew/reports.py:99 the message is not reached",
    )

    findings = _findings("review")

    assert len(findings) == 2
    assert any("tests/test_x.py:12" in finding for finding in findings)
    assert any("reckon/crew/reports.py:99" in finding for finding in findings)


def test_a_record_whose_findings_all_state_a_severity_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    severities = review_module.FINDING_SEVERITIES
    assert len(severities) == 2  # one finding per declared value, none unmarked
    _store_record(
        tmp_path,
        monkeypatch,
        *(
            f"FINDING tests/test_x.py:{12 + index} {severity}: the guard never fires"
            for index, severity in enumerate(severities)
        ),
    )

    assert _findings("review") == []


def test_a_stored_record_without_findings_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stored on the legacy path, the spelling a record lacking the revision
    # pair keeps, so the check reads both record names the store writes.
    _store_record(tmp_path, monkeypatch, with_head=False)

    assert _findings("review") == []


def test_an_implement_run_is_untouched_by_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same unmarked record sits declared beside a node that is not a
    # review: its deliverable is repository work, so a store record is another
    # run's and the check must not read it.
    _store_record(
        tmp_path, monkeypatch, "FINDING tests/test_x.py:12 the guard never fires"
    )

    findings = _findings("implement")

    assert not any("tests/test_x.py:12" in finding for finding in findings)
