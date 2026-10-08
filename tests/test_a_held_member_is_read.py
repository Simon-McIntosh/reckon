"""A held member and a silent launch are read from the machine, not the label.

Two measured failures sit in this file, and both are a label standing in for
the fact that mattered.

*Held.* A roster member's session is a single-writer resource. The member guard
released a member whose run had recorded a terminal phase without asking the
process table, so a run that reached its end while its worker still held the
session was launched onto, and the launch died against the backend's writer
store before its first event. Dispatch now withholds a prior same-task session
while its worker is unproven stopped and starts the new run on a fresh
conversation, recording the substitution rather than losing it.

*Silent.* A launch that dies before its first turn leaves a zero-length stream
and its reason in ``stderr.log``, which nothing read: ``observe`` classified the
run as an orphan whose detail named only the path, and, because launch-failed
is not a terminal phase, an observation overwrote a stored launch-failed phase
with the same orphan reading. ``observe`` now reads stderr when the stream is
empty, classifies the run as the launch failure it was, and leaves a stored
launch-failed phase alone.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import cli as cli_module
from reckon import crew
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

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

STDERR_REASON = (
    "Error: thread-store conflict: thread thread-held already has an active writer"
)


@pytest.fixture()
def dispatch_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """A throwaway config home and repository, as a dispatch needs."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

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
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


def _node(manifest_path: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="candidate-node",
        goal="record one dispatch session substitution",
        plan="fixture",
        section="guard",
        spec_level="guided",
        done_when="pytest reports one passing session case",
        write_paths=["src/candidate.py"],
        time_budget="20m",
        manifest_path=str(manifest_path),
    )


def _dead_pid() -> int:
    """A pid the kernel has already reaped, so a liveness probe reads gone."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _observation_pointer(
    run_id: str, *, phase: str, stderr_text: str
) -> dict[str, object]:
    """A CLI live pointer whose run directory holds the launch's disk state."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_bytes(b"")
    stderr = directory / "stderr.log"
    stderr.write_text(stderr_text, encoding="utf-8")
    pointer: dict[str, object] = {
        "run_id": run_id,
        "project": "proj",
        "launch": "cli",
        "backend": "claude",
        "command": sys.executable,
        "argv": [sys.executable, "-c", "pass"],
        "phase": phase,
        "pid": _dead_pid(),
        "launcher_host": socket.gethostname(),
        "log_path": str(stream),
        "stderr_path": str(stderr),
        "session_id": "thread-held",
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def test_a_dispatch_onto_a_held_session_starts_a_session_of_its_own(
    dispatch_context: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prior run recorded its end, its worker still holds the session.

    Dispatch must not continue that session: the backend's writer store would
    kill the launch before its first event. It starts a fresh conversation and
    the substitution is on the record.
    """
    config_home, repo = dispatch_context
    holder = {
        "run_id": "holder-run",
        "project": "proj",
        "node": {"id": "candidate-node", "plan": "fixture"},
        "phase": "complete",
        "pid": os.getpid(),
        "launcher_host": socket.gethostname(),
        "session_id": "thread-held",
        "session_harness": "codex",
        "member": "worker-a",
    }
    monkeypatch.setattr(dispatch_module, "list_live", lambda **_kwargs: [holder])
    launched: list[object] = []

    record = crew.dispatch(
        node=_node(config_home / "manifests" / "candidate.md"),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="coordinator-session",
        launcher=lambda plan, **_kwargs: (launched.append(plan), os.getpid())[1],
    )

    assert record["session_id"] is None
    assert launched and launched[0].resumed_session is None
    withheld = record["session_withheld"]
    assert withheld["session_id"] == "thread-held"
    assert "holder-run" in withheld["reason"]
    assert "proven stopped" in withheld["reason"]
    assert record["session_id_absent"]["point"] == "dispatch-same-task-session-withheld"
    assert record["session_id_absent"]["reason"] == withheld["reason"]


def test_a_prior_session_whose_worker_is_gone_is_still_continued(
    dispatch_context: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery direction: a dead worker's session is the one to continue.

    Withholding is for a session that may still have a writer. A prior run whose
    worker is proven gone leaves a conversation there to resume, and dropping it
    would cost the continuity this whole path exists for.
    """
    config_home, repo = dispatch_context
    holder = {
        "run_id": "holder-run",
        "project": "proj",
        "node": {"id": "candidate-node", "plan": "fixture"},
        "phase": "working",
        "pid": _dead_pid(),
        "launcher_host": socket.gethostname(),
        "session_id": "thread-dead",
        "session_harness": "codex",
        "member": "worker-a",
    }
    monkeypatch.setattr(dispatch_module, "list_live", lambda **_kwargs: [holder])
    launched: list[object] = []

    record = crew.dispatch(
        node=_node(config_home / "manifests" / "candidate.md"),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="coordinator-session",
        launcher=lambda plan, **_kwargs: (launched.append(plan), os.getpid())[1],
    )

    assert record["session_id"] == "thread-dead"
    assert record["session_withheld"] is None
    assert launched and launched[0].resumed_session == "thread-dead"


def test_a_zero_length_stream_is_read_through_its_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch wrote no turn and left its reason in the file nobody read."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    run_id = "r-read-the-silent-launch"
    _observation_pointer(run_id, phase="starting", stderr_text=STDERR_REASON)

    observed = crew.observe(run_id)

    assert observed["phase"] == "launch-failed"
    assert observed["phase"] != "orphaned"
    assert STDERR_REASON.split(": ", 1)[1] in observed["detail"]
    failure = observed["launch_failures"][-1]
    assert failure["kind"] == "launch-failed"
    assert failure["stderr_tail"] == STDERR_REASON


def test_the_silent_launch_is_classified_through_the_cli_observe_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason must reach a reader of the command, not only of the function."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    run_id = "r-cli-reads-the-silent-launch"
    _observation_pointer(run_id, phase="starting", stderr_text=STDERR_REASON)

    result = CliRunner().invoke(
        cli_module.main, ["crew", "observe", "--run", run_id, "--pretty"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["phase"] == "launch-failed"
    assert "thread-store conflict" in payload["detail"]
    assert payload["launch_failures"][-1]["stderr_tail"] == STDERR_REASON


def test_a_stored_launch_failure_is_not_reclassified_as_an_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later observation must not turn a recorded launch failure into an
    interruption: the phase is final for the pointer and the reason stays on it.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    run_id = "r-stored-launch-failure"
    pointer = _observation_pointer(
        run_id, phase="launch-failed", stderr_text=STDERR_REASON
    )
    pointer["launch_failures"] = [
        {
            "recorded_at": "2026-10-04T08:00:00Z",
            "kind": "launch-failed",
            "backend": "claude",
            "stderr_tail": STDERR_REASON,
        }
    ]
    runs._write_json(runs.pointer_path(run_id), pointer)

    observed = crew.observe(run_id)

    assert observed["phase"] == "launch-failed"
    assert observed["phase"] != "orphaned"
    assert observed["launch_failures"][-1]["stderr_tail"] == STDERR_REASON


def test_an_empty_stream_with_no_stderr_still_reads_as_an_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary orphan keeps its reading: nothing was written to quote.

    A launch failure is named only when there is a reason to attach, so the
    classification does not invent a cause for a process that left none.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    run_id = "r-quiet-orphan"
    _observation_pointer(run_id, phase="starting", stderr_text="\n")

    observed = crew.observe(run_id)

    assert observed["phase"] == "orphaned"
    assert observed.get("launch_failures") in (None, [])


def test_the_member_guard_and_the_session_guard_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One predicate decides both, so a held member is never a reused session.

    The guard's verdict is what the dispatch path withholds on, which is what
    keeps the two halves from drifting: a member the guard refuses to release is
    a session the dispatch refuses to continue.
    """
    from reckon.crew import node as node_module

    pointer = {
        "run_id": "holder-run",
        "phase": "complete",
        "pid": os.getpid(),
        "launcher_host": socket.gethostname(),
        "session_id": "thread-held",
    }
    verdict = node_module.member_in_flight_verdict(pointer)
    assert verdict.blocks is True

    monkeypatch.setattr(
        dispatch_sessions_module,
        "member_in_flight_verdict",
        lambda _pointer: verdict,
    )
    held = dispatch_module._prior_session_still_held(pointer, [pointer])
    assert held is not None and "proven stopped" in held

    monkeypatch.setattr(
        dispatch_sessions_module,
        "member_in_flight_verdict",
        lambda _pointer: node_module.MemberInFlightVerdict(
            blocks=False, liveness="gone", reason="its worker process is gone"
        ),
    )
    assert dispatch_module._prior_session_still_held(pointer, [pointer]) is None


def test_a_record_that_names_no_process_does_not_hold_its_member() -> None:
    """A launch that produced no process left nothing to collide with.

    A launcher that returns no worker writes the recorded pid as zero, which
    names no process on this host or any other, so the member is free. The
    phase is not what frees it: the same pointer with no recorded pid at all
    still blocks as unknown, because a pointer is written before its worker is
    spawned and the worker may yet arrive.
    """
    from reckon.crew import node as node_module

    holder = {
        "run_id": "r-stub-launch",
        "phase": "complete",
        "pid": 0,
        "launcher_host": socket.gethostname(),
    }
    verdict = node_module.member_in_flight_verdict(holder)
    assert verdict.blocks is False
    assert verdict.liveness == "gone"

    unborn = {**holder, "pid": None}
    unproven = node_module.member_in_flight_verdict(unborn)
    assert unproven.blocks is True
    assert unproven.liveness == "unknown"
