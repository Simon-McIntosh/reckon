"""A worker whose first turn ends without acting is named as a premature end.

A print-mode worker gets no turn after the one it is in, so a worker that opens
its first turn, says one thing and calls no tool has stopped: nothing was
attempted, nothing reported a fault, and the session survives to continue. Its
process is gone and it delivered no manifest, so the reading the fleet had was
whatever a dead run gets — the halted word a coordinator reacts to by
diagnosing a failure that never happened. Measured on a review run that ended
after ``I'll start by orienting…`` and was resumed by hand.

Three cases pin the new reading and the reading it must not swallow.

* A stream holding one assistant message, no tool call and no error reads as a
  premature end, carries the resume remedy, and quotes the text it ended on —
  on the classified row and on the obligations duty the same classifier
  produces.
* The same stream whose message is an API error still reads as that error: the
  text is the cause, and continuing it would send the same request again.
* A one-turn stream whose message called a tool is not a premature end at all:
  the worker acted, and that is a different run.

The declared mutation removes the distinction — ``_first_turn_without_action``
answers None — and the first case then reads as halted, which is the defect this
node measured and which the first case fails on.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_classification, runs
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "premature-end-fixture"
SESSION = "coordinator-fixture"
HOST = socket.gethostname()

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "The negative control removes the distinction, and the first case then "
    "reads as halted."
)

NEGATIVE_CONTROL = os.environ.get("RECKON_PREMATURE_END_NEGATIVE_CONTROL", "").strip()

# The words the premature end composes.
PREMATURE_CLAUSE = "a premature end"
RESUME_REMEDY_WORDS = ("--advice", "continue")

STARTED_AT = datetime.now(tz=UTC) - timedelta(hours=1)

OK_RESULT = '{"type":"result","subtype":"success"}'


def _assistant(text: str, *blocks: dict) -> str:
    content = [{"type": "text", "text": text}, *blocks]
    return json.dumps({"type": "assistant", "message": {"content": content}})


def _tool_call(name: str = "Bash") -> dict:
    return {"type": "tool_use", "name": name, "input": {"command": "pwd"}}


def _tool_result() -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tool-1"}],
            },
        }
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every directory the reads resolve through is this test's own."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))


@pytest.fixture(autouse=True)
def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One mounted project, so the obligations view can name a duty.

    The mount resolves to a real repository because the obligations pass reads
    a project's worktree registry, and a synthetic directory answers that read
    with an error rather than an empty registry.
    """
    config_home = Path(os.environ["RECKON_HOME"])
    config_home.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the mounted project"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: STARTED_AT)


@pytest.fixture(autouse=True)
def _removes_the_distinction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared mutation, applied only where the control asks for it."""
    if NEGATIVE_CONTROL:
        monkeypatch.setattr(
            recovery_classification, "_first_turn_without_action", lambda record: None
        )


def _dispatch(tmp_path: Path, run_id: str, records: list[str]) -> dict:
    """One stub run whose process is gone and whose stream is on disk."""
    stream = tmp_path / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text("\n".join(records) + "\n", encoding="utf-8")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "node": {"id": run_id, "plan": "fixture-plan", "time_budget": "20m"},
        "phase": "working",
        "created_at": STARTED_AT.isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "process_alive": False,
        "pid": None,
        # The capture the launcher folds into the pointer before the worker
        # acts, so the session a resume would continue is the one on record.
        "session_id": f"session-{run_id}",
        "launcher_host": HOST,
        "worktree": str(tmp_path / "tree"),
        "base_sha": "",
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def test_a_single_turn_that_never_acted_reads_as_a_premature_end(
    tmp_path: Path,
) -> None:
    text = "I'll start by orienting myself in the tree."
    run_id = "r-premature-end"
    pointer = _dispatch(
        tmp_path,
        run_id,
        [
            '{"type":"turn.started"}',
            _assistant(text),
            OK_RESULT,
        ],
    )

    row = recovery.classify_pointer(pointer)

    assert row["recovery_classification"] == (
        recovery_classification.ENDED_WITHOUT_ACTING
    )
    assert row["recovery"] == "resume"
    assert row["classification"] == recovery.INTERRUPTED_RUN_PHASE
    assert PREMATURE_CLAUSE in row["detail"]
    # The text it ended on is the account of why it stopped, so the row quotes
    # it rather than describing it.
    assert text in row["detail"]
    assert row["next_action"] == (
        f"reckon crew resume --run {run_id} --advice continue"
    )

    duties = obligations_module.obligations(PROJECT, SESSION)["obligations"]
    mine = [item for item in duties if item.get("run_id") == run_id]
    assert mine, f"no duty owed for {run_id}: {duties}"
    assert "crew resume" in mine[0]["next_command"]
    for word in RESUME_REMEDY_WORDS:
        assert word in mine[0]["next_command"]


def test_a_single_turn_that_ended_on_an_api_error_still_reads_as_that_error(
    tmp_path: Path,
) -> None:
    error = "API Error: Connection refused"
    pointer = _dispatch(
        tmp_path,
        "r-premature-end-error",
        [
            '{"type":"turn.started"}',
            _assistant(error),
            '{"type":"result","subtype":"success","is_error":true,"result":'
            f'"{error}"}}',
        ],
    )

    row = recovery.classify_pointer(pointer)

    assert row["classification"] == "failed"
    assert row["recovery_classification"] != (
        recovery_classification.ENDED_WITHOUT_ACTING
    )
    assert error in row["detail"]


def test_a_one_turn_that_called_a_tool_is_not_a_premature_end(
    tmp_path: Path,
) -> None:
    pointer = _dispatch(
        tmp_path,
        "r-acted",
        [
            '{"type":"turn.started"}',
            _assistant("I'll start by orienting myself.", _tool_call()),
            _tool_result(),
            OK_RESULT,
        ],
    )

    row = recovery.classify_pointer(pointer)

    # The instrument sees the tool call rather than reading every one-turn
    # stream as a premature end: a worker that acted left work behind, and that
    # is a different reading with a different remedy.
    assert row["recovery_classification"] != (
        recovery_classification.ENDED_WITHOUT_ACTING
    )
    assert row["classification"] == "abandoned"
