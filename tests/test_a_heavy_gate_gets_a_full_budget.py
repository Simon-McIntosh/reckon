"""A node whose gate names heavy compute is sized at the resolved fence.

The picker sizes a node by the time budget it is given, and reports that size
class beside each candidate's recorded wall time. A brief that names a queued
cluster job states no budget of its own, so it reaches the picker at the small
implement default; sized at that default it reads as small-node work, which is
the one figure a job that waits in a partition queue cannot have.
"""

from __future__ import annotations

from datetime import UTC, datetime

from reckon.crew.node import TaskNode
from reckon.crew.picker import lane_context
from reckon.crew.picker.types import Candidate

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
FENCE = "6h"
HEAVY_BRIEF = (
    "Run the acceptance gate as a single job on the betelgeuse H200 partition, "
    "then land the figures and the gate log."
)
PLAIN_BRIEF = "Tidy the retry helper's docstring and its two call sites."


def _config(fence: str = FENCE) -> dict[str, object]:
    return {"fences": {"time_budget": fence}}


def _node(
    *,
    brief: str = "",
    goal: str = "g",
    done_when: str = "d",
    time_budget: str = "",
) -> TaskNode:
    return TaskNode(
        id="n",
        goal=goal,
        plan="",
        role="implement",
        spec_level="guided",
        done_when=done_when,
        brief=brief,
        time_budget=time_budget,
    )


def _candidate(backend: str = "local") -> Candidate:
    return Candidate(
        backend=backend,
        family=backend,
        model=None,
        effort="high",
        local=backend == "local",
        availability="served",
        utilisation_pct=None,
        burn_multiple=None,
        pace_allowance=None,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={"passed": 0, "failed": 0, "not-run": 0, "unknown": 0},
    )


def _block(node: TaskNode, config: dict[str, object]) -> dict[str, object]:
    blocks = lane_context.return_times({}, node, [_candidate()], config=config, now=NOW)
    return blocks[lane_context.option_key(_candidate())]


def test_a_brief_naming_an_h200_job_takes_the_resolved_fence():
    node = _node(brief=HEAVY_BRIEF)

    assert lane_context.node_budget(node, config=_config()) == FENCE


def test_the_picker_sizes_a_heavy_brief_at_the_fence_not_the_small_default():
    node = _node(brief=HEAVY_BRIEF, time_budget="8m")

    block = _block(node, _config())

    assert block["size_key"] == "time_budget"
    assert block["size_bucket"] == "over_60m"


def test_a_node_naming_no_compute_keeps_its_sized_budget():
    node = _node(brief=PLAIN_BRIEF, time_budget="8m")

    assert lane_context.node_budget(node, config=_config()) == "8m"
    assert _block(node, _config())["size_bucket"] == "up_to_30m"


def test_a_declared_budget_above_the_fence_is_not_lowered():
    node = _node(brief=HEAVY_BRIEF, time_budget="12h")

    assert lane_context.node_budget(node, config=_config()) == "12h"


def test_a_heavy_brief_with_no_fence_configured_keeps_its_declared_budget():
    node = _node(brief=HEAVY_BRIEF)

    assert lane_context.node_budget(node, config={}) == ""
