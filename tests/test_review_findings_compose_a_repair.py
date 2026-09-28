"""A finding-bearing review composes exactly one repair node per round.

The composure is driven from a real stored review, written through the shared
store into a temporary root, so the finding set under test is the one another
reader would select for the same run. The reviewed run's fence is supplied on
its own record, as a dispatch would carry it, and one finding deliberately names
a path outside that fence: the repair must be granted that path rather than
refused the file its own brief asks it to edit.

The second composition is the idempotence check. Composing the same round twice
must return one node, not two — otherwise a failed attempt redispatched is
counted as a second round, which is exactly what the round identity exists to
prevent.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew import repair
from reckon.crew import review as review_module

PROJECT = "reckon"
RUN_ID = "r-20260101T000000000000-reviewed-run"
NODE_ID = "a-reviewed-node"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40

# The paths the reviewed run was itself granted.
FENCE = ["reckon/crew/thing.py", "tests/test_thing.py"]

# The third finding names a path the reviewed run's fence does not hold.
OUT_OF_FENCE = "reckon/crew/new_module.py"

FINDINGS = [
    {"file": "reckon/crew/thing.py", "line": "10", "text": "off-by-one in the loop"},
    {"file": "tests/test_thing.py", "line": "3", "text": "test asserts a stale value"},
    {"file": OUT_OF_FENCE, "line": "5", "text": "a branch no test reaches"},
]


def _store_review(tmp_path: Path, findings: list[dict[str, str]]) -> str:
    """Write a review record into a temporary store root and return that root."""
    base_dir = str(tmp_path / "reviews")
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": HEAD_SHA,
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


def test_a_findings_review_composes_one_repair_node(tmp_path: Path) -> None:
    base_dir = _store_review(tmp_path, FINDINGS)
    run_record = _run_record()

    node = repair.compose_repair_for_run(
        PROJECT, RUN_ID, base_dir=base_dir, run_record=run_record
    )

    # One repair node, composed as a single mapping — not one node per finding.
    assert isinstance(node, dict)

    # The brief names every finding by the id the composer minted for it.
    ids = [finding["id"] for finding in repair.review_findings({"findings": FINDINGS})]
    assert len(ids) == len(FINDINGS)
    for finding_id in ids:
        assert finding_id in node["brief"]

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


def test_a_clean_review_composes_nothing(tmp_path: Path) -> None:
    base_dir = _store_review(tmp_path, [])

    node = repair.compose_repair_for_run(
        PROJECT, RUN_ID, base_dir=base_dir, run_record=_run_record()
    )

    assert node is None
