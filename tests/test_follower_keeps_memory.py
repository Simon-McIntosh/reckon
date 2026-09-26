"""The follower keeps what its pane showed, and writes only when that moves.

Two properties of one arming. The pane's memory has to survive a reload: a
reload replaces the process image, and with it the grid that remembered what
each run's last row said, so the next row would fall back to the producer's own
``from_state`` and rewrite the story the reader already watched. And a quiet
follower has to leave a quiet filesystem: the per-tick hook used to write the
checkpoint on every poll, each write two fsyncs and a rename into the reader's
own home, so an idle arming churned inodes for as long as it sat there.

Both are measured on the follower's own output and its own writes, because both
are about what a *second* arming does after a *first* one has ended.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import follow_checkpoint, runs
from tests.test_follow_rearm_continues import (
    PROJECT,
    RUN_A,
    SESSION,
    _arm,
    _event,
    _fleet_lines,
    _iso,
    _run_follow,
    _two_live_runs,
)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests, streams and checkpoints in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def follow_lines(monkeypatch):
    """Capture the follower command's own lines instead of writing them out."""
    lines: list[str] = []

    def capture(line, *, stream=None):
        lines.append(line)

    monkeypatch.setattr(cli, "_echo_follow_line", capture)
    return lines


def test_a_reload_keeps_the_pane_memory_and_renders_the_transition(
    home, follow_lines, monkeypatch
) -> None:
    """A reloaded pane shows ``blocked → working``, not the producer's record.

    The reader watched the run sit at ``blocked``. The producer's own record
    knows nothing of that, and the reloaded grid starts empty, so without the
    memory the row falls back to the producer's ``from_state`` and prints
    ``dispatched → working`` — the bare-state defect the pane measured on
    2026-09-25. The follower's remembered map travels with the attach event, so
    the row keeps chaining from what the reader last saw.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _run_follow()
        record = follow_checkpoint.read(PROJECT, SESSION)
        assert record, "the first arming leaves a place behind"

        # The pane last drew RUN_A blocked. The checkpoint is rewritten to say
        # so at the place the first arming left, and the producer's own record
        # says the run left ``dispatched`` for ``working``.
        follow_checkpoint.write(
            PROJECT,
            SESSION,
            stream_path=stream_path,
            offset=int(record["offset"]),
            reported={**dict(record["reported"]), RUN_A: "blocked"},
        )
        record = follow_checkpoint.read(PROJECT, SESSION)
        assert record["reported"].get(RUN_A) == "blocked", record
        with stream_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _event(
                        RUN_A,
                        "node-a",
                        state="working",
                        observed_at=_iso(time.time() - 5 * 60),
                        previous="dispatched",
                    )
                )
                + "\n"
            )
        follow_lines.clear()

        monkeypatch.setenv(
            cli._FOLLOWER_CHECKPOINT_ENV,
            json.dumps({"project": PROJECT, "checkpoint": record}),
        )
        _run_follow()
        monkeypatch.delenv(cli._FOLLOWER_CHECKPOINT_ENV, raising=False)

        rows = _fleet_lines(follow_lines)

    chained = [line for line in rows if "→" in line and "working" in line]
    assert chained, f"the reload delivered no transition row; got {rows!r}"
    left, _, right = chained[0].partition("→")
    assert "blocked" in left, (
        f"the reloaded row must continue from the state the pane already showed; "
        f"got {chained[0]!r}"
    )
    assert "working" in right, chained[0]


def test_an_idle_arming_writes_no_checkpoint_across_several_polls(
    home, monkeypatch
) -> None:
    """A quiet follower writes its place once, not once per poll.

    The per-tick hook runs on every wait pass and used to write the checkpoint
    unconditionally, each write two fsyncs and a rename into the reader's home;
    an idle arming therefore churned inodes for as long as it sat. The write
    must follow a change in the place — the offset, the stream identity or the
    reported map — rather than the poll.

    The declared control mutation is the same arming with that guard removed,
    which writes once per pass; the assertion below is the one it trips.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _arm()
        assert follow_checkpoint.read(PROJECT, SESSION), (
            "the first arming leaves a durable place"
        )

        writes: list[int] = []
        real_write = follow_checkpoint.write

        def counting_write(*args, **kwargs):
            writes.append(int(kwargs.get("offset", -1)))
            return real_write(*args, **kwargs)

        monkeypatch.setattr(follow_checkpoint, "write", counting_write)

        polls = 0
        stop = threading.Event()

        def sleeper(_seconds: float) -> None:
            nonlocal polls
            polls += 1
            if polls >= 12:
                stop.set()

        list(
            cli._follow_watch_lines(
                PROJECT,
                session=SESSION,
                poll_interval=0.0,
                sweep=None,
                sleeper=sleeper,
                stop=stop,
            )
        )

    assert polls >= 12, f"the arming did not idle long enough to measure; {polls}"
    assert len(writes) <= 2, (
        f"an idle arming must not rewrite its checkpoint on every poll; "
        f"{len(writes)} writes over {polls} polls, at {writes!r}"
    )
