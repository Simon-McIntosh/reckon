"""A dispatch carries the reservation's reach, and the reach counts the guard's population.

One unkeyed record holds the host's placement allocation, and its roster cap is
summed over every placed live run on the host — so a session that arms the
reservation through a dispatch must be told the cap's reach: the projects whose
placed runs it counts right now. The dispatch route keeps the ensure's report
and copies it onto the run's payload, which is the only report the dispatching
session reads, and a dispatch that finds the reservation already held gets the
same reach from the read-back.

The reach and the guard that refuses past the cap must count one population: a
placed worker of any backend is a step inside the same allocation, so a run
under a second placing backend neither escapes the cap nor goes unnamed by the
reach.

Everything here is hermetic. The scheduler verbs are recording stubs on the
PATH, the crew state is a temporary ``RECKON_HOME``, the launcher is substituted
so no worker is spawned, the login memory slice is a stubbed reading, and the
repository is a throwaway git repo.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew, flight
from reckon.crew import placement as placement_module
from reckon.crew import runs
from reckon.crew import summary as summary_module

dispatch_module = importlib.import_module("reckon.crew.dispatch")

GIB = 1024**3
GRANTED_ID = "55600001"
RESERVATION_JOB = "1277272"

PLACED = {"scheduler": "srun", "options": ["--ntasks=1"]}

# The reach statement must say whose work the cap counts: the whole host's and
# every project's, never the holding project's own.
HOST_WIDE = "counts every project's placed live runs on this host"
NOT_ONLY_HOLDER = "not only the holding project's"

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
    placement:
      scheduler: srun
      options:
        - --ntasks=1
      state_query: ["squeue", "-h", "-j", "{job}", "-o", "%T"]
      reason_query: ["squeue", "-h", "-j", "{job}", "-o", "%r"]
"""

PLAN_HTML = """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="dispatch">Dispatch</h2></body></html>
"""

# Every stub appends its own argv, so a case shows which questions were asked.
# salloc grants a fixed job id; squeue answers the only state the reservation's
# liveness probe reads, and no case declares a job-id probe that would read it
# any other way.
_RECORDING_SCHEDULER = """#!/bin/sh
name=${0##*/}
printf '%s\\t%s\\n' "$name" "$*" >> "$FAKE_SCHEDULER_LOG"
case "$name" in
  salloc)
    printf 'salloc: Granted job allocation %s\\n' "${FAKE_JOB:-__GRANTED__}"
    ;;
  squeue)
    printf 'RUNNING\\n'
    ;;
esac
exit 0
""".replace("__GRANTED__", GRANTED_ID)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("FAKE_SCHEDULER_LOG", str(config_home / "scheduler.log"))
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
    (root / "docs" / "plans" / "plan-a.html").write_text(PLAN_HTML)
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


def _roomy_login_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the login memory reading so only the reservation's own bound can refuse."""
    monkeypatch.setattr(
        summary_module,
        "read_login_slice",
        lambda **kwargs: summary_module.LoginSlice(
            readable=True,
            current=8 * GIB,
            maximum=64 * GIB,
            alloc_stalls=0,
            oom_kills=0,
        ),
    )


def _recording_scheduler(directory: Path) -> Path:
    """Executables named for the scheduler verbs, each recording its own argv."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("salloc", "sbatch", "squeue", "srun"):
        executable = directory / name
        executable.write_text(_RECORDING_SCHEDULER, encoding="utf-8")
        executable.chmod(0o755)
    return directory


def _scheduler_path(bin_dir: Path) -> str:
    """The recording stubs first on the system PATH, so they shadow any client."""
    return os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])


def _asked(name: str) -> list[str]:
    """The recorded invocations of one scheduler verb."""
    log = Path(os.environ["FAKE_SCHEDULER_LOG"])
    if not log.exists():
        return []
    return [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith(name + "\t")
    ]


def _resolved_config(home: Path) -> dict:
    """Resolve the flight layer whose default backend declares a placement."""
    layer = home / "flight.yaml"
    layer.write_text(FLIGHT_LAYER)
    return flight.resolve(host_path=layer).config


def _node(node_id: str, write_paths: list[str]) -> crew.TaskNode:
    """A well-formed node; the cases vary only the identity between dispatches."""
    return crew.TaskNode(
        id=node_id,
        goal="land one worker on a placed backend",
        plan="plan-a",
        section="Dispatch",
        done_when="uv run pytest tests/test_reservation_reach_reaches_dispatch.py reports passed",
        write_paths=write_paths,
        negative_control=(
            "the dispatch route discards the ensure_reservation result again, so "
            "the arming case finds no reach in the payload"
        ),
        time_budget="20m",
        spec_level="guided",
    )


def _launcher(*args, **kwargs):
    """Launcher substitute: a fake pid, never a spawned worker."""
    return 1


def _dispatch(config: dict, repo: Path, node_id: str, write_paths: list[str]) -> dict:
    return crew.dispatch(
        node=_node(node_id, write_paths),
        project="proj",
        repo=repo,
        config=config,
        session="sess",
        launcher=_launcher,
    )


def _seed_placed_pointer(run_id: str, project: str, backend: str) -> None:
    """Write one live pointer placed inside the reservation, its worker this process.

    Recording this process as the pid makes the roster's liveness read see a
    resident placed worker wherever the real probe runs, so the seat is real
    without stubbing the process table.
    """
    pointer = {
        "run_id": run_id,
        "project": project,
        "backend": backend,
        "launcher_host": os.uname().nodename,
        "pid": os.getpid(),
        "phase": "working",
        "placement": dict(PLACED),
    }
    path = crew.pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer), encoding="utf-8")


def test_a_dispatch_that_arms_or_finds_the_reservation_carries_its_reach(
    home, repo, tmp_path, monkeypatch
):
    """Both payloads name the reach, and the read-back names the cap.

    A placed live run of another project is seeded first, so the reach the
    payloads carry names a project that is not the dispatching one — the
    distinction the statement exists for. The first dispatch mints the
    allocation and its payload states the reach; the second starts nothing and
    its payload states the same cap's reach, because a session that joins the
    reservation is still about to run inside it.
    """
    _roomy_login_slice(monkeypatch)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    config = _resolved_config(home)
    _seed_placed_pointer("r-beta-seat", "beta", "beta-backend")

    armed = _dispatch(config, repo, "node-one", ["reckon/crew/placement.py"])

    hold = armed["placement_reservation"]
    assert hold["reason"] == "held"
    assert hold["job_id"] == GRANTED_ID
    assert HOST_WIDE in hold["detail"]
    assert NOT_ONLY_HOLDER in hold["detail"]
    assert "beta" in hold["detail"], "the reach names the other project's seat"
    assert hold["roster_reach"]["statement"] in hold["detail"]
    assert len(_asked("salloc")) == 1

    # The run's persisted pointer carries the same report a later reader sees.
    pointer = json.loads(crew.pointer_path(armed["run_id"]).read_text())
    assert pointer["placement_reservation"] == hold

    joined = _dispatch(
        config, repo, "node-two", ["tests/test_reservation_reach_reaches_dispatch.py"]
    )

    read_back = joined["placement_reservation"]
    assert read_back["reason"] == "already-held"
    assert read_back["job_id"] == GRANTED_ID
    assert "started nothing" in read_back["detail"]
    assert HOST_WIDE in read_back["detail"]
    assert NOT_ONLY_HOLDER in read_back["detail"]
    assert "beta" in read_back["detail"]
    assert len(_asked("salloc")) == 1, "the second dispatch minted no allocation"


def test_the_reach_and_the_guard_count_the_same_population(home, tmp_path, monkeypatch):
    """Twenty six placed runs across two backends are one roster and one reach.

    Thirteen placed runs on each of two placing backends sit one over the cap
    when counted together. The dispatch guard must refuse on all twenty six —
    not on the dispatching backend's thirteen — and the reach must name exactly
    the projects of the runs the refusal counts, so a reader of the statement
    sees the same population the refusal was taken over.
    """
    _roomy_login_slice(monkeypatch)
    placement_module.publish_reservation(
        {
            "job_id": RESERVATION_JOB,
            "size": {"cores": 64, "memory_gb": 128},
            "partition": "all",
        }
    )
    project_of: dict[str, str] = {}
    for backend, project in (("alpha-backend", "alpha"), ("beta-backend", "beta")):
        for index in range(13):
            run_id = f"r-{backend}-{index}"
            _seed_placed_pointer(run_id, project, backend)
            project_of[run_id] = project

    placed_backend = {"launch": "cli", "command": "codex", "placement": dict(PLACED)}
    with pytest.raises(runs.CrewError) as refused:
        dispatch_module._refuse_over_concurrency_ceiling(
            "alpha-backend", placed_backend, "alpha"
        )

    message = str(refused.value)
    assert "26 workers already occupy" in message, message
    named = [
        item.strip()
        for item in message.split("Occupying runs: ", 1)[1].rstrip(".").split(",")
    ]
    assert len(named) == 26

    reach = placement_module._roster_reach()
    assert HOST_WIDE in reach["statement"]
    assert sorted(reach["projects"]) == ["alpha", "beta"]
    assert sorted({project_of[run_id] for run_id in named}) == sorted(
        reach["projects"]
    ), "the reach names exactly the population the guard counted"
