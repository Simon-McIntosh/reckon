"""Review node identities share one prefix definition and recogniser."""

from pathlib import Path

import pytest

from reckon.crew import recovery
from reckon.crew.dispatch_sessions import _task_identity
from reckon.crew.recovery import _is_review_run


def test_plan_review_prefix_has_one_definition():
    matches = [
        (path.name, line.strip())
        for path in Path(recovery.__file__).parent.rglob("*.py")
        for line in path.read_text().splitlines()
        if "plan-review-of-" in line
    ]
    assert matches == [
        ("recovery_vocabulary.py", 'PLAN_REVIEW_NODE_PREFIX = "plan-review-of-"')
    ]


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


def test_node_only_review_role_keys_the_run_it_reviews():
    """A review role carried only on the node is a review to both readers.

    The dispatch copy read the role from the record root alone, so this record
    keyed as a plain node and its session was decided as a separate task.
    """
    record = {"node": {"id": "implement-fixture", "role": "review"}}
    assert _is_review_run(record)
    assert _task_identity(record, "reckon", []) == (
        "review",
        "reckon",
        "implement-fixture",
    )


@pytest.mark.parametrize("slug", ["alpha", "beta"])
def test_plan_review_keys_by_the_plan_it_reviews(slug):
    """A plan review's task is the plan, taken from the slug in its node id."""
    node_id = recovery.PLAN_REVIEW_NODE_PREFIX + slug
    record = {"node": {"id": node_id}, "role": "review"}
    assert _is_review_run(record)
    assert _task_identity(record, "reckon", []) == ("plan-review", "reckon", slug)


def test_two_reviews_of_one_plan_share_a_key():
    """Two plan reviews of one plan are the same task; different plans are not."""

    def key(node_id):
        return _task_identity({"node": {"id": node_id}, "role": "review"}, "reckon", [])

    first = key(recovery.PLAN_REVIEW_NODE_PREFIX + "alpha")
    second = key(recovery.PLAN_REVIEW_NODE_PREFIX + "alpha")
    other = key(recovery.PLAN_REVIEW_NODE_PREFIX + "beta")
    assert first == second
    assert first != other
    assert first == ("plan-review", "reckon", "alpha")


def test_run_review_keys_the_reviewed_run():
    """A review-of-<run> record keys to the id of the run it reviews."""
    records = [{"run_id": "r-reviewed-fixture", "node": {"id": "implement-fixture"}}]
    record = {"node": {"id": "review-of-implement-fixture"}, "role": "review"}
    assert _is_review_run(record)
    assert _task_identity(record, "reckon", records) == (
        "review",
        "reckon",
        "r-reviewed-fixture",
    )


def test_plain_implement_node_keys_by_plan_and_node():
    record = {"node": {"id": "implement-fixture"}, "role": "implement"}
    assert _task_identity(record, "reckon", []) == (
        "node",
        "reckon",
        "",
        "implement-fixture",
    )
