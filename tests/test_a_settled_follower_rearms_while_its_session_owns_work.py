"""A settled follower re-arms while its session still owns work.

A session's pane is fed by the one follower its host runs. A follower that finds
its session drained prints one idle line and exits with the settle status, and
``SessionHost.tick`` does not restart it on the backoff -- correctly, because
the pane has already said it is idle. But a run can appear without any request
to the host: an admission that launches a queued request, a resume, a delivery
whose review is still owed. Nothing restarted the pane over those, so a session
whose last action was a resume lost the follower showing the work it had just
restarted, and a delivery went unseen for eighty minutes until an earlier
dispatch happened to re-request the pair.

The cases drive ``SessionHost.tick`` directly. Spawning and the exit wait are
the only two places the host touches a real process, so a stub replaces them and
carries a child's settle on demand; everything between them -- the settle
judgement, the live-work read, the census -- is the host's own code under test.
The live view is stated by a case rather than written as pointer files, except
for the case that exercises the default view, which reads a temporary crew home.

The control is the behaviour this replaces: left to their next request, settled
children are never restarted, so the working and queued cases spawn nothing
after the settle. That is the mutation the negative-control run applies.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from reckon.crew import runs, session_host

PROJECT = "proj"
SESSION = "sess-owner"
OTHER_SESSION = "sess-peer"

WORKING_RUN = "r-owns-a-working-run"
QUEUED_RUN = "r-holds-a-queued-request"
OTHER_RUN = "r-belongs-to-a-peer-session"

# The wait status a child that settled leaves behind.
SETTLED_STATUS = session_host.SETTLED_EXIT_CODE << 8


@pytest.fixture(autouse=True)
def isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew-home read at a temporary tree.

    The host reads a follower registration and, through its default view, the
    live pointers, both resolved from the environment of the process running
    the case rather than from the mapping handed to the host. Moving
    ``RECKON_HOME`` is what keeps a case off a real fleet.
    """
    home = tmp_path / "crew-home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    yield home
    landed = [
        path
        for path in (
            Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew" / "live",
        )
        for run_id in (WORKING_RUN, QUEUED_RUN, OTHER_RUN)
        if (path / f"{run_id}.json").exists()
    ]
    assert not landed, f"the real crew home carries this case's run pointers: {landed}"


class StubHost(session_host.SessionHost):
    """A host whose children are pids this case hands out and ends on demand."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.spawned: list[int] = []
        self.exits: dict[int, int | None] = {}
        self._next_pid = 9_000

    def _spawn(self, child: session_host._Child) -> int | None:
        self._next_pid += 1
        self.spawned.append(self._next_pid)
        return self._next_pid

    def _poll_exit(self, child: session_host._Child) -> int | None:
        return self.exits.pop(child.pid, None)

    def settle(self, pid: int) -> None:
        """Make this host read the child as having settled on its next tick."""
        self.exits[pid] = SETTLED_STATUS

    def child(self) -> session_host._Child:
        return self._children[(PROJECT, SESSION)]


def _host(tmp_path: Path, view: Callable[[str, str], str | None]) -> StubHost:
    """A host fenced to a temporary record and log directory, with a stated view."""
    return StubHost(
        owner={"pid": os.getpid(), "start_time": "a-state-time-for-the-stub"},
        follower_argv=["a-stub-follower"],
        environ={
            session_host.STATE_DIR_ENV: str(tmp_path / "state"),
            session_host.LOG_DIR_ENV: str(tmp_path / "log"),
            session_host.POLL_ENV: "0.05",
            session_host.BACKOFF_ENV: "0.0",
        },
        live_view=view,
    )


def _only_this_session(view_run: str | None) -> Callable[[str, str], str | None]:
    """A view that names one run for this case's pair and nothing for any other."""

    def view(project: str, session: str) -> str | None:
        return view_run if (project, session) == (PROJECT, SESSION) else None

    return view


def _request(host: StubHost) -> None:
    """The owner dispatches the pair, so its request carries the ownership mark."""
    host.handle(
        json.dumps({"project": PROJECT, "session": SESSION, "owner_checked": True})
    )


def _settled_over_a_run(tmp_path: Path, run_id: str) -> StubHost:
    """Run one child to a settle over a session that owns ``run_id``.

    The first tick shows the running child is left alone; the second carries
    the settle and whatever the view says. The count of spawns is what each case
    reads, because a re-armed pair is a second child and nothing else is.
    """
    host = _host(tmp_path, _only_this_session(run_id))
    _request(host)
    assert len(host.spawned) == 1, "the request must start one child"
    pid = host.child().pid
    host.tick()
    assert len(host.spawned) == 1, "a running child is not restarted"
    host.settle(pid)
    host.tick()
    return host


def test_a_child_settled_over_a_working_run_is_spawned_again(tmp_path: Path) -> None:
    """The session owns a run in flight, published with no request to the host."""
    host = _settled_over_a_run(tmp_path, WORKING_RUN)

    assert len(host.spawned) == 2, "the settled child must be spawned again"
    child = host.child()
    assert child.pid is not None, "the pair runs a fresh child"
    assert child.settled is False
    assert child.settled_at is None
    assert child.rearmed_for == WORKING_RUN, "the census names the run that caused it"
    recorded = json.loads(host.record_path.read_text(encoding="utf-8"))
    assert recorded["children"] == [
        {
            "project": PROJECT,
            "session": SESSION,
            "pid": child.pid,
            "settled_at": None,
            "rearmed_for": WORKING_RUN,
            "owner_checked": True,
        }
    ], "the census the fleet reads must carry the run that re-armed the pair"


def test_a_child_settled_over_a_queued_request_is_spawned_again(
    tmp_path: Path,
) -> None:
    """A queued request is work owed the pane as much as a run in flight."""
    host = _settled_over_a_run(tmp_path, QUEUED_RUN)

    assert len(host.spawned) == 2, "the settled child must be spawned again"
    assert host.child().rearmed_for == QUEUED_RUN


def test_a_child_settled_with_nothing_owed_stays_settled(tmp_path: Path) -> None:
    """A session owning nothing keeps its pane idle, and repeats no idle line."""
    host = _settled_over_a_run(tmp_path, None)

    assert len(host.spawned) == 1, "a session owning nothing spawns nothing"
    child = host.child()
    assert child.pid is None
    assert child.settled is True
    assert child.settled_at is not None
    assert child.rearmed_for is None
    # Further ticks with nothing owed change nothing either: the check runs
    # every tick, so a settle is not a one-shot decision that a later tick would
    # undo by restarting the host's ordinary backoff path.
    host.tick()
    host.tick()
    assert len(host.spawned) == 1


def test_the_default_view_rearms_over_its_own_sessions_live_pointer(
    tmp_path: Path,
) -> None:
    """The rule the host applies is the fleet's own, read from the live pointers.

    The first reading is the known-present control: a working pointer whose
    session is not this pane's is a live pointer the reader sees and must not
    count, so the pair staying idle is a statement about ownership rather than
    about an instrument that found no pointer directory at all.
    """
    runs._write_json(
        runs.pointer_path(OTHER_RUN),
        {
            "run_id": OTHER_RUN,
            "project": PROJECT,
            "session": OTHER_SESSION,
            "phase": "working",
        },
    )
    assert runs.pointer_path(OTHER_RUN).exists(), "the pointer the control reads exists"
    assert session_host.live_work_run(PROJECT, SESSION) is None, (
        "another session's working run is not this session's work"
    )

    runs._write_json(
        runs.pointer_path(WORKING_RUN),
        {
            "run_id": WORKING_RUN,
            "project": PROJECT,
            "session": SESSION,
            "phase": "queued",
        },
    )
    assert session_host.live_work_run(PROJECT, SESSION) == WORKING_RUN

    host = _host(tmp_path, session_host.live_work_run)
    _request(host)
    pid = host.child().pid
    host.settle(pid)
    host.tick()

    assert len(host.spawned) == 2, "a live pointer of its own session re-arms the pair"
    assert host.child().rearmed_for == WORKING_RUN
