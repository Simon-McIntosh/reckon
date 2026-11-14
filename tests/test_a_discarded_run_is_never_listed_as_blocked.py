"""A discarded run leaves every list it was drawn on, and the stop draws nothing.

A coordinator stops an unwanted run and then discards it, and must not see the
run again: the pane named a discarded review ``needs you`` with a killed
counter after the discard was issued, and drew a transition out of that word
while the run it named was already gone.

The guarantee is a chain rather than one filter. The stop moves the run into its
stopped phase while the pane has already drawn it ``needs you``; the discard
removes the pointer and writes its own record; and the run then reads as a
dropped departure rather than the block it left. The needs-you set, the
obligations list and the pane all lose the run on that word.

The cases drive the real ``crew stop`` (``crew.terminate``) against a child
process spawned for the run, the real ``crew discard``, and the real
classifier, obligations derivation and transition renderer. Every crew
directory resolves under a temporary home, and the last case proves the real
ones were left alone.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from reckon import crew
from reckon.crew import obligations, promotion, recovery, ticker
from reckon.crew.process_signal import RUN_ID_ENV

PROJECT = "discarded-run-proj"
SESSION = "discarded-run-session"
RUN_ID = "r-discarded-review"
NODE = "review-of-some-run-0f00d"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point every crew directory at a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _spawn_the_run_worker(run_id):
    """The run's leftover process, and the supervisor whose pid the run records.

    ``crew stop`` resolves a run's tree by descent from the recorded pid and by
    the run id in a process's environment. This is the shape a blocked run has
    when the worker reported its block and the supervisor exited without reaping
    the process it had started: the recorded pid is gone, and the process left
    behind still carries the run id, which is how the stop reaches it.

    Both start their own session, so the stop's process-group signal reaches the
    run without reaching the test that spawned it.
    """
    environment = dict(os.environ)
    environment[RUN_ID_ENV] = run_id
    leftover = subprocess.Popen(
        ["sleep", "60"],
        env=environment,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    supervisor = subprocess.Popen(["true"], start_new_session=True)
    supervisor.wait()
    return supervisor.pid, leftover


def _blocked_run(home, supervisor_pid):
    """A review run whose manifest reads blocked, as its worker left it."""
    stream = home / "streams" / f"{RUN_ID}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    manifest = home / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-some-run-0f00d\n"
        "status: blocked\n"
        "blocked: the reviewed run's head could not be resolved\n",
        encoding="utf-8",
    )
    crew.run_dir(RUN_ID).mkdir(parents=True, exist_ok=True)
    crew._write_json(
        crew.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "role": "review",
            "launch": "in-harness",
            "node": {"id": NODE, "plan": "plan-a", "time_budget": "20m"},
            "phase": "blocked",
            "created_at": "2026-11-14T08:00:00+00:00",
            "manifest_path": str(manifest),
            "log_path": str(stream),
            "pid": supervisor_pid,
            "launcher_host": os.uname().nodename,
            # The recorded process is gone, so the run carries no proof of a
            # live worker and is read from the verdict its manifest holds. The
            # process left behind is reached by the stop's environment mark
            # rather than by this pid, which is what a run whose supervisor
            # exited first looks like.
            "process_alive": False,
        },
    )
    return crew.read_pointer(RUN_ID)


def _snapshot(pointer):
    return recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)


def _live_states():
    """The live fleet's rows, as the pane and the recovery sweep read them."""
    return {
        str(pointer.get("run_id") or ""): _snapshot(pointer)
        for pointer in crew.list_live()
    }


def _needs_you_run_ids():
    return {
        run_id
        for run_id, snapshot in _live_states().items()
        if str(snapshot.get("state") or "") in ticker.NEEDS_ACTION
    }


def _obligation_run_ids():
    view = obligations.obligations(PROJECT, SESSION)
    return [
        (str(item.get("kind") or ""), str(item.get("run_id") or ""))
        for item in view.get("obligations", [])
    ]


def _halt_event(snapshot):
    """The halt row the pane drew for the run before the coordinator acted."""
    counts = recovery._fleet_counts({RUN_ID: snapshot})
    return recovery._watch_transition(
        PROJECT,
        kind="transition",
        snapshot=snapshot,
        previous="working",
        current="blocked",
        counts=counts,
    )


def _transition_event(snapshot, previous, state, counts):
    return recovery._watch_transition(
        PROJECT,
        kind="transition",
        snapshot=snapshot,
        previous=previous,
        current=state,
        counts=counts,
    )


def test_a_discarded_run_leaves_the_needs_you_set_and_the_obligations_list(home):
    """Present and blocked before the stop; absent from every list after it."""
    supervisor_pid, leftover = _spawn_the_run_worker(RUN_ID)
    try:
        pointer = _blocked_run(home, supervisor_pid)
        before = _snapshot(pointer)

        # Positive controls: each instrument sees a run known to be present and
        # blocked. They are asserted before the discard, so the absence asserted
        # afterwards cannot be a reader that saw nothing at all.
        assert before["state"] == "blocked", before
        assert RUN_ID in _needs_you_run_ids(), _live_states()
        assert ("blocked", RUN_ID) in _obligation_run_ids(), _obligation_run_ids()
        assert recovery._fleet_counts({RUN_ID: before})["blocked"] == 1

        # The pane draws the halt row first: that is the word the run leaves,
        # and it is what makes a later drop row print at all. The renderer is
        # shown working before anything is dropped.
        pane = ticker.Ticker(width=200, color=False)
        halted_line = pane.render(_halt_event(before))
        assert "→ needs you" in halted_line, halted_line
        assert " 1n " in halted_line, halted_line

        # The stop ends the run's own process and records the phase. It asks
        # for nothing the coordinator has not already answered by stopping, so
        # it emits no transition and the stop precedes the discard silently.
        stopped = crew.terminate(RUN_ID)
        assert stopped["phase"] == "stopped", stopped
        assert leftover.pid in [int(pid) for pid in stopped["target_pids"]], stopped
        leftover.wait(timeout=10)
        after_stop = _snapshot(crew.read_pointer(RUN_ID))
        folded, _running = recovery.fleet_transitions(
            {RUN_ID: before}, {RUN_ID: after_stop}
        )
        assert folded == [], folded

        # The discard removes the pointer and writes its own record, and the
        # stopped run leaves every list the pane had drawn it on.
        discard = crew.discard(RUN_ID)
        assert discard["pointer_removed"] is True, discard
        assert promotion.discard_recorded(RUN_ID), discard

        assert RUN_ID not in _needs_you_run_ids(), _live_states()
        assert ("blocked", RUN_ID) not in _obligation_run_ids(), _obligation_run_ids()
        recovered = recovery.recover()
        assert [row["run_id"] for row in recovered["runs"]] == [], recovered
        assert recovered["counts"].get("blocked", 0) == 0, recovered

        # The departure is worded a drop rather than the block it left, and its
        # figures are the fleet it left: no run behind it and no blocked count.
        departed, running_next = recovery.fleet_transitions({RUN_ID: after_stop}, {})
        assert running_next == {}, running_next
        ((row, previous, state, counts),) = departed
        assert state == "discarded", departed
        assert previous in ticker.NEEDS_ACTION, departed
        drop_line = pane.render(_transition_event(row, previous, state, counts))
        assert "→ dropped" in drop_line, drop_line
        assert "→ needs you" not in drop_line, drop_line
        assert " 0n " in drop_line, drop_line
    finally:
        if leftover.poll() is None:
            leftover.kill()
