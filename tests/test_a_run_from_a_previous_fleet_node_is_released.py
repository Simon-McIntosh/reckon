"""A promoted run whose worker was on a previous fleet node gives up its claim.

The fleet moves node every few days. A pointer written on the node the fleet has
since left records a pid that answers only on that machine, so its worker can be
proved neither dead nor alive from here, and the number it carries may name an
unrelated process on this host.

Its liveness therefore reads unknown, and unknown is kept for a run showing a
turn in progress. Once the run's newest stream shows no turn inside the window
the stall clock applies there is nobody left to work it, and the pointer is
retired so the claim it holds is released.

Every run here is built the way production builds it: the pointer carries the
promotion mark and the run has its ledger row. A quiet previous-node run whose
pid happens to name a live process on this host is released, and a
previous-node run still writing keeps its pointer even though the promotion row
would otherwise retire it. A workspace on disk, an unfinished landing journal,
and a pointer with no promotion evidence are each left to the paths that own
them.

The negative control runs the same quiet pointer against the base revision,
where it keeps its pointer: that retirement is the change this case exists to
show.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import pending_landing, runs
from tests.helpers.landing_repository import PROJECT, _write_origin_and_checkout

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


def _live_pointers(home: Path) -> set[str]:
    """The live pointer file names under a crew home."""
    return {path.name for path in home.glob("live/*.json")}


@pytest.fixture()
def crew(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A temporary crew home and run store, with the live ones left untouched.

    The live-pointer directory and the run store the promotion row is read from
    are both resolved from the environment, so pointing each at ``tmp_path`` is
    what keeps the case from reading or writing the fleet's own records.
    """
    real_home = runs.crew_home()
    before = _live_pointers(real_home)
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "run_store.db"))
    record = tmp_path / "fleet-record.json"
    record.write_text(json.dumps({"node": FLEET_NODE}) + "\n")
    monkeypatch.setenv("RECKON_FLEET_RECORD", str(record))
    _origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repository / "docs")}))
    (repository / "docs" / "state" / PROJECT / "runs").mkdir(
        parents=True, exist_ok=True
    )
    yield {"home": runs.crew_home(), "repository": repository}
    assert _live_pointers(real_home) == before, "the case wrote into the fleet's home"


def _write_pointer(
    run_id: str,
    *,
    launcher_host: str,
    pid: object,
    project: str = PROJECT,
    session: str = "owner",
    promoted: bool = True,
    stream: str | None = None,
    stream_age_seconds: float | None = None,
    worktree: str | None = None,
) -> dict:
    """Write one live pointer, and its stream when asked for."""
    pointer = {
        "run_id": run_id,
        "project": project,
        "repo": "/nonexistent/repository",
        "session": session,
        "role": "implement",
        "phase": "complete",
        "launch": "in-harness",
        "launcher_host": launcher_host,
        "pid": pid,
    }
    if promoted:
        pointer["promoted"] = True
    if worktree is not None:
        pointer["worktree"] = worktree
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


def _promote(pointer: dict, repository: Path) -> None:
    """Write the ledger row a promotion leaves behind for the same run."""
    ledger.write_run_file(
        PROJECT,
        {
            **pointer,
            "commits": ["0" * 40],
            "promoted_revision": "0" * 40,
            "gate": "passed",
        },
        repository,
    )


def test_a_quiet_run_from_a_previous_fleet_node_gives_up_its_claim(crew: dict) -> None:
    """The pid names a live process here; the host is what decides."""
    pointer = _write_pointer(
        "r-previous-node-quiet",
        launcher_host=PREVIOUS_NODE,
        pid=os.getpid(),
        stream=ASSISTANT_RECORD,
        stream_age_seconds=QUIET_SECONDS,
    )
    _promote(pointer, crew["repository"])

    results = pending_landing.retire_promoted_pointers(release_workspaces=False)
    by_run = {row["run_id"]: row for row in results}

    assert not runs.pointer_path(pointer["run_id"]).exists(), results
    assert by_run[pointer["run_id"]]["basis"] == "previous-fleet-node"


def test_a_working_run_from_a_previous_fleet_node_keeps_its_claim(crew: dict) -> None:
    """A turn inside the window outranks the promotion row that would retire it."""
    pointer = _write_pointer(
        "r-previous-node-working",
        launcher_host=PREVIOUS_NODE,
        pid=os.getpid(),
        stream=ASSISTANT_RECORD,
        stream_age_seconds=IN_TURN_SECONDS,
    )
    _promote(pointer, crew["repository"])

    results = pending_landing.retire_promoted_pointers(release_workspaces=False)
    by_run = {row["run_id"]: row for row in results}

    assert runs.pointer_path(pointer["run_id"]).exists(), results
    assert by_run[pointer["run_id"]]["status"] == "release-held"
    assert "turn is in progress" in by_run[pointer["run_id"]]["reason"]


def test_a_dead_run_on_this_fleet_node_gives_up_its_claim(crew: dict) -> None:
    """The control: the ordinary dead path, with this host's table readable."""
    pointer = _write_pointer(
        "r-fleet-node-dead",
        launcher_host=FLEET_NODE,
        pid=_absent_pid(),
    )
    _promote(pointer, crew["repository"])

    results = pending_landing.retire_promoted_pointers(release_workspaces=False)
    by_run = {row["run_id"]: row for row in results}

    assert not runs.pointer_path(pointer["run_id"]).exists(), results
    assert by_run[pointer["run_id"]]["basis"] == "proved-dead"


def test_a_journal_without_a_promotion_mark_is_not_this_pass_s(crew: dict) -> None:
    """``write_pending`` runs before the promotion, so a journal proves nothing.

    A worker that died mid-promotion leaves the journal behind for a run that
    has delivered nothing, and the interrupted-landing retry finishes it from
    the live pointer.
    """
    pointer = _write_pointer(
        "r-mid-promotion",
        launcher_host=FLEET_NODE,
        pid=_absent_pid(),
        promoted=False,
    )
    pending_landing.write_pending(pointer["run_id"], pointer["project"], head="0" * 40)

    pending_landing.retire_promoted_pointers(release_workspaces=False)

    assert runs.pointer_path(pointer["run_id"]).exists()


def test_an_unfinished_landing_holds_a_promoted_pointer(crew: dict) -> None:
    """A promoted run whose landing has not finished keeps its pointer."""
    pointer = _write_pointer(
        "r-unfinished-landing",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        stream=ASSISTANT_RECORD,
        stream_age_seconds=QUIET_SECONDS,
    )
    pending_landing.write_pending(pointer["run_id"], pointer["project"], head="0" * 40)

    pending_landing.retire_promoted_pointers(release_workspaces=False)

    assert runs.pointer_path(pointer["run_id"]).exists()


def test_a_workspace_on_disk_holds_a_promoted_pointer(
    crew: dict, tmp_path: Path
) -> None:
    """The landing paths decide a workspace, so a dead pid cannot release it."""
    workspace = tmp_path / "worktrees" / "uncommitted"
    workspace.mkdir(parents=True)
    pointer = _write_pointer(
        "r-uncommitted-workspace",
        launcher_host=FLEET_NODE,
        pid=_absent_pid(),
        worktree=str(workspace),
    )
    _promote(pointer, crew["repository"])

    pending_landing.retire_promoted_pointers(release_workspaces=False)

    assert runs.pointer_path(pointer["run_id"]).exists()


def test_a_scoped_pass_leaves_the_runs_it_was_not_given(crew: dict) -> None:
    """A station tick scoped to one project and session retires only those."""
    mine = _write_pointer(
        "r-scoped-mine",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        session="this-session",
        stream=ASSISTANT_RECORD,
        stream_age_seconds=QUIET_SECONDS,
    )
    _promote(mine, crew["repository"])
    other_project = _write_pointer(
        "r-scoped-other-project",
        project="another-project",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
    )
    other_session = _write_pointer(
        "r-scoped-other-session",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        session="another-session",
    )

    pending_landing.retire_promoted_pointers(
        project=PROJECT, session="this-session", release_workspaces=False
    )

    assert not runs.pointer_path(mine["run_id"]).exists()
    assert runs.pointer_path(other_project["run_id"]).exists()
    assert runs.pointer_path(other_session["run_id"]).exists()


def test_a_released_claim_is_not_restored_by_a_later_pass(crew: dict) -> None:
    pointer = _write_pointer(
        "r-previous-node-discarded",
        launcher_host=PREVIOUS_NODE,
        pid=UNREADABLE_PID,
        stream=ASSISTANT_RECORD,
        stream_age_seconds=QUIET_SECONDS,
    )
    _promote(pointer, crew["repository"])
    pending_landing.retire_promoted_pointers(release_workspaces=False)
    assert not runs.pointer_path(pointer["run_id"]).exists()

    for _pass in range(2):
        pending_landing.retire_promoted_pointers(release_workspaces=False)
        assert not runs.pointer_path(pointer["run_id"]).exists()
