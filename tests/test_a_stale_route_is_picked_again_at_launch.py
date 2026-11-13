"""A queued request whose route the router chose is picked again at launch.

A request routed while a lane was reserved keeps the route it was given at
dispatch, and a request that waits hours for its upstream therefore launches on
a lane chosen from a picture that no longer holds. When admission releases such
a request, the pick is taken again from the lanes as they are at that moment,
and the launch keeps both routes so a reader can see why they differ. A route
the coordinator named, or one whose pick is still fresh, is left exactly as
given.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs

PROJECT = "proj"
SESSION = "coordinator-a"

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "queue": {"repick_after_seconds": 60},
    "backends": {
        "clive": {"launch": "cli", "command": "codex", "model": "local-model"},
        "codex": {"launch": "cli", "command": "codex", "model": "gpt-6-luna"},
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


class StubPicker:
    """A picker that records every call and answers from a script."""

    def __init__(self, *answers: dict) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    def __call__(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        if not self.answers:
            raise AssertionError("the router was asked more often than scripted")
        return self.answers.pop(0)


def _stored_route(*, age_seconds: float, backend: str = "clive") -> dict:
    chosen_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return {
        "action": "route",
        "backend": backend,
        "family": "local" if backend == "clive" else backend,
        "model": "local-model",
        "picked_at": chosen_at.isoformat(),
    }


def _pointer(*, picked_at: float, local: bool = False) -> dict:
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id="queued-node",
        goal="launch on the lane the router names now",
        plan="plan-a",
        section="dispatch",
        done_when="pytest reports passed",
        write_paths=["queued-node.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    stored = _stored_route(age_seconds=picked_at)
    return {
        "run_id": "r-stale",
        "project": PROJECT,
        "session": SESSION,
        "repo": str(Path(__file__).parent),
        "node": node.as_dict(),
        "backend": "clive",
        "launch": "cli",
        "local": local,
        "phase": "queued",
        "queued_at": joined,
        "created_at": joined,
        "hold": {"held": True},
        "picker_selection": stored,
        "dispatch_options": {
            "project": PROJECT,
            "picker_selection": stored,
            "plan_slug": "plan-a",
            "section": "dispatch",
            "session": SESSION,
            "no_watch": True,
            "overrides": [],
        },
    }


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _config_for() -> dict:
    return json.loads(json.dumps(CONFIG))


def _fake_launcher(recorded: list, picker: StubPicker, *, hold: bool = False):
    """Stand in for dispatch, asking the router exactly where dispatch would.

    A launch handed no stored answer and routed through the picker asks the
    router itself, which is the behaviour this node's decision has to reach.
    """

    def launch(**kwargs: object) -> dict:
        entry = {
            "route": kwargs["route"],
            "picker_selection": kwargs["picker_selection"],
        }
        if kwargs["route"] == "picker" and kwargs["picker_selection"] is None:
            answer = picker(comment="launch")
            entry["picked"] = answer
            if answer.get("action") == "hold":
                recorded.append(entry)
                return {
                    "run_id": kwargs["run_id"],
                    "phase": "queued",
                    "hold": {"reason": answer.get("reason")},
                }
            entry["backend"] = answer.get("backend")
        else:
            entry["picked"] = None
            entry["backend"] = (kwargs["picker_selection"] or {}).get("backend")
        recorded.append(entry)
        return {
            "run_id": kwargs["run_id"],
            "phase": "running",
            "backend": entry["backend"],
        }

    return launch


@pytest.fixture()
def launched(monkeypatch: pytest.MonkeyPatch):
    recorded: list = []
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(queue_admission, "_announce_submission", lambda *a, **k: None)

    def install(picker: StubPicker, *, hold: bool = False) -> None:
        monkeypatch.setattr(
            dispatch_module,
            "dispatch",
            _fake_launcher(recorded, picker, hold=hold),
        )

    install.calls = recorded
    return install


def test_a_stale_route_is_picked_again_at_launch(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch, launched
) -> None:
    config = _config_for()
    runs.queue_dispatch(_pointer(picked_at=7200))
    picker = StubPicker(
        {"action": "route", "backend": "codex", "family": "codex", "model": "gpt-6-luna"}
    )
    launched(picker)

    result = queue_admission._launch_queued(
        runs.read_pointer("r-stale"), launcher=lambda **k: os.getpid(), config_for=lambda p: config
    )

    assert len(picker.calls) == 1
    handoff = launched_handoff(launched.calls)
    assert handoff["route"] == "picker"
    assert handoff["picker_selection"] is None
    assert result["backend"] == "codex"
    pointer = runs.read_pointer("r-stale")
    assert pointer["route_previous"]["backend"] == "clive"
    assert pointer["route_previous"]["family"] == "local"
    assert "past the 60s bound" in pointer["route_repicked_because"]


def launched_handoff(recorded: list) -> dict:
    assert recorded, "dispatch was never called"
    return recorded[-1]


def test_a_named_local_route_is_never_repicked(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch, launched
) -> None:
    config = _config_for()
    runs.queue_dispatch(_pointer(picked_at=7200, local=True))
    picker = StubPicker()
    launched(picker)
    stored = runs.dispatch_field(runs.read_pointer("r-stale"), "picker_selection")

    queue_admission._launch_queued(
        runs.read_pointer("r-stale"), launcher=lambda **k: os.getpid(), config_for=lambda p: config
    )

    assert picker.calls == []
    handoff = launched_handoff(launched.calls)
    assert handoff["route"] == "deterministic"
    assert handoff["picker_selection"] == stored


def test_a_route_picked_within_the_bound_is_replayed(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch, launched
) -> None:
    config = _config_for()
    runs.queue_dispatch(_pointer(picked_at=10))
    picker = StubPicker()
    launched(picker)

    queue_admission._launch_queued(
        runs.read_pointer("r-stale"), launcher=lambda **k: os.getpid(), config_for=lambda p: config
    )

    assert picker.calls == []
    handoff = launched_handoff(launched.calls)
    assert handoff["route"] == "picker"
    assert handoff["picker_selection"]["backend"] == "clive"
    assert handoff["backend"] == "clive"


def test_a_second_pick_that_holds_keeps_the_request_queued(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch, launched
) -> None:
    config = _config_for()
    queued = _pointer(picked_at=7200)
    runs.queue_dispatch(queued)
    before = runs.read_pointer("r-stale")
    picker = StubPicker({"action": "hold", "reason": "the lane is reserved"})
    launched(picker)

    result = queue_admission._launch_queued(
        runs.read_pointer("r-stale"), launcher=lambda **k: os.getpid(), config_for=lambda p: config
    )

    assert len(picker.calls) == 1
    assert result["phase"] == "queued"
    assert result["hold"]["reason"] == "the lane is reserved"
    after = runs.read_pointer("r-stale")
    assert after["phase"] == "queued"
    assert after["queued_at"] == before["queued_at"]
    assert after["created_at"] == before["created_at"]
