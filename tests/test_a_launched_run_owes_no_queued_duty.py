"""A launched run owes no queued duty.

An obligations snapshot is computed once per station tick and served for as
long as the tick runs, which can be minutes. A request that was still queued
when the snapshot was taken and launched before it was read therefore leaves a
``queued-awaiting-session`` row behind, whose published next command is the
dispatch that would put a second worker on a run already working. The phase the
row describes is the run's own live pointer's, so it is compared with that
pointer when the answer is served: a run that has moved on owes nothing, and a
request still queued keeps its duty through the same snapshot.

Every case drives the two readers a coordinator acts on: the settled rows the
obligations view serves, and the stop hook's own reading list.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew.node import UnreconciledRuns
from reckon.crew.obligations import obligations as obligations_view
from reckon.hooks import coordinator_obligations
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT = "launched-run-fixture"
SESSION = "coordinator-fixture"
LAUNCHED_RUN = "r-launched-request"
STILL_QUEUED_RUN = "r-still-queued-request"
LIVE_NAMED_RUN = "run-still-open"
REFUSAL_ACTION = "reckon crew complete --run run-still-open"

# A pid no process can hold, so the drive's session resolves through the
# registration's own name rather than the runner's ambient harness identity.
_ABSENT_HARNESS_PID = 2_147_483_647


@pytest.fixture()
def mounted_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and one mounted project, so nothing real is read."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    repo = tmp_path / "repo"
    docs = repo / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed launched-run fixture"),
    ):
        _git(repo, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    return repo


@pytest.fixture()
def follower() -> None:
    with runs.follower_claim(PROJECT, SESSION) as claim:
        assert claim[0] is True, claim[1]
        yield


def _session_refusal() -> str:
    """The stamped refusal the admission pass stores on a queued pointer."""
    refusal = UnreconciledRuns(
        [{"run_id": LIVE_NAMED_RUN, "next_action": REFUSAL_ACTION}], "15m"
    )
    return f"UnreconciledRuns: {refusal}"


def _write_pointer(run_id: str, *, phase: str) -> dict[str, object]:
    record: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "process_alive": True,
        "launcher_host": "some-host.invalid",
    }
    if phase == "queued":
        record["failure"] = _session_refusal()
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _producer() -> dict[str, object]:
    return {
        "pid": os.getpid(),
        "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
        "started_at": datetime.now(tz=UTC).isoformat(),
        "code_stamp": obligation_snapshot.source_code_stamp(),
    }


def _publish(payload: dict[str, object]) -> Path:
    document = obligation_snapshot.document_for(
        payload,
        computed_at=datetime.now(tz=UTC),
        stream_offset=0,
        producer=_producer(),
    )
    return obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _served_rows() -> list[dict[str, object]]:
    """The rows the obligations view and the stop hook act on, from the snapshot."""
    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None, "this case needs a published snapshot"
    return obligation_snapshot.without_departed_run_duties(
        obligation_snapshot.live_payload(document)
    )["obligations"]


def _drive_stop(repository: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = (
        str(REPO_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    )
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"),
            "--hook",
            "stop",
        ],
        input=json.dumps({"session_id": SESSION, "cwd": str(repository)}),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def test_a_snapshot_taken_while_queued_owes_the_duty(
    mounted_home: Path, follower: None
) -> None:
    """The snapshot raised while both requests were queued lists both duties."""
    _write_pointer(LIVE_NAMED_RUN, phase="working")
    _write_pointer(LAUNCHED_RUN, phase="queued")
    _write_pointer(STILL_QUEUED_RUN, phase="queued")
    _publish(obligations_view(PROJECT, SESSION))

    rows = _served_rows()

    assert sorted(str(row["run_id"]) for row in rows) == sorted(
        [LAUNCHED_RUN, STILL_QUEUED_RUN]
    ), rows
    assert {str(row["kind"]) for row in rows} == {"queued-awaiting-session"}, rows


def test_a_launched_run_owes_no_queued_duty_through_the_held_snapshot(
    mounted_home: Path, follower: None
) -> None:
    """The launched run's row is dropped, the still-queued request keeps its own.

    The snapshot on disk is the one taken while both requests were queued; only
    the pointer has moved. A reader that serves the rows as stored lists the
    launched run and offers it the dispatch that would duplicate the worker.
    """
    _write_pointer(LIVE_NAMED_RUN, phase="working")
    _write_pointer(LAUNCHED_RUN, phase="queued")
    _write_pointer(STILL_QUEUED_RUN, phase="queued")
    _publish(obligations_view(PROJECT, SESSION))

    # The pass launches the first request: its pointer moves on, the snapshot
    # is left exactly as it was computed.
    _write_pointer(LAUNCHED_RUN, phase="working")

    rows = _served_rows()

    assert [str(row["run_id"]) for row in rows] == [STILL_QUEUED_RUN], rows
    assert str(rows[0]["kind"]) == "queued-awaiting-session", rows
    assert all(
        "crew dispatch" not in str(row.get("next_command") or "") for row in rows
    )

    # The stop hook's own reading list drops the same row.
    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None
    hook_rows = coordinator_obligations.recheck_worktree_held_rows(
        obligation_snapshot.live_payload(document), project=PROJECT
    )["obligations"]
    assert [str(row["run_id"]) for row in hook_rows] == [STILL_QUEUED_RUN], hook_rows

    driven = _drive_stop(mounted_home)
    assert driven.returncode == 0, driven.stderr
    assert driven.stderr == "", driven.stderr
    decision = json.loads(driven.stdout)
    assert decision["decision"] == "block", decision
    assert STILL_QUEUED_RUN in decision["reason"], decision
    assert LAUNCHED_RUN not in decision["reason"], decision


def test_serving_the_rows_uncompared_lists_the_launched_run(
    mounted_home: Path, follower: None
) -> None:
    """Negative-control witness: the raw rows alone still carry the duty."""
    _write_pointer(LIVE_NAMED_RUN, phase="working")
    _write_pointer(LAUNCHED_RUN, phase="queued")
    _write_pointer(STILL_QUEUED_RUN, phase="queued")
    _publish(obligations_view(PROJECT, SESSION))
    _write_pointer(LAUNCHED_RUN, phase="working")

    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None
    raw = obligation_snapshot.live_payload(document)["obligations"]

    assert [str(row["run_id"]) for row in raw] == [
        LAUNCHED_RUN,
        STILL_QUEUED_RUN,
    ], raw
