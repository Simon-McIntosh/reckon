"""Review node identities share one prefix definition and recogniser."""

from pathlib import Path

import pytest

from reckon.crew import recovery
from reckon.crew.dispatch import _is_review_run


def test_plan_review_prefix_has_one_definition():
    matches = [
        (path.name, line.strip())
        for path in Path(recovery.__file__).parent.rglob("*.py")
        for line in path.read_text().splitlines()
        if "plan-review-of-" in line
    ]
    assert matches == [("recovery_vocabulary.py", 'PLAN_REVIEW_NODE_PREFIX = "plan-review-of-"')]


@pytest.mark.parametrize("promoted", [False, True])
def test_plan_review_identity_is_recognised_without_review_role(promoted):
    node_id = recovery.PLAN_REVIEW_NODE_PREFIX + "fixture"
    record = {"node": node_id if promoted else {"id": node_id}, "role": "implement"}
    assert recovery._is_review_node(record)
    assert _is_review_run(record)


@pytest.mark.parametrize("promoted", [False, True])
def test_plain_implement_node_is_not_a_review(promoted):
    record = {
        "node": "implement-fixture" if promoted else {"id": "implement-fixture"},
        "role": "implement",
    }
    assert not recovery._is_review_node(record)
    assert not _is_review_run(record)
