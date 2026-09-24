"""The binding bound: which resource limits concurrency, and the refusal it makes.

Concurrency is bounded by whichever resource runs out first, and a surface that
reports one number cannot say which. Three candidates are read: the roster
ceiling a backend declares, the cores a placement's reservation admits, and the
login memory slice the coordinator still lives inside. The admission check
refuses a dispatch that would exceed the bound that is actually binding, naming
it and its measured value.

The memory slice is read as ``memory.current`` against ``memory.max`` with the
allocation-stall counter beside them, because those move before a kill.
``oom_kill`` is reported beside them as history only: it increments once a
process is already dead, so it can confirm a kill and can never warn of one. A
slice whose files cannot be read reports its bound as unknown and refuses
nothing, because a reading that was never taken is not evidence of exhaustion.

Every test is hermetic. ``RECKON_HOME`` moves the crew directory into a temp
tree, the cgroup tree is synthesised under a temp path, the repository is a real
but throwaway git repo, and the one launch substitutes a launcher so nothing
spawns a harness and nothing reaches a network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import summary as summary_module
from reckon.crew import ticker
from reckon.crew.dispatch import _refuse_over_concurrency_ceiling

GIB = 1024**3

# A placement that declares no requirement, so the placement-requirement check
# reaches no verdict of its own and the bound under test is the only refusal.
PLACEMENT_QUERIES = {
    "state_query": ["squeue", "-h", "-j", "{job}", "-o", "%T"],
    "reason_query": ["squeue", "-h", "-j", "{job}", "-o", "%r"],
}

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


def _node(**overrides) -> crew.TaskNode:
    """A well-formed node; each test spoils exactly the property it studies."""
    fields = {
        "id": "node-a",
        "goal": "land one worker on a backend",
        "plan": "plan-a",
        "section": "§3",
        "done_when": (
            "uv run pytest tests/test_crew_binding_bound.py reports passed"
        ),
        "write_paths": ["reckon/_backends.py"],
        "time_budget": "20m",
        "spec_level": "guided",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


def _config(**alpha) -> dict:
    """CONFIG with the alpha backend's keys set to what a test studies."""
    config = json.loads(json.dumps(CONFIG))
    config["backends"]["alpha"].update(alpha)
    return config


def _placement(*options: str) -> dict:
    """A placement declaring the given scheduler options and no requirement."""
    return {"scheduler": "srun", "options": list(options), **PLACEMENT_QUERIES}


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


def _fake_slice(
    tmp_path: Path,
    monkeypatch,
    *,
    current: int,
    maximum: int,
    stalls: int = 2912593,
    kills: int = 529,
    scope: str = "user.slice/user-1000.slice/session-9.scope",
) -> None:
    """Synthesise a cgroup tree and point the reader at it.

    The session scope declares ``max``, exactly as the kernel does for a leaf
    under a limited slice, so a reader that takes the first ``memory.max`` it
    finds would read an unlimited leaf and report a bound that is not there.
    """
    root = tmp_path / "cgroup"
    slice_dir = root / "user.slice" / "user-1000.slice"
    scope_dir = root / Path(scope)
    scope_dir.mkdir(parents=True, exist_ok=True)
    slice_dir.mkdir(parents=True, exist_ok=True)
    (scope_dir / "memory.max").write_text("max\n")
    (slice_dir / "memory.max").write_text(f"{maximum}\n")
    (slice_dir / "memory.current").write_text(f"{current}\n")
    (slice_dir / "memory.events").write_text(
        f"low 0\nhigh 0\nmax {stalls}\noom 5536\noom_kill {kills}\n"
        "oom_group_kill 0\n"
    )
    cgroup_file = tmp_path / "self-cgroup"
    cgroup_file.write_text(f"0::/{scope}\n")
    monkeypatch.setattr(summary_module, "LOGIN_CGROUP_ROOT", root)
    monkeypatch.setattr(summary_module, "SELF_CGROUP_PATH", cgroup_file)


def _unreadable_slice(tmp_path: Path, monkeypatch) -> None:
    """Point the reader at a cgroup membership that does not resolve."""
    missing = tmp_path / "absent-cgroup"
    monkeypatch.setattr(summary_module, "LOGIN_CGROUP_ROOT", tmp_path / "no-such-tree")
    monkeypatch.setattr(summary_module, "SELF_CGROUP_PATH", missing)


# ── Each bound being the binding one in turn ────────────────────────────────


def test_the_roster_ceiling_refuses_when_it_is_the_binding_bound(
    home, tmp_path, monkeypatch
):
    """At its declared ceiling, the roster refuses and keeps its known sentence."""
    _fake_slice(tmp_path, monkeypatch, current=8 * GIB, maximum=64 * GIB)
    config = _config(max_concurrent_runs=2)

    first = _seed_run("alpha", "working", "occupying-a")
    second = _seed_run("alpha", "working", "occupying-b")
    with pytest.raises(crew.CrewError) as excinfo:
        _refuse_over_concurrency_ceiling("alpha", config["backends"]["alpha"])

    message = str(excinfo.value)
    assert message.startswith("node is not dispatchable")
    assert "alpha" in message
    assert "2 live runs of 2 max" in message
    assert first in message
    assert second in message


def test_the_partition_admitted_cores_refuse_when_they_are_the_binding_bound(
    home, tmp_path, monkeypatch
):
    """A reservation whose cores are spent refuses, naming the cores and the figure."""
    _fake_slice(tmp_path, monkeypatch, current=8 * GIB, maximum=64 * GIB)
    config = _config(placement=_placement("--cpus=3", "--cpus-per-task=1"))
    for token in ("a", "b", "c"):
        _seed_run("alpha", "working", f"occupying-{token}")

    with pytest.raises(crew.CrewError) as excinfo:
        _refuse_over_concurrency_ceiling("alpha", config["backends"]["alpha"])

    message = str(excinfo.value)
    assert "node is not dispatchable" in message
    assert "partition admitted cores" in message
    assert "3 of 3 cores admitted (1 per worker)" in message


def test_the_login_memory_slice_refuses_when_it_is_the_binding_bound(
    home, tmp_path, monkeypatch
):
    """A slice that cannot hold one more reserved worker refuses, naming the slice."""
    _fake_slice(tmp_path, monkeypatch, current=8 * GIB, maximum=20 * GIB)
    config = _config(placement=_placement("--mem=16G"))
    _seed_run("alpha", "working", "occupying-a")

    with pytest.raises(crew.CrewError) as excinfo:
        _refuse_over_concurrency_ceiling("alpha", config["backends"]["alpha"])

    message = str(excinfo.value)
    assert "node is not dispatchable" in message
    assert "login memory slice" in message
    assert "8.0 GiB of 20.0 GiB resident, 16.0 GiB per worker" in message
    # The warning is the resident figure against the limit. The kill count is
    # carried beside it, labelled as history, and is never the refusal itself.
    assert "oom_kill 529 (history)" in message


# ── The unknown case, and the counter that cannot warn ──────────────────────


def test_an_unreadable_slice_reports_the_bound_unknown_and_refuses_nothing(
    home, tmp_path, monkeypatch
):
    """No reading is not exhaustion: an unreadable cgroup refuses no worker."""
    _unreadable_slice(tmp_path, monkeypatch)
    config = _config(placement=_placement("--mem=16G"))

    report = summary_module.fleet_bound_report(
        config["backends"]["alpha"], occupancy=40
    )
    memory = next(row for row in report["bounds"] if row["name"] == "login-memory")
    assert memory["value"].startswith("unknown")
    assert memory["utilisation"] is None
    assert memory["admits_one_more"] is True
    # With the roster and the cores unstated too, nothing readable can refuse.
    assert report["binding"] is None
    assert report["refused"] is False
    # And the admission check agrees: with no bound readable, it dispatches.
    _refuse_over_concurrency_ceiling("alpha", config["backends"]["alpha"])


def test_the_oom_kill_counter_is_reported_only_as_history(
    home, tmp_path, monkeypatch
):
    """A large kill count beside ample headroom is history, never a refusal."""
    _fake_slice(
        tmp_path, monkeypatch, current=8 * GIB, maximum=64 * GIB, kills=999999
    )
    config = _config(placement=_placement("--mem=16G"))
    _seed_run("alpha", "working", "occupying-a")

    reading = summary_module.read_login_slice()
    assert reading.oom_kills == 999999
    assert reading.alloc_stalls == 2912593
    bound = summary_module.memory_bound(
        config["backends"]["alpha"], occupancy=1, login_slice=reading
    )
    # The kill count is beside the warning, not the warning: the bound admits.
    assert bound.admits_one_more is True
    assert "oom_kill 999999 (history)" in bound.history
    assert "resident" in bound.value
    _refuse_over_concurrency_ceiling("alpha", config["backends"]["alpha"])


def test_an_undeclared_reservation_leaves_the_core_bound_unknown(
    home, tmp_path, monkeypatch
):
    """A placement stating no admitted cores cannot refuse on cores."""
    _fake_slice(tmp_path, monkeypatch, current=8 * GIB, maximum=64 * GIB)
    config = _config(placement=_placement("--partition=all"))

    report = summary_module.fleet_bound_report(
        config["backends"]["alpha"], occupancy=99
    )
    cores = next(row for row in report["bounds"] if row["name"] == "partition-cores")
    assert cores["value"] == "unknown"
    assert cores["utilisation"] is None
    assert cores["admits_one_more"] is True


# ── The surface reports the binding bound with its measured value ───────────


def test_the_surface_names_the_binding_bound_with_its_value():
    """Each bound in turn is reported as binding, with the figure beside the label."""
    cases = [
        (
            {"max_concurrent_runs": 2},
            2,
            None,
            "roster",
            "2 live runs of 2 max",
        ),
        (
            {"placement": _placement("--cpus=3", "--cpus-per-task=1")},
            3,
            8 * GIB,
            "partition-cores",
            "3 of 3 cores admitted (1 per worker)",
        ),
        (
            {"placement": _placement("--mem=16G")},
            1,
            8 * GIB,
            "login-memory",
            "8.0 GiB of 20.0 GiB resident, 16.0 GiB per worker",
        ),
    ]
    for backend, occupancy, current, name, value in cases:
        maximum = 20 * GIB if name == "login-memory" else 64 * GIB
        reading = summary_module.LoginSlice(
            readable=True, current=current or 0, maximum=maximum
        )
        report = summary_module.fleet_bound_report(
            backend, occupancy=occupancy, login_slice=reading
        )
        assert report["binding"] == name
        assert report["value"] == value
        assert report["refused"] is True
        # The rendered clause carries the label and the measured value, so a
        # reader gets the figure rather than a name they must look up.
        clause = ticker.bound_clause(report)
        assert clause.startswith(f"bind {name} ")
        assert value[:18] in clause


def test_the_surface_says_unknown_rather_than_naming_a_bound_it_could_not_read():
    """No readable bound is reported as unknown, not as the first candidate."""
    report = summary_module.fleet_bound_report(
        {}, occupancy=0, login_slice=summary_module.LoginSlice(readable=False)
    )
    assert report["binding"] is None
    assert report["value"] == "unknown"
    assert report["refused"] is False
    assert ticker.bound_clause(report) == "bind unknown"


def test_a_transition_without_a_bound_reading_carries_no_bound_cell():
    """An older record adds no clause rather than an unknown one."""
    assert ticker.bound_cells(None) == []
    assert ticker.bound_cells({}) == []


# ── The reader against a synthesised tree and the live host ─────────────────


def test_the_reader_walks_out_to_the_slice_that_declares_a_limit(tmp_path, monkeypatch):
    """The leaf scope declares ``max``; the slice above it declares the limit."""
    _fake_slice(tmp_path, monkeypatch, current=3 * GIB, maximum=64 * GIB)

    reading = summary_module.read_login_slice()

    assert reading.readable is True
    assert reading.maximum == 64 * GIB
    assert reading.current == 3 * GIB
    assert reading.utilisation == pytest.approx(3 / 64)


def test_a_slice_whose_memory_max_is_unlimited_is_unknown(tmp_path, monkeypatch):
    """A slice that declares no limit is unbounded, never empty."""
    root = tmp_path / "cgroup"
    directory = root / "user.slice" / "user-1000.slice"
    directory.mkdir(parents=True)
    (directory / "memory.max").write_text("max\n")
    (directory / "memory.current").write_text(f"{GIB}\n")
    cgroup_file = tmp_path / "self-cgroup"
    cgroup_file.write_text("0::/user.slice/user-1000.slice/session-9.scope\n")
    monkeypatch.setattr(summary_module, "LOGIN_CGROUP_ROOT", root)
    monkeypatch.setattr(summary_module, "SELF_CGROUP_PATH", cgroup_file)

    reading = summary_module.read_login_slice()

    assert reading.readable is False
    assert reading.maximum is None
    assert reading.utilisation is None


def test_the_shipped_slice_reader_finds_the_host_memory_limit():
    """The real host slice is read without an injected tree, when it exists."""
    reading = summary_module.read_login_slice()
    if not reading.readable:
        pytest.skip("this host exposes no finite login memory slice to read")
    assert reading.maximum and reading.maximum > 0
    assert reading.current is not None and reading.current >= 0
    assert reading.alloc_stalls is not None
    assert reading.oom_kills is not None


def test_a_bound_that_cannot_price_a_worker_does_not_refuse():
    """A readable slice with no per-worker reservation reports, but refuses none."""
    reading = summary_module.LoginSlice(readable=True, current=63 * GIB, maximum=64 * GIB)
    bound = summary_module.memory_bound({}, occupancy=10, login_slice=reading)
    assert bound.utilisation == pytest.approx(63 / 64)
    assert bound.admits_one_more is True