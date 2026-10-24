"""A lift moves an elevated node onto its lane, and the choice records it.

A lane under a lift is a lane whose window is meant to be spent. The picker is
shown that fact in its state, and the dispatcher acts on it: an elevated node a
pick sent to the open local lane is asked once more with that lane left out, so
the lifted lane is reached rather than idled. Ordinary work is left where it
is, and a named lane is never touched by either.

The stand-in for the live pick models the measured behaviour this replaces: a
pick shown no lift preference takes the local lane whenever it is offered. What
moves that answer is the lift state in the rendered state, so a state without it
routes elevated work locally and fails the first test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import budget_lift, dispatch_picker
from reckon.crew import picker as picker_module
from reckon.crew.node import TaskNode
from reckon.crew.picker import client
from reckon.crew.picker import snapshot as snapshot_module
from tests.test_picker_in_dispatch import repo as repo_fixture  # noqa: F401

LOCAL = "clive"
CODEX = "codex"
PROJECT = "proj"
GROUP = "codex-wallet"

ELEVATED = {"class": "general", "requirements": {"risk": "elevated"}}
ROUTINE = {"class": "general", "requirements": {"risk": "low"}}


def _config() -> dict:
    return {
        "default_backend": CODEX,
        "local_backend": LOCAL,
        "routing": {"picker": "route"},
        "backends": {
            LOCAL: {
                "launch": "cli",
                "command": "clive",
                "model": "deepseek",
                "budget_group": "local-wallet",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            },
            CODEX: {
                "launch": "cli",
                "command": "codex",
                "model": "gpt-5",
                "budget_group": GROUP,
                "sandbox": "worktree-full",
                "time_budget": "25m",
            },
        },
        "roles": {"implement": {}, "test": {}, "review": {}},
        "budget": {"pace_multiple": 1.1},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


def _node() -> TaskNode:
    return TaskNode(
        id="n",
        plan="",
        role="implement",
        spec_level="guided",
        goal="g",
        done_when="d",
        time_budget="60m",
    )


def _window(pct: float) -> dict:
    return {
        "headroom": "known",
        "utilisation_pct": pct,
        "burn_multiple": 1.0,
        "resets_at": None,
        "expired": False,
        "observed_at": datetime.now(UTC).isoformat(),
        "source": "account-surface",
    }


def _view(*, lift: dict | None) -> dict:
    """One composed budget view: both lanes open, the metered one lifted."""
    return {
        "groups": [
            {
                "group": GROUP,
                "members": [CODEX],
                "allowance": {
                    "utilisation": 0.2,
                    "burn_multiple": 1.0,
                    "effective_limit": 0.92,
                    "resets_at": None,
                    "reset_available": False,
                    **({"lift": lift} if lift else {}),
                },
            }
        ],
        "backends": [
            {"backend": LOCAL, "held": False, "state": _window(5.0)},
            {"backend": CODEX, "held": False, "state": _window(20.0)},
        ],
    }


def _write_lift(home: Path, *, ends: dict | None = None) -> dict:
    now = datetime.now(UTC)
    record = {
        "id": "lift-1",
        "group": GROUP,
        "form": "uncapped",
        "scope": "global",
        "pace_multiple": 1.0,
        "granted_at": now.isoformat(),
        "cleared_at": None,
        "ends": ends or {"kind": "at", "at": (now + timedelta(hours=6)).isoformat()},
    }
    home.mkdir(parents=True, exist_ok=True)
    (home / budget_lift.LIFTS_LEAF).write_text(
        json.dumps({"version": 1, "lifts": [record]}), encoding="utf-8"
    )
    return record


def _lift_block(config: dict, *, readings: list | None = None) -> dict | None:
    """The lift in force for the group, resolved by the budget resolver itself."""
    block = budget_lift.effective_budget(
        config,
        group=GROUP,
        readings=readings,
        now=datetime.now(UTC),
    )
    lift = block.get("lift")
    return dict(lift) if lift else None


@pytest.fixture()
def repo(repo_fixture):  # noqa: F811 - the pytest fixture name
    """The dispatch fixture, exposed under the name this module's tests use."""
    return repo_fixture


@pytest.fixture(autouse=True)
def isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew-home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


@pytest.fixture()
def state_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot_module, "_fit", lambda *a, **k: [])
    monkeypatch.setattr(
        snapshot_module, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot_module, "_lane", lambda *a, **k: (4, {"waiting": 0}, {})
    )
    monkeypatch.setattr(snapshot_module.routing, "shared_verdict_inputs", lambda *a: {})
    monkeypatch.setattr(
        snapshot_module.routing, "_estimated_hours", lambda *a, **k: (None, None)
    )
    monkeypatch.setattr(
        snapshot_module.routing, "_context_fit_verdict", lambda **k: {"allowed": True}
    )
    monkeypatch.setattr(
        snapshot_module.routing, "_competence_verdict", lambda **k: {"allowed": True}
    )
    from reckon.crew import resumption

    monkeypatch.setattr(
        resumption, "_read_lane_probe_cache", lambda *a, **k: None, raising=False
    )


@pytest.fixture()
def jev_prefers_local(monkeypatch: pytest.MonkeyPatch) -> list:
    """A stand-in for a pick shown no lift preference.

    It takes the local lane whenever the offering carries one, which is what the
    measured runs did while the local lane had room, and otherwise the first
    option it is offered. The distribution is real, so the pick's own answer
    validation runs unchanged.
    """

    calls: list = []

    def ask(state, questions, *, env_path):
        calls.append(state)
        options = list(questions["route"]["criteria"])
        offered = list(state["candidates"])
        local_options = [key for key in offered if key.split(":")[0] == "local"]
        choice = (local_options or offered or ["hold"])[0]
        if choice not in options:
            choice = "hold"
        others = [option for option in options if option != choice]
        odds = {option: 0.1 / len(others) for option in others}
        odds[choice] = 1.0 - 0.1 if others else 1.0
        return {
            "answers": {
                "route": {
                    "choice": choice,
                    "confidence": 0.8,
                    "probabilities": odds,
                }
            }
        }

    monkeypatch.setattr(client, "ask", ask)
    return calls


def _pick(config: dict, capability: dict, view: dict, monkeypatch) -> dict:
    """One dispatch ask, with the requirement the pick reads from the plan.

    dispatch_picker_selection does not take a capability, so the pick's plan
    read -- a repository lookup this test does not need -- is replaced by the
    requirement the test states.
    """
    monkeypatch.setattr(
        picker_module,
        "_declared_difficulty",
        lambda request, _config, **_kwargs: (capability, None, None),
    )
    return dispatch_picker.dispatch_picker_selection(
        node=_node(),
        config=config,
        project=PROJECT,
        repo=Path.cwd(),
        session="session",
        records=[],
        verdict_inputs={},
        budget_snapshot=view,
    )


# ── the preference ───────────────────────────────────────────────────────────


def test_an_elevated_node_routes_to_the_lifted_lane_and_the_lift_is_recorded(
    isolated_crew_home: Path, state_readers, jev_prefers_local, monkeypatch
) -> None:
    """Elevated work takes the lifted lane, and the choice names the lift."""
    config = _config()
    record = _write_lift(isolated_crew_home)
    view = _view(lift=_lift_block(config))

    selection = _pick(config, ELEVATED, view, monkeypatch)

    assert selection["action"] == "route"
    assert selection["backend"] == CODEX
    assert selection["lift"]["lane"] == "codex"
    assert selection["lift"]["lift"]["id"] == record["id"]
    assert selection["reissued"] is True


def test_low_risk_work_stays_local_beneath_the_lift(
    isolated_crew_home: Path, state_readers, jev_prefers_local, monkeypatch
) -> None:
    """A low-risk node keeps the local lane, and no lift is recorded."""
    config = _config()
    _write_lift(isolated_crew_home)
    view = _view(lift=_lift_block(config))

    selection = _pick(config, ROUTINE, view, monkeypatch)

    assert selection["action"] == "route"
    assert selection["backend"] == LOCAL
    assert "lift" not in selection


def test_with_the_lift_cleared_elevated_work_stays_local(
    isolated_crew_home: Path, state_readers, jev_prefers_local, monkeypatch
) -> None:
    """No lift in force means no preference, so even elevated work stays local."""
    config = _config()
    view = _view(lift=None)

    selection = _pick(config, ELEVATED, view, monkeypatch)

    assert selection["backend"] == LOCAL
    assert "lift" not in selection


def test_a_lift_its_reset_has_ended_is_not_preferred(
    isolated_crew_home: Path, state_readers, jev_prefers_local, monkeypatch
) -> None:
    """A reset-anchored lift ends at the rollover the lane's own reading shows."""
    config = _config()
    resets = (datetime.now(UTC) + timedelta(hours=100)).isoformat()
    _write_lift(
        isolated_crew_home,
        ends={
            "kind": "reset",
            "clock": "seven_day",
            "utilisation": 0.63,
            "resets_at": resets,
        },
    )
    readings = [{"seven_day": {"utilisation": 0.04, "resets_at": resets}}]
    lift = _lift_block(config, readings=readings)
    assert lift is None, "the lane's rollover should have ended the lift"

    selection = _pick(config, ELEVATED, view=_view(lift=lift), monkeypatch=monkeypatch)

    assert selection["backend"] == LOCAL
    assert "lift" not in selection


# ── a named lane is authoritative ────────────────────────────────────────────


def test_a_named_lane_is_never_overridden_by_a_lift(
    repo: Path, isolated_crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch naming its lane keeps it, lift in force or not."""
    config = _config()
    _write_lift(isolated_crew_home)
    asked: list = []
    monkeypatch.setattr(picker_module, "pick", lambda *a, **k: asked.append(a))
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--project",
            PROJECT,
            "--plan",
            "example",
            "--section",
            "dispatch",
            "--role",
            "implement",
            "--spec-level",
            "exact",
            "--node",
            "named",
            "--goal",
            "keep the named lane",
            "--done-when",
            "pytest tests/test_picker_prefers_lifted_lane.py passes",
            "--write-path",
            "result.json",
            "--session",
            "session",
            "--repo",
            str(repo),
            "--route",
            "deterministic",
            "--backend",
            LOCAL,
            "--dry-run",
            "--no-watch",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["backend"] == LOCAL
    assert asked == [], "the picker decided over a named lane"
