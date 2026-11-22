"""The queue view counts its waiting requests in four parts, by recorded cause."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew import queue_view as queue_view_module
from reckon.crew import ticker as ticker_module
from reckon.crew.node import TaskNode
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

PROJECT = fixtures.PROJECT
SESSION = fixtures.SESSION
PLAN = fixtures.PLAN
HOUR = 3600.0


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    layer = repo.parent / "flight.yaml"
    layer.write_text(
        json.dumps({**fixtures.CONFIG, "queue": {"priority_budget": 10}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(layer))
    return repo


def _node(node_id: str, section: str) -> TaskNode:
    return fixtures._node(node_id, section)


def _queue(home: Path, nodes: list[TaskNode], run_ids: dict[str, str]) -> None:
    queue_admission.queue_requests(
        nodes,
        project=PROJECT,
        session=SESSION,
        repo_root=home,
        config=fixtures.CONFIG,
        run_ids=run_ids,
    )


def _restamp(
    run_id: str,
    phase: str,
    wait_class: str | None = None,
    waits: list[str] | None = None,
    age: float = 0.0,
) -> None:
    arrived = datetime.now(UTC) - timedelta(seconds=age)

    def mutate(pointer: dict) -> dict:
        pointer["phase"] = phase
        if wait_class is None:
            pointer.pop("wait_class", None)
        else:
            pointer["wait_class"] = wait_class
        if waits is not None:
            pointer["waits"] = list(waits)
        pointer["queued_at"] = arrived.isoformat()
        return pointer

    runs._mutate_pointer(run_id, mutate)


def _parts(document: dict) -> dict[str, dict]:
    counts = document["counts"]
    split = counts.get("waiting_by_cause")
    assert split is not None, f"waiting count given alone: {counts!r}"
    return {entry["part"]: entry for entry in split}


def _seed(home: Path) -> None:
    nodes = [
        _node("slot-a", "s1"),
        _node("slot-b", "s1"),
        _node("adv-a", "s2"),
        _node("adv-b", "s2"),
        _node("adv-c", "s2"),
        _node("idle-a", "s2"),
        _node("idle-b", "s2"),
    ]
    ids = {
        "slot-a": "r-slot-a",
        "slot-b": "r-slot-b",
        "adv-a": "r-adv-a",
        "adv-b": "r-adv-b",
        "adv-c": "r-adv-c",
        "idle-a": "r-idle-a",
        "idle-b": "r-idle-b",
    }
    _queue(home, nodes, ids)
    _restamp("r-slot-a", "queued", age=4 * HOUR)
    _restamp("r-slot-b", "queued", age=2 * HOUR)
    _restamp("r-adv-a", "awaiting-upstream", "progressing", [f"{PLAN}#s1"], 2 * HOUR)
    _restamp("r-adv-b", "awaiting-upstream", "progressing", [f"{PLAN}#s1"], HOUR)
    _restamp("r-adv-c", "awaiting-upstream", "progressing", [f"{PLAN}#s1"], HOUR / 2)
    _restamp("r-idle-a", "awaiting-upstream", "idle", [f"{PLAN}#s1"], HOUR / 2)
    _restamp("r-idle-b", "awaiting-upstream", "idle", ["plan-b#s2"], HOUR / 6)


def test_the_four_parts_carry_counts_and_oldest_ages(home: Path) -> None:
    _seed(home)
    document = queue_view_module.queue_view(PROJECT)
    parts = _parts(document)
    assert parts["slot"]["count"] == 2
    assert parts["advancing"]["count"] == 3
    assert parts["idle"]["count"] == 2
    assert parts["stranded"]["count"] == 0
    assert abs(parts["slot"]["oldest_waited_seconds"] - 4 * HOUR) < 120
    assert abs(parts["advancing"]["oldest_waited_seconds"] - 2 * HOUR) < 120
    assert abs(parts["idle"]["oldest_waited_seconds"] - HOUR / 2) < 120
    assert document["counts"]["requests"] == 7
    assert "waiting" not in document["counts"]

    row = document["pane"]
    assert row == ticker_module.queue_pane_row(document)
    assert "2s@4h" in row
    assert "3a@2h" in row
    assert "2i@" in row
    assert "0x" in row


def test_no_running_work_and_idle_waits_draws_the_fault(home: Path) -> None:
    _seed(home)
    document = queue_view_module.queue_view(PROJECT)
    assert document["running"] == 0
    idle = _parts(document)["idle"]
    assert idle["count"] == 2
    assert sorted(idle["upstream_plans"]) == ["plan-a", "plan-b"]

    row = ticker_module.queue_pane_row(document)
    assert "FAULT" in row
    assert "nothing is running" in row
    assert "plan-a" in row
    assert "plan-b" in row


def test_slot_and_advancing_waits_draw_no_fault(home: Path) -> None:
    _queue(
        home,
        [_node("slot", "s1"), _node("adv", "s2")],
        {"slot": "r-slot", "adv": "r-adv"},
    )
    _restamp("r-slot", "queued", age=HOUR)
    _restamp("r-adv", "awaiting-upstream", "progressing", [f"{PLAN}#s1"], HOUR)

    document = queue_view_module.queue_view(PROJECT)
    assert document["running"] == 0
    assert _parts(document)["idle"]["count"] == 0
    assert "FAULT" not in ticker_module.queue_pane_row(document)


def test_changing_a_stamp_moves_a_request_between_counts(home: Path) -> None:
    _queue(home, [_node("mover", "s2")], {"mover": "r-mover"})
    _restamp("r-mover", "awaiting-upstream", "progressing", [f"{PLAN}#s1"], HOUR)

    before = queue_view_module.queue_view(PROJECT)
    assert _parts(before)["advancing"]["count"] == 1
    assert _parts(before)["idle"]["count"] == 0
    assert before["counts"]["requests"] == 1
    assert before["requests"][0]["wait_part"] == "advancing"

    _restamp("r-mover", "awaiting-upstream", "idle", [f"{PLAN}#s1"], HOUR)

    after = queue_view_module.queue_view(PROJECT)
    assert _parts(after)["advancing"]["count"] == 0
    assert _parts(after)["idle"]["count"] == 1
    assert after["counts"]["requests"] == 1
    assert after["requests"][0]["wait_part"] == "idle"


def test_a_parked_request_counts_as_held_not_as_a_slot_wait(home: Path) -> None:
    _queue(home, [_node("parked", "s1")], {"parked": "r-parked"})
    _restamp("r-parked", "held", age=HOUR)

    document = queue_view_module.queue_view(PROJECT)
    parts = _parts(document)
    assert parts["slot"]["count"] == 0
    assert parts["held"]["count"] == 1
    assert abs(parts["held"]["oldest_waited_seconds"] - HOUR) < 120
    assert "0s " in document["pane"]
    assert "1h@" in document["pane"]


def test_the_pane_and_the_view_share_one_vocabulary(home: Path) -> None:
    assert ticker_module.IN_FLIGHT_PHASES is queue_view_module.RUNNING_PHASES

    _seed(home)
    document = queue_view_module.queue_view(PROJECT)
    named = [entry["part"] for entry in document["counts"]["waiting_by_cause"]]
    assert named[: len(queue_view_module.QUEUE_WAIT_PARTS)] == list(
        queue_view_module.QUEUE_WAIT_PARTS
    )
    for entry in document["counts"]["waiting_by_cause"]:
        assert entry["letter"] == queue_view_module.PART_LETTERS[entry["part"]]
