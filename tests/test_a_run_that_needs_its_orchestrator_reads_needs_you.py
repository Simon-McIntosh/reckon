"""The pane draws ``needs you`` where it drew ``halted``.

Every run in the halted group is the orchestrator's move next: blocked,
needs-help, held, stalled, stopped, failed, abandoned, a launch refused at
admission, and the rest of the group's states. ``halted`` read as a state the
run sits in by itself, so the pane draws ``needs you`` and labels that counter
``n``, which no other counter uses.

A checkpoint, a stored history and a delivered-state record hold the word the
pane drew when each was written, so a record written before the rename says
``halted`` for the same group. One mapping reads such a word as the word the
pane draws now, and both the grid's remembered-word path and the pipe re-arm's
delivered-state path read it there. A run whose only change since the record is
therefore the word draws no row on the first re-arm after the upgrade.

The delivered record below is written with the literal old word, never computed
by the functions under test, so the pre-rename case cannot pass by agreeing
with itself.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import ticker as ticker_module
from tests.helpers.git import git as _git

follow_module = importlib.import_module("reckon.crew_follow_commands")

PROJECT = "needs-you-proj"
SESSION = "s-needs-you"
RUN_ID = "r-needs-you"
NODE = "needs-you-node"

# The word the pane drew before the rename, written here as a literal because a
# record already on disk holds it and nothing in the tree produces it any more.
OLD_WORD = "halted"


def _event(**overrides) -> dict:
    event = {
        "project": PROJECT,
        "event": "transition",
        "observed_at": "2026-11-13T10:00:00Z",
        "run_id": RUN_ID,
        "node": NODE,
        "from_state": "working",
        "to_state": "blocked",
        "working": 0,
        "blocked": 1,
        "unpromoted": 0,
    }
    event.update(overrides)
    return event


def _line(event: dict) -> str:
    return recovery.format_watch_transition(
        event, ticker=ticker_module.Ticker(width=208, color=False)
    )


def test_a_blocked_transition_renders_needs_you() -> None:
    line = _line(_event())

    assert "new → needs you" in line, line
    assert OLD_WORD not in line, line


def test_the_counter_block_labels_the_bucket_n() -> None:
    line = _line(_event())

    # The bucket carries the blocked count and its letter. The letter is ``n``,
    # which no other counter uses, so a reader cannot confuse the column with
    # the working or delivered one.
    assert " 0w  1n  0d  0q" in line, line
    assert "1h" not in line, line
    assert ticker_module.STAT_LETTER["needs you"] == "n"


def test_a_pane_memory_seeded_with_the_old_word_reads_the_word_it_draws() -> None:
    # The grid maps a remembered word on the way in, so a memory seeded from a
    # record written before the rename holds the word the pane draws now.
    assert ticker_module._remembered_word(OLD_WORD) == "needs you"

    policy = ticker_module.RowPolicy()
    policy.seed(RUN_ID, OLD_WORD)
    assert policy._shown[RUN_ID] == "needs you"


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
        ("commit", "-q", "-m", "test: seed the re-arm fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_pointer(home: Path, *, phase: str) -> None:
    log = home / "logs" / f"{RUN_ID}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "node": {
                "id": RUN_ID,
                "plan": "needs-you-plan",
                "section": "needs-you-section",
                "time_budget": "20m",
            },
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{RUN_ID}.md"),
            "log_path": str(log),
            "process_alive": False,
        },
    )


class _Pipe:
    """A stdout stand-in that answers as a pipe, which is where a re-arm draws."""

    written = io.StringIO()

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        return self.written.write(text)

    def flush(self) -> None:
        return None


def test_a_pipe_rearm_over_the_old_word_draws_no_row(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = repository.parent / "config"
    _write_pointer(home, phase="blocked")
    runs.write_delivered(PROJECT, SESSION, {RUN_ID: OLD_WORD})
    monkeypatch.setattr(sys, "stdout", _Pipe())

    output = follow_module._follow_resume_output(
        PROJECT,
        SESSION,
        read_history=list,
        history_caps=(40, 3600.0),
        dim=str,
    )

    # The run now stands blocked, which is the group the old word named: the
    # only change since the record is the word, so the re-arm draws nothing.
    assert output == "", output
