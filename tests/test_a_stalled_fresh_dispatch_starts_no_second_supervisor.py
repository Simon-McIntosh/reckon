"""A fresh dispatch that stalls past the launch bound starts no supervisor.

The window this closes: a fresh dispatch takes its reservation, then stalls on
storage before it reaches its supervisor start. Meanwhile its reservation passes
``UNREACHED_LAUNCH_BOUND_SECONDS``, stops holding the run, and another launch
takes the next attempt. If the stalled dispatch then resumes and starts its own
supervisor, one run has two supervisors and one worktree.

The fence is the same one the relaunch path already reads: immediately before
starting its supervisor, a fresh dispatch re-reads the reservation it took, and
a newer attempt reserved for the run means it abandons. The abandoned dispatch
writes only its own attempt's exit receipt — the pointer, the run directory and
the worktree now belong to the newer attempt and must be left standing.

Every case works against a temporary crew home and repository, and asserts
afterwards that the operator's own crew home is untouched.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import dispatch_launch as dispatch_launch_module
from reckon.crew import process_liveness
from reckon.crew import runs as runs_module
from reckon.crew.node import CrewError

RUN_ID = "r-stalled-fresh-dispatch"
NODE_ID = "stalled-node"
SESSION = "dispatch-session"

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
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

dispatch_module = importlib.import_module("reckon.crew.dispatch")


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def real_home_untouched() -> None:
    """Every case leaves the operator's own crew home exactly as it found it."""
    real = runs_module.crew_home()
    before = _listing(real)
    yield
    assert _listing(real) == before, "the case wrote into the real crew home"


def _listing(directory: Path) -> list[str] | None:
    try:
        return sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return None


@pytest.fixture()
def repo_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary crew home, project mount and git repository."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKTREE_ROOT", str(tmp_path / "trees"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: None)

    repo = tmp_path / "repo"
    (repo / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (repo / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "docs" / "plans" / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", ".gitignore", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


def _node(manifest_path: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        goal="record one stalled dispatch result",
        plan="fixture",
        section="guard",
        spec_level="guided",
        done_when="pytest reports one passing guard case",
        write_paths=["src/candidate.py"],
        time_budget="20m",
        manifest_path=str(manifest_path),
    )


def _seed_launched_run() -> Path:
    """Mark the run as one that has launched before, so a reservation is taken."""
    directory = runs_module.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    runs_module._write_json(
        directory / "attempt.json",
        {"run_id": RUN_ID, "attempt": 1, "attempt_kind": "dispatch"},
    )
    return directory


def _dispatch(repo: Path, manifest: Path) -> dict:
    return dispatch_module.dispatch(
        node=_node(manifest),
        project="proj",
        repo=repo,
        config=CONFIG,
        session=SESSION,
        run_id=RUN_ID,
        base="HEAD",
    )


def _stub_supervisor(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every supervisor start, and never spawn a real process."""
    started: list[list[str]] = []

    def _spawn(argv: list[str], stderr_path: Path) -> int:
        started.append(list(argv))
        return os.getpid()

    monkeypatch.setattr(dispatch_launch_module, "_spawn_detached_supervisor", _spawn)
    monkeypatch.setattr(
        dispatch_launch_module,
        "_confirm_supervisor_survived",
        lambda pid, run_directory, run_id: None,
    )
    monkeypatch.setattr(
        dispatch_launch_module, "_require_fleet_gate_open", lambda: None
    )
    monkeypatch.setattr(dispatch_module, "_require_fleet_gate_open", lambda: None)
    return started


def _reserve_newer_attempt(directory: Path) -> dict:
    """Age this attempt past the bound and take the next one for the run.

    The caller reaches this from the last step before the fence, which is the
    point a launch stalled on storage would resume from.
    """
    stale = dispatch_launch_module.UNREACHED_LAUNCH_BOUND_SECONDS + 600.0
    own = directory / "attempt-1.reservation"
    stamp = time.time() - stale
    os.utime(own, (stamp, stamp))
    newer = {
        "run_id": RUN_ID,
        "attempt": 2,
        "kind": "dispatch",
        "pid": os.getpid() + 1,
        "pid_start_time": None,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
        "supervisor_pid": None,
    }
    runs_module._write_json(directory / "attempt-2.reservation", newer)
    return newer


def _pointer() -> dict:
    return json.loads(runs_module.pointer_path(RUN_ID).read_text(encoding="utf-8"))


# ── the fence ───────────────────────────────────────────────────────────────


def test_a_stalled_fresh_dispatch_starts_no_second_supervisor(
    repo_context: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stalled dispatch abandons and leaves the newer attempt's records."""
    _config_home, repo = repo_context
    directory = _seed_launched_run()
    started = _stub_supervisor(monkeypatch)

    pointer_before: list[dict] = []
    worktree: list[str] = []
    newer_spec: list[bytes] = []

    def stall_once() -> None:
        # A newer attempt is reserved for the run while this one is stalled,
        # and the newer attempt publishes its own pointer and supervisor spec.
        pointer_before.append(_pointer())
        worktree.append(str(pointer_before[-1]["worktree"]))
        _reserve_newer_attempt(directory)
        newer_pointer = {**pointer_before[-1], "attempt": 2, "phase": "starting"}
        runs_module._write_json(runs_module.pointer_path(RUN_ID), newer_pointer)
        pointer_before.append(newer_pointer)
        spec = directory / "supervisor.json"
        runs_module._write_json(
            spec, {"run_id": RUN_ID, "attempt": 2, "written_by": "the newer attempt"}
        )
        newer_spec.append(spec.read_bytes())

    # The stall lands at the step before the supervisor spec is written, so the
    # abandonment is decided before the shared spec can be replaced.
    original_prepare = dispatch_module._prepare_attempt_records

    def prepare_then_stall(*args: object, **kwargs: object) -> None:
        original_prepare(*args, **kwargs)
        stall_once()

    monkeypatch.setattr(dispatch_module, "_prepare_attempt_records", prepare_then_stall)

    refusal: CrewError | None = None
    try:
        _dispatch(repo, tmp_path / "manifests" / "stalled.md")
    except CrewError as exc:
        refusal = exc

    assert refusal is not None, (
        f"the stalled dispatch started a supervisor instead of refusing: {started}"
    )
    assert "attempt 2" in str(refusal), str(refusal)
    assert "abandons without starting a supervisor" in str(refusal)
    assert started == [], "the superseded dispatch started a supervisor anyway"

    receipt = json.loads(
        (directory / "attempt-1-exit.json").read_text(encoding="utf-8")
    )
    assert receipt["run_id"] == RUN_ID
    assert "abandons" in str(receipt.get("cause")), receipt

    assert (directory / "attempt-2.reservation").exists(), (
        "the abandoning launch removed the newer attempt's reservation"
    )
    assert _pointer()["attempt"] == 2, "the abandoning launch touched the pointer"
    assert directory.is_dir(), "the abandoning launch removed the run directory"
    assert Path(worktree[0]).is_dir(), "the abandoning launch removed the worktree"
    assert (directory / "supervisor.json").read_bytes() == newer_spec[0], (
        "the stalled launch overwrote the newer attempt's supervisor spec"
    )


def test_a_fresh_dispatch_that_is_still_newest_starts_its_supervisor(
    repo_context: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unchanged path: an unreserved-against attempt still launches."""
    _config_home, repo = repo_context
    _seed_launched_run()
    started = _stub_supervisor(monkeypatch)

    record = _dispatch(repo, tmp_path / "manifests" / "fresh.md")

    assert started, "the fence blocked a dispatch whose attempt was still newest"
    assert record.get("pid") == os.getpid()
    assert not (runs_module.run_dir(RUN_ID) / "attempt-1-exit.json").exists(), (
        "a launch that started its supervisor wrote an exit receipt"
    )
