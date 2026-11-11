"""A follower re-armed into a pipe shows the session's live work.

A session with its host down runs its follower as a Monitor, and a Monitor is a
re-pipe, so the follower is re-armed every half hour. The history burst is
withheld from a pipe on purpose — a reader with no scrollback would take
restored rows for fresh transitions — and a replayed row whose state the pane
already draws is dropped as inventory. Between them a re-arm into a pipe could
convey neither history nor current state, so a reader could not tell "nothing
is running" from "something is running quietly".

The resume branch now draws the runs the pane has not been shown: the
delivered record names the state word each run was last drawn at, and a run
standing at a word the record does not hold — one dispatched while the pane was
away, or one that moved — is drawn once from its current state. A run the
record already holds at that word is not repeated.

Driven directly against a temporary config home: no producer, no subprocess, so
the output can only come from the derivation under test. The delivered record
is written with literal strings exactly as the follower's own writer receives
them, never computed by the functions under test, so a case cannot pass by
agreeing with itself.
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

PROJECT = "snapshot-proj"
SESSION = "s-snapshot"
OTHER_SESSION = "s-other"
RUN_WORKING = "r-snapshot-working"
RUN_QUEUED = "r-snapshot-queued"
RUN_STALLED = "r-snapshot-stalled"
RUN_AWAY = "r-snapshot-away"


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
) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    runs._write_json(
        runs.pointer_path(run_id),
        {
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
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": alive,
        },
    )


# The words the follower's own writer receives, as literal strings: 'running',
# 'queued' and 'abandoned' are the states three of the runs below stand in, and
# each is written here rather than derived, so the cases judge the resume
# branch against production's vocabulary instead of against itself. 'queued'
# and 'abandoned' are the pairs that matter: their counter buckets are
# 'waiting' and 'halted', so a comparison that buckets the delivered word
# before reading it cannot match these rows.
DRAWN_WORDS = {
    RUN_WORKING: "working",
    RUN_QUEUED: "queued",
    RUN_STALLED: "halted",
}


@pytest.fixture()
def fleet(repository: Path) -> Path:
    """Four runs the session owns: three the pane has drawn, one it has not.

    ``RUN_AWAY`` was dispatched while nothing was attached, so the record does
    not name it and a re-arm must draw it.
    """
    home = repository.parent / "config"
    _write_pointer(home, RUN_WORKING, phase="working", alive=True)
    _write_pointer(home, RUN_QUEUED, phase="queued")
    _write_pointer(home, RUN_STALLED, phase="stalled", alive=False)
    _write_pointer(home, RUN_AWAY, phase="working", alive=True)
    runs.write_delivered(PROJECT, SESSION, DRAWN_WORDS)
    return home


def _snapshot(
    monkeypatch: pytest.MonkeyPatch,
    *,
    terminal: bool = False,
    session: str = SESSION,
) -> str:
    """What the resume branch writes into a pipe (or a terminal)."""
    stream = _Stream(terminal=terminal)
    monkeypatch.setattr(sys, "stdout", stream)
    return follow_module._follow_resume_output(
        session and PROJECT,
        session,
        read_history=lambda: follow_checkpoint.read_history(PROJECT, session),
        history_caps=(40, 3600.0),
        dim=str,
    )


def _pane_run_ids(output: str) -> set[str]:
    return {
        token
        for line in output.splitlines()
        for token in line.split()
        if token.startswith("r-snapshot-")
    }


def test_a_pipe_rearm_draws_the_runs_the_pane_has_not_drawn(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _snapshot(monkeypatch)

    assert RUN_AWAY in output, output
    assert f"{RUN_AWAY}  working" in output
    # The three the record names at the word they still stand in are not
    # repeated: a re-arm is a reading of what the pane lacks, not a redraw.
    for run_id in (RUN_WORKING, RUN_QUEUED, RUN_STALLED):
        assert run_id not in output, output
    for marker in ("→", "->"):
        assert marker not in output


def test_a_run_the_pane_drew_at_its_word_draws_nothing(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs.write_delivered(
        PROJECT, SESSION, {**DRAWN_WORDS, RUN_AWAY: "working"}
    )

    assert _snapshot(monkeypatch) == ""


def test_a_run_that_moved_is_drawn_again(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record still says the queued run stood at 'working', so the run moved
    # since the pane last drew it and its current word is drawn once.
    runs.write_delivered(
        PROJECT,
        SESSION,
        {RUN_WORKING: "working", RUN_QUEUED: "working", RUN_STALLED: "halted"},
    )

    output = _snapshot(monkeypatch)

    assert f"{RUN_QUEUED}  queued" in output, output
    assert RUN_WORKING not in output
    assert RUN_STALLED not in output


def test_the_drawn_row_carries_the_word_the_record_is_written_in(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record's vocabulary is the row's, or no comparison could hold.

    The record above is written by hand from production's words; this case
    reads the rows the re-arm judges and requires the same three words back, so
    a later change to the row derivation that silently switches vocabulary
    (say to the fleet's classification) fails here rather than in the field.
    """
    rows = follow_module._follow_live_state_rows(PROJECT, SESSION)

    assert {row["run_id"]: row["to_state"] for row in rows} == {
        **DRAWN_WORDS,
        RUN_AWAY: "working",
    }


def test_a_session_owning_nothing_draws_nothing(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pointer(
        repository.parent / "config", RUN_WORKING, phase="working", alive=True
    )

    assert _snapshot(monkeypatch, session=OTHER_SESSION) == ""


def test_a_terminal_still_receives_the_history_burst(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    follow_checkpoint.append_history(
        PROJECT, SESSION, text="r-old  working → running", at=time.time()
    )

    output = _snapshot(monkeypatch, terminal=True)

    assert "r-old  working → running" in output
    assert RUN_AWAY not in output