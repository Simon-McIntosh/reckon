"""One read carries recorded and plan-authored decisions across mounts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
        assert row["question"] == _record()["question"]
        assert row["context"] == _record()["context"]
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


@pytest.mark.parametrize(
    "question,context",
    [
        (
            "Which transport carries the work?",
            "One route is full. Another is available.",
        ),
        (
            "Which format should readers accept?",
            "Readers support two formats. Writers need one.",
        ),
    ],
)
def test_authored_question_and_context_survive_recording(
    decision_home, question, context
):
    from reckon.crew.lead_decisions import record_decision

    record_decision(_record(question=question, context=context))
    row = serve_crew_view("decisions")["decisions"][0]
    assert row["question"] == question
    assert row["context"] == context


def test_option_normalization_uses_the_plan_option_owner(decision_home, monkeypatch):
    from reckon import _store
    from reckon.crew.lead_decisions import record_decision

    authored = _record()
    calls = []
    normalize = _store._decision_options_as_choices

    def observed(options):
        calls.append(options)
        return normalize(options)

    monkeypatch.setattr(_store, "_decision_options_as_choices", observed)
    saved = record_decision(authored)
    assert calls == [authored["options"]]
    assert saved["options"][0]["value"] == "route"


@pytest.mark.parametrize(
    "options",
    [
        ["wait", "route"],
        {"wait": "wait", "route": "route"},
        [{"value": "wait"}, {"value": "route"}],
    ],
)
def test_shared_option_spellings_receive_the_same_strict_checks(decision_home, options):
    from reckon.crew.lead_decisions import record_decision

    saved = record_decision(_record(options=options))
    assert saved["options"] == [
        {"value": "route", "label": "route"},
        {"value": "wait", "label": "wait"},
    ]


def test_pending_run_moves_to_ready_only_after_its_own_report(decision_home):
    from reckon.crew.lead_decisions import record_decision

    home, _ = decision_home
    saved = record_decision(_record(pending_on={"run_id": "awaited-run"}))
    before = serve_crew_view("decisions")
    assert before["decisions"] == []
    assert [row["id"] for row in before["pending"]] == [saved["id"]]
    receipt = home / "crew" / "runs" / "awaited-run" / "exit.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"run_id": "different-run", "exit_code": 0}))
    assert serve_crew_view("decisions")["decisions"] == []
    receipt.write_text(json.dumps({"run_id": "awaited-run"}))
    assert serve_crew_view("decisions")["decisions"] == []
    receipt.write_text(json.dumps({"run_id": "awaited-run", "exit_code": 1}))
    after = serve_crew_view("decisions")
    assert after["pending"] == []
    assert [row["id"] for row in after["decisions"]] == [saved["id"]]
    assert after["decisions"][0]["pending_on"] == {"run_id": "awaited-run"}


def test_pending_gate_requires_a_reported_result(decision_home):
    from reckon.crew import gate_watch
    from reckon.crew.lead_decisions import record_decision

    _, mounts = decision_home
    gate = gate_watch.start(
        "sample", verb="verify-gate", checkout=mounts["sample"].parent, command="pytest"
    )
    saved = record_decision(_record(pending_on={"gate_id": gate["id"]}))
    assert serve_crew_view("decisions")["pending"][0]["id"] == saved["id"]
    gate_watch.end(gate["id"], exit_status=0)
    result = serve_crew_view("decisions")
    assert result["pending"] == []
    assert result["decisions"][0]["id"] == saved["id"]


def test_taken_and_reversible_is_separate_from_ready_and_answered(decision_home):
    from reckon.crew.lead_decisions import record_decision

    taken = record_decision(
        _record(choice="route", reversible=True, standing_rule="Use available capacity")
    )
    ready = record_decision(_record())
    result = serve_crew_view("decisions")
    assert [row["id"] for row in result["decisions"]] == [ready["id"]]
    assert result["pending"] == []
    assert [row["id"] for row in result["taken_and_reversible"]] == [taken["id"]]
    assert result["taken_and_reversible"][0]["choice"] == "route"
    assert (
        result["taken_and_reversible"][0]["standing_rule"] == "Use available capacity"
    )


def test_release_context_and_recommendation_provenance_survive(decision_home):
    from reckon.crew.lead_decisions import record_decision

    authored = _record(
        risk="The schema can diverge while this is deferred.",
        lead_action="Store the service credentials.",
        recommended_by="reviewing-session",
        recommendation_source="reviewer",
        held_runs=["deferred-run"],
        held_sections=["writer"],
    )
    record_decision(authored)
    row = serve_crew_view("decisions")["decisions"][0]
    for key in (
        "risk",
        "lead_action",
        "recommended_by",
        "recommendation_source",
        "held_runs",
    ):
        assert row[key] == authored[key]


def test_plan_recommendation_and_live_ownership_use_the_existing_resolver(
    decision_home, monkeypatch
):
    from reckon.crew import plan_coordinator

    _, mounts = decision_home
    _plan(
        mounts["sample"],
        {
            "format": {
                "title": "Which format?",
                "choices": ["compact", "expanded"],
                "recommended": "compact",
                "recommended_by": "review-session",
            }
        },
    )
    calls = []

    def resolve(project, **kwargs):
        calls.append(project)
        return {
            "plans": {
                "transport": {
                    "coordinator": "owner-session",
                    "live": True,
                    "plan_owner": "Plan owner",
                    "tier": "coordinator",
                }
            }
        }

    monkeypatch.setattr(plan_coordinator, "resolve_plan_coordinators", resolve)
    row = serve_crew_view("decisions")["decisions"][0]
    assert "sample" in calls
    assert row["recommended_by"] == "review-session"
    assert row["recommendation_source"] == "reviewer"
    assert row["owner_session"] == "owner-session"
    assert row["plan_has_live_session"] is True
    assert row["session"] == "owner-session"


def test_unowned_plan_is_explicit_without_queue_work(decision_home):
    from reckon.crew.lead_decisions import record_decision

    _, mounts = decision_home
    _plan(mounts["sample"], {})
    record_decision(_record(risk="Deferred compatibility work accumulates."))
    row = serve_crew_view("decisions")["decisions"][0]
    assert row["plan_has_live_session"] is False
    assert row["owner_session"] is None
    assert row["risk"] == "Deferred compatibility work accumulates."


def test_live_owner_and_owner_recommendation_need_no_queued_request(
    decision_home, monkeypatch
):
    from reckon.crew import plan_coordinator
    from reckon.crew.lead_decisions import record_decision

    home, mounts = decision_home
    _plan(mounts["sample"], {})
    _pointer(
        home,
        "active-run",
        phase="working",
        plan="transport",
        created_at=datetime.now(UTC).isoformat(),
    )
    monkeypatch.setattr(
        plan_coordinator, "_live_sessions", lambda project, pointers: {"owner-session"}
    )
    record_decision(_record(recommended_by="owner-session"))
    row = serve_crew_view("decisions")["decisions"][0]
    assert row["held_requests"] == []
    assert row["owner_session"] == "owner-session"
    assert row["plan_has_live_session"] is True
    assert row["recommendation_source"] == "owner"
    monkeypatch.setattr(
        plan_coordinator, "_live_sessions", lambda project, pointers: set()
    )
    ended = serve_crew_view("decisions")["decisions"][0]
    assert ended["owner_session"] == "owner-session"
    assert ended["plan_has_live_session"] is False


def test_a_direct_plan_answer_hides_its_linked_crew_question(decision_home):
    from reckon.crew.lead_decisions import record_decision

    _, mounts = decision_home
    _plan(mounts["sample"], {"format": {"title": "Which format?"}})
    record_decision(_record(decision_key="format"))
    assert serve_crew_view("decisions")["count"] == 2
    _plan(
        mounts["sample"],
        {"format": {"title": "Which format?", "choice": "compact", "by": "Plan owner"}},
    )
    assert serve_crew_view("decisions")["decisions"] == []
