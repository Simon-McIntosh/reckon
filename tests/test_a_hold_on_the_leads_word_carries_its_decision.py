"""A hold on the lead's word carries the decision that releases it.

A queued request held for the lead was released by whoever held it, with the
question living in that session's message history. The lead could not read the
question anywhere, and the hold could not outlive the session that placed it.
This pins the rule that replaces it: a hold naming the lead as what it waits on
is refused without a complete decision record, the record is what the decision
read shows holding that request, and a hold naming any other reason is
unchanged and needs no record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon._store import write_json_atomically
from reckon.crew import queue_edit, runs
from reckon.crew.lead_decisions import (
    decision_path,
    read_decisions,
    record_decision,
)
from reckon.crew.node import CrewError
from reckon.mcp_views import serve_crew_view


@pytest.fixture
def decision_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home with two mounted projects and no shared state."""
    root = tmp_path / "config"
    (root / "crew" / "live").mkdir(parents=True)
    (root / "crew" / "locks").mkdir(parents=True)
    mounts = {}
    for project in ("sample", "companion"):
        docs = tmp_path / project / "docs"
        (docs / "plans").mkdir(parents=True)
        mounts[project] = str(docs)
    registry = root / "mounts.json"
    registry.write_text(json.dumps(mounts))
    monkeypatch.setenv("RECKON_HOME", str(root), prepend=False)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(registry), prepend=False)
    monkeypatch.setenv("RECKON_STATE_ROOT", str(root / "state"), prepend=False)
    return root


def _request(home: Path, run_id: str, *, project: str = "sample") -> dict:
    """One waiting request the hold verbs can act on."""
    path = home / "crew" / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "project": project,
        "session": "owner-session",
        "phase": "queued",
        "queued_at": "2026-01-01T00:00:00+00:00",
        "node": {"id": run_id, "plan": "transport", "section": "payload"},
    }
    path.write_text(json.dumps(record))
    return record


def _record(**extra: Any) -> dict[str, Any]:
    return {
        "question": "Which transport carries the work?",
        "context": "The current transport is saturated. A second route is available.",
        "options": [
            {"value": "wait", "label": "Wait for capacity"},
            {"value": "route", "label": "Use the second route"},
        ],
        "recommended": "route",
        "project": "sample",
        "session": "owner-session",
        "plan": "transport",
        "section": "payload",
        "held_requests": [],
        "held_sections": [],
        **extra,
    }


def _malformed(home: Path, decision_id: str, **extra: Any) -> str:
    """Write a record the recorder would refuse, as a hand edit would."""
    record = {
        "id": decision_id,
        "question": "Which transport carries the work?",
        "context": "The current transport is saturated.",
        "project": "sample",
        "session": "owner-session",
        "plan": "transport",
        "section": "payload",
        "options": [
            {"value": "wait", "label": "Wait for capacity"},
            {"value": "route", "label": "Use the second route"},
        ],
        "recommended": "route",
        "choice": "",
        "reversible": False,
        "standing_rule": "",
        "held_requests": [],
        "held_sections": [],
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "source": "crew",
        **extra,
    }
    write_json_atomically(decision_path(decision_id), record)
    return decision_id


def test_a_complete_record_is_accepted_and_the_read_holds_the_request(
    decision_home: Path,
) -> None:
    _request(decision_home, "lead-request")
    decision = record_decision(_record())

    result = queue_edit.hold(
        "lead-request",
        reason="the lead chooses the transport",
        awaiting="lead",
        decision=decision["id"],
    )
    assert result["count"] == 1

    pointer = runs.read_pointer("lead-request")
    assert pointer["phase"] == "held"
    assert pointer["queue_hold"]["decision_id"] == decision["id"]
    assert pointer["queue_hold"]["awaits"] == "lead"

    read = serve_crew_view("decisions")
    row = next(row for row in read["decisions"] if row["id"] == decision["id"])
    assert row["held_requests"] == ["lead-request"]
    assert row["held_request_count"] == 1


def test_a_plan_hold_binds_every_held_request_to_its_record(
    decision_home: Path,
) -> None:
    _request(decision_home, "section-request")
    _request(decision_home, "queued-request")
    decision = record_decision(_record())

    queue_edit.hold(
        plan="transport",
        reason="the lead chooses",
        awaiting="lead",
        decision=decision["id"],
    )

    assert (
        runs.read_pointer("section-request")["queue_hold"]["decision_id"]
        == (decision["id"])
    )
    assert (
        runs.read_pointer("queued-request")["queue_hold"]["decision_id"]
        == (decision["id"])
    )
    read = read_decisions("sample")
    row = next(row for row in read["decisions"] if row["id"] == decision["id"])
    assert row["held_requests"] == ["queued-request", "section-request"]


def test_a_lead_hold_with_no_record_is_refused_and_holds_nothing(
    decision_home: Path,
) -> None:
    _request(decision_home, "lead-request")

    with pytest.raises(CrewError) as caught:
        queue_edit.hold("lead-request", reason="the lead chooses", awaiting="lead")
    message = str(caught.value)
    for field in ("question", "context", "options", "recommended", "plan"):
        assert field in message

    pointer = runs.read_pointer("lead-request")
    assert pointer["phase"] == "queued"
    assert "queue_hold" not in pointer


@pytest.mark.parametrize(
    "record, field",
    [
        ({"options": [{"value": "route", "label": "Use the second route"}]}, "options"),
        (
            {
                "options": [
                    {"value": "wait", "label": "Wait"},
                    {"value": "route", "label": "Route"},
                    {"value": "split", "label": "Split"},
                    {"value": "hold", "label": "Hold"},
                    {"value": "other", "label": "Other"},
                ]
            },
            "options",
        ),
        ({"recommended": ""}, "recommended"),
    ],
)
def test_an_incomplete_record_is_refused_naming_the_field(
    decision_home: Path, record: dict[str, Any], field: str
) -> None:
    _request(decision_home, "lead-request")
    identifier = _malformed(decision_home, "hand-edited", **record)

    with pytest.raises(CrewError) as caught:
        queue_edit.hold(
            "lead-request",
            reason="the lead chooses",
            awaiting="lead",
            decision=identifier,
        )
    assert field in str(caught.value)

    pointer = runs.read_pointer("lead-request")
    assert pointer["phase"] == "queued"
    assert "queue_hold" not in pointer


def test_a_hold_with_another_reason_needs_no_record(decision_home: Path) -> None:
    _request(decision_home, "review-request")

    queue_edit.hold("review-request", reason="reviewing")

    pointer = runs.read_pointer("review-request")
    assert pointer["phase"] == "held"
    assert pointer["queue_hold"]["reason"] == "reviewing"
    assert "decision_id" not in pointer["queue_hold"]
    assert read_decisions("sample")["count"] == 0


def test_the_cli_refuses_a_lead_hold_without_a_record(decision_home: Path) -> None:
    _request(decision_home, "lead-request")

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "queue",
            "hold",
            "--run",
            "lead-request",
            "--reason",
            "the lead chooses",
            "--awaiting",
            "lead",
        ],
    )

    assert result.exit_code != 0, result.output
    assert "decision record" in result.output
    assert "recommended" in result.output
    assert runs.read_pointer("lead-request")["phase"] == "queued"


def test_the_cli_records_a_complete_lead_hold(decision_home: Path) -> None:
    _request(decision_home, "lead-request")
    decision = record_decision(_record())

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "queue",
            "hold",
            "--run",
            "lead-request",
            "--reason",
            "the lead chooses",
            "--awaiting",
            "lead",
            "--decision",
            decision["id"],
        ],
    )
    assert result.exit_code == 0, result.output
    emitted = json.loads(result.output)
    assert emitted["held"][0]["decision_id"] == decision["id"]
    assert runs.read_pointer("lead-request")["phase"] == "held"
