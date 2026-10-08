"""A horizon that was never measured must not refuse a node.

The competence verdict compared a node's neutral estimate against a
configuration's measured horizon. A configuration whose horizon was absent or
unreadable was coerced to ``0.0`` — the same number as a horizon that was
measured and came out zero — so an unmeasured configuration refused nothing in
one direction and would refuse everything in the other. Measured on 2026-09-25:
a redispatch carried an earlier reproduction naming the invented figure
directly, ``12.0 worker-hours exceeds the 0.0 worker-hour competence horizon``.

The range below is the input domain rather than one node: no horizon at all, a
measured horizon large enough to bound the node, a measured zero, and the
redispatch writer that now carries an explicit estimate.

Every test here is hermetic: ``RECKON_HOME`` moves the crew directory into a
temp tree, the repository is a throwaway git repo, and every launch substitutes
a launcher — so nothing spawns a harness and nothing reaches a network.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, ledger

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
        "beta": {
            "launch": "cli",
            "command": "codex",
            "model": "another-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
        "clive": {
            "launch": "cli",
            "command": "clive",
            "model": "clive-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    """A throwaway git repository carrying the plan the node points at."""
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text()
    )
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="s3">§3 — Dispatch</h2></body></html>
"""
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/plan-a.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    return root


def _node(*, estimated_hours: float | None = 12.0) -> crew.TaskNode:
    return crew.TaskNode(
        id="unmeasured-horizon",
        goal="record whether an unmeasured horizon refuses an oversize node",
        plan="plan-a",
        section="§3",
        done_when=(
            "tests/test_unmeasured_horizon_never_refuses.py passes: an unmeasured "
            "horizon allows the node and a measured one still bounds the node"
        ),
        write_paths=["reckon/crew/routing.py"],
        time_budget="20m",
        spec_level="guided",
        estimated_hours=estimated_hours,
    )


def _capability_cache(*, horizon: float | None, speed: float = 1.0) -> dict:
    """A cache whose only configuration is the agent this node routes to."""
    agent = {
        "backend": "alpha",
        "launch": "cli",
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
    }
    return {
        "configurations": [
            {
                "key": json.dumps(agent, sort_keys=True, separators=(",", ":")),
                "competence_horizon_hours": horizon,
                "speed": {"mean": speed},
            }
        ]
    }


def _dispatch(
    home: Path, repo: Path, *, horizon: float | None, estimated_hours: float | None
) -> dict:
    return crew.dispatch(
        node=_node(estimated_hours=estimated_hours),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="coordinator",
        launcher=lambda *args, **kwargs: 4242,
    )


def _set_plan_hours(repo: Path, hours: float) -> None:
    """Give the plan an effort figure, for a node that carries no estimate."""
    plan = repo / "docs" / "plans" / "plan-a.html"
    plan.write_text(
        plan.read_text().replace(
            '<meta name="plan-slug" content="plan-a">',
            '<meta name="plan-slug" content="plan-a">\n'
            f'<meta name="plan-effort-hours" content="{hours}">',
        )
    )
    subprocess.run(
        ["git", "add", "docs/plans/plan-a.html"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: set plan hours"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_an_unmeasured_horizon_allows_an_oversize_node(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No horizon was measured, so no horizon can refuse this node."""
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: _capability_cache(horizon=None),
    )

    record = _dispatch(home, repo, horizon=None, estimated_hours=12.0)

    competence = record["competence"]
    assert competence["allowed"] is True
    assert competence["reason"] == "no-measured-horizon"
    assert competence["estimated_hours"] == 12.0


def test_an_unreadable_horizon_allows_an_oversize_node(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unparseable horizon is unmeasured, not a measured zero."""
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: _capability_cache(horizon="not-a-number"),
    )

    record = _dispatch(home, repo, horizon=None, estimated_hours=12.0)

    competence = record["competence"]
    assert competence["allowed"] is True
    assert competence["reason"] == "no-measured-horizon"


def test_a_measured_horizon_refuses_an_oversize_node_naming_both(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measured horizon smaller than the node still refuses, both figures named."""
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: _capability_cache(horizon=4.0),
    )

    with pytest.raises(crew.CompetenceLimit) as raised:
        _dispatch(home, repo, horizon=4.0, estimated_hours=12.0)

    message = str(raised.value)
    assert "12.0 worker-hours" in message
    assert "4.0 worker-hour competence horizon" in message


def test_a_measured_zero_is_not_an_unmeasured_horizon(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A horizon measured as zero is a measurement, and it refuses the node."""
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: _capability_cache(horizon=0.0),
    )

    with pytest.raises(crew.CompetenceLimit) as raised:
        _dispatch(home, repo, horizon=0.0, estimated_hours=12.0)

    message = str(raised.value)
    assert "12.0 worker-hours" in message
    assert "0.0 worker-hour competence horizon" in message


def test_the_estimate_falls_back_to_the_plan_effort(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node with no estimate of its own still gets one, from the plan."""
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: _capability_cache(horizon=None),
    )
    _set_plan_hours(repo, 12.0)

    record = _dispatch(home, repo, horizon=None, estimated_hours=None)

    competence = record["competence"]
    assert competence["allowed"] is True
    assert competence["reason"] == "no-measured-horizon"
    assert competence["estimated_hours"] == 12.0
    assert competence["estimate_provenance"] == "plan-fallback"


def _live_run(home: Path, repo: Path) -> dict:
    ledger.register_member("proj", "worker-a", harness="alpha", root=repo)
    record = crew.dispatch(
        node=_node(estimated_hours=12.0),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="coordinator",
        member="worker-a",
        launcher=lambda *args, **kwargs: 999931,
    )
    pointer = crew.read_pointer(str(record["run_id"]))
    pointer.update({"phase": "working", "pid": 41001})
    crew._write_json(crew.pointer_path(str(record["run_id"])), pointer)
    return pointer


def test_redispatch_records_the_estimate_it_was_given(home: Path, repo: Path) -> None:
    """A redispatch carrying an explicit estimate replaces and records it."""
    from reckon.crew.dispatch import change_lane

    pointer = _live_run(home, repo)

    moved = change_lane(
        str(pointer["run_id"]),
        "clive",
        "the first lane is spent",
        config=CONFIG,
        estimated_hours=7.5,
        launcher=lambda *args, **kwargs: 42002,
    )

    assert moved["estimated_hours"] == 7.5
    assert moved["node"]["estimated_hours"] == 7.5
    assert moved["lane_changes"][-1]["to_backend"] == "clive"
    reread = crew.read_pointer(str(pointer["run_id"]))
    assert reread["estimated_hours"] == 7.5
    assert reread["node"]["estimated_hours"] == 7.5


def test_redispatch_without_an_estimate_keeps_the_carried_one(
    home: Path, repo: Path
) -> None:
    """Absent an override, the estimate the run carried is left in place."""
    from reckon.crew.dispatch import change_lane

    pointer = _live_run(home, repo)

    moved = change_lane(
        str(pointer["run_id"]),
        "clive",
        "the first lane is spent",
        config=CONFIG,
        launcher=lambda *args, **kwargs: 42003,
    )

    assert moved["estimated_hours"] == 12.0
    assert moved["node"]["estimated_hours"] == 12.0


def test_redispatch_cli_accepts_estimated_hours(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command surface exposes the override and passes it through."""
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        crew, "read_pointer", lambda run_id: {"run_id": run_id, "project": ""}
    )

    def _fake_change_lane(run_id, backend, reason, **kwargs):
        captured.update({"run_id": run_id, "backend": backend, **kwargs})
        return {"run_id": run_id, "backend": backend}

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(dispatch_module, "change_lane", _fake_change_lane)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "redispatch",
            "--run",
            "run-1",
            "--backend",
            "clive",
            "--reason",
            "the first lane is spent",
            "--estimated-hours",
            "7.5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["estimated_hours"] == 7.5
    assert captured["backend"] == "clive"
