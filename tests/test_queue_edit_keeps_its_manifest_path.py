"""Editing a waiting request validates against its recorded delivery path."""

import importlib
import json
from pathlib import Path

import pytest

from reckon.crew.node import CrewError

queue_edit = importlib.import_module("reckon.crew.queue_edit")
dispatch_commands = importlib.import_module("reckon.crew_dispatch_commands")


@pytest.fixture
def waiting_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Path]:
    home = tmp_path / "config"
    for name in ("crew/live", "crew/locks", "crew/runs"):
        (home / name).mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(
        dispatch_commands,
        "_resolved_flight",
        lambda *_args, **_kwargs: {"fences": {"time_budget": "25m"}},
    )
    run_id = "r-waiting-request"
    manifest = home / "crew" / "runs" / run_id / "manifest.md"
    pointer = home / "crew" / "live" / f"{run_id}.json"
    pointer.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": "example",
                "phase": "awaiting-upstream",
                "node": {
                    "id": "waiting-request",
                    "goal": "Update a queued request's measure.",
                    "plan": "example-plan",
                    "section": "s5",
                    "role": "implement",
                    "spec_level": "exact",
                    "done_when": "pytest reports one passing queue edit check",
                    "write_paths": ["src/queue.py"],
                    "time_budget": "20m",
                    "manifest_path": str(manifest),
                    "negative_control": "none: this queued request writes no test",
                },
                "dispatch_options": {"locked_decisions": []},
            }
        ),
        encoding="utf-8",
    )
    return run_id, pointer


def test_awaiting_request_stores_measurable_done_when(
    waiting_request: tuple[str, Path],
) -> None:
    run_id, pointer = waiting_request
    measure = "pytest reports two passing queue edit checks"
    original = json.loads(pointer.read_text(encoding="utf-8"))

    queue_edit.edit(run_id, done_when=measure)

    stored = json.loads(pointer.read_text(encoding="utf-8"))
    assert stored["node"]["done_when"] == measure
    assert stored["node"]["manifest_path"] == original["node"]["manifest_path"]


def test_awaiting_request_refuses_unmeasurable_done_when(
    waiting_request: tuple[str, Path],
) -> None:
    run_id, pointer = waiting_request
    before = pointer.read_bytes()

    with pytest.raises(CrewError, match="demonstrable"):
        queue_edit.edit(run_id, done_when="make it good")

    assert pointer.read_bytes() == before
