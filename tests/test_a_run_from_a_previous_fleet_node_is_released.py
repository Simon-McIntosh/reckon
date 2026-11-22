"""A promoted run whose worker was on a previous fleet node gives up its claim.

The fleet moves node every few days. A pointer written on the node the fleet has
since left records a pid that answers only on that machine, so its worker can be
proved neither dead nor alive from here.

Its liveness therefore reads unknown, and unknown is kept for a run showing a
turn in progress. Once the run's newest stream shows no turn inside the window
the stall clock applies there is nobody left to work it, and the pointer is
retired so the claim it holds is released.

Three promoted runs are built in a temporary crew home, recorded on a host other
than the fleet node's:

- no turn in progress: released on the first pass;
- a stream whose turn was written inside the window, so a turn is in progress:
  kept;
- recorded on the fleet node itself with a process proved dead: released, the
  control that shows the stall window and the ordinary dead path untouched.

The negative control reads the first run against the base revision, so it keeps
its pointer there: that retirement is the change this case exists to show.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from reckon.crew import pending_landing, runs

PREVIOUS_NODE = "98dci4-clu-2001.example.invalid"
FLEET_NODE = "98dci4-clu-3026.example.invalid"
UNREADABLE_PID = "not-a-pid"
# The silence window the stall clock applies, comfortably exceeded by the quiet
# run's stream and comfortably inside the working run's.
QUIET_SECONDS = 3600.0
IN_TURN_SECONDS = 5.0
ASSISTANT_RECORD = '{"type": "assistant"}\n'


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, with the real one asserted untouched afterwards."""
    real = runs.crew_home()
    before = {path.name for path in sorted(real.glob("live/*.json"))}
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    record = tmp_path / "fleet-record.json"
    record.write_text(json.dumps({"node": FLEET_NODE}) + "\n")
    monkeypatch.setenv("RECKON_FLEET_RECORD", str(record))
    yield runs.crew_home()
    after = {path.name for path in sorted(real.glob("live/*.json"))}
    assert after == before, "the case wrote into the live fleet's own home"


def _write_promoted_pointer(
    run_id: str,
    *,
    launcher_host: str,
    pid: object,
    project: str = "release-fixture",
    session: str | None = None,
    promoted: bool = True,
    stream: str | None = None,
    stream_age_seconds: float | None = None,
) -> dict:
    """Write one promoted run's live pointer, and its stream when asked for."""
    pointer = {
        "run_id": run_id,
        "project": project,
        "repo": "/nonexistent/repository",
        "phase": "complete",
        "launcher_host": launcher_host,
        "pid": pid,
    }
    if promoted:
        pointer["promoted"] = True
    if session is not None:
        pointer["session"] = session
    runs._write_json(runs.pointer_path(run_id), pointer)
    if stream is not None:
        directory = runs.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "stream.jsonl"
        path.write_text(stream)
        if stream_age_seconds is not None:
            moment = time.time() - stream_age_seconds
            os.utime(path, (moment, moment))
    return pointer


def test_a_promoted_run_from_a_previous_fleet_node_gives_up_its_claim(
    crew_home: Path,
) -> None:
    quiet = _write_promoted_pointer(
        "r-previous-node-quiet",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        stream=ASSISTANT_RECORD,
        stream_age_seconds=QUIET_SECONDS,
    )
    working = _write_promoted_pointer(
        "r-previous-node-working",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        stream=ASSISTANT_RECORD,
        stream_age_seconds=IN_TURN_SECONDS,
    )
    dead_here = _write_promoted_pointer(
        "r-fleet-node-dead",
        launcher_host=FLEET_NODE,
        pid=_absent_pid(),
        stream=None,
    )

    results = pending_landing.retire_promoted_pointers(release_workspaces=False)
    by_run = {row["run_id"]: row for row in results}

    assert not runs.pointer_path(quiet["run_id"]).exists(), results
    assert not runs.pointer_path(dead_here["run_id"]).exists(), results
    assert runs.pointer_path(working["run_id"]).exists(), results

    assert by_run[quiet["run_id"]]["basis"] == "previous-fleet-node"
    assert by_run[dead_here["run_id"]]["basis"] == "proved-dead"
    assert by_run[working["run_id"]]["status"] == "release-held"
    assert "turn is in progress" in by_run[working["run_id"]]["reason"]


def test_a_pending_landing_journal_keeps_the_run_its_retry_will_finish(
    crew_home: Path,
) -> None:
    """A journal is written before the promotion completes, so it is not proof.

    A worker that dies mid-promotion leaves the journal behind for a run that
    has delivered nothing yet. The interrupted-landing pass finishes that run
    from its live pointer, so the pointer must outlive a quiet stream.
    """
    pointer = _write_promoted_pointer(
        "r-mid-promotion",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        promoted=False,
        stream=ASSISTANT_RECORD,
        stream_age_seconds=QUIET_SECONDS,
    )
    pending_landing.write_pending(pointer["run_id"], pointer["project"], head="0" * 40)

    pending_landing.retire_promoted_pointers(release_workspaces=False)

    assert runs.pointer_path(pointer["run_id"]).exists()


def test_a_scoped_pass_leaves_the_runs_it_was_not_given(crew_home: Path) -> None:
    """A station tick scoped to one project and session retires only those."""
    mine = _write_promoted_pointer(
        "r-scoped-mine",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        session="this-session",
        stream=ASSISTANT_RECORD,
        stream_age_seconds=QUIET_SECONDS,
    )
    other_project = _write_promoted_pointer(
        "r-scoped-other-project",
        project="another-project",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        stream=ASSISTANT_RECORD,
        stream_age_seconds=QUIET_SECONDS,
    )
    other_session = _write_promoted_pointer(
        "r-scoped-other-session",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        session="another-session",
        stream=ASSISTANT_RECORD,
        stream_age_seconds=QUIET_SECONDS,
    )

    pending_landing.retire_promoted_pointers(
        project="release-fixture", session="this-session", release_workspaces=False
    )

    assert not runs.pointer_path(mine["run_id"]).exists()
    assert runs.pointer_path(other_project["run_id"]).exists()
    assert runs.pointer_path(other_session["run_id"]).exists()


def test_a_released_claim_is_not_restored_by_a_later_pass(crew_home: Path) -> None:
    quiet = _write_promoted_pointer(
        "r-previous-node-discarded",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        stream=ASSISTANT_RECORD,
        stream_age_seconds=QUIET_SECONDS,
    )
    pending_landing.retire_promoted_pointers(release_workspaces=False)
    assert not runs.pointer_path(quiet["run_id"]).exists()

    for _pass in range(2):
        pending_landing.retire_promoted_pointers(release_workspaces=False)
        assert not runs.pointer_path(quiet["run_id"]).exists()
