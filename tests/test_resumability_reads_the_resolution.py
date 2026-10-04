"""A resumability reader keys on the resolution, never on the lagging field.

A live pointer's ``session_id`` field mirrors the session its run is driving,
and the mirror can lag: measured on a run whose pointer carried
``session_id: null`` while its worker had written 239 stream records and a
complete blocked manifest. The resolution consults the pointer, the run's
stream, and its promoted ledger row in that order, and reports which of the
three named the session. These cases pin the recovery classification to that
answer in both directions: a null field over a stream that names a session
reads resumable and names the stream, and a null field over evidence that
names nothing reads unresolved.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from reckon.crew import recovery
from reckon.crew.runs import _write_json, pointer_path

THREAD_ID = "01a0635f-62a3-7283-a81b-61cd39bedb60"
RUN_ID = "r-20261004T000000000000-node-a"

# The stream a cli launch opens with: its first record names the thread id, so
# a run whose pointer field is empty still has a session on record.
STREAM_OPENING_WITH_THE_THREAD = (
    f'{{"type":"thread.started","thread_id":"{THREAD_ID}"}}\n'
    '{"type":"turn.started"}\n'
)


def _dead_pid() -> int:
    return int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8")) + 4096


@pytest.fixture
def reckon_home(tmp_path, monkeypatch):
    """Point the classification at a temporary home so it reads only this fixture."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _incident_run(tmp_path: Path, stream_text: str) -> dict:
    """Write the incident's run: a null session field over a stream.

    The pointer, the stream and the blocked manifest all live under
    ``tmp_path``, and the worker's pid is one the kernel cannot have issued,
    so the run reads as one whose process has already ended.
    """
    directory = tmp_path / "runs" / RUN_ID
    directory.mkdir(parents=True)
    stream = directory / "stream.jsonl"
    stream.write_text(stream_text, encoding="utf-8")
    tree = tmp_path / "trees" / RUN_ID
    tree.mkdir(parents=True)
    manifest = directory / "manifest.md"
    manifest.write_text("node: node-a\nstatus: blocked\n", encoding="utf-8")
    record = {
        "run_id": RUN_ID,
        "project": "proj-a",
        "repo": str(tmp_path / "repo"),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "role": "implement",
        "created_at": "2026-10-04T08:00:00Z",
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "phase": "starting",
        "pid": _dead_pid(),
        "launcher_host": socket.gethostname(),
        # The lagging field, present and null, exactly as the incident found it.
        "session_id": None,
        "node": {
            "id": "node-a",
            "plan": "plan-a",
            "section": "s7",
            "time_budget": "30m",
            "write_paths": ["reckon/one.py"],
        },
    }
    _write_json(pointer_path(RUN_ID), record)
    return record


def test_a_null_session_field_reads_resumable_through_the_stream(reckon_home, tmp_path):
    record = _incident_run(tmp_path, STREAM_OPENING_WITH_THE_THREAD)

    row = recovery.classify_pointer(record)

    assert row["classification"] == "blocked"
    resolution = row["session_resolution"]
    assert resolution["resolved"] is True
    assert resolution["source"] == "stream"
    assert resolution["session_id"] == THREAD_ID
    assert "stream" in resolution["consulted"]


def test_a_null_session_field_with_no_session_anywhere_reads_unresolved(
    reckon_home, tmp_path
):
    record = _incident_run(tmp_path, '{"type":"turn.started"}\n')

    row = recovery.classify_pointer(record)

    resolution = row["session_resolution"]
    assert resolution["resolved"] is False
    assert not resolution["session_id"]
    assert set(resolution["consulted"]) == {"pointer", "stream", "ledger"}
