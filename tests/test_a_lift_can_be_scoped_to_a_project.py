"""Project lifts cover future sessions while preserving scope and credit limits."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import budget, crew_dispatch_commands, crew_run_commands
from reckon.cli import main
from reckon.crew import budget_lift as bl
from reckon.crew import lift_watch, routing
from reckon.crew.picker import client, lane_context, snapshot
from reckon.crew_follow_commands import _follow_selects
from tests.test_picker_in_dispatch import repo as repo_fixture  # noqa: F401

GROUP = "project-lift-fixture"
BACKEND = "codex"
PROJECT = "proj"
OTHER = "other-project"
BALANCE = 60_000.0
CAP = 10_000.0


def _config():
    return {
        "default_backend": BACKEND,
        "routing": {"picker": "route"},
        "backends": {
            BACKEND: {
                "budget_group": GROUP,
                "launch": "cli",
                "command": "codex",
                "model": "fixture-model",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            },
        },
        "roles": {"implement": {}, "review": {}},
        "budget": {
            "pace_multiple": 1.1,
            "utilisation_ceiling_pct": 100.0,
            "resume_reserve_pct": 5.0,
            "coordinator_reserve_pct": 3.0,
            "bookend_reserve_pct": 20.0,
        },
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


@pytest.fixture
def dispatch_repo(repo_fixture):  # noqa: F811
    return repo_fixture


@pytest.fixture
def fleet(isolated_reckon_home, tmp_path, monkeypatch):
    config = _config()
    monkeypatch.setattr(crew_run_commands, "_resolved_flight", lambda *a, **k: config)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *a, **k: config
    )
    home = bl.lifts_path().parent
    assert home in {isolated_reckon_home, tmp_path / "config"}
    return SimpleNamespace(home=home, config=config, now=datetime.now(UTC))


def _invoke(*args):
    return CliRunner().invoke(main, ["crew", "budget", *args])


def _grant_cli(*projects):
    args = ["--lift", "--group", GROUP, "--uncapped", "--reason", "spend this project"]
    for project in projects:
        args += ["--for-project", project]
    result = _invoke(*args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _grant(fleet, *, scope=None, form=bl.UNCAPPED, offset=0):
    return bl.grant(
        fleet.config,
        group=GROUP,
        reason="spend the selected scope",
        scope=scope or f"project:{PROJECT}",
        form=form,
        multiple=1.8 if form == bl.MULTIPLE else None,
        credits=CAP if form == bl.CREDITS else None,
        balance_at_grant=BALANCE if form == bl.CREDITS else None,
        now=fleet.now + timedelta(seconds=offset),
        environ={},
    )


def _state(fleet, *, pct=95.0, balance=None):
    result = {
        "backend": BACKEND,
        "headroom": "known",
        "utilisation_pct": pct,
        "expired": False,
        "observed_at": fleet.now.isoformat(),
        "resets_at": (fleet.now + timedelta(hours=2)).isoformat(),
    }
    if balance is not None:
        result.update(credits_available=True, credit_balance=balance)
    return result


def _dispatch_verdict(fleet, project=PROJECT, session="future-session", **state):
    return routing._budget_verdict(
        project=project,
        root=fleet.home / "repo",
        config=fleet.config,
        backend_name=BACKEND,
        backend=fleet.config["backends"][BACKEND],
        purpose="dispatch",
        budget_state=_state(fleet, **state),
        now=datetime.now(UTC),
        session=session,
    )


def _effective(fleet, *, project=PROJECT, session=None):
    return bl.effective_budget(
        fleet.config,
        group=GROUP,
        project=project,
        session=session,
        readings=None,
        now=fleet.now + timedelta(minutes=1),
    )


def test_project_lift_admits_a_future_session_through_dispatch_but_not_another_project(
    fleet,
):
    assert _dispatch_verdict(fleet)["held"] is True
    lift = _grant_cli(PROJECT)
    admitted = _dispatch_verdict(fleet)
    assert admitted["held"] is False
    assert admitted["effective_ceiling_pct"] == 100.0
    assert (
        _effective(fleet, session="created-after-grant")["lift_id"] == lift["lift_id"]
    )
    assert _dispatch_verdict(fleet, OTHER)["held"] is True
    assert _dispatch_verdict(fleet, OTHER, session="another-session")["held"] is True


def test_session_then_project_then_global_govern_before_grant_time(fleet):
    project_lift = _grant(fleet)
    global_lift = _grant(fleet, scope=bl.GLOBAL, form=bl.MULTIPLE, offset=2)
    assert _effective(fleet)["lift_id"] == project_lift["id"]
    assert _effective(fleet, project=OTHER)["lift_id"] == global_lift["id"]
    session_lift = _grant(fleet, scope="session:owner", form=bl.MULTIPLE, offset=1)
    assert _effective(fleet, session="owner")["lift_id"] == session_lift["id"]
    # A newer project grant still cannot supersede the owner's session scope.
    newer = _grant(fleet, offset=3)
    assert _effective(fleet, session="owner")["lift_id"] == session_lift["id"]
    assert _effective(fleet, session="peer")["lift_id"] == newer["id"]


def test_repeated_projects_write_independent_records_and_clearing_one_preserves_the_other(
    fleet,
):
    result = _grant_cli(PROJECT, OTHER)
    assert {row["scope"] for row in result["results"]} == {
        f"project:{PROJECT}",
        f"project:{OTHER}",
    }
    document = bl.read_document()
    assert len(document["lifts"]) == 2
    assert {row["reason"] for row in document["lifts"]} == {"spend this project"}
    assert _dispatch_verdict(fleet, PROJECT)["held"] is False
    assert _dispatch_verdict(fleet, OTHER)["held"] is False
    listed = _invoke("--list", "--group", GROUP)
    assert listed.exit_code == 0, listed.output
    assert {row["scope"] for row in json.loads(listed.output)["lifts"]} == {
        f"project:{PROJECT}",
        f"project:{OTHER}",
    }
    cleared = _invoke("--lift", "--clear", "--group", GROUP, "--for-project", PROJECT)
    assert cleared.exit_code == 0, cleared.output
    assert json.loads(cleared.output)["cleared"]["scope"] == f"project:{PROJECT}"
    assert _dispatch_verdict(fleet, PROJECT)["held"] is True
    assert _dispatch_verdict(fleet, OTHER)["held"] is False
    again = _invoke("--lift", "--clear", "--group", GROUP, "--for-project", PROJECT)
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["cleared"] is None


def test_clearing_a_project_without_a_project_lift_keeps_the_global_lift(fleet):
    lift = _grant(fleet, scope=bl.GLOBAL)
    result = _invoke("--lift", "--clear", "--group", GROUP, "--for-project", PROJECT)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["cleared"] is None
    assert _effective(fleet)["lift_id"] == lift["id"]


def test_project_credit_cap_measures_the_account_draw_across_sessions(fleet):
    lift = _grant(fleet, form=bl.CREDITS)
    for session in ("first", "future-session"):
        block = _effective(fleet, session=session)
        assert block["credit_lift"]["id"] == lift["id"]
        assert (
            _dispatch_verdict(fleet, session=session, pct=100, balance=BALANCE - 100)[
                "held"
            ]
            is False
        )
        assert (
            _dispatch_verdict(fleet, session=session, pct=100, balance=BALANCE - CAP)[
                "held"
            ]
            is True
        )
    assert (
        _dispatch_verdict(fleet, OTHER, pct=100, balance=BALANCE - 100)["held"] is True
    )


def test_project_lift_rows_reach_only_that_projects_panes(fleet):
    lift = _grant(fleet)
    rows = {}
    for project in (PROJECT, OTHER):
        output = []
        seen = lift_watch.sweep(
            project,
            config=fleet.config,
            now=fleet.now,
            readings_for=lambda *args: [],
            stream_path=fleet.home / f"{project}.events",
            append=lambda path, values, output=output: output.extend(values),
        )
        rows[project] = output
        assert bool(seen) == (project == PROJECT)
    assert len(rows[PROJECT]) == 1
    row = rows[PROJECT][0]
    assert row["lift_id"] == lift["id"]
    assert _follow_selects(row, session="owner")
    assert _follow_selects(row, session="future-session")
    assert rows[OTHER] == []
    bl.clear(fleet.config, group=GROUP, project=PROJECT, now=fleet.now, environ={})
    ended = []
    lift_watch.sweep(
        PROJECT,
        seen={lift["id"]: lift},
        config=fleet.config,
        now=fleet.now,
        readings_for=lambda *args: [],
        stream_path=fleet.home / "ended.events",
        append=lambda path, values: ended.extend(values),
    )
    assert ended[0]["to_state"] == lift_watch.LIFT_ENDED


@pytest.mark.parametrize("extra", [("--global",), ("--session", "owner")])
def test_project_scope_cannot_be_combined_with_another_scope(fleet, extra):
    result = _invoke(
        "--lift",
        "--group",
        GROUP,
        "--for-project",
        PROJECT,
        "--uncapped",
        "--reason",
        "ambiguous scope",
        *extra,
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    assert bl.read_document()["lifts"] == []


def test_check_keeps_its_single_project_argument(fleet, monkeypatch):
    calls = []

    def check(project, *args):
        calls.append(project)
        return {"ok": True, "project": project}, 0

    monkeypatch.setattr(crew_run_commands, "_budget_check_report", check)
    result = _invoke("--check", "--project", "checked-project")
    assert result.exit_code == 0, result.output
    assert calls == ["checked-project"]
    refused = _invoke(
        "--check", "--project", "checked-project", "--for-project", PROJECT
    )
    assert refused.exit_code != 0
    assert "--for-project requires --lift" in refused.output


def test_unnamed_dispatch_offers_the_lane_its_project_lift_opens(
    dispatch_repo, fleet, monkeypatch
):
    from reckon.crew import picker

    _grant_cli(PROJECT)
    monkeypatch.setattr(
        lane_context,
        "local_lane",
        lambda **kwargs: {"admission": "open", "worker_slots": 4},
    )
    monkeypatch.setattr(snapshot, "_fit", lambda *a, **k: [])
    monkeypatch.setattr(snapshot, "_dispatch_lane_gate", lambda *a: {"state": "open"})
    monkeypatch.setattr(snapshot, "_lane", lambda *a: (4, {"waiting": 0}, {}))
    monkeypatch.setattr(
        snapshot.routing, "_context_fit_verdict", lambda **k: {"allowed": True}
    )
    monkeypatch.setattr(
        snapshot.routing, "_competence_verdict", lambda **k: {"allowed": True}
    )
    monkeypatch.setattr(
        picker, "_declared_difficulty", lambda *a, **k: ({}, None, None)
    )
    view = {
        "groups": [
            {
                "group": GROUP,
                "members": [BACKEND],
                "allowance": {"state": budget.OBSERVED},
            }
        ],
        "backends": [{"backend": BACKEND, "held": True, "state": _state(fleet)}],
    }
    monkeypatch.setattr(snapshot, "budget_view", lambda *a, **k: view)
    asked = []

    def ask(state, questions, **kwargs):
        asked.append(state)
        offered = list(state["candidates"])
        assert len(offered) == 1, state
        key = offered[0]
        criteria = questions["route"]["criteria"]
        return {
            "answers": {
                "route": {
                    "choice": key,
                    "confidence": 0.9,
                    "probabilities": {
                        name: (1.0 if name == key else 0.0) for name in criteria
                    },
                }
            }
        }

    monkeypatch.setattr(client, "ask", ask)
    result = CliRunner().invoke(
        main,
        [
            "crew",
            "dispatch",
            "--project",
            PROJECT,
            "--repo",
            str(dispatch_repo),
            "--plan",
            "example",
            "--section",
            "dispatch",
            "--role",
            "implement",
            "--spec-level",
            "exact",
            "--node",
            "project-lift-routing",
            "--goal",
            "route through the project lift",
            "--done-when",
            "pytest checks that the lifted lane is offered",
            "--write-path",
            "result.json",
            "--session",
            "future-session",
            "--route",
            "picker",
            "--dry-run",
            "--no-watch",
        ],
    )
    assert result.exit_code == 0, result.output
    assert asked, "an unnamed dispatch did not ask the picker"
    payload = json.loads(result.output.splitlines()[0])
    assert payload["backend"] == BACKEND
    assert any(row["backend"] == BACKEND for row in asked[0]["candidates"].values())
