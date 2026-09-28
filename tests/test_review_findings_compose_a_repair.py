"""A finding-bearing review composes exactly one repair node per round.

The composure is driven from a real stored review, written through the shared
store into a temporary root, so the finding set under test is the one another
reader would select for the same run. The reviewed run's fence is supplied on
its own record, as a dispatch would carry it, and one finding deliberately names
a path outside that fence: the repair must be granted that path rather than
refused the file its own brief asks it to edit.

The finding ids are asserted against an expectation this file derives itself,
from the finding's own file, line and text, so the test cannot pass merely by
agreeing with whatever the module mints. Round identity is exercised with a
discriminating pair — the same run at two heads — rather than only with a
record composed twice, so a round key that ignored the head would fail.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from reckon.crew import plan_review, repair
from reckon.crew import review as review_module

PROJECT = "reckon"
RUN_ID = "r-20260101T000000000000-reviewed-run"
NODE_ID = "a-reviewed-node"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
NEW_HEAD_SHA = "3" * 40

# The paths the reviewed run was itself granted.
FENCE = ["reckon/crew/thing.py", "tests/test_thing.py"]

# The third finding names a path the reviewed run's fence does not hold.
OUT_OF_FENCE = "reckon/crew/new_module.py"

FINDINGS = [
    {"file": "reckon/crew/thing.py", "line": "10", "text": "off-by-one in the loop"},
    {"file": "tests/test_thing.py", "line": "3", "text": "test asserts a stale value"},
    {"file": OUT_OF_FENCE, "line": "5", "text": "a branch no test reaches"},
]


def _expected_id(finding: dict[str, str]) -> str:
    """Return the id the documented rule yields for one finding.

    This is the oracle, not the code under test: it re-derives the id from the
    finding's own file, line and text using the rule the module documents (a
    prefix plus the first ten hex digits of the sha256 of the NUL-joined
    fields). It is written out here so the assertion holds against an
    expectation the module cannot satisfy by returning a constant.
    """
    material = "\x00".join(
        (finding["file"].strip(), finding["line"].strip(), finding["text"].strip())
    )
    return "f" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


def _store_review(
    tmp_path: Path, findings: list[dict[str, str]], *, head_sha: str = HEAD_SHA
) -> str:
    """Write a review record into a temporary store root and return that root."""
    base_dir = str(tmp_path / "reviews")
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": head_sha,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
        "findings": findings,
    }
    review_module.store_review(record, base_dir=base_dir)
    return base_dir


def _run_record() -> dict[str, object]:
    """The reviewed run's own record, carrying the fence it was dispatched with."""
    return {
        "run_id": RUN_ID,
        "project": PROJECT,
        "node": {"id": NODE_ID, "write_paths": list(FENCE)},
    }


def _review(
    head_sha: str = HEAD_SHA, findings: list[dict[str, str]] | None = None
) -> dict[str, object]:
    """A review record in memory, for the direct-composition cases."""
    return {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": head_sha,
        "status": "parsed",
        "findings": list(FINDINGS if findings is None else findings),
    }


def test_a_findings_review_composes_one_repair_node(tmp_path: Path) -> None:
    base_dir = _store_review(tmp_path, FINDINGS)
    run_record = _run_record()

    node = repair.compose_repair_for_run(
        PROJECT, RUN_ID, base_dir=base_dir, run_record=run_record
    )

    # One repair node, composed as a single mapping — not one node per finding.
    assert isinstance(node, dict)

    # The brief names every finding by the id the finding's own content yields.
    for finding in FINDINGS:
        assert _expected_id(finding) in node["brief"]

    # The write scope is the union of the fence and the finding paths, so the
    # finding outside the reviewed run's fence is granted rather than refused.
    for path in FENCE:
        assert path in node["write_paths"]
    assert OUT_OF_FENCE in node["write_paths"]

    # A second composition of the same round returns the same node, not a new
    # one: redispatching an attempt does not open a second round.
    again = repair.compose_repair_for_run(
        PROJECT, RUN_ID, base_dir=base_dir, run_record=run_record
    )
    assert again is not None
    assert again["node_id"] == node["node_id"]
    assert again["round_id"] == node["round_id"]


def test_finding_ids_are_distinct_and_content_derived() -> None:
    ids = [finding["id"] for finding in repair.review_findings(_review())]

    # Three findings, three ids — a constant would collide all three.
    assert len(set(ids)) == len(FINDINGS)
    # Each id is the one the documented rule derives from that finding's content,
    # computed here independently of the module.
    assert ids == [_expected_id(finding) for finding in FINDINGS]


def test_findings_differing_only_in_text_get_different_ids() -> None:
    shared = {"file": "reckon/crew/thing.py", "line": "10"}
    findings = [
        {**shared, "text": "first reading of the same line"},
        {**shared, "text": "second reading of the same line"},
    ]

    ids = [
        finding["id"] for finding in repair.review_findings(_review(findings=findings))
    ]

    assert ids[0] != ids[1]
    assert ids == [_expected_id(finding) for finding in findings]


def test_a_review_at_a_new_head_is_a_different_round() -> None:
    first = repair.compose_repair_node(_review(HEAD_SHA), run_record=_run_record())
    same_head = repair.compose_repair_node(_review(HEAD_SHA), run_record=_run_record())
    new_head = repair.compose_repair_node(
        _review(NEW_HEAD_SHA), run_record=_run_record()
    )
    assert first is not None and same_head is not None and new_head is not None

    # Same review, same head: one round, so the same node.
    assert same_head["node_id"] == first["node_id"]
    assert same_head["round_id"] == first["round_id"]

    # Same run, new head: a different round, so a different node. A round key
    # that ignored the reviewed head would return the first node here.
    assert new_head["round_id"] != first["round_id"]
    assert new_head["node_id"] != first["node_id"]


def test_the_brief_uses_the_shared_response_vocabulary() -> None:
    node = repair.compose_repair_node(_review(), run_record=_run_record())
    assert node is not None

    # The vocabulary is plan review's, not a private copy: the words the brief
    # teaches are the words a response record validates against. Renaming it in
    # one place and not the other turns this red.
    assert plan_review.RESPONSE_ACTIONS == ("acted", "declined")
    for word in plan_review.RESPONSE_ACTIONS:
        assert word in node["brief"]


def test_a_clean_review_composes_nothing(tmp_path: Path) -> None:
    base_dir = _store_review(tmp_path, [])

    node = repair.compose_repair_for_run(
        PROJECT, RUN_ID, base_dir=base_dir, run_record=_run_record()
    )

    assert node is None
