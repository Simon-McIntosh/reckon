"""Per-backend concurrency ceiling: refuse a dispatch that would overload a lane.

A backend that declares ``max_concurrent_runs`` must never be asked to carry
more live runs than that ceiling. The harness retry budget is fixed and reckon
passes no retry configuration, so once an overloaded lane refuses long enough
a 429 turns from a pause at the protocol into a dead print-mode worker —
measured, a sixth concurrent worker on the local lane killed two already
running runs. Adding work destroyed work, so the only reliable remedy is not
to create the overload: the refusal names the backend, the ceiling, the
current count and the occupying run ids, uses the not-dispatchable exit
convention, and happens before any worktree, process or pointer exists. A
finished run holds no slot, and a refusal never touches a run already in
flight.

Every test is hermetic. ``RECKON_HOME`` moves the crew directory into a temp
tree; live pointers claiming a backend are seeded directly into that tree;
the repository is a real but throwaway git repo; and the one launch
substitutes a launcher, so nothing spawns a harness and nothing reaches a
network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, flight
from reckon._flight_schema import BackendConfig
from reckon.crew import runs

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
        "native": {"launch": "in-harness", "time_budget": "25m"},
    },
    "roles": {
        "implement": {},
        "review": {"sandbox": "read-only"},
        "inline": {"backend": "native"},
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path, home):
    """A throwaway git repository carrying the worktree fleet script."""
    root = tmp_path / "repo"
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    source = (
        Path(__file__).absolute().parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text()
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


def _node(**overrides) -> crew.TaskNode:
    """A well-formed node; each test spoils exactly the property it studies."""
    fields = {
        "id": "node-a",
        "goal": "land one worker on a backend",
        "plan": "plan-a",
        "section": "§3",
        "done_when": (
            "uv run pytest tests/test_lane_concurrency_ceiling.py reports passed"
        ),
        "write_paths": ["reckon/_backends.py"],
        "time_budget": "20m",
        "spec_level": "guided",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


def _config(ceiling: int | None) -> dict:
    """CONFIG with the alpha backend's ceiling set, or the key removed."""
    config = json.loads(json.dumps(CONFIG))
    if ceiling is None:
        config["backends"]["alpha"].pop("max_concurrent_runs", None)
    else:
        config["backends"]["alpha"]["max_concurrent_runs"] = ceiling
    return config


def _seed_run(backend: str, phase: str, token: str, *, project: str = "proj") -> str:
    """Write one live pointer claiming a backend, returning its run id."""
    run_id = f"r-20260906T000000000000-{token}"
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "backend": backend,
            "phase": phase,
            "node": {"id": f"peer-{token}", "write_paths": []},
        },
    )
    return run_id


def _worktree_count(repo: Path) -> int:
    """Number of registered git worktrees for the throwaway repository."""
    out = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return len([line for line in out.splitlines() if line.strip()]) - 1


def _launcher(*args, **kwargs):
    """Default launcher substitute: a fake pid, never a spawned worker."""
    return 1


def _dispatch(config: dict, repo: Path, tmp_path: Path, launcher=_launcher) -> dict:
    return crew.dispatch(
        node=_node(manifest_path=str(tmp_path / "manifest.md")),
        project="proj",
        repo=repo,
        config=config,
        session="sess",
        launcher=launcher,
    )


def test_backend_without_a_ceiling_dispatches_unlimited(home, repo, tmp_path):
    """A backend that declares no ceiling keeps dispatching at any occupancy."""
    _seed_run("alpha", "working", "a")
    _seed_run("alpha", "working", "b")
    _seed_run("alpha", "working", "c")

    record = _dispatch(_config(None), repo, tmp_path)

    assert record["phase"] == "starting"
    assert crew.pointer_path(record["run_id"]).is_file()


def test_backend_under_its_ceiling_dispatches(home, repo, tmp_path):
    """Occupancy below the declared ceiling dispatches exactly as today."""
    config = _config(ceiling=5)
    _seed_run("alpha", "working", "a")
    _seed_run("alpha", "working", "b")

    record = _dispatch(config, repo, tmp_path)

    assert record["phase"] == "starting"
    assert crew.pointer_path(record["run_id"]).is_file()


def test_backend_at_ceiling_with_terminal_occupiers_dispatches(home, repo, tmp_path):
    """A finished run holds no slot, so terminal occupiers never refuse."""
    config = _config(ceiling=2)
    _seed_run("alpha", "complete", "finished-a")
    _seed_run("alpha", "failed", "finished-b")

    record = _dispatch(config, repo, tmp_path)

    assert record["phase"] == "starting"
    assert crew.pointer_path(record["run_id"]).is_file()


def test_backend_at_ceiling_refuses_before_creating_anything(home, repo, tmp_path):
    """At its ceiling, a new dispatch is refused with an actionable reason."""
    config = _config(ceiling=2)
    first = _seed_run("alpha", "working", "occupying-a")
    second = _seed_run("alpha", "working", "occupying-b")
    before = set(runs.live_dir().glob("*.json"))
    worktree_count_before = _worktree_count(repo)

    def refusing_launcher(*args, **kwargs):
        raise AssertionError("dispatch must refuse before any launch")

    with pytest.raises(crew.CrewError) as excinfo:
        _dispatch(config, repo, tmp_path, launcher=refusing_launcher)

    message = str(excinfo.value)
    assert message.startswith("node is not dispatchable")
    assert "alpha" in message
    assert "2 live runs of 2 max" in message
    assert first in message
    assert second in message
    # Nothing was created or touched: no pointer, no worktree, no launch.
    assert set(runs.live_dir().glob("*.json")) == before
    assert _worktree_count(repo) == worktree_count_before


def test_non_integer_ceiling_dispatches_instead_of_crashing(home, repo, tmp_path):
    """A malformed ceiling degrades to unlimited rather than breaking dispatch.

    The schema rejects a non-integer ceiling at load time; this guards a
    caller that bypassed the schema and hands dispatch a raw config directly.
    An unknown ceiling cannot justify refusing work, so the dispatch proceeds.
    """
    config = _config(ceiling=1)
    config["backends"]["alpha"]["max_concurrent_runs"] = 1.5
    _seed_run("alpha", "working", "a")

    record = _dispatch(config, repo, tmp_path)

    assert record["phase"] == "starting"
    assert crew.pointer_path(record["run_id"]).is_file()


def test_ceiling_counts_only_runs_on_the_same_backend(home, repo, tmp_path):
    """Another backend's occupancy never consumes this backend's slots."""
    config = _config(ceiling=1)
    _seed_run("alpha", "working", "alpha-run")
    _seed_run("native", "working", "native-run")

    with pytest.raises(crew.CrewError) as excinfo:
        _dispatch(config, repo, tmp_path, launcher=lambda *a, **k: 1)

    assert "node is not dispatchable" in str(excinfo.value)
    assert "alpha-run" in str(excinfo.value)


def _alpha_flight(ceiling: int) -> str:
    """A host flight layer declaring one backend with a max_concurrent_runs."""
    return (
        "backends:\n  alpha:\n"
        "    launch: in-harness\n"
        f"    max_concurrent_runs: {ceiling}\n"
    )


def test_schema_declares_and_validates_max_concurrent_runs(tmp_path, monkeypatch):
    """The backend entry declares the ceiling, defaults to unlimited, and
    rejects a value below one."""
    assert "max_concurrent_runs" in BackendConfig.model_fields

    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    write = config_home / "flight.yaml"
    write.write_text(_alpha_flight(4))
    resolved = flight.resolve(host_path=write)
    assert resolved.config["backends"]["alpha"]["max_concurrent_runs"] == 4

    write.write_text(_alpha_flight(0))
    with pytest.raises(flight.FlightConfigError) as excinfo:
        flight.resolve(host_path=write)
    assert "backends.alpha.max_concurrent_runs" in str(excinfo.value)
