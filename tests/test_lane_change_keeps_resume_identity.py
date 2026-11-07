"""An ordinary resume finds the session a lane change started.

A run that moves to a lane on a different harness starts a fresh conversation
there, because a session id belongs to the harness that minted it. The failure
this records is not the fresh start — that is unavoidable — but that nothing
ever wrote the new session id down, so the next plain ``crew resume --advice``
consulted the live pointer, the run's stream and the ledger, found none, and
refused. The only continuation left was another lane change to a different
backend name, and there are finitely many of those.

So the case here performs a real lane change onto a codex-family lane, lets the
launcher write the stream that lane really emits, and then asks for an ordinary
resume. It asserts the resume composes the current lane's command carrying the
new session id, and that the pointer now names the same harness and session.

The declared negative control restores the old first-harness session lookup,
which answers the source harness's id across a fresh move: the ordinary resume
then either refuses (no id on the current harness) or composes a resume of a
session the destination harness does not own. The red log's first line prints
that mutation verbatim and the run ends ``EXIT=1``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import crew
from reckon.crew import recovery

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

def test_an_ordinary_resume_finds_the_new_lanes_session(
    run: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _directory = run
    monkeypatch.setattr(dispatch_sessions_module, "process_alive", lambda pid: False)

    moved = _lane_change_to_codex(config, monkeypatch)
    assert moved["lane_change"]["session"] == "fresh"
    assert moved["backend"] == "codex"
    after = crew.read_pointer(RUN)
    assert after["session_id"] is None

    plan = dispatch_sessions_module.resume_plan(
        RUN, "continue after the move", config=config
    )

    argv = list(plan.argv)
    assert Path(argv[0]).name == "codex"
    assert "resume" in argv, argv
    assert argv[argv.index("resume") + 1] == CODEX_THREAD, argv
    assert plan.dialect == "codex"
    assert CLIVE_SESSION not in argv


def test_the_first_observed_turn_records_the_session_on_the_run(
    run: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane change itself cannot know the new session id — the destination
    harness announces it on its first turn. The observation that folds that turn
    in is therefore the write point, and it must record the id beside the
    harness that owns it, replacing the source harness the run carried in."""
    config, _directory = run
    monkeypatch.setattr(dispatch_sessions_module, "process_alive", lambda pid: False)

    _lane_change_to_codex(config, monkeypatch)
    observed = dispatch_sessions_module.observe(RUN, config=config)

    assert observed["session_id"] == CODEX_THREAD
    assert observed["session_harness"] == "codex"
    assert observed["session_capture"]["captured"] is True
    recorded = crew.read_pointer(RUN)
    assert recorded["session_id"] == CODEX_THREAD
    assert recorded["session_harness"] == "codex"
    assert recorded["session_id"] != CLIVE_SESSION

