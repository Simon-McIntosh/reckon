"""A follower re-armed into a pipe shows the session's live work.

A session with its host down runs its follower as a Monitor, and a Monitor is a
pipe, so the follower is re-armed every half hour. The history burst is withheld
from a pipe on purpose — a reader with no scrollback would take restored rows for
fresh transitions — and the baseline row of a run the pane has already announced
is dropped as well. Between them a re-arm into a pipe conveyed neither history
nor current state, so a reader could not tell "nothing is running" from
"something is running quietly".

The resume branch now hands a pipe one framed snapshot instead: a frame line, a
row per run the session owns or owes an action on, each carrying its state and
no transition arrow, and a closing frame. The rows are checked here against the
live and obligations views over the same store, so the pane and those views
cannot disagree about which runs are the session's, and a session that owns
nothing prints the frame with "none" rather than an empty pane.

Driven directly against a temporary config home: no producer, no subprocess, so
the output can only come from the derivation under test. The negative control
returns early from the pipe branch, which makes the first case print nothing.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import follow_checkpoint, runs
from tests.helpers.git import git as _git

follow_module = importlib.import_module("reckon.crew_follow_commands")
views_module = importlib.import_module("reckon.mcp_views")

PROJECT = "snapshot-proj"
SESSION = "s-snapshot"
OTHER_SESSION = "s-other"
RUN_WORKING = "r-snapshot-working"
RUN_QUEUED = "r-snapshot-queued"
RUN_REVIEW = "r-snapshot-review"
RUN_PROMOTED = "r-snapshot-promoted"


class _Stream:
    """A stdout stand-in whose terminal answer the case decides."""

    def __init__(self, *, terminal: bool) -> None:
        self._terminal = terminal
        self.written = io.StringIO()

    def isatty(self) -> bool:
        return self._terminal

    def write(self, text: str) -> int:
        return self.written.write(text)

    def flush(self) -> None:
        return None


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config home and a repository the project's views can read."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the snapshot fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_pointer(
    home: Path,
    run_id: str,
    *,
    phase: str = "",
    session: str = SESSION,
    alive: bool | None = None,
    manifest: Path | None = None,
    **extra: object,
) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "node": {
            "id": run_id,
            "plan": "snapshot-plan",
            "section": "snapshot-section",
            "time_budget": "20m",
        },
        "phase": phase,
        "created_at": runs._utc_now(),
        "manifest_path": str(manifest or home / "manifests" / f"{run_id}.md"),
        "log_path": str(log),
        "process_alive": alive,
    }
    pointer.update(extra)
    runs._write_json(runs.pointer_path(run_id), pointer)


def _deliver(home: Path, run_id: str, commits: list[str]) -> Path:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: snapshot-node\n"
        "status: complete\n"
        f"commits: {json.dumps(commits)}\n"
        "blockers: none\n",
        encoding="utf-8",
    )
    return manifest


@pytest.fixture()
def fleet(repository: Path) -> Path:
    """Four runs the session owns: working, queued, a delivery, and a promoted run.

    The delivery is a run whose worker has ended with work committed and no
    review recorded, so its promotion is a duty the session owes rather than a
    live row alone. The promoted run's pointer is gone, as a promotion leaves
    it, so it is in neither view and must not appear on the pane.
    """
    home = repository.parent / "config"
    head = _git(repository, "rev-parse", "HEAD")
    _write_pointer(home, RUN_WORKING, phase="working", alive=True)
    _write_pointer(home, RUN_QUEUED, phase="queued")
    manifest = _deliver(home, RUN_REVIEW, [head])
    _write_pointer(
        home,
        RUN_REVIEW,
        manifest=manifest,
        repo=str(repository),
        worktree=str(repository),
        base_sha=head,
        role="implement",
    )
    _write_pointer(home, RUN_PROMOTED, phase="complete")
    runs.pointer_path(RUN_PROMOTED).unlink()
    return home


def _snapshot(monkeypatch: pytest.MonkeyPatch, *, terminal: bool = False) -> str:
    """What the resume branch writes into a pipe (or a terminal)."""
    stream = _Stream(terminal=terminal)
    monkeypatch.setattr(sys, "stdout", stream)
    return follow_module._follow_resume_output(
        PROJECT,
        SESSION,
        read_history=lambda: follow_checkpoint.read_history(PROJECT, SESSION),
        history_caps=(40, 3600.0),
        dim=str,
    )


def _owned_run_ids() -> tuple[set[str], set[str]]:
    """The session's live runs and its run-scoped duties, from the two views."""
    live = views_module.serve_crew_view(
        "live", project=PROJECT, session=SESSION, mine=True
    )
    duties = views_module.serve_crew_view(
        "obligations", project=PROJECT, session=SESSION
    )
    live_ids = {str(row.get("run_id") or "") for row in live.get("runs") or ()}
    duty_ids = {
        str(row.get("run_id") or "")
        for row in duties.get("obligations") or ()
        if str(row.get("run_id") or "")
    }
    return live_ids - {""}, duty_ids - {""}


def _pane_run_ids(output: str) -> set[str]:
    return {
        token
        for line in output.splitlines()
        for token in line.split()
        if token.startswith("r-snapshot-")
    }


def test_a_pipe_rearm_shows_the_sessions_live_work(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _snapshot(monkeypatch)

    frame = "── live · 3 runs (state, not transitions) ──"
    assert frame in output

    lines = output.splitlines()
    assert lines[0] == frame
    assert lines[-1] == "── end live ──"

    # Each owned run is named with its current state, and no row carries a
    # transition arrow: this is a reading of state, not a run of transitions.
    assert f"{RUN_WORKING}  running" in output
    assert f"{RUN_QUEUED}  queued" in output
    assert RUN_REVIEW in output
    for marker in ("→", "->"):
        assert marker not in output

    # A promoted run has left the live set and owes nothing, so it is absent.
    assert RUN_PROMOTED not in output


def test_the_snapshot_agrees_with_the_live_and_obligation_views(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _snapshot(monkeypatch)
    live_ids, duty_ids = _owned_run_ids()

    assert live_ids
    assert _pane_run_ids(output) == live_ids | duty_ids

    # The delivery is in the union through its duty, so the snapshot shows a
    # run the live pointer alone would not have named as owed.
    assert RUN_REVIEW in duty_ids
    assert RUN_REVIEW in _pane_run_ids(output)


def test_a_session_owning_nothing_prints_the_frame_with_none(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pointer(
        repository.parent / "config", RUN_WORKING, phase="working", alive=True
    )

    stream = _Stream(terminal=False)
    monkeypatch.setattr(sys, "stdout", stream)
    output = follow_module._follow_resume_output(
        PROJECT,
        OTHER_SESSION,
        read_history=list,
        history_caps=(40, 3600.0),
        dim=str,
    )

    assert "── live · 0 runs (state, not transitions) ──" in output
    assert "none" in output
    assert "── end live ──" in output
    assert RUN_PROMOTED not in output
    assert RUN_WORKING not in output


def test_a_terminal_still_receives_the_history_burst(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    follow_checkpoint.append_history(
        PROJECT, SESSION, text="r-old  working → running", at=time.time()
    )

    output = _snapshot(monkeypatch, terminal=True)

    assert "r-old  working → running" in output
    # The live frame belongs to the pipe only; a terminal gets the replay.
    assert "── live ·" not in output
