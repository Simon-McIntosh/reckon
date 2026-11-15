"""One read carries recorded and plan-authored decisions across mounts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon._plan_html import write_state
from reckon.cli import main
from reckon.mcp_views import serve_crew_view


@pytest.fixture
def decision_home(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    mounts = {}
    for project in ("sample", "companion"):
        docs = tmp_path / project / "docs"
        (docs / "plans").mkdir(parents=True)
        mounts[project] = str(docs)
    registry = home / "mounts.json"
    registry.write_text(json.dumps(mounts))
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(registry))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(home / "state"))
    return home, {key: Path(value) for key, value in mounts.items()}


def _record(project="sample", **extra):
    return {
        "question": "Which transport carries the work?",
        "context": "The current transport is saturated. A second route is available.",
        "options": [
            {"value": "wait", "label": "Wait for capacity"},
            {"value": "route", "label": "Use the second route"},
        ],
        "recommended": "route",
        "project": project,
        "session": "owner-session",
        "plan": "transport",
        "section": "payload",
        "held_requests": [],
        "held_sections": [],
        **extra,
    }


def _pointer(home, run_id, project="sample", **extra):
    path = home / "crew" / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": project,
                "phase": "held",
                "session": "owner-session",
                "node": {"plan": "transport", "section": "payload"},
                **extra,
            }
        )
    )


def _plan(docs, decisions, *, status="active"):
    path = docs / "plans" / "transport.html"
    path.write_text(
        write_state(
            "<html><head><title>Transport</title></head><body>"
            '<h2 id="payload">Payload</h2></body></html>',
            {
                "slug": "transport",
                "status": status,
                "modified": "2026-10-01",
                "decisions": decisions,
            },
        )
    )
    return path


def test_every_open_decision_is_one_read_in_held_work_order(decision_home):
    home, mounts = decision_home
    first = serve_crew_view("decisions")
    assert first.get("ok") is True, first
    assert first["decisions"] == []

    from reckon.crew.lead_decisions import record_decision

    records = []
    for project, requests in (
        ("sample", []),
        ("companion", ["single-request"]),
        ("sample", [f"busy-request-{index}" for index in range(4)]),
    ):
        for run_id in requests:
            _pointer(home, run_id, project)
        records.append(record_decision(_record(project, held_requests=requests)))

    result = serve_crew_view("decisions")
    rows = result["decisions"]
    assert [row["id"] for row in rows] == [row["id"] for row in reversed(records)]
    assert [row["held_request_count"] for row in rows] == [4, 1, 0]
    assert [row["held_requests"] for row in rows] == [
        [f"busy-request-{index}" for index in range(4)],
        ["single-request"],
        [],
    ]
    for row in rows:
        assert row["question"] == records[0]["question"]
        assert row["context"] == records[0]["context"]
        assert row["options"][0] == {"value": "route", "label": "Use the second route"}
        assert row["recommended"] == "route"
        assert row["session"] == "owner-session"
        assert row["plan"] == "transport"
        assert row["section"] == "payload"
        assert row["recorded_at"]
        assert row["choice"] == ""
        assert row["held_sections"] == []
        assert row["source"] == "crew"
    assert result["count"] == 3
    assert set(result["projects"]) == set(mounts)
    assert len(list((home / "crew" / "decisions").glob("*.json"))) == 3


def test_plan_only_unanswered_decision_has_its_plan_and_section(decision_home):
    home, mounts = decision_home
    _plan(
        mounts["companion"],
        {
            "format": {
                "title": "Which payload format?",
                "context": "Readers need a common representation. Both formats are supported.",
                "choices": ["compact", "expanded"],
                "option_labels": {"compact": "Compact", "expanded": "Expanded"},
                "recommended": "expanded",
                "sections": ["payload"],
            },
            "answered": {"title": "Already answered", "choice": "accepted"},
            "withdrawn": {"title": "Withdrawn", "rationale": "No longer required"},
        },
    )
    _pointer(home, "plan-request", "companion")
    result = serve_crew_view("decisions")
    assert result["ok"] is True, result
    assert result["count"] == 1
    row = result["decisions"][0]
    assert row["key"] == "format"
    assert row["source"] == "plan"
    assert row["project"] == "companion"
    assert row["plan"] == "transport"
    assert row["section"] == "payload"
    assert row["held_sections"] == ["payload"]
    assert row["held_requests"] == ["plan-request"]
    assert row["session"] == "owner-session"
    assert row["recorded_at"] is None
    assert row["options"][0]["value"] == "expanded"


def test_cli_and_agent_read_the_same_document(decision_home):
    from reckon.crew.lead_decisions import record_decision
    from reckon.mcp import _crew

    record_decision(_record())
    cli = CliRunner().invoke(main, ["crew", "view", "decisions"])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output) == _crew(view="decisions")


def test_record_command_persists_a_decision(decision_home, tmp_path):
    record = tmp_path / "record.json"
    record.write_text(json.dumps(_record()))
    result = CliRunner().invoke(
        main, ["crew", "decision", "record", "--record", str(record)]
    )
    assert result.exit_code == 0, result.output
    saved = json.loads(result.output)["decision"]
    assert saved["id"] == serve_crew_view("decisions")["decisions"][0]["id"]


def test_answered_record_is_absent_and_missing_requests_do_not_hold(decision_home):
    from reckon._store import write_json_atomically
    from reckon.crew.lead_decisions import decision_path, record_decision

    home, _ = decision_home
    _pointer(home, "running-request", phase="working")
    saved = record_decision(
        _record(held_requests=["missing-request", "running-request"])
    )
    row = serve_crew_view("decisions")["decisions"][0]
    assert row["held_requests"] == []
    assert row["held_request_count"] == 0
    write_json_atomically(decision_path(saved["id"]), {**saved, "choice": "route"})
    assert serve_crew_view("decisions")["decisions"] == []


@pytest.mark.parametrize(
    "change,field",
    [
        ({"options": [{"value": "only", "label": "Only"}]}, "options"),
        ({"recommended": ""}, "recommended"),
        ({"recommended": "absent"}, "recommended"),
        ({"session": ""}, "session"),
        ({"project": "unmounted"}, "project"),
        ({"choice": "route"}, "choice"),
    ],
)
def test_incomplete_record_is_refused_without_a_write(decision_home, change, field):
    from reckon.crew.lead_decisions import record_decision

    home, _ = decision_home
    with pytest.raises(ValueError, match=field):
        record_decision(_record(**change))
    assert not list((home / "crew" / "decisions").glob("*.json"))


def test_unmounted_records_are_not_returned(decision_home):
    from reckon.crew.lead_decisions import record_decision

    home, mounts = decision_home
    record_decision(_record("companion"))
    (home / "mounts.json").write_text(json.dumps({"sample": str(mounts["sample"])}))
    assert serve_crew_view("decisions")["decisions"] == []


def test_section_holds_and_bound_requests_contribute_to_order(decision_home):
    from reckon.crew.lead_decisions import record_decision

    home, _ = decision_home
    empty = record_decision(_record())
    held = record_decision(_record(held_sections=["reader", "writer", "reader"]))
    _pointer(home, "bound-request", queue_hold={"decision_id": held["id"]})
    _pointer(home, "foreign-request", "companion", decision_id=held["id"])
    rows = serve_crew_view("decisions")["decisions"]
    assert [row["id"] for row in rows] == [held["id"], empty["id"]]
    assert rows[0]["held_sections"] == ["reader", "writer"]
    assert rows[0]["held_requests"] == ["bound-request"]
    assert rows[0]["held_work_count"] == 3


def test_project_filter_and_unknown_project_are_explicit(decision_home):
    from reckon.crew.lead_decisions import record_decision

    record_decision(_record())
    other = record_decision(_record("companion"))
    rows = serve_crew_view("decisions", project="companion")["decisions"]
    assert [row["id"] for row in rows] == [other["id"]]
    assert serve_crew_view("decisions", project="*")["count"] == 2
    refused = serve_crew_view("decisions", project="absent")
    assert refused["ok"] is False
    assert "not mounted" in refused["detail"]


def test_corrupt_record_refuses_the_read_instead_of_claiming_no_decisions(
    decision_home,
):
    from reckon.crew.lead_decisions import decision_path, record_decision

    saved = record_decision(_record())
    assert serve_crew_view("decisions")["count"] == 1
    decision_path(saved["id"]).write_text("{broken")
    result = serve_crew_view("decisions")
    assert result["ok"] is False
    assert result["error"] == "decision_error"
    assert saved["id"] in result["detail"]


def test_record_command_refuses_a_non_object_without_writing(decision_home):
    home, _ = decision_home
    result = CliRunner().invoke(
        main, ["crew", "decision", "record", "--record", "-"], input="[]"
    )
    assert result.exit_code == 1
    assert "JSON object" in json.loads(result.output)["detail"]
    assert not list((home / "crew" / "decisions").glob("*.json"))
