"""The admission check refuses a dispatch over the binding bound, naming it.

The fleet's concurrency is bounded by whichever resource runs out first: the
roster ceiling a backend declares, the cores a placement's reservation admits,
or the login memory slice the coordinator still lives inside. The bound model
reads all three and reports the one that actually limits the next worker; the
admission check at the dispatch entry point refuses against that one and names
it with its measured value, so a reader learns which resource ran out rather
than which one somebody happened to be watching.

These cases exercise that from outside, through ``crew.dispatch``, with each
measured input synthesised: the roster count is live pointer files in a temp
crew directory, the cores are the placement options a backend declares, and the
login slice is a cgroup tree written under a temp path. Nothing here calls the
bound helpers directly — the subject is the dispatch entry point's refusal, so
a helper that reads correctly while the call site passes the wrong occupancy or
the wrong reading still fails these cases. Each refusal case asserts the
sentence for the bound that bound *and* the absence of the other bounds'
sentences, so a refusal for the wrong reason is not read as a pass.

Every case is hermetic. ``RECKON_HOME`` moves the crew directory into a temp
tree, the cgroup tree is synthesised under a temp path, the repository is a
real but throwaway git repo, the launcher is substituted so nothing spawns a
harness and nothing reaches a network, and a recording scheduler is on PATH so
the admitted case's reservation ensure is intercepted rather than reaching the
host's real scheduler.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import summary as summary_module

GIB = 1024**3

# The scheduler verbs a placed dispatch may run, and a recording stub for each.
# A stub appends its own argv to FAKE_SCHEDULER_LOG, grants a job id for
# ``salloc`` and answers a probe for ``squeue``, so a dispatch that reaches the
# ensure is intercepted here rather than reaching the host's real scheduler.
_SCHEDULER_VERBS = ("salloc", "sbatch", "srun", "squeue", "scontrol", "scancel")

_RECORDING_SCHEDULER = """#!/bin/sh
name=${0##*/}
printf '%s\\t%s\\n' "$name" "$*" >> "$FAKE_SCHEDULER_LOG"
case "$name" in
  salloc)
    printf 'salloc: Granted job allocation 88000001\\n'
    ;;
  squeue)
    printf '%s\\n' RUNNING
    ;;
esac
exit 0
"""

# Every run this module dispatches is named for this node, and the node id is
# what the minted run id carries, so a scan of the real live-pointer directory
# can tell this module's writes apart from the fleet's. Every id here is minted
# for this module, and this name is minted nowhere else.
_NODE_ID = "node-named-bound"

# The real live-pointer directory is where a fleet's runs actually live, so a
# write that landed there instead of in the temp home would collide with a live
# session rather than fail a test. Pointing ``RECKON_HOME`` at a temp tree is
# what should stop that, and this is the assertion that it did.
_REAL_LIVE_DIR = Path.home() / ".config" / "reckon" / "crew" / "live"


@pytest.fixture(scope="module", autouse=True)
def _real_home_gains_no_pointer():
    """The temporary home holds every pointer this module writes.

    A regression that stopped the temp home taking effect would be invisible to
    the module: the dispatch would still refuse and the assertions would still
    pass, while the write landed in the live fleet's own directory. So the real
    directory is read before and after, keyed on the node name this module
    alone mints, and the run id it gains must be none.
    """
    before = (
        set(_REAL_LIVE_DIR.glob(f"*{_NODE_ID}*.json"))
        if _REAL_LIVE_DIR.is_dir()
        else set()
    )
    yield
    after = (
        set(_REAL_LIVE_DIR.glob(f"*{_NODE_ID}*.json"))
        if _REAL_LIVE_DIR.is_dir()
        else set()
    )
    assert not after - before, "this module wrote a live pointer into the real home"


@pytest.fixture(autouse=True)
def _resolvable_backend_command(tmp_path, monkeypatch):
    """Put a stub backend command and a recording scheduler on PATH.

    Only the admitted case reaches launch composition — a refusal is raised
    before any launch is composed — but a module that resolved the backend's
    command from the host would pass or fail on what that host happens to have
    installed. The stub is never executed: the launcher is substituted, so
    nothing spawns a harness.

    The admitted case also reaches the ensure that holds the shared reservation.
    With no reservation in the temp home the ensure would ask the host's real
    ``salloc`` for an allocation, so this module provides a recording scheduler
    whose ``salloc`` grants a job id without touching a scheduler: the case
    keeps its assertion on the admitted dispatch and the suite never holds a real
    allocation.
    """
    directory = tmp_path / "stub-bin"
    directory.mkdir()
    stub = directory / "codex"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    scheduler = directory / "scheduler"
    scheduler.mkdir()
    for verb in _SCHEDULER_VERBS:
        recording = scheduler / verb
        recording.write_text(_RECORDING_SCHEDULER)
        recording.chmod(0o755)
    monkeypatch.setenv("FAKE_SCHEDULER_LOG", str(directory / "scheduler.log"))
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([str(scheduler), str(directory), os.environ.get("PATH", "")]),
    )


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
        ["commit", "-q", "-m", "chore: dispatch fixture"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    return root


def _node(**overrides) -> crew.TaskNode:
    """A well-formed node; each case declares the occupancy it studies."""
    fields = {
        "id": _NODE_ID,
        "goal": "land one worker on a backend",
        "plan": "plan-a",
        "section": "§3",
        "done_when": (
            "uv run pytest tests/test_concurrency_bound_refuses_by_name.py "
            "reports passed"
        ),
        "write_paths": ["reckon/_backends.py"],
        "time_budget": "20m",
        "spec_level": "guided",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


def _config(**alpha) -> dict:
    """CONFIG with the alpha backend's keys set to what a case studies."""
    config = json.loads(json.dumps(CONFIG))
    config["backends"]["alpha"].update(alpha)
    return config


def _placement(*options: str) -> dict:
    """A placement declaring the given scheduler options and no requirement."""
    return {"scheduler": "srun", "options": list(options), **PLACEMENT_QUERIES}


def _seed_run(backend: str, phase: str, token: str, *, project: str = "proj") -> str:
    """Write one live pointer claiming a backend, returning its run id.

    This is the roster count the admission check reads: a live run holds a
    slot, and the number of them is the occupancy every bound is measured at.
    """
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
    """Launcher substitute: a fake pid, never a spawned worker."""
    return 1


def _dispatch(config: dict, repo: Path, tmp_path: Path) -> dict:
    """One dispatch through the entry point, with every input synthesised."""
    return crew.dispatch(
        node=_node(manifest_path=str(tmp_path / "manifest.md")),
        project="proj",
        repo=repo,
        config=config,
        session="sess",
        launcher=_launcher,
    )


def _synthesise_slice(tmp_path: Path, monkeypatch, *, current: int, maximum: int):
    """Write a cgroup tree and point the reader at it.

    The session scope declares ``max``, exactly as the kernel does for a leaf
    under a limited slice, so the reading resolves to the slice above it — the
    level the fleet's limit is declared at.
    """
    root = tmp_path / "cgroup"
    slice_dir = root / "user.slice" / "user-1000.slice"
    scope_dir = slice_dir / "session-9.scope"
    scope_dir.mkdir(parents=True)
    (scope_dir / "memory.max").write_text("max\n")
    (slice_dir / "memory.max").write_text(f"{maximum}\n")
    (slice_dir / "memory.current").write_text(f"{current}\n")
    (slice_dir / "memory.events").write_text(
        "low 0\nhigh 0\nmax 2912593\noom 5536\noom_kill 529\noom_group_kill 0\n"
    )
    cgroup_file = tmp_path / "self-cgroup"
    cgroup_file.write_text("0::/user.slice/user-1000.slice/session-9.scope\n")
    monkeypatch.setattr(summary_module, "LOGIN_CGROUP_ROOT", root)
    monkeypatch.setattr(summary_module, "SELF_CGROUP_PATH", cgroup_file)


# ── The roster bound refusing, and naming itself ────────────────────────────


def test_a_dispatch_over_the_roster_bound_is_refused_naming_the_roster(
    home, repo, tmp_path, monkeypatch
):
    """A roster at its ceiling refuses, naming that bound and its measured value.

    The slice is synthesised with room to spare and the placement declares no
    admitted cores, so the roster is the only bound that can refuse: a refusal
    here can only have come from the roster, and its sentence must carry the
    count that did the refusing.
    """
    _synthesise_slice(tmp_path, monkeypatch, current=8 * GIB, maximum=64 * GIB)
    config = _config(max_concurrent_runs=2)
    first = _seed_run("alpha", "working", "occupying-a")
    second = _seed_run("alpha", "working", "occupying-b")

    with pytest.raises(crew.CrewError) as excinfo:
        _dispatch(config, repo, tmp_path)

    message = str(excinfo.value)
    # The bound that bound, and the figure that refused the dispatch.
    assert "concurrency ceiling" in message
    assert "2 live runs of 2 max" in message
    assert "max_concurrent_runs" in message
    assert first in message and second in message
    # Neither of the other bounds' sentences, so a refusal for some other
    # reason is not read as this case passing.
    assert "partition admitted cores" not in message
    assert "login memory slice" not in message


# ── The cores bound refusing, and naming itself ─────────────────────────────


def test_a_dispatch_over_the_cores_bound_is_refused_naming_the_cores(
    home, repo, tmp_path, monkeypatch
):
    """A reservation whose cores are spent refuses, naming that bound.

    The backend declares no roster ceiling and the slice has room to spare, so
    the placement's admitted cores are the only bound that can refuse. Three
    occupants asking one core each against three admitted cores is the figure
    the sentence must carry.
    """
    _synthesise_slice(tmp_path, monkeypatch, current=8 * GIB, maximum=64 * GIB)
    config = _config(placement=_placement("--cpus=3", "--cpus-per-task=1"))
    for token in ("a", "b", "c"):
        _seed_run("alpha", "working", f"occupying-{token}")

    with pytest.raises(crew.CrewError) as excinfo:
        _dispatch(config, repo, tmp_path)

    message = str(excinfo.value)
    assert "partition admitted cores" in message
    assert "3 of 3 cores admitted (1 per worker)" in message
    # The roster keeps its own sentence for the roster's own bound, and this
    # refusal is not it.
    assert "concurrency ceiling" not in message
    assert "login memory slice" not in message


# ── A dispatch inside every bound being admitted ────────────────────────────


def test_a_dispatch_under_both_bounds_is_admitted(home, repo, tmp_path, monkeypatch):
    """Occupancy inside the roster and the admitted cores dispatches.

    Both bounds are declared and measured, and neither is spent: one occupant
    against a roster of four, and two cores asked of six admitted for a
    reservation of eight. A check that refused here would be refusing work the
    fleet has room for.
    """
    _synthesise_slice(tmp_path, monkeypatch, current=8 * GIB, maximum=64 * GIB)
    config = _config(
        max_concurrent_runs=4,
        placement=_placement("--cpus=8", "--cpus-per-task=2", "--mem=16G"),
    )
    _seed_run("alpha", "working", "occupying-a")

    record = _dispatch(config, repo, tmp_path)

    assert record["phase"] == "starting"
    assert crew.pointer_path(record["run_id"]).is_file()
