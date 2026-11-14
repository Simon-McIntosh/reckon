"""A repair round's advice carries every finding disposed into its node.

A coordinator answers a non-blocking finding by folding it into a node: the
disposition names the node the work went into, and the finding stops being open
work of its own. The advice composed for that node then has to carry it, because
the fold is a decision about where the work lands rather than a decision that
nothing is owed — a brief that listed only the open blocking findings left the
fold's node with no account of the finding it had been handed.

Each case stores a review whose one blocking finding is a dimension under its
declared floor (a dimension has no source location, so the dimension itself is
the work), and disposes two non-blocking findings against it. The advice is read
back out of the composed node, and the finding ids are re-derived here from each
finding's own file, line and text, so a case cannot pass by agreeing with
whatever the module happens to mint.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from reckon.crew import repair
from reckon.crew import review as review_module
from reckon.crew.review import review_path

PROJECT = "repair-advice-fixture"
RUN_ID = "r-20260101T000000000000-reviewed-run"
REVIEW_RUN_ID = "r-20260101T010000000000-review-of-the-run"
NODE_ID = "the-node-the-findings-went-into"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40

DIMENSION = "reuse"
FLOOR = 10
SCORE = 6

FOLDED_ONE = {
    "file": "reckon/crew/ticker.py",
    "line": "88",
    "text": "the request key is built twice, once here and once in the helper",
    "severity": "follow-on",
}
FOLDED_TWO = {
    "file": "reckon/crew/obligations.py",
    "line": "412",
    "text": "the row is sorted twice on the same key",
    "severity": "nit",
}


def _expected_id(finding: dict[str, str]) -> str:
    """The id the documented rule yields, re-derived here as the canonical value."""
    material = "\x00".join(
        (finding["file"].strip(), finding["line"].strip(), finding["text"].strip())
    )
    return "f" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


FINDINGS = [dict(FOLDED_ONE), dict(FOLDED_TWO)]


def _store(tmp_path: Path, dispositions: dict[str, dict[str, str]]) -> str:
    """Write a review under its own temporary store root and return that root."""
    scores = dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15)
    scores[DIMENSION] = SCORE
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "review_run_id": REVIEW_RUN_ID,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "status": "parsed",
        "scores": scores,
        "total": sum(scores.values()),
        "findings": FINDINGS,
        "finding_dispositions": dispositions,
    }
    base_dir = str(tmp_path / "reviews")
    path = review_path(PROJECT, RUN_ID, base_dir, reviewed_head_sha=HEAD_SHA)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return base_dir


def _composed(base_dir: str) -> dict:
    """Compose the round's node, requiring that a dimension finding blocks."""
    node = repair.compose_repair_for_run(
        PROJECT,
        RUN_ID,
        base_dir=base_dir,
        dimension_floors={DIMENSION: FLOOR},
    )
    assert isinstance(node, dict)
    return node


def test_advice_carries_the_dimension_and_both_folded_findings(tmp_path: Path) -> None:
    base_dir = _store(
        tmp_path,
        {
            "0": {"kind": "folded", "node": NODE_ID},
            "1": {"kind": "folded", "node": NODE_ID},
        },
    )

    node = _composed(base_dir)
    brief = str(node["brief"])

    dimension_id = f"dimension-{DIMENSION}"
    first_id = _expected_id(FOLDED_ONE)
    second_id = _expected_id(FOLDED_TWO)

    # Every carried finding is named by id and by text, the dimension too.
    for expected in (dimension_id, first_id, second_id):
        assert expected in brief, brief
    for expected in (FOLDED_ONE["text"], FOLDED_TWO["text"]):
        assert expected in brief, brief

    # The done-when names the same three, so a repair cannot be accepted for
    # answering only the blocking one.
    for expected in (dimension_id, first_id, second_id):
        assert expected in str(node["done_when"]), node["done_when"]

    # A folded finding is named apart from the open work, and folding it does
    # not hand the round a write path the node was never granted.
    assert [finding["id"] for finding in node["folded_findings"]] == [first_id, second_id]
    assert [finding["id"] for finding in node["findings"]] == [dimension_id]
    assert FOLDED_ONE["file"] not in node["write_paths"]


def test_a_fold_naming_another_node_is_left_to_that_node(tmp_path: Path) -> None:
    other = "another-runs-node"
    base_dir = _store(
        tmp_path,
        {
            "0": {"kind": "folded", "node": other},
            "1": {"kind": "folded", "node": other},
        },
    )

    brief = str(_composed(base_dir)["brief"])

    assert _expected_id(FOLDED_ONE) not in brief
    assert FOLDED_ONE["text"] not in brief
    assert f"dimension-{DIMENSION}" in brief


def test_an_exempted_finding_carries_no_work(tmp_path: Path) -> None:
    base_dir = _store(
        tmp_path,
        {
            "0": {"kind": "exempted", "reason": "measured once, so nobody repeats it"},
            "1": {"kind": "folded", "node": NODE_ID},
        },
    )

    node = _composed(base_dir)
    brief = str(node["brief"])

    assert _expected_id(FOLDED_ONE) not in brief
    assert FOLDED_ONE["text"] not in brief
    assert _expected_id(FOLDED_TWO) in brief