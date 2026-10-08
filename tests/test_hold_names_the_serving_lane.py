"""A held pick names the lanes that can serve the pick's node.

The rendered state tells Jev which lanes can serve the node and what a hold
waits on. A node whose other lanes are withdrawn from its role -- a review that
must run on the local lane, because every metered lane is review-excluded and
the in-harness backend cannot execute a routed review -- can only be served by
one lane, so a hold waits for that lane alone. Without the fact, a hold reads as
"wait for any lane to free", which for such a node waits on nothing it can
reach: the two recorded holds in ``docs/state/reckon/picker-holds.jsonl`` are
replayed here and the state that made them now names the single serving lane.
"""

from __future__ import annotations

import json

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, pick, prompts, snapshot
from tests import test_picker as fixtures

live_facts = fixtures.live_facts

#: The two recorded holds judged by this node's section, each read from
#: docs/state/reckon/picker-holds.jsonl in the main checkout: the node, the
#: recorded hold probability, and the reasons the record carries.
RECORDED_HOLDS = {
    "review-of-member-friction-asserts-its-own-writes": 0.83,
    "review-of-subscription-lanes-record-no-spend": 0.84,
}

#: A review config in the shape the flight layer uses: every metered lane is
#: review-excluded, the in-harness backend cannot execute a routed review, and
#: the local lane is the only lane left.
REVIEW_CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "budget": {"pace_multiple": 1.1},
    "roles": {"review": {}},
    "review_excluded_backends": ["claude", "codex", "native"],
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "clive",
            "model": "local-model",
            "effort": "high",
        },
        "claude": {
            "launch": "cli",
            "command": "claude",
            "model": "sonnet",
            "effort": "high",
        },
        "codex": {
            "launch": "cli",
            "command": "codex",
            "model": "remote-model",
            "effort": "high",
        },
        "native": {
            "launch": "in-harness",
            "command": "native",
            "model": "native-model",
        },
    },
}


def _request(node_id: str, *, role: str = "review") -> PickRequest:
    return PickRequest(
        "reckon",
        TaskNode(
            id=node_id,
            goal="Read the picker state",
            plan="",
            section="",
            role=role,
            spec_level="guided",
            done_when="The state names the serving lane",
        ),
        capability={"class": "general"},
        estimated_context=8000,
    )


def _hold_answer(keys, hold_probability: float, confidence: float = 0.66):
    """A hold answer with the recorded probability, over exactly the keys offered."""

    keys = list(keys)
    others = [key for key in keys if key != "hold"]
    share = (1.0 - hold_probability) / len(others) if others else 0.0
    probabilities = dict.fromkeys(others, share)
    probabilities["hold"] = hold_probability
    return {
        "model": "stub",
        "answers": {
            "route": {
                "choice": "hold",
                "confidence": confidence,
                "probabilities": probabilities,
            }
        },
    }


def _replay(node_id: str, config, tmp_path, *, probability: float, role="review"):
    """Run one pick with a stub client that holds at the recorded probability.

    Returns the state Jev was shown and the selection it produced, so a test can
    assert both the fact in the state and the action it stands beside.
    """

    seen: list[dict] = []

    def caller(state, questions, **kwargs):
        seen.append(state)
        return _hold_answer(
            questions["route"]["criteria"], probability, confidence=0.66
        )

    selection = pick(
        _request(node_id, role=role), config, repo=tmp_path, records=[], caller=caller
    )
    return seen[0], selection


@pytest.mark.parametrize("node_id", sorted(RECORDED_HOLDS))
def test_recorded_review_hold_names_the_single_serving_lane(
    live_facts, tmp_path, node_id
):
    """Each recorded review hold now renders one serving lane and what a hold waits on."""

    state, selection = _replay(
        node_id, REVIEW_CONFIG, tmp_path, probability=RECORDED_HOLDS[node_id]
    )
    assert selection.action == "hold"
    assert selection.confidence == 0.66
    lane = state["local_lane"]
    assert lane["serving_lanes"] == ["local"]
    assert lane["serving_lane_count"] == 1
    assert lane["hold_waits_for"] == (
        "the local lane, the only lane that can serve this node"
    )


def test_review_exclusions_leave_only_the_local_lane(live_facts, tmp_path):
    """The single serving lane is the one the exclusion set left, not a preference."""

    candidates = snapshot.candidates(_request("n"), REVIEW_CONFIG, tmp_path, records=[])
    excluded = {
        candidate.backend: candidate.reasons
        for candidate in candidates
        if candidate.reasons
    }
    assert "claude" in excluded and "codex" in excluded
    assert excluded["claude"] == ["review-excluded-backend"]
    assert excluded["codex"] == ["review-excluded-backend"]
    assert excluded["native"] == [
        "review-excluded-backend",
        "in-harness-backend",
    ]
    assert [candidate.backend for candidate in candidates if not candidate.reasons] == [
        "clive"
    ]


def test_serving_lane_count_follows_the_offered_lanes(live_facts, tmp_path):
    """Two lanes that can run the node read as two, so the count is not a constant."""

    config = {
        **REVIEW_CONFIG,
        "roles": {"implement": {}},
        "backends": {
            "clive": REVIEW_CONFIG["backends"]["clive"],
            "codex": REVIEW_CONFIG["backends"]["codex"],
        },
    }
    state, _ = _replay(
        "implement-node", config, tmp_path, probability=0.2, role="implement"
    )
    lane = state["local_lane"]
    assert lane["serving_lane_count"] == 2
    assert lane["hold_waits_for"] == (
        "any one of 2 lanes that can serve this node to have room"
    )


def test_no_serving_lane_names_nothing_to_wait_for(live_facts, tmp_path):
    """With no offered lane the state names no serving lane and no wait target."""

    config = {
        **REVIEW_CONFIG,
        "review_excluded_backends": ["clive", "claude", "codex", "native"],
    }
    state = json.loads(
        prompts.render(
            "state.jinja",
            node=_request("n").node,
            capability={},
            estimated_context=0,
            comment="",
            candidates=[],
            config=config,
            now=None,
        )
    )
    lane = state["local_lane"]
    assert lane["serving_lanes"] == []
    assert lane["serving_lane_count"] == 0
    assert lane["hold_waits_for"] is None


def test_glossary_defines_the_serving_lane_facts():
    """The terms the state carries are defined for the model that reads them."""

    questions = json.loads(prompts.render("questions.jinja", candidates=[]))
    glossary = questions["route"]["glossary"]
    assert "serving_lanes" in glossary
    assert "hold_waits_for" in glossary
