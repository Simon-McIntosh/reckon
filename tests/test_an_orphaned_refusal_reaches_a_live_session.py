"""A refusal whose dispatching session has ended reaches a live coordinator.

A refused queued request is charged to the session that dispatched it, and an
obligations view is built for one session. When no live process carries that
session the refusal sits in a view nobody reads, and a request can wait for
hours with the whole fleet believing nothing is owed: on 22 November 2026 four
queued requests sat refused for between four and nineteen hours behind
dispatchers whose sessions had ended.

These cases pin the route out of that dead end: a refused request whose
dispatching session is observed ended is listed, as its own kind, to a live
session of the same project, and -- when the project has no live session at all
-- to a live session on the lane. The row names the ended session. A session
the harness keeps no record for has not been observed to end, so its refusals
stay with it, and the ended session is never told about its own orphan.

The fleet is the queue fixture's own: a real checkout, a real project mount and
one request per section, so a refusal here is what an admission pass would have
written.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import reckon.crew.obligations as obligations_module
from reckon.crew import harness_sessions, runs
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

PROJECT = fixtures.PROJECT
chain_repo = fixtures.chain_repo

#: The dispatcher of the refused request, whose process this host no longer has.
ENDED_SESSION = "8e41a6d3-0000-4a2b-9c3d-endedsession"
#: A coordinator of the same project, its harness process running.
PROJECT_SESSION = "b0a1c2d3-1111-4a2b-9c3d-livecoordinator"
#: A live session on the lane with no run of its own in this project.
LANE_SESSION = "c0a1c2d3-2222-4a2b-9c3d-lanelivecoord"

#: A pid this host cannot hold, the suite's convention for a process that has
#: ended: the suite reads it as ended on every host that runs it.
DEAD_PID = 2_147_483_647

REFUSAL_KIND = (
    "WatcherRequired: this session has no follower armed to deliver its duties"
)

#: The published kind of a refusal whose dispatching session has ended. Spelled
#: here as the reader of an obligations payload sees it, so a renamed constant
#: cannot quietly move the reading.
ORPHANED_REFUSAL_KIND = "orphaned-refusal"


@pytest.fixture()
def fleet(chain_repo: Path) -> Path:
    """The queue fixture's checkout, which the cases queue requests against."""
    return chain_repo


def _record_session(session: str, *, pid: int) -> None:
    """One harness record, as the harness keeps it for a session's process."""
    directory = harness_sessions.sessions_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{session}.json").write_text(
        json.dumps({"sessionId": session, "name": session, "pid": pid}),
        encoding="utf-8",
    )


def _queue(repository: Path, run_id: str, *, session: str) -> None:
    """Queue one request on the dependency-free section, dispatched by a session."""
    node = fixtures._node(run_id.removeprefix("r-"), "s1")
    stored = fixtures.queue_admission.queue_requests(
        [node],
        project=PROJECT,
        session=session,
        repo_root=repository,
        config=fixtures.CONFIG,
        run_ids={node.id: run_id},
    )
    assert stored[run_id]["phase"] == fixtures.queue_admission.QUEUED_PHASE, stored
    pointer = dict(runs.read_pointer(run_id))
    assert pointer["session"] == session
    pointer["failure"] = {"kind": REFUSAL_KIND}
    pointer["failure_at"] = datetime.now(UTC).isoformat()
    runs._write_json(runs.pointer_path(run_id), pointer)


def _orphan_row(payload: dict, run_id: str) -> dict | None:
    for row in payload["obligations"]:
        if (
            str(row.get("run_id") or "") == run_id
            and str(row.get("kind") or "")
            == ORPHANED_REFUSAL_KIND
        ):
            return row
    return None


def test_a_refusal_reaches_a_live_coordinator_of_the_project(fleet: Path) -> None:
    """The ended dispatcher's refusal is listed to a live session of its project."""
    _record_session(ENDED_SESSION, pid=DEAD_PID)
    _record_session(PROJECT_SESSION, pid=os.getpid())
    _queue(fleet, "r-ended", session=ENDED_SESSION)
    # A live coordinator is one with a run of its own in the live pointer
    # directory, which is where the view reads the project's sessions from.
    _queue(fleet, "r-live", session=PROJECT_SESSION)

    payload = obligations_module.obligations(PROJECT, PROJECT_SESSION)

    row = _orphan_row(payload, "r-ended")
    assert row is not None, payload["obligations"]
    assert row["ended_session"] == ENDED_SESSION, row
    assert REFUSAL_KIND in row["reason"], row
    assert row["next_command"], row
    assert (
        obligations_module.ORPHANED_REFUSAL_DUTY_KIND == ORPHANED_REFUSAL_KIND
    )


def test_the_ended_session_is_never_told_about_its_own_orphan(fleet: Path) -> None:
    """The orphan is the live session's reading, not an extra row for the gone one."""
    _record_session(ENDED_SESSION, pid=DEAD_PID)
    _queue(fleet, "r-ended", session=ENDED_SESSION)

    payload = obligations_module.obligations(PROJECT, ENDED_SESSION)

    assert _orphan_row(payload, "r-ended") is None, payload["obligations"]
    # The refusal is still the ended session's own duty; the orphan row is what
    # a live session reads instead of nobody reading anything.
    kinds = {
        str(row.get("kind") or "")
        for row in payload["obligations"]
        if str(row.get("run_id") or "") == "r-ended"
    }
    assert obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND in kinds, kinds


def test_a_project_with_no_live_session_tells_the_lane(fleet: Path) -> None:
    """With no live session of the project, a live lane session is told instead."""
    _record_session(ENDED_SESSION, pid=DEAD_PID)
    _record_session(LANE_SESSION, pid=os.getpid())
    _queue(fleet, "r-ended", session=ENDED_SESSION)

    payload = obligations_module.obligations(PROJECT, LANE_SESSION)

    row = _orphan_row(payload, "r-ended")
    assert row is not None, payload["obligations"]
    assert row["ended_session"] == ENDED_SESSION, row


def test_a_session_with_no_harness_record_is_not_read_as_ended(fleet: Path) -> None:
    """An unobserved session keeps its refusal: absence of a record is not death."""
    _record_session(LANE_SESSION, pid=os.getpid())
    _queue(fleet, "r-ended", session="d0a1c2d3-3333-4a2b-9c3d-norecord")

    payload = obligations_module.obligations(PROJECT, LANE_SESSION)

    assert _orphan_row(payload, "r-ended") is None, payload["obligations"]
