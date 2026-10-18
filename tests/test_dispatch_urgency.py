"""A dispatch deadline reaches routing and remains visible on its run."""

from __future__ import annotations

import copy
import importlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import dispatch_picker, runs
from reckon.crew.node import TaskNode
from reckon.crew.picker import lane_context, prompts
from tests import test_dispatch_names_its_backend as base

pytest_plugins = ("tests.test_dispatch_names_its_backend",)
dispatch_module = importlib.import_module("reckon.crew.dispatch")


def _route_fixture(repo, monkeypatch):
    lane = repo.parent / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 0,
                "admission": {
                    "worker_slots": 0,
                    "observed_seconds": 900,
                    "sessions": {"session": {"worker_slots": 0, "live_runs": 1}},
                    "verdict": "full",
                },
            }
        )
    )
    config = copy.deepcopy(base.CONFIG)
    config["local_backend"] = "clive"
    config["backends"]["clive"]["lane_document"] = str(lane)
    config["routing"] = {"picker": "route"}
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )


def _selection(action):
    return {
        "action": action,
        "backend": "beta" if action == "route" else None,
        "confidence": 0.9,
        "probabilities": {action: 0.9},
        "excluded": [],
        "offered": [
            {"backend": "clive", "local": True},
            {
                "backend": "beta",
                "local": False,
                "worker_slots": 1,
                "congestion": {"lift": {"form": "uncapped"}},
            },
        ],
    }


def test_due_reaches_picker_and_run_record(dispatch_repo, monkeypatch):
    _route_fixture(dispatch_repo, monkeypatch)
    seen = []

    def pick(**kwargs):
        seen.append(kwargs)
        return _selection("route")

    monkeypatch.setattr(dispatch_module, "dispatch_picker_selection", pick)
    deadline = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    result = CliRunner().invoke(
        cli.main,
        [
            *base._arguments(dispatch_repo, node="due-run", dry_run=False),
            "--due",
            deadline,
            "--urgency",
            "release today",
            "--no-watch",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["due"] == deadline
    assert seen[0]["urgency"] == "release today"
    payload = base._payload(result)
    pointer = runs.read_pointer(payload["run_id"])
    assert pointer["due"] == deadline
    assert pointer["urgency"] == "release today"
    assert payload["due"] == deadline
    assert payload["urgency"] == "release today"


def test_picker_node_input_and_template_include_deadline():
    node = SimpleNamespace(
        role="implement",
        spec_level="exact",
        goal="ship",
        done_when="gate passes",
        estimated_hours=1,
        write_paths=[],
        negative_control="",
        urgency={
            "due": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "reason": "release today",
        },
    )
    lane = {"return_times": {}, "lanes": {}, "local_lane": {"expected_wait_s": 10000}}
    state = prompts.build_state(
        node=node,
        capability={},
        estimated_context=None,
        comment="",
        candidates=[],
        lane=lane,
    )
    urgency = state["node"]["urgency"]
    assert urgency["due"] == node.urgency["due"]
    assert 7100 < urgency["seconds_to_due"] <= 7200
    assert urgency["reason"] == "release today"
    rendered = json.loads(prompts.render("questions.jinja"))
    instructions = rendered["route"]["instructions"]
    assert (
        "local_lane.queue_position against node.urgency.seconds_to_due" in instructions
    )
    assert "lift or headroom" in instructions
    assert "context fit or worker slots" in instructions
    assert "urgency" in rendered["route"]["glossary"]

    node.urgency = {"due": None, "reason": None}
    assert prompts.build_state(
        node=node,
        capability={},
        estimated_context=None,
        comment="",
        candidates=[],
        lane=lane,
    )["node"]["urgency"] == {"due": None, "seconds_to_due": None, "reason": None}


def test_dispatch_picker_passes_urgency_into_rendered_node(tmp_path, monkeypatch):
    from reckon.crew import picker
    from reckon.crew.picker import snapshot

    monkeypatch.setattr(snapshot, "estimated_context_tokens", lambda *_a, **_k: 0)
    captured = []

    def pick(request, *_args, **_kwargs):
        rendered = prompts.build_state(
            node=request.node,
            capability={},
            estimated_context=None,
            comment="",
            candidates=[],
            lane={"return_times": {}, "lanes": {}, "local_lane": {}},
        )
        captured.append(rendered["node"]["urgency"])
        return SimpleNamespace(as_dict=lambda: _selection("route"))

    monkeypatch.setattr(picker, "pick", pick)
    node = TaskNode(
        id="route", goal="ship", plan="", role="implement", spec_level="exact"
    )
    due = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    for deadline, reason in ((due, "release today"), (None, None)):
        answer = dispatch_picker.dispatch_picker_selection(
            node=node,
            config={},
            project="proj",
            repo=tmp_path,
            records=[],
            verdict_inputs={},
            budget_snapshot={},
            authority={},
            due=deadline,
            urgency=reason,
        )
        assert answer["action"] == "route"
    assert captured[0]["due"] == due
    assert 3500 < captured[0]["seconds_to_due"] <= 3600
    assert captured[0]["reason"] == "release today"
    assert captured[1] == {"due": None, "seconds_to_due": None, "reason": None}


def test_local_queue_position_uses_published_waiting_count(monkeypatch):
    monkeypatch.setattr(
        lane_context, "local_lane_load", lambda: {"waiting": 2, "document": {}}
    )
    monkeypatch.setattr(
        lane_context, "_dispatch_lane_gate", lambda *_a: {"state": "open"}
    )
    monkeypatch.setattr(
        lane_context.lane_document,
        "read_lane_document",
        lambda *_a, **_k: {"stale": False, "admission_verdict": "full"},
    )
    monkeypatch.setattr(lane_context, "_expected_wait", lambda **_k: (100, None))
    state = lane_context.local_lane(config={}, candidates=[])
    assert state["waiting"] == 2
    assert state["queue_position"] == 3


def test_urgent_lifted_route_does_not_queue_but_unmarked_hold_does(
    dispatch_repo, monkeypatch
):
    _route_fixture(dispatch_repo, monkeypatch)
    picks = []

    def pick(**kwargs):
        picks.append(kwargs)
        return _selection("route" if kwargs["due"] or kwargs["urgency"] else "hold")

    monkeypatch.setattr(dispatch_module, "dispatch_picker_selection", pick)
    urgent = CliRunner().invoke(
        cli.main,
        [
            *base._arguments(dispatch_repo, node="urgent-route", dry_run=False),
            "--due",
            "3h",
            "--urgency",
            "release today",
            "--no-watch",
        ],
    )
    assert urgent.exit_code == 0, urgent.output
    urgent_payload = base._payload(urgent)
    assert urgent_payload["backend"] == "beta"
    assert urgent_payload.get("queued") is not True
    assert runs.read_pointer(urgent_payload["run_id"])["phase"] != "queued"
    assert picks[0]["due"] and picks[0]["urgency"] == "release today"

    ordinary = CliRunner().invoke(
        cli.main,
        [
            *base._arguments(dispatch_repo, node="ordinary-hold", dry_run=False),
            "--no-watch",
        ],
    )
    assert ordinary.exit_code == 0, ordinary.output
    ordinary_payload = base._payload(ordinary)
    assert ordinary_payload["queued"] is True
    pointer = runs.read_pointer(ordinary_payload["run_id"])
    assert pointer["backend"] == "clive"
    assert pointer["due"] is None
    assert pointer["urgency"] is None
    assert picks[1]["due"] is None and picks[1]["urgency"] == ""


def test_urgent_hold_with_lift_is_not_rewritten_as_local_queue(
    dispatch_repo, monkeypatch
):
    _route_fixture(dispatch_repo, monkeypatch)
    monkeypatch.setattr(
        dispatch_module, "dispatch_picker_selection", lambda **_k: _selection("hold")
    )
    result = CliRunner().invoke(
        cli.main,
        [
            *base._arguments(dispatch_repo, node="urgent-hold", dry_run=False),
            "--due",
            "1h",
            "--no-watch",
        ],
    )
    assert result.exit_code == 3, result.output
    assert base._payload(result)["error"] == "budget-hold"
    assert runs.list_live(project="proj") == []
