"""Long budgets remain visible without blocking dispatch or changing routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import dispatch_batch, picker, queue_admission, runs
from reckon.crew.node import TaskNode, validate_node
from reckon.crew.picker import PickRequest, lane_context
from tests.test_a_heavy_gate_gets_a_full_budget import _candidate
from tests.test_every_path_meets_the_node_contract import CONFIG, _entry
from tests.test_every_path_meets_the_node_contract import (
    crew_home as crew_home,  # noqa: PLC0414 - shared fixture
)
from tests.test_every_path_meets_the_node_contract import (
    repository as repository,  # noqa: PLC0414 - shared fixture
)

pytestmark = pytest.mark.timeout(120)
BUDGET = "240m"
REASON = "The measured acceptance gate takes three hours."


def _config():
    return json.loads(json.dumps(CONFIG))


def _assert_record(pointer, reason):
    node = pointer["node"]
    assert node["time_budget"] == BUDGET
    assert node["over_fence_reason"] == reason
    assert node["over_fence_note"] == "budget 240m exceeds the resolved fence 2h"
    rebuilt = queue_admission._node_from_pointer(pointer)
    assert rebuilt.over_fence_note == node["over_fence_note"]
    assert rebuilt.over_fence_reason == reason


@pytest.mark.parametrize("reason", ["", REASON])
def test_dispatch_accepts_and_records_a_budget_past_the_fence(
    repository: Path, crew_home: Path, monkeypatch, reason: str
):
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *a, **k: None
    )
    entry = _entry(time_budget=BUDGET)
    arguments = [
        "crew",
        "dispatch",
        "--project",
        entry["project"],
        "--plan",
        entry["plan"],
        "--section",
        entry["section"],
        "--node",
        entry["node"],
        "--goal",
        entry["goal"],
        "--done-when",
        entry["done_when"],
        "--write-path",
        "src/module.py",
        "--spec-level",
        "exact",
        "--time-budget",
        BUDGET,
        "--session",
        "coordinator-a",
        "--repo",
        str(repository),
        "--no-watch",
    ]
    if reason:
        arguments += ["--over-fence", reason]
    result = CliRunner().invoke(cli.main, arguments)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True, payload
    pointer = runs.read_pointer(payload["run_id"])
    _assert_record(pointer, reason)


@pytest.mark.parametrize("reason", ["", REASON])
def test_batch_queues_and_records_a_budget_past_the_fence(
    repository: Path, crew_home: Path, reason: str
):
    entry = _entry(time_budget=BUDGET)
    if reason:
        entry["over_fence_reason"] = reason
    result = dispatch_batch.queue_batch(
        [entry],
        session="coordinator-a",
        config_for=lambda _: _config(),
        repo_root=repository,
    )
    assert result["ok"] is True, result
    assert result["queued"] == 1, result
    pointer = runs.read_pointer(result["results"][0]["run_id"])
    _assert_record(pointer, reason)


def test_a_budget_note_is_recomputed_when_the_budget_changes(tmp_path):
    node = TaskNode(
        **{
            key: value
            for key, value in _entry(time_budget=BUDGET).items()
            if key not in {"project", "node"}
        },
        id="budget",
        manifest_path=str(tmp_path / "manifest.md"),
    )
    assert validate_node(node, budget_ceiling="2h").ok
    assert node.over_fence_note
    node.time_budget = "45m"
    assert validate_node(node, budget_ceiling="2h").ok
    assert node.over_fence_note == ""
    node.time_budget = "not-a-duration"
    assert not validate_node(node, budget_ceiling="2h").ok


def test_the_picker_keeps_the_declared_budget_and_lane_choice(
    repository: Path, crew_home: Path, monkeypatch
):
    node = TaskNode(
        id="budget",
        goal="Run the measured gate",
        plan="",
        done_when="pytest reports the gate holding",
        time_budget=BUDGET,
    )
    assert lane_context.node_budget(node, config=_config()) == BUDGET
    monkeypatch.setattr(
        picker, "_declared_difficulty", lambda *a, **k: (None, None, None)
    )
    candidate = _candidate("stub-lane")
    candidate.local = True

    def render(name, **kwargs):
        if name == "questions.jinja":
            return json.dumps({"route": {"criteria": {"hold": {}}}})
        return json.dumps(
            {"budget": lane_context.node_budget(kwargs["node"], config=_config())}
        )

    monkeypatch.setattr(picker.prompts, "render", render)

    def unavailable(*args, **kwargs):
        raise ConnectionError("fixture transport unavailable")

    result = picker.pick(
        PickRequest(project="proj", node=node),
        _config(),
        repo=repository,
        records=[],
        budget_snapshot={},
        snapshotter=lambda *a, **k: [candidate],
        caller=unavailable,
    )
    print(f"PICKER action={result.action} backend={result.backend} budget={BUDGET}")
    assert (result.action, result.backend) == ("fallback", "stub-lane")
    assert json.loads(result.rendered_state)["budget"] == BUDGET
