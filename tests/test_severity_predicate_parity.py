"""The audit and the reflex read a stored finding's severity the same way.

A stored review finding carries a severity in its declared spelling or nothing
at all. Two readers decide this on the promotion path — the write-time audit
:func:`reckon.crew.reports._severity_stated` and the repair reflex
:func:`reckon.crew.repair._finding_kind` — and they used to disagree about a
value written directly into a record rather than parsed from the emitted form:
``'minor'`` and ``''`` were unmarked to the audit but a never-repaired follow-on
to the reflex, so the reflex's own rule that an unreadable severity blocks never
fired for them. One predicate, :func:`reckon.crew.review.declared_severity`, now
decides it for every reader.

This module runs the same three inputs — a word outside the vocabulary, an empty
string, and an absent key — through both readers and asserts they agree, and
that each blocks in the reflex. The agreement is asserted as a relation between
the readers (and between each reader and the shared predicate), never as a
literal about either, so a copy that drifts from the predicate fails here.
"""

from __future__ import annotations

from reckon.crew import repair, reports
from reckon.crew import review as review_module

# Recorded after the write-time severity audit began flagging unmarked findings,
# so an unreadable severity on one of these is a finding the reviewer was told
# to restate, not one that predates the instruction.
POST_AUDIT_STAMP = "2026-10-02T00:00:00+00:00"

# The three inputs the readers disagreed on. Each states no usable severity:
# a value outside the vocabulary, an empty string, and an absent key.
UNREADABLE = {
    "out-of-vocabulary": {
        "file": "reckon/crew/thing.py",
        "line": "10",
        "text": "a value outside the vocabulary",
        "severity": "minor",
    },
    "empty": {
        "file": "reckon/crew/other.py",
        "line": "3",
        "text": "an empty severity",
        "severity": "",
    },
    "missing": {
        "file": "reckon/crew/legacy.py",
        "line": "7",
        "text": "an absent severity key",
    },
}

# The two declared severities, so the readers are also checked where they must
# agree off the unreadable set rather than only on it.
BLOCKING = {
    "file": "reckon/crew/blocking.py",
    "line": "1",
    "text": "a declared blocking finding",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}
FOLLOW_ON = {
    "file": "reckon/crew/follow_on.py",
    "line": "2",
    "text": "a declared follow-on finding",
    "severity": "follow-on",
}


def _audit_unmarked(finding: dict[str, str]) -> bool:
    """The promotion audit's view: is this finding's severity unreadable?"""
    return not reports._severity_stated(finding)


def _reflex_blocks(finding: dict[str, str]) -> bool:
    """The repair reflex's view: does this finding block its node?"""
    return repair._finding_kind(finding) == repair._BLOCKING


def test_the_predicate_reads_the_three_disagreeing_inputs_as_no_severity() -> None:
    """Every unreadable input is ``None``; every declared one is its own value."""
    for name, finding in UNREADABLE.items():
        assert review_module.declared_severity(finding) is None, name
    assert (
        review_module.declared_severity(BLOCKING)
        == review_module.BLOCKING_FINDING_SEVERITY
    )
    assert review_module.declared_severity(FOLLOW_ON) == "follow-on"


def test_the_audit_and_the_reflex_agree_on_every_input() -> None:
    """The two readers never disagree: each reads the one shared predicate.

    The audit answers "is a severity declared?" and the reflex "does it block?",
    so the relation is stated through the predicate both consult, not as a
    literal either reader owns. An unreadable severity is unmarked to the audit
    and blocks in the reflex; a declared one is stated to the audit, and the
    declared spelling. When a reader stops consulting the predicate — the
    mutation this test exists to catch — the two sides drift and this fails.
    """
    every_input = {**UNREADABLE, "blocking": BLOCKING, "follow-on": FOLLOW_ON}
    for name, finding in every_input.items():
        declared = review_module.declared_severity(finding)
        assert _audit_unmarked(finding) == (declared is None)
        assert _reflex_blocks(finding) == (
            declared is None or declared == review_module.BLOCKING_FINDING_SEVERITY
        ), name


def test_every_unreadable_input_blocks_in_the_reflex() -> None:
    """The three inputs now block: an unreadable severity is repaired, not dropped.

    Asserted as a relation between the readers as well as against the reflex, so
    a reflex that files one of them as a follow-on — the silent downgrade the
    shared predicate removes — fails on the agreement half too.
    """
    for name, finding in UNREADABLE.items():
        assert _reflex_blocks(finding) is True, name
        assert _audit_unmarked(finding) is True, name
        # The readers agree about each of these inputs, which is the parity the
        # three separately-written readers did not hold.
        assert _audit_unmarked(finding) == _reflex_blocks(finding), name


def test_a_declared_follow_on_is_stated_but_does_not_block() -> None:
    """Control: the readers still separate a declared follow-on from unreadable.

    Without this arm the parity above could pass on readers that called every
    finding blocking, erasing the follow-on the vocabulary exists to name.
    """
    assert _audit_unmarked(FOLLOW_ON) is False
    assert _reflex_blocks(FOLLOW_ON) is False
    assert _audit_unmarked(FOLLOW_ON) == _reflex_blocks(FOLLOW_ON)


def test_unmarked_findings_like_the_audit_reads_them() -> None:
    """The reflex's unmarked list is the audit's set on the same record.

    A post-audit record carrying all five findings: the three unreadable ones
    are unmarked to the audit and listed by the reflex, the declared follow-on
    and the declared blocking one are not. This pins the two readers to one set
    rather than to two that happen to match, so the reflex cannot report a
    finding unmarked that the audit would not, or the reverse.
    """
    findings = [*UNREADABLE.values(), BLOCKING, FOLLOW_ON]
    review = {
        "project": "reckon",
        "reviewed_run_id": "r-20260101T000000000000-reviewed-run",
        "reviewed_base_sha": "1" * 40,
        "reviewed_head_sha": "2" * 40,
        "status": "parsed",
        "timestamp": POST_AUDIT_STAMP,
        "findings": findings,
    }

    unmarked = repair.unmarked_findings(review)
    from_reflex = {finding["file"] for finding in unmarked}
    from_audit = {
        finding["file"] for finding in findings if not reports._severity_stated(finding)
    }

    assert from_reflex == from_audit
    assert from_reflex == {finding["file"] for finding in UNREADABLE.values()}
    # Each unreadable finding, and only those, blocks in the reflex too.
    blocking_files = {finding["file"] for finding in repair.blocking_findings(review)}
    assert from_reflex <= blocking_files
    assert FOLLOW_ON["file"] not in blocking_files
