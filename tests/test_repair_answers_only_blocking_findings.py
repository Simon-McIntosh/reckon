"""A repair round answers only the findings whose severity blocks.

The severity a finding declares is what tells a defect that must be repaired
from one the reviewer recorded as a follow-on: only the blocking value
commissions work, and a finding that declares nothing blocks too, because a
record stored before the field existed carries no key and dropping it would
silently discard a finding a reviewer did raise. The brief, the goal, the ids
the done-when names and the write scope are all built from one filtered list,
so a follow-on must appear in none of them, and a round of follow-ons alone
must compose no repair at all.

The finding ids are asserted against an expectation this file derives from the
finding's own file, line and text, so a case cannot pass by agreeing with
whatever the module mints. The scope is asserted in both directions: the
blocking finding's path is present and the follow-on's is absent, so a scope
that swept every finding's path fails.
"""

from __future__ import annotations

import hashlib

from reckon.crew import repair
from reckon.crew import review as review_module

RUN_ID = "r-20260101T000000000000-reviewed-run"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40

BLOCKING_PATH = "reckon/crew/thing.py"
FOLLOW_ON_PATH = "reckon/crew/other.py"

BLOCKING = {
    "file": BLOCKING_PATH,
    "line": "10",
    "text": "the guard never fires",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}
FOLLOW_ON = {
    "file": FOLLOW_ON_PATH,
    "line": "3",
    "text": "a tidy-up left for later",
    "severity": "follow-on",
}
# A record stored before the severity field existed: the key is absent, not
# defaulted, so it must be repaired rather than dropped.
NO_SEVERITY = {
    "file": "reckon/crew/legacy.py",
    "line": "7",
    "text": "an older record with no declared severity",
}


def _expected_id(finding: dict[str, str]) -> str:
    """The id the documented rule yields, re-derived here as the oracle."""
    material = "\x00".join(
        (finding["file"].strip(), finding["line"].strip(), finding["text"].strip())
    )
    return "f" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


def _review(findings: list[dict[str, str]]) -> dict[str, object]:
    return {
        "project": "reckon",
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "status": "parsed",
        "findings": list(findings),
    }


def test_mixed_findings_compose_a_node_naming_only_the_blocking_one() -> None:
    review = _review([BLOCKING, FOLLOW_ON])

    node = repair.compose_repair_node(review)

    assert node is not None
    blocking_id = _expected_id(BLOCKING)
    follow_on_id = _expected_id(FOLLOW_ON)

    # The brief, the goal and the done-when name the blocking finding and only
    # it: a follow-on is recorded on the review, not commissioned as work.
    assert blocking_id in node["brief"]
    assert follow_on_id not in node["brief"]
    assert blocking_id in node["goal"]
    assert follow_on_id not in node["goal"]
    assert blocking_id in node["done_when"]
    assert follow_on_id not in node["done_when"]

    # The scope is asserted in both directions, so a scope that swept every
    # finding's path — the unfiltered behaviour — fails on the absent half.
    assert BLOCKING_PATH in node["write_paths"]
    assert FOLLOW_ON_PATH not in node["write_paths"]


def test_a_round_of_follow_ons_composes_no_repair() -> None:
    assert repair.compose_repair_node(_review([FOLLOW_ON])) is None


def test_a_finding_with_no_severity_key_is_repaired() -> None:
    node = repair.compose_repair_node(_review([NO_SEVERITY]))

    assert node is not None
    legacy_id = _expected_id(NO_SEVERITY)
    assert legacy_id in node["brief"]
    assert legacy_id in node["goal"]
    assert NO_SEVERITY["file"] in node["write_paths"]


def test_review_findings_keeps_the_severity_and_blocking_findings_filters() -> None:
    review = _review([BLOCKING, FOLLOW_ON, NO_SEVERITY])

    # Every finding is readable with its declared severity carried through, and
    # a finding that declared none carries no key rather than a defaulted one.
    all_findings = repair.review_findings(review)
    assert len(all_findings) == 3
    by_id = {finding["id"]: finding for finding in all_findings}
    assert by_id[_expected_id(BLOCKING)]["severity"] == (
        review_module.BLOCKING_FINDING_SEVERITY
    )
    assert by_id[_expected_id(FOLLOW_ON)]["severity"] == "follow-on"
    assert "severity" not in by_id[_expected_id(NO_SEVERITY)]

    # The filtered list is the blocking finding plus the unstated one, in the
    # record's order, and the declared follow-on is the only omission.
    kept = [finding["id"] for finding in repair.blocking_findings(review)]
    assert kept == [_expected_id(BLOCKING), _expected_id(NO_SEVERITY)]
