"""Local admission and measured wait reach the routing judgment together."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.picker import lane_context, prompts

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.fixture
def lane(monkeypatch, tmp_path):
    path = tmp_path / "lane.json"
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(path))
    monkeypatch.setattr(lane_context, "list_live", list)
    return path


def render(**kwargs):
    return json.loads(
        prompts.render(
            "state.jinja",
            node=TaskNode(
                id="task",
                plan="",
                role="documentation",
                spec_level="exact",
                goal="Correct two spelling errors",
                done_when="Text matches",
            ),
            capability={},
            estimated_context=0,
            comment="",
            candidates=[],
            now=NOW,
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    ("slots", "headroom", "paused", "expected"),
    [
        (3, 5, False, "admitting"),
        (0, 5, False, "full"),
        (3, 5, True, "paused"),
        (None, None, False, "unavailable"),
    ],
)
def test_rendered_local_admission(lane, slots, headroom, paused, expected):
    lane.write_text(
        json.dumps(
            {
                "observed_at": NOW.isoformat(),
                "headroom": headroom,
                "admission": {"worker_slots": slots, "observed_seconds": 300},
                "router_generation_gate": {"paused": paused},
            }
        )
    )
    state = render()["local_lane"]
    assert state["admission"] == expected
    assert state["expected_wait_s"] is None


def test_missing_lane_is_unavailable(lane):
    state = render()["local_lane"]
    assert state["admission"] == "unavailable"
    assert state["expected_wait_s"] is None


def test_stale_lane_is_unavailable(lane):
    lane.write_text(
        json.dumps(
            {
                "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
                "suggested_shelf_life_seconds": 45,
                "headroom": 8,
            }
        )
    )
    assert render()["local_lane"]["admission"] == "unavailable"


def test_filtered_local_backend_is_unavailable(lane):
    lane.write_text(json.dumps({"observed_at": NOW.isoformat(), "headroom": 8}))
    assert (
        render(config={"local_backend": "local"})["local_lane"]["admission"]
        == "unavailable"
    )


def test_gate_pause_survives_local_candidate_exclusion(lane, tmp_path):
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({"paused": True}))
    state = render(
        config={
            "local_backend": "local",
            "backends": {
                "local": {"gate_document": str(gate)},
            },
        }
    )
    assert state["local_lane"]["admission"] == "paused"


def test_wait_profiles_only_live_local_shapes(lane, monkeypatch):
    active = {
        "project": "sample",
        "backend": "local",
        "phase": "working",
        "agent": {"local": True, "effort": "high"},
        "node": {"role": "implement", "spec_level": "guided"},
    }
    monkeypatch.setattr(
        lane_context,
        "list_live",
        lambda: [
            active,
            active,
            active | {"node": {"role": "documentation", "spec_level": "exact"}},
            active | {"phase": "complete"},
            active | {"backend": "remote", "agent": {"local": False}},
        ],
    )
    rows = [
        {
            "backend": "local",
            "agent": {"effort": "high"},
            "role": role,
            "spec_level": spec,
            "wall_seconds": wall,
            "completed_at": (NOW - timedelta(days=1)).isoformat(),
        }
        for role, spec, wall in [
            ("implement", "guided", 100),
            ("implement", "guided", 300),
            ("documentation", "exact", 900),
        ]
    ]
    monkeypatch.setattr(
        "reckon.crew.run_time_profile.ledger.runs", lambda *a, **k: rows
    )
    assert render()["local_lane"]["expected_wait_s"] == 200


def test_wait_reuses_records_without_filtering_for_incoming_shape(lane, monkeypatch):
    monkeypatch.setattr(
        lane_context,
        "list_live",
        lambda: [
            {
                "project": "sample",
                "backend": "local",
                "phase": "working",
                "agent": {"local": True, "effort": "high"},
                "node": {"role": "implement", "spec_level": "guided"},
            }
        ],
    )
    monkeypatch.setattr(
        lane_context, "run_time_profile", lambda *a, **k: pytest.fail("ledger reread")
    )
    rows = [
        {
            "backend": "local",
            "agent": {"effort": "high"},
            "role": "implement",
            "spec_level": "guided",
            "wall_seconds": wall,
            "completed_at": stamp,
        }
        for wall, stamp in [
            (240, NOW.isoformat()),
            (9999, (NOW - timedelta(days=30)).isoformat()),
        ]
    ]
    assert (
        render(project="sample", records=rows)["local_lane"]["expected_wait_s"] == 240
    )


def test_unknown_live_shape_has_no_invented_wait(lane, monkeypatch):
    monkeypatch.setattr(
        lane_context,
        "list_live",
        lambda: [
            {
                "project": "sample",
                "backend": "local",
                "phase": "working",
                "agent": {"local": True},
                "node": {"role": "design", "spec_level": "open"},
            }
        ],
    )
    assert render(project="sample", records=[])["local_lane"]["expected_wait_s"] is None


def test_hold_and_route_both_explain_the_sliding_scale():
    question = json.loads(prompts.render("questions.jinja", candidates=[]))["route"]
    for text in (question["instructions"], question["criteria"]["hold"]):
        for phrase in (
            "sliding scale",
            "routine or deferrable",
            "usually hold",
            "pressed metered lane",
            "hard, high-risk",
            "urgent",
            "full, paused or unavailable",
            "without a numeric threshold",
        ):
            assert phrase in text
