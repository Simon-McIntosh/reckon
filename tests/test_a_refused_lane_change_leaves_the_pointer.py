"""A lane change that starts no worker must not move the pointer.

On 20 November a stopped run was resumed with a lane change to a lane whose
step was accepted and never reached a worker. The command reported a refusal
and started nothing, yet the pointer was rewritten to the destination backend at
the next attempt. Nothing could then clear it: the run read as executing on a
backend it had never run on, its dialect no longer matched the stream its
previous harness had written, and every verb that reads the pointer was refused.

So the four fields naming what the run is executing — backend, attempt, agent
and dialect — move only when the launch leaves a worker behind. An accepted
launch is not a started worker: the launch path records a launch failure on the
run when the process it started ended without reaching a model, and returns the
pid regardless. That receipt is the fact this reads.

The cases below split on that boundary:

* an accepted launch whose worker never starts leaves the four fields alone and
  returns a reason naming why no worker started, never a bare failure;
* a lane change refused before any launch leaves them alone too;
* a lane change whose worker starts moves them, and is the only case in which a
  lane-change record is written on the pointer.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import crew
from reckon.crew import dispatch_launch, recovery

PROJECT = "sample"
RUN = "r-lane-keeps-resume"
CLIVE_SESSION = "clive-session-from-the-first-lane"
CODEX_THREAD = "019ff509-8a60-7723-94fd-65942a6d8faa"

# The declared mutation, printed verbatim as the red log's first line.
NEGATIVE_CONTROL_MUTATION = (
    "Restoring the old first-harness session lookup makes ordinary resume refuse."
)


def _stream(*events: dict) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _clive_stream() -> str:
    return _stream(
        {"type": "system", "subtype": "init", "session_id": CLIVE_SESSION},
        {
            "type": "assistant",
            "session_id": CLIVE_SESSION,
            "message": {"content": "ok"},
        },
        {
            "type": "result",
            "session_id": CLIVE_SESSION,
            "subtype": "success",
            "is_error": False,
            "result": "done",
        },
    )


def _codex_stream() -> str:
    # The shape a codex exec stream arrives in: the thread is announced once,
    # before any turn, and the harness offers no other field carrying it.
    return _stream(
        {"type": "thread.started", "thread_id": CODEX_THREAD},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": "ready"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 5,
            },
        },
    )


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Path]:
    """A clive run as a lane change finds it, plus the two lane settings."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("clive", "codex"):
        binary = bindir / name
        binary.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
        binary.chmod(0o755)

    config = {
        "default_backend": "clive",
        "backends": {
            "clive": {
                "launch": "cli",
                "command": str(bindir / "clive"),
                "sandbox": "worktree-full",
                "time_budget": "25m",
                "session_reuse": True,
            },
            "codex": {
                "launch": "cli",
                "command": str(bindir / "codex"),
                "sandbox": "worktree-full",
                "time_budget": "25m",
                "session_reuse": True,
            },
        },
        "roles": {"implement": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "exhausted_statuses": [],
        },
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }

    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "tree"
    worktree.mkdir()
    directory = crew.run_dir(RUN)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stream.jsonl").write_text(_clive_stream(), encoding="utf-8")
    (directory / "prompt.txt").write_text("carry the node home\n", encoding="utf-8")
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps({"run_id": RUN, "exit_code": 0}), encoding="utf-8"
    )
    manifest = tmp_path / "manifest.md"
    manifest.write_text("node: lane\nstatus: waiting\n", encoding="utf-8")

    pointer = {
        "run_id": RUN,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(worktree),
        "launch": "cli",
        "backend": "clive",
        "dialect": "clive",
        "role": "implement",
        "pid": None,
        "pid_start_time": None,
        "session_id": CLIVE_SESSION,
        "session_harness": "clive",
        "attempt": 1,
        "base_sha": "HEAD",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "prompt_path": str(directory / "prompt.txt"),
        "phase": "blocked",
        "agent": {"dialect": "clive", "model": "clive-model"},
        "node": {
            "id": "lane-node",
            "goal": "keep one run identity across a lane change",
            "plan": "fixture",
            "section": "movement",
            "spec_level": "guided",
            "done_when": "the ordinary resume names the new lane's session",
            "role": "implement",
            "time_budget": "25m",
            "write_paths": ["package/target.py"],
        },
    }
    crew._write_json(crew.pointer_path(RUN), pointer)
    yield config, directory
    landed = [
        path
        for path in (
            Path.home() / ".config" / "reckon" / "crew" / "runs" / RUN,
            Path.home() / ".config" / "reckon" / "crew" / "live" / f"{RUN}.json",
        )
        if path.exists()
    ]
    assert not landed, f"the real crew home carries this file's run paths: {landed}"


def _resolution(config: dict, destination: str) -> SimpleNamespace:
    return SimpleNamespace(
        backend=destination,
        launch="cli",
        backend_settings=dict(config["backends"][destination]),
        lane_gate={
            "state": "open",
            "gate_path": "fixture-gate.json",
            "paused": False,
            "reason": None,
            "detail": "",
            "path_check": "skipped",
            "path_check_handled": False,
            "path_check_detail": "fixture",
        },
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        authority="fixture-authority",
        sandbox_write_roots=None,
    )


def _lane_change_to_codex(
    config: dict, monkeypatch: pytest.MonkeyPatch, *, stream: str | None = None
) -> dict:
    """Move the run onto the codex lane and let the launcher write its stream."""
    body = stream if stream is not None else _codex_stream()

    def launcher(plan, *, log_path, stderr_path=None, prompt_path=None):
        Path(log_path).write_text(body, encoding="utf-8")
        return 51001

    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", False)
    monkeypatch.setattr(
        dispatch_sessions_module,
        "plan_dispatch",
        lambda **kwargs: _resolution(config, "codex"),
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        dispatch_sessions_module,
        "resolve_dispatch_ledger_root",
        lambda authority: authority,
    )
    return dispatch_sessions_module.change_lane(
        RUN,
        "codex",
        "the clive lane stopped serving",
        config=config,
        advice="continue after the move",
        launch=True,
        launcher=launcher,
    )

def _lane_change_that_starts_no_worker(
    config: dict, monkeypatch: pytest.MonkeyPatch
) -> dict:
    """Move onto codex through a launcher that leaves no worker behind.

    The launch path records a launch failure when the process it started ended
    with an empty stream, and returns the pid regardless, so this is exactly
    the accepted-launch-with-no-worker shape observed on 20 November.
    """

    def launcher(plan, *, log_path, stderr_path=None, prompt_path=None):
        dispatch_launch._record_launch_failure(
            {
                "run_id": RUN,
                "stream_path": str(log_path),
                "stderr_path": str(stderr_path),
            },
            exit_status=1,
        )
        return 51002

    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", False)
    monkeypatch.setattr(
        dispatch_sessions_module,
        "plan_dispatch",
        lambda **kwargs: _resolution(config, "codex"),
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        dispatch_sessions_module,
        "resolve_dispatch_ledger_root",
        lambda authority: authority,
    )
    return dispatch_sessions_module.change_lane(
        RUN,
        "codex",
        "the clive lane stopped serving",
        config=config,
        advice="continue after the move",
        launch=True,
        launcher=launcher,
    )


def test_an_accepted_launch_that_starts_no_worker_leaves_the_pointer(
    run: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 20 November shape: the spawn is accepted and no worker starts."""
    config, _directory = run
    monkeypatch.setattr(dispatch_sessions_module, "process_alive", lambda pid: False)
    before = crew.read_pointer(RUN)

    result = _lane_change_that_starts_no_worker(config, monkeypatch)

    after = crew.read_pointer(RUN)
    # Each of the four fields is exactly the source attempt's.
    assert after["backend"] == "clive"
    assert after["attempt"] == before["attempt"]
    assert after["agent"] == before["agent"]
    assert after["dialect"] == before["dialect"]
    # No lane-change record was written on the pointer.
    assert not after.get("lane_changes")
    assert "lane_change" not in after
    # The refusal is the command's result, naming why no worker started.
    assert result["ok"] is False
    assert "no worker" in result["reason"]
    assert result["backend"] == "clive"
    assert result["attempt"] == before["attempt"]


def test_a_second_lane_change_that_starts_no_worker_leaves_the_pointer(
    run: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run already in the launch-failed phase gets no second receipt.

    The launch path leaves the pointer untouched when it finds the run already
    marked launch-failed, so the failure of a second attempt is recorded
    nowhere. The move must still be refused, or the pointer ends on a backend
    the run never executed on exactly as it did on 20 November.
    """
    config, _directory = run
    monkeypatch.setattr(dispatch_sessions_module, "process_alive", lambda pid: False)
    record = crew.read_pointer(RUN)
    record["phase"] = "launch-failed"
    record["launch_failures"] = [
        {
            "kind": "launch-failed",
            "detail": "the worker exited before its first event",
        }
    ]
    crew._write_json(crew.pointer_path(RUN), record)
    before = crew.read_pointer(RUN)

    result = _lane_change_that_starts_no_worker(config, monkeypatch)

    after = crew.read_pointer(RUN)
    assert after["backend"] == "clive"
    assert after["attempt"] == before["attempt"]
    assert after["agent"] == before["agent"]
    assert after["dialect"] == before["dialect"]
    assert not after.get("lane_changes")
    # The pointer was already in this phase, and nothing added a second
    # receipt; the refusal therefore rests on the spawn leaving no worker.
    assert len(after["launch_failures"]) == len(before["launch_failures"])
    assert result["ok"] is False
    assert "no worker" in result["reason"]
    assert result["backend"] == "clive"


def test_a_lane_change_whose_worker_starts_moves_the_pointer(
    run: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real launch starts the worker, so the move is committed."""
    config, _directory = run
    monkeypatch.setattr(dispatch_sessions_module, "process_alive", lambda pid: False)

    _lane_change_to_codex(config, monkeypatch)

    after = crew.read_pointer(RUN)
    assert after["backend"] == "codex"
    assert after["attempt"] == 2
    assert after["dialect"] == "codex"
    assert after["lane_changes"][-1]["from_backend"] == "clive"
    assert after["lane_changes"][-1]["to_backend"] == "codex"


def test_a_lane_change_refused_before_launch_leaves_the_pointer(
    run: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane change refused before any launch leaves the four fields alone."""
    config, _directory = run
    monkeypatch.setattr(dispatch_sessions_module, "process_alive", lambda pid: False)
    before = crew.read_pointer(RUN)

    def refuse_resolution(config: dict, destination: str) -> SimpleNamespace:
        verdict = _resolution(config, destination)
        verdict.validation = SimpleNamespace(
            ok=False,
            findings=[{"property": "lane", "detail": "not open on the destination"}],
        )
        return verdict

    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", False)
    monkeypatch.setattr(
        dispatch_sessions_module,
        "plan_dispatch",
        lambda **kwargs: refuse_resolution(config, "codex"),
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        dispatch_sessions_module,
        "resolve_dispatch_ledger_root",
        lambda authority: authority,
    )

    with pytest.raises(crew.CrewError):
        dispatch_sessions_module.change_lane(
            RUN,
            "codex",
            "the clive lane stopped serving",
            config=config,
            advice="continue after the move",
            launch=True,
            launcher=lambda *args, **kwargs: 51003,
        )

    after = crew.read_pointer(RUN)
    assert after["backend"] == before["backend"] == "clive"
    assert after["attempt"] == before["attempt"]
    assert after["agent"] == before["agent"]
    assert after["dialect"] == before["dialect"]