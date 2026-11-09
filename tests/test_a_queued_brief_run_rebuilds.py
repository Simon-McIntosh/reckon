"""A queued brief-carried run rebuilds the node it was dispatched with.

A plan review is a brief-carried run: it has no plan section to move, so the
dispatcher builds its node with ``plan=""`` and stores it that way. A queued
request rebuilds that node from the stored data at admission, and the rebuild
drops a stored empty string for a field whose dataclass default is a non-empty
string — a rule meant to keep a real default rather than launch a field with
nothing.

The default of a field that has none is ``dataclasses.MISSING``, and ``MISSING``
is truthy, so the rule also dropped ``plan``'s empty string and the constructor
raised. Every queued brief-carried run was affected whenever it had to wait for
a slot. The rule now applies only to a real non-empty string default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.queue_admission import _node_from_pointer

BRIEF_RUN_ID = "r-20261108T221245094166-plan-review-of-a-dispatch-run"
PLAN_RUN_ID = "r-20261108T221246000000-a-plan-carried-request"


@pytest.fixture()
def queued_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the crew home at a temporary directory for this test."""
    home = tmp_path / "reckon-home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _brief_node() -> TaskNode:
    """The shape a brief-carried review is dispatched with: no plan, no section."""
    return TaskNode(
        id="node-plan-review",
        goal="review the run that dispatched without its worker",
        plan="",
        section="",
        brief="the brief the review reads",
        role="review",
        spec_level="exact",
        done_when="the review is committed with its findings",
        write_paths=["docs/reviews/demo.html"],
        time_budget="30m",
    )


def test_a_brief_carried_pointer_rebuilds_an_empty_plan(
    queued_home: Path,
) -> None:
    """A queued brief-carried review rebuilds with its empty plan and its brief."""
    dispatched = _brief_node()
    stored = dispatched.as_dict()
    assert stored["plan"] == ""

    rebuilt = _node_from_pointer({"run_id": BRIEF_RUN_ID, "node": stored})

    assert rebuilt.plan == ""
    assert rebuilt.section == ""
    assert rebuilt.brief == "the brief the review reads"
    assert rebuilt.role == "review"
    assert rebuilt.done_when == "the review is committed with its findings"
    assert rebuilt.write_paths == ["docs/reviews/demo.html"]
    assert rebuilt == dispatched


def test_a_plan_carried_pointer_rebuilds_unchanged(queued_home: Path) -> None:
    """A plan-carried pointer rebuilds the node it was dispatched with."""
    dispatched = TaskNode(
        id="node-implement",
        goal="narrow the queued rebuild's empty-string guard",
        plan="a-queue-holds-the-whole-plan",
        section="s39",
        brief="",
        role="implement",
        spec_level="exact",
        done_when="the new test passes",
        write_paths=["reckon/crew/queue_admission.py"],
    )
    stored = dispatched.as_dict()

    rebuilt = _node_from_pointer({"run_id": PLAN_RUN_ID, "node": stored})

    assert rebuilt.plan == "a-queue-holds-the-whole-plan"
    assert rebuilt.section == "s39"
    assert rebuilt == dispatched


def test_a_real_default_keeps_itself_when_the_stored_string_is_empty(
    queued_home: Path,
) -> None:
    """A field whose dataclass default is a non-empty string keeps that default.

    This is the case the guard exists for. Removing it outright would launch
    such a field with an empty string where its default belongs, so the third
    case fails when the guard is deleted rather than narrowed. It uses a
    plan-carried node, so it fails for this reason alone and not because a
    brief-carried node could not be built at all.
    """
    dispatched = TaskNode(
        id="node-implement",
        goal="narrow the queued rebuild's empty-string guard",
        plan="a-queue-holds-the-whole-plan",
        section="s39",
        role="implement",
        spec_level="exact",
        done_when="the new test passes",
        write_paths=["reckon/crew/queue_admission.py"],
    )
    stored = dispatched.as_dict()
    stored["role"] = ""
    stored["spec_level"] = ""

    rebuilt = _node_from_pointer({"run_id": PLAN_RUN_ID, "node": stored})

    assert rebuilt.role == "implement"
    assert rebuilt.spec_level == ""
