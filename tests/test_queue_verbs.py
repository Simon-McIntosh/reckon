"""A waiting request can be edited, re-ranked and dropped before it launches.

``crew queue edit`` replaces the request's steering text, its measure, its
write scope, its time budget or its route, and re-runs the contract checks a
dispatch applies before any worker exists. ``crew queue reprioritise`` records
an explicit priority override on the request. ``crew queue drop`` removes a
queued or awaiting-upstream request and refuses every phase that is no longer
waiting.

Each MCP crew action runs the same verb callback the CLI verb runs, so the two
surfaces answer one call with one document. The parity is compared rather than
asserted about either surface.

The negative control makes edit alone skip the launched-request check. Its
refusal arm then fails while the reprioritise and drop arms still pass, which
shows that edit's check holds that boundary.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

crew_run_commands = importlib.import_module("reckon.crew_run_commands")
crew_dispatch_commands = importlib.import_module("reckon.crew_dispatch_commands")

CREW = crew_dispatch_commands.crew
PROJECT = "reckon"

CATALOGUE: dict = {
    "version": 1,
    "lanes": {
        "claude": {
            "default_model": "sonnet",
            "models": {"sonnet": {"model": "claude-sonnet-5-5", "effort": "medium"}},
        },
        "codex": {
            "default_model": "sol",
            "models": {"sol": {"model": "gpt-6-sol", "effort": "high"}},
        },
    },
    "aliases": {},
}

CONFIG: dict = {
    "default_backend": "claude",
    "local_backend": "claude",
    "backends": {
        "claude": {
            "launch": "cli",
            "model": "claude-sonnet-5-5",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "claude",
            "model_key": "sonnet",
        },
        "codex": {
            "launch": "cli",
            "model": "gpt-6-sol",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "codex",
            "model_key": "sol",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

NODE: dict = {
    "id": "queue-verb-node",
    "goal": "Add one queue verb.",
    "plan": "a-plan",
    "section": "§5",
    "brief": "",
    "brief_sha256": "",
    "brief_path": "",
    "role": "implement",
    "spec_level": "exact",
    "done_when": "pytest -q tests/test_queue_verbs.py reports 3 passed.",
    "write_paths": ["reckon/crew/queue_edit.py"],
    "time_budget": "20m",
    "manifest_path": "",
    "negative_control": "none: the node writes no check",
    "estimated_hours": 1.0,
    "requires_decisions": [],
    "peer_scopes": {},
    "regenerate_paths": [],
    "images": [],
    "inputs": [],
    "compares_with": "",
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic configuration home with one mounted project and a catalogue."""
    home = tmp_path / "config"
    for directory in ("crew/live", "crew/locks", "crew/runs", "crew/discards"):
        (home / directory).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    catalogue = tmp_path / "catalogue.yaml"
    catalogue.write_text(yaml.safe_dump(CATALOGUE), encoding="utf-8")
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(catalogue))
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )
    return home


def _write_request(
    home: Path,
    run_id: str = "r-queued",
    *,
    phase: str = "queued",
    steering: str | None = "original steering text",
) -> Path:
    """Store one waiting request's pointer, and its steering file if it has one."""
    node = dict(NODE)
    node["manifest_path"] = str(home.parent / "manifest.md")
    if steering is not None:
        directory = home / "crew" / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "steering.md"
        path.write_text(steering, encoding="utf-8")
        node["brief_path"] = str(path)
    pointer = home / "crew" / "live" / f"{run_id}.json"
    pointer.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "plan": "a-plan",
                "section": "§5",
                "repo": str(home / "repo"),
                "session": "session-1",
                "node": node,
                "backend": "claude",
                "launch": "cli",
                "local": True,
                "phase": phase,
                "queued_at": "2026-10-24T05:00:00+00:00",
                "created_at": "2026-10-24T05:00:00+00:00",
                "dispatch_options": {"locked_decisions": []},
            }
        ),
        encoding="utf-8",
    )
    return pointer


def _pointer(home: Path, run_id: str = "r-queued") -> dict[str, Any]:
    return json.loads(
        (home / "crew" / "live" / f"{run_id}.json").read_text(encoding="utf-8")
    )


def _answer(result) -> dict[str, Any]:
    """The document a CLI call prints, or its refusal normalised to tool shape."""
    text = result.output.strip()
    try:
        return json.loads(text)
    except ValueError:
        return {
            "ok": False,
            "error": "crew_error",
            "detail": text.removeprefix("Error: "),
        }


def test_edit_replaces_steering_and_the_declared_fields(
    home: Path, tmp_path: Path
) -> None:
    """An edit replaces the steering file and the node fields it names."""
    _write_request(home)
    replacement = tmp_path / "replacement.md"
    replacement.write_text("replacement steering text", encoding="utf-8")

    result = CliRunner().invoke(
        CREW,
        [
            "queue",
            "edit",
            "--run",
            "r-queued",
            "--brief",
            str(replacement),
            "--done-when",
            "pytest -q tests/test_queue_verbs.py reports 9 passed.",
            "--write-path",
            "reckon/crew/queue_edit.py",
            "--write-path",
            "tests/test_queue_verbs.py",
            "--time-budget",
            "25m",
        ],
    )
    payload = _answer(result)
    assert result.exit_code == 0, result.output

    steering = home / "crew" / "runs" / "r-queued" / "steering.md"
    assert steering.read_text(encoding="utf-8") == "replacement steering text"
    assert payload["steering_sha256"]
    record = _pointer(home)
    assert record["node"]["done_when"] == (
        "pytest -q tests/test_queue_verbs.py reports 9 passed."
    )
    assert record["node"]["write_paths"] == [
        "reckon/crew/queue_edit.py",
        "tests/test_queue_verbs.py",
    ]
    assert record["node"]["time_budget"] == "25m"
    assert record["node"]["brief_sha256"] == payload["steering_sha256"]


def test_an_over_ceiling_edit_is_refused_and_stores_nothing(home: Path) -> None:
    """An edit obeys the same project fence as a dispatch."""
    pointer = _write_request(home)
    before = pointer.read_bytes()

    result = CliRunner().invoke(
        CREW, ["queue", "edit", "--run", "r-queued", "--time-budget", "35m"]
    )

    assert result.exit_code != 0, result.output
    assert "35m" in result.output
    assert "25m" in result.output
    assert pointer.read_bytes() == before


def test_an_edit_that_fails_the_contract_is_refused_and_stores_nothing(
    home: Path,
) -> None:
    """The dispatch's own check runs before the write, so a bad edit changes nothing."""
    pointer = _write_request(home)
    before = pointer.read_bytes()

    result = CliRunner().invoke(
        CREW,
        [
            "queue",
            "edit",
            "--run",
            "r-queued",
            "--done-when",
            "make it good",
        ],
    )

    assert result.exit_code != 0, result.output
    assert "contract" in result.output
    assert pointer.read_bytes() == before
    steering = home / "crew" / "runs" / "r-queued" / "steering.md"
    assert steering.read_text(encoding="utf-8") == "original steering text"


def test_a_route_set_by_edit_is_recorded_as_named(home: Path) -> None:
    """A lane and model named by edit land on the request as a named route."""
    _write_request(home)

    result = CliRunner().invoke(
        CREW,
        ["queue", "edit", "--run", "r-queued", "--backend", "codex", "--model", "sol"],
    )
    payload = _answer(result)
    assert result.exit_code == 0, result.output

    record = _pointer(home)
    assert record["backend"] == "codex"
    assert record["lane"] == "codex"
    assert record["model_key"] == "sol"
    assert record["named_route"]["set_by"] == "queue-edit"
    assert record["dispatch_options"]["route"] == "deterministic"
    assert payload["route"]["model_key"] == "sol"


def test_a_model_the_lane_does_not_declare_is_refused(home: Path) -> None:
    """A model outside the named lane is refused, and the request is untouched."""
    pointer = _write_request(home)
    before = pointer.read_bytes()

    result = CliRunner().invoke(
        CREW,
        ["queue", "edit", "--run", "r-queued", "--backend", "claude", "--model", "sol"],
    )

    assert result.exit_code != 0, result.output
    assert "sol" in result.output
    assert pointer.read_bytes() == before


@pytest.mark.parametrize(
    "argv",
    [
        [
            "queue",
            "edit",
            "--run",
            "r-launched",
            "--done-when",
            "pytest reports 1 passed.",
        ],
        ["queue", "reprioritise", "--run", "r-launched", "--priority", "5"],
        ["queue", "drop", "--run", "r-launched"],
    ],
    ids=["edit", "reprioritise", "drop"],
)
def test_a_launched_request_is_refused_by_every_verb(
    home: Path, argv: list[str]
) -> None:
    """A request past the waiting phases is a run, and no queue verb changes it."""
    pointer = _write_request(home, "r-launched", phase="working")
    before = pointer.read_bytes()

    result = CliRunner().invoke(CREW, argv)

    assert result.exit_code != 0, result.output
    assert "working" in result.output
    assert pointer.read_bytes() == before


def test_an_awaiting_upstream_request_is_waiting_too(home: Path) -> None:
    """The awaiting-upstream phase is still the coordinator's to change."""
    _write_request(home, "r-upstream", phase="awaiting-upstream")

    result = CliRunner().invoke(
        CREW, ["queue", "reprioritise", "--run", "r-upstream", "--priority", "4"]
    )

    assert result.exit_code == 0, result.output
    assert _pointer(home, "r-upstream")["priority_override"] == 4


def test_reprioritise_stores_the_override_on_the_request(home: Path) -> None:
    """The override is stored, with its reason, so every later ranking reads it."""
    _write_request(home)

    result = CliRunner().invoke(
        CREW,
        [
            "queue",
            "reprioritise",
            "--run",
            "r-queued",
            "--priority",
            "7",
            "--reason",
            "the lead needs this first",
        ],
    )
    payload = _answer(result)
    assert result.exit_code == 0, result.output

    record = _pointer(home)
    assert record["priority_override"] == 7
    assert record["priority_override_reason"] == "the lead needs this first"
    assert record["priority_override_set_at"]
    assert payload["priority_override"] == 7


@pytest.mark.parametrize("phase", ["queued", "awaiting-upstream"])
def test_drop_removes_a_waiting_request(home: Path, phase: str) -> None:
    """A drop removes the pointer and records the departure as a discard does."""
    run_id = f"r-drop-{phase}"
    pointer = _write_request(home, run_id, phase=phase)

    result = CliRunner().invoke(CREW, ["queue", "drop", "--run", run_id])
    payload = _answer(result)
    assert result.exit_code == 0, result.output

    assert not pointer.exists()
    assert payload["dropped_phase"] == phase
    from reckon.crew.promotion import discard_recorded

    assert discard_recorded(run_id)


def test_each_mcp_action_returns_its_cli_verb_document(home: Path) -> None:
    """The MCP action layer and the CLI answer one call with one document."""
    from reckon.mcp_crew_actions import CREW_WRITE_ACTIONS, crew_action

    for name in ("queue-edit", "queue-reprioritise", "queue-drop"):
        assert name in CREW_WRITE_ACTIONS

    cases = [
        (
            ["queue", "reprioritise", "--run", "r-missing", "--priority", "3"],
            "queue-reprioritise",
            {"run_id": "r-missing", "priority": 3},
        ),
        (
            ["queue", "drop", "--run", "r-missing"],
            "queue-drop",
            {"run_id": "r-missing"},
        ),
        (
            ["queue", "edit", "--run", "r-missing", "--time-budget", "30m"],
            "queue-edit",
            {"run_id": "r-missing", "time_budget": "30m"},
        ),
    ]
    for argv, action, kwargs in cases:
        cli = _answer(CliRunner().invoke(CREW, argv))
        tool = crew_action(action, **kwargs)
        assert tool == cli, f"{action}: tool {tool} != cli {cli}"


def test_a_request_that_launches_before_the_discard_is_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch landing between drop's read and its discard refuses the drop."""
    pointer = _write_request(home, "r-racing")
    queue_edit_module = importlib.import_module("reckon.crew.queue_edit")
    original = queue_edit_module._read_request

    def read_then_launch(run_id: str, project: str | None) -> dict[str, Any]:
        record = original(run_id, project)
        launched = json.loads(pointer.read_text(encoding="utf-8"))
        launched["phase"] = "working"
        pointer.write_text(json.dumps(launched), encoding="utf-8")
        return record

    monkeypatch.setattr(queue_edit_module, "_read_request", read_then_launch)

    result = CliRunner().invoke(CREW, ["queue", "drop", "--run", "r-racing"])

    assert result.exit_code != 0, result.output
    assert "working" in result.output
    assert pointer.exists()


def test_the_new_verbs_document_their_state() -> None:
    """Each new verb is reachable and its first help line names what it acts on."""
    expected = {
        ("queue", "edit"): "waiting request's steering text",
        ("queue", "reprioritise"): "priority override on a waiting request",
        ("queue", "drop"): "queued or awaiting-upstream request",
    }
    for path, phrase in expected.items():
        result = CliRunner().invoke(CREW, [*path, "--help"])
        assert result.exit_code == 0, result.output
        command = CREW.commands["queue"].commands[path[1]]
        first_line = " ".join((command.help or "").splitlines()[0].split())
        assert phrase in first_line
        assert first_line in " ".join(result.output.split())
