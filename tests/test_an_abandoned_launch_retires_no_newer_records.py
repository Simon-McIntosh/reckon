"""An abandoned fresh dispatch leaves the newer attempt's records standing.

The window this closes: a fresh dispatch takes its reservation, then stalls on
storage. Its reservation stops holding the run, a newer attempt is reserved and
the newer attempt writes the run's canonical attempt, worker and exit records —
but its worker is not live yet, so the liveness refusal inside the attempt
preparation does not fire. If the stalled dispatch then resumes, preparing its
own records overwrites the canonical attempt record and unlinks the canonical
worker and exit records, and the run reads as one whose newer attempt left no
account of itself.

So the fence that refuses a superseded launch is read before the attempt records
are prepared, not only before the supervisor spec is written. A superseded
dispatch writes only its own attempt's exit receipt and starts no supervisor.

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

RUN_ID = "r-an-abandoned-launch"
NODE_ID = "abandoned-launch-node"
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
        goal="record one abandoned launch result",
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


NEWER_RECORDS = ("attempt.json", "worker.json", "exit.json")


def _write_newer_attempt_records(directory: Path) -> dict[str, bytes]:
    """The newer attempt publishes its own records; its worker is not live."""
    newer = {
        "run_id": RUN_ID,
        "attempt": 2,
        "kind": "dispatch",
        "pid": os.getpid() + 1,
        "pid_start_time": None,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "supervisor_pid": None,
    }
    runs_module._write_json(directory / "attempt-2.reservation", newer)
    runs_module._write_json(
        directory / "attempt.json",
        {"run_id": RUN_ID, "attempt": 2, "attempt_kind": "dispatch"},
    )
    runs_module._write_json(directory / "worker.json", {"run_id": RUN_ID, "attempt": 2})
    runs_module._write_json(
        directory / "exit.json",
        {"run_id": RUN_ID, "attempt": 2, "exit_code": 0},
    )
    return {name: (directory / name).read_bytes() for name in NEWER_RECORDS}


# ── the fence ───────────────────────────────────────────────────────────────


def test_an_abandoned_fresh_dispatch_retires_no_newer_record(
    repo_context: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The abandoned dispatch refuses before it touches the newer records."""
    _config_home, repo = repo_context
    directory = _seed_launched_run()
    started = _stub_supervisor(monkeypatch)

    before: dict[str, bytes] = {}
    stalled: list[bool] = []

    original_consume = dispatch_module.consume_context_refusal

    def stall_then_consume(*args: object, **kwargs: object) -> None:
        # The newer attempt's records appear while this launch is stalled on
        # storage, before the step that prepares its own attempt records.
        if not stalled:
            stalled.append(True)
            before.update(_write_newer_attempt_records(directory))
        return original_consume(*args, **kwargs)

    monkeypatch.setattr(dispatch_module, "consume_context_refusal", stall_then_consume)

    refusal: CrewError | None = None
    try:
        _dispatch(repo, tmp_path / "manifests" / "abandoned.md")
    except CrewError as exc:
        refusal = exc

    assert stalled, "the fresh dispatch never reached its attempt preparation"
    assert refusal is not None, (
        f"the abandoned dispatch started a supervisor instead of refusing: {started}"
    )
    assert started == [], "the abandoned dispatch started a supervisor"

    for name in NEWER_RECORDS:
        path = directory / name
        assert path.is_file(), f"the abandoned dispatch unlinked the newer {name}"
        assert path.read_bytes() == before[name], (
            f"the abandoned dispatch overwrote the newer attempt's {name}"
        )

    receipt = json.loads(
        (directory / "attempt-1-exit.json").read_text(encoding="utf-8")
    )
    assert receipt["run_id"] == RUN_ID
    assert "abandon" in str(receipt.get("cause")).lower(), receipt


def test_a_fresh_dispatch_that_is_still_newest_prepares_and_launches(
    repo_context: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unchanged path: an unreserved-against attempt still launches."""
    _config_home, repo = repo_context
    directory = _seed_launched_run()
    started = _stub_supervisor(monkeypatch)

    record = _dispatch(repo, tmp_path / "manifests" / "fresh.md")

    assert started, "the fence blocked a dispatch whose attempt was still newest"
    assert record.get("pid") == os.getpid()
    marker = json.loads((directory / "attempt.json").read_text(encoding="utf-8"))
    assert marker["attempt"] == 1, marker
    assert not (directory / "attempt-1-exit.json").exists(), (
        "a launch that started its supervisor wrote an exit receipt"
    )
