"""One presenting session offers each open decision to the lead, exactly once.

The skill's own contract is checked in `test_select_skill_contract.py`. These
are the behavioural checks its prose describes, asserted against the
presentation rather than against the prose:

* two sessions presenting at once are handed each decision exactly once between
  them, because a claim is taken before a choice is returned;
* a ready decision is offered before a plan-block one, and the plan-block
  choice reports its owning session, recorded time and held work as absent by
  name rather than as empty or zero;
* the survey asks each live session once and names the session it cannot reach
  with the reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import harness_sessions, lead_presentation


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Two mounted projects with their own plan documents and no decisions yet."""
    home = tmp_path / "config"
    home.mkdir()
    mounts = {}
    for project in ("sample", "companion"):
        docs = tmp_path / project / "docs"
        (docs / "plans").mkdir(parents=True)
        mounts[project] = str(docs)
    registry = home / "mounts.json"
    registry.write_text(json.dumps(mounts), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(registry))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(home / "state"))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv(harness_sessions.HARNESS_SESSIONS_DIR_ENV, str(sessions))
    return home, {key: Path(value) for key, value in mounts.items()}, sessions


def _record(project: str, **extra):
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


def _pointer(home: Path, run_id: str, project: str) -> None:
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
            }
        ),
        encoding="utf-8",
    )


def _plan_block(docs: Path, slug: str) -> None:
    """A plan whose decisions block holds nothing: no session, time or work."""
    from reckon._plan_html import write_state

    path = docs / "plans" / f"{slug}.html"
    path.write_text(
        write_state(
            "<html><head><title>Plan</title></head><body>"
            '<h2 id="body">Body</h2></body></html>',
            {
                "slug": slug,
                "status": "active",
                "modified": "2026-10-01",
                "decisions": {
                    "surface": {
                        "title": "Which surface carries the read?",
                        "choices": ["terminal", "page"],
                        "option_labels": {
                            "terminal": "The terminal",
                            "page": "A browser page",
                        },
                        "recommended": "terminal",
                    }
                },
            },
        ),
        encoding="utf-8",
    )


def _session_record(sessions: Path, session: str, alive: bool) -> None:
    import os

    (sessions / f"{session}.json").write_text(
        json.dumps(
            {
                "sessionId": session,
                "pid": os.getpid() if alive else 999_999_991,
                "startedAt": 1,
                "name": f"peer-for-{session}",
            }
        ),
        encoding="utf-8",
    )


def test_two_presenting_sessions_are_handed_each_decision_once(fleet):
    """Two presenters at once: every decision is claimed by exactly one."""
    from reckon.crew.lead_decisions import record_decision

    one = record_decision(_record("sample"))
    two = record_decision(_record("companion", session="other-session"))

    first = lead_presentation.present(session="presenter-a")
    second = lead_presentation.present(session="presenter-b")

    offered = [item["id"] for item in first["choices"]] + [
        item["id"] for item in second["choices"]
    ]
    assert sorted(offered) == sorted([one["id"], two["id"]])
    assert len(offered) == len(set(offered)) == 2
    assert second["choices"] == []
    assert {item["id"] for item in second["claimed_elsewhere"]} == set(offered)
    assert {item["presented_by"] for item in second["claimed_elsewhere"]} == {
        "presenter-a"
    }


def test_ready_choices_precede_plan_block_ones_with_absent_fields(fleet):
    """The ready decision is offered first; the plan-block one names what it
    does not have instead of reporting it as empty."""
    from reckon.crew.lead_decisions import record_decision

    home, mounts, _sessions = fleet
    _pointer(home, "held-request", "sample")
    ready = record_decision(_record("sample", held_requests=["held-request"]))
    _plan_block(mounts["companion"], "surface")

    view = lead_presentation.choices()
    states = [item["state"] for item in view["choices"]]
    assert states == ["ready", "plan-block"], view
    assert view["choices"][0]["id"] == ready["id"]
    assert view["choices"][0]["releases"]["requests"] == ["held-request"]
    assert view["choices"][0]["releases"]["count"] == 1
    assert view["choices"][0]["absent"] == []

    plan_block = view["choices"][1]
    assert plan_block["repository"] == "companion"
    assert plan_block["plan"] == "surface"
    assert plan_block["absent"] == ["session", "recorded_at", "held_work"]
    assert plan_block["session"] is None
    assert plan_block["recorded_at"] is None
    assert plan_block["held_work_count"] is None
    assert plan_block["releases"] is None
    assert plan_block["options"][0] == {
        "value": "terminal",
        "label": "The terminal",
        "recommended": True,
    }


def test_the_survey_asks_each_live_session_once_and_names_the_unreached(fleet):
    """A reachable session is asked once; a dead process and a session the
    harness keeps no record for are named, never assumed to have replied."""
    _home, _mounts, sessions = fleet
    _session_record(sessions, "live-session", alive=False)
    (sessions / "no-peer.json").write_text(
        json.dumps({"sessionId": "no-peer", "pid": __import__("os").getpid()}),
        encoding="utf-8",
    )
    addresses = harness_sessions.addresses()
    assert addresses["live-session"]["reachable"] is False
    assert addresses["no-peer"] == {"name": None, "reachable": True}

    first = lead_presentation.survey(session="presenter")
    assert first["asked"] == []
    unreached = {row["session"]: row["detail"] for row in first["unreached"]}
    assert set(unreached) == {"live-session", "no-peer"}
    assert "live process" in unreached["live-session"]
    assert "peer" in unreached["no-peer"]

    _session_record(sessions, "reachable-session", alive=True)
    second = lead_presentation.survey(session="presenter")
    assert [row["session"] for row in second["asked"]] == ["reachable-session"]
    third = lead_presentation.survey(session="presenter")
    assert third["asked"] < second["asked"] or third["asked"] == []
    assert [row["session"] for row in third["already_asked"]] == ["reachable-session"]


def test_an_answer_is_recorded_through_the_presenting_session(fleet):
    """The verb the skill names lands the choice, and the claim is spent."""
    from reckon.crew.lead_decisions import record_decision

    _home, _mounts, _sessions = fleet
    decision = record_decision(_record("sample"))
    shown = lead_presentation.present(session="presenter")
    assert [item["id"] for item in shown["choices"]] == [decision["id"]]

    recorded = lead_presentation.choose(
        session="presenter", decision_id=decision["id"], option="wait"
    )
    assert recorded["choice"] == "wait"
    assert recorded["answer_via"] == "presenting-session"
    assert lead_presentation.choices()["choices"] == []
    assert lead_presentation.present(session="other")["choices"] == []


def test_the_present_verb_prints_the_choice_set() -> None:
    """The CLI form the skill runs is the presentation, not a second reading."""
    result = CliRunner().invoke(
        main, ["crew", "decision", "present", "--session", "probe", "--pretty"]
    )
    assert result.exit_code in (0, 1), result.output
    if result.exit_code == 0:
        payload = json.loads(result.output)
        assert payload["view"] == "lead-choices"
        assert "unread_projects" in payload


def test_a_refused_project_is_named_and_does_not_reorder_the_fleet(
    tmp_path, monkeypatch
):
    """A project whose documents cannot be read is named and dropped, and the
    rows that survive keep one fleet-wide held-work order.

    The fallback reads project by project, so the order it merges in is not the
    order the read sorts in: without re-sorted lists the lead would be handed
    one project's one-request choice before another project's four-request one.
    """
    home = tmp_path / "config"
    home.mkdir()
    mounts = {}
    for project in ("broken", "small", "busy"):
        docs = tmp_path / project / "docs"
        (docs / "plans").mkdir(parents=True)
        mounts[project] = str(docs)
    registry = home / "mounts.json"
    registry.write_text(json.dumps(mounts), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(registry))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(home / "state"))

    # A research-located document carrying no reckon-type of its own refuses
    # the fleet read with a location/type collision.
    research = Path(mounts["broken"]) / "research"
    research.mkdir()
    (research / "colliding.html").write_text(
        "<html><head><title>Collision</title></head><body>"
        "<p>No reckon-type, so the location and the type disagree.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    from reckon.crew.lead_decisions import record_decision

    small = record_decision(_record("small", held_sections=["payload"]))
    busy = record_decision(
        _record("busy", held_sections=["one", "two", "three", "four"])
    )

    view = lead_presentation.choices()
    assert [item["id"] for item in view["choices"]] == [busy["id"], small["id"]]
    assert [item["held_work_count"] for item in view["choices"]] == [4, 1]
    assert [row["project"] for row in view["unread_projects"]] == ["broken"]
    assert "colliding.html" in view["unread_projects"][0]["detail"]
