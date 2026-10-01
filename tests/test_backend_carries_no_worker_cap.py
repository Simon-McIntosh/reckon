"""A backend carries no worker cap: the retired roster key and the bounds that remain.

``max_concurrent_runs`` once capped how many live runs a backend could hold,
and a dispatch over the cap was refused. The cap measured the promotion backlog
across every project rather than load on the engine, and a session is meant to
hold its runs for as long as it needs them. So the key is retired: it stays
readable in flight config so an existing host file keeps loading, it is
reported as retired rather than as a bound, and dispatch no longer enforces it.

Only the roster bound retires. The cores a placement's reservation admits and
the login memory slice the coordinator still lives inside protect real
resources, and they stay enforced. A case here drives the cores bound to
exhaustion and shows the dispatch is still refused on it, so a change that
removed every bound would fail rather than pass.

Every case is hermetic. ``RECKON_HOME`` moves the crew directory into a temp
tree, the flight layer and the published reservation record are written under
that tree, the cgroup tree is synthesised under a temp path, the repository is
a real but throwaway git repo, and the launcher is substituted so nothing
spawns a harness and nothing reaches a network.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew, flight
from reckon.crew import placement as placement_module
from reckon.crew import summary as summary_module

GIB = 1024**3

PLACEMENT_QUERIES = {
    "state_query": ["squeue", "-h", "-j", "{job}", "-o", "%T"],
    "reason_query": ["squeue", "-h", "-j", "{job}", "-o", "%r"],
}

# The stub backend declares a roster of 2, which dispatch now ignores. The key
# is declared in a real flight layer rather than a bare dict so the case also
# proves an existing host file carrying it still loads.
FLIGHT_LAYER = """\
default_backend: alpha
backends:
  alpha:
    launch: cli
    command: codex
    model: some-model
    effort: high
    sandbox: worktree-full
    session_reuse: true
    time_budget: 25m
    max_concurrent_runs: 2
"""


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
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    source = (
        Path(__file__).absolute().parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
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


def _resolved_config(home: Path) -> dict:
    """Resolve the flight layer declaring the stub backend's roster of 2."""
    layer = home / "flight.yaml"
    layer.write_text(FLIGHT_LAYER)
    return flight.resolve(host_path=layer).config


def _node(**overrides) -> crew.TaskNode:
    """A well-formed node; each case spoils exactly the property it studies."""
    fields = {
        "id": "node-a",
        "goal": "land one worker on a backend",
        "plan": "plan-a",
        "section": "§3",
        "done_when": (
            "uv run pytest tests/test_backend_carries_no_worker_cap.py reports passed"
        ),
        "write_paths": ["reckon/_backends.py"],
        "time_budget": "20m",
        "spec_level": "guided",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


def _placement(*options: str) -> dict:
    """A placement declaring the given scheduler options and no requirement."""
    return {"scheduler": "srun", "options": list(options), **PLACEMENT_QUERIES}


def _seed_run(
    backend: str,
    phase: str,
    token: str,
    *,
    project: str = "proj",
    placement: dict | None = None,
) -> str:
    """Write one live pointer claiming a backend, returning its run id.

    A pointer given a ``placement`` records a live pid — this process — so the
    reservation roster counts it as a placed worker holding its seat.
    """
    run_id = f"r-20260906T000000000000-{token}"
    pointer = {
        "run_id": run_id,
        "project": project,
        "backend": backend,
        "phase": phase,
        "node": {"id": f"peer-{token}", "write_paths": []},
    }
    if placement is not None:
        pointer["placement"] = placement
        pointer["pid"] = os.getpid()
    crew._write_json(crew.pointer_path(run_id), pointer)
    return run_id


def _publish_reservation(cores: int) -> None:
    """Publish the reservation record the cores bound reads its size from."""
    path = placement_module.reservation_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "job_id": "88000001",
                "size": {"cores": cores, "memory_gb": 120},
                "partition": "all",
            }
        ),
        encoding="utf-8",
    )


def _slice_with_room(tmp_path: Path, monkeypatch) -> None:
    """Synthesise an ample login slice so only the studied bound can refuse."""
    root = tmp_path / "cgroup"
    slice_dir = root / "user.slice" / "user-1000.slice"
    scope_dir = slice_dir / "session-9.scope"
    scope_dir.mkdir(parents=True)
    (scope_dir / "memory.max").write_text("max\n")
    (slice_dir / "memory.max").write_text(f"{64 * GIB}\n")
    (slice_dir / "memory.current").write_text(f"{8 * GIB}\n")
    (slice_dir / "memory.events").write_text(
        "low 0\nhigh 0\nmax 2912593\noom 5536\noom_kill 529\noom_group_kill 0\n"
    )
    cgroup_file = tmp_path / "self-cgroup"
    cgroup_file.write_text("0::/user.slice/user-1000.slice/session-9.scope\n")
    monkeypatch.setattr(summary_module, "LOGIN_CGROUP_ROOT", root)
    monkeypatch.setattr(summary_module, "SELF_CGROUP_PATH", cgroup_file)


def _launcher(*args, **kwargs):
    """Launcher substitute: a fake pid, never a spawned worker."""
    return 1


def _dispatch(config: dict, repo: Path, tmp_path: Path) -> dict:
    return crew.dispatch(
        node=_node(manifest_path=str(tmp_path / "manifest.md")),
        project="proj",
        repo=repo,
        config=config,
        session="sess",
        launcher=_launcher,
    )


# ── The retired roster key: three runs and a fourth dispatch admitted ───────


def test_a_fourth_dispatch_is_admitted_past_the_declared_roster(
    home, repo, tmp_path, monkeypatch
):
    """A backend declaring a roster of 2 admits a fourth run claiming it.

    Three non-terminal pointers claim the stub backend against its declared
    ``max_concurrent_runs: 2``. The key is ignored, so the fourth dispatch is
    admitted rather than refused — the behaviour the cap used to prevent.
    """
    _slice_with_room(tmp_path, monkeypatch)
    config = _resolved_config(home)
    for token in ("occupying-a", "occupying-b", "occupying-c"):
        _seed_run("alpha", "working", token)

    record = _dispatch(config, repo, tmp_path)

    assert record["phase"] == "starting"
    assert crew.pointer_path(record["run_id"]).is_file()


def test_the_summary_reports_the_roster_key_as_retired(home, monkeypatch, tmp_path):
    """The summary names the key and says it is retired, not a bound.

    The entry is still emitted so a reader can find the key, and it never
    binds: three live runs against a declared roster of 2 leave the binding
    bound to a resource, not to the roster.
    """
    _slice_with_room(tmp_path, monkeypatch)
    config = _resolved_config(home)
    backend = config["backends"]["alpha"]
    assert backend["max_concurrent_runs"] == 2  # the key stays readable

    report = summary_module.fleet_bound_report(backend, occupancy=3)

    roster = next(row for row in report["bounds"] if row["name"] == "roster")
    assert roster["value"] == "retired"
    assert roster["admits_one_more"] is True
    assert roster["binding"] is False
    assert report["binding"] != "roster"


# ── The cores bound is still enforced ───────────────────────────────────────


def test_an_exhausted_partition_core_bound_still_refuses(
    home, repo, tmp_path, monkeypatch
):
    """Only the roster bound retired: spent partition cores still refuse.

    A reservation records 2 admitted cores, two placed workers ask one core
    each, and the backend still declares ``max_concurrent_runs: 2``. The cores
    bound is the one that binds, and the refusal names it rather than the
    retired roster — so a change that removed every bound would fail here.
    """
    _slice_with_room(tmp_path, monkeypatch)
    _publish_reservation(cores=2)
    placement = _placement("--cpus-per-task=1")
    config = _resolved_config(home)
    assert config["backends"]["alpha"]["max_concurrent_runs"] == 2
    config["backends"]["alpha"]["placement"] = placement
    for token in ("a", "b"):
        _seed_run("alpha", "working", f"occupying-{token}", placement=placement)

    with pytest.raises(crew.CrewError) as excinfo:
        _dispatch(config, repo, tmp_path)

    message = str(excinfo.value)
    assert "partition admitted cores" in message
    assert "2 of 2 cores admitted (1 per worker)" in message
    assert "concurrency ceiling" not in message
