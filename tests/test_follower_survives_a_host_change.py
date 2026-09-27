"""A follower continues across a host change on the shared filesystem.

The stream lives on GPFS and is read from whichever client node the session is
placed on. GPFS hands each client its own device number for the same mount, so
the ``st_dev`` a follower records on one node does not match the ``st_dev`` the
same file reports from another — while the inode is the mount's own and does not
change. A checkpoint that compared both fields therefore read as *not this
stream* on every arming that crossed a node, and the follower fell back to
re-reading the whole stream: days of history arrived as fresh transitions,
including rows for sessions the arming was never asked to follow.

These tests pin the identity a continuation rests on, the fallback a genuinely
replaced stream still takes, and the silence an arm with no place keeps about a
stream it has not been asked to replay.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import follow_checkpoint, runs

PROJECT = "host-change-proj"
SESSION = "s-mine"
OTHER_SESSION = "s-other"
RUN_MINE = "r-host-change-mine"
RUN_OTHER = "r-host-change-other"

_FLEET_EVENTS = frozenset({"baseline", "transition", "manifest-rewritten"})


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, streams and checkpoints in a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(home: Path, run_id: str, *, session: str, node: str) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": "working",
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _transition(run_id: str, *, session: str, state: str, previous: str) -> dict:
    return {
        "project": PROJECT,
        "event": "transition",
        "run_id": run_id,
        "node": run_id,
        "session": session,
        "from_state": previous,
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "observed_at": runs._utc_now(),
        "legacy": False,
    }


def _arm(*, lifetime: float = 0.75) -> list[dict]:
    """Run one arming to its own lifetime and collect the fleet rows it drew."""
    generator = cli._follow_watch_lines(
        PROJECT,
        session=SESSION,
        poll_interval=0.001,
        sweep=None,
        lifetime=lifetime,
    )
    return [event for event in generator if event.get("event") in _FLEET_EVENTS]


# ── The stream's identity across a host change ──────────────────────────────


def test_a_checkpoint_with_a_foreign_device_number_still_continues(tmp_path) -> None:
    """A checkpoint recorded on another client of the mount continues here.

    The recorded device is the one the other node saw; the inode is the mount's
    own and is the same. Comparing the device would reject a file that never
    moved, which is exactly the host change this node repairs.
    """
    stream = tmp_path / "stream.jsonl"
    stream.write_text('{"event":"seed"}\n', encoding="utf-8")
    real = follow_checkpoint.stream_identity(stream)

    follow_checkpoint.write(
        PROJECT,
        SESSION,
        stream_path=stream,
        offset=len('{"event":"seed"}\n'),
        reported={"r-1": "working"},
        # The device a sibling client node of the same GPFS mount reports: a
        # different number for the very same file.
        identity={"dev": int(real["dev"]) + 2, "ino": int(real["ino"])},
    )
    record = follow_checkpoint.read(PROJECT, SESSION)
    assert record, "the checkpoint under test must have been written"
    assert record["stream_identity"]["dev"] != real["dev"], record

    assert follow_checkpoint.continues(record, stream) is True, (
        "the same inode reached from another node is the same stream"
    )


def test_a_replaced_stream_does_not_continue(tmp_path) -> None:
    """A fresh file at the same path is a different stream and falls back."""
    stream = tmp_path / "stream.jsonl"
    stream.write_text('{"event":"seed"}\n', encoding="utf-8")
    follow_checkpoint.write(
        PROJECT,
        SESSION,
        stream_path=stream,
        offset=1,
        reported={},
    )
    record = follow_checkpoint.read(PROJECT, SESSION)
    assert record

    replacement = tmp_path / "stream.replacement"
    replacement.write_text('{"event":"replaced"}\n', encoding="utf-8")
    os.replace(replacement, stream)

    assert follow_checkpoint.continues(record, stream) is False, (
        "a new inode at the same path is a replaced stream, not a continuation"
    )


# ── An arm with no place does not replay the stream ─────────────────────────


def test_an_arm_with_no_checkpoint_replays_no_transitions_and_no_peer_rows(
    home,
) -> None:
    """A first arm emits state and never the stream's history.

    With no place to resume from, the arming derives the fleet as it stands: the
    rows it draws carry no row for a session it was not asked to follow, and it
    does not walk the stream's earlier transitions as though they were news.
    """
    _write_pointer(home, RUN_MINE, session=SESSION, node="node-mine")
    _write_pointer(home, RUN_OTHER, session=OTHER_SESSION, node="node-other")

    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        stream_path.parent.mkdir(parents=True, exist_ok=True)
        stream_path.write_text(
            json.dumps(
                _transition(
                    RUN_MINE, session=SESSION, state="working", previous="dispatched"
                )
            )
            + "\n"
            + json.dumps(
                _transition(
                    RUN_OTHER,
                    session=OTHER_SESSION,
                    state="blocked",
                    previous="working",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        crew.list_live(project=PROJECT)
        assert not follow_checkpoint.exists(PROJECT, SESSION)

        emitted = _arm()

    assert all(event.get("event") != "transition" for event in emitted), (
        f"an arm with no place must not replay the stream's transitions; got {emitted!r}"
    )
    assert all(
        str(event.get("session") or "") != OTHER_SESSION for event in emitted
    ), f"no row for a session this arm was not asked to follow; got {emitted!r}"