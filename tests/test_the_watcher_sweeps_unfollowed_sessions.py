# ruff: noqa: F811 - fixtures imported beside the cases that use them.
"""The project watcher sweeps a session nobody is following.

Once a follower's sweep acts only on the session it serves, a run whose session
armed no follower has no sweeper: its lapsed hold is never resumed and its
finished run is never reviewed by the reflex. The project watcher fills that gap
on each pass it makes, and the two loops a watcher reaches the fleet through --
the single-event ``runs.watch`` a hand-run watch takes and the streaming ticker
a registered producer runs -- both call the one sweep. Each loop is driven here
over the same eight-session fleet, and each must sweep the same three sessions
and leave the same five alone:

* the eleven-minute session has had no follower and no run activity for longer
  than the grace window, and is swept;
* the nine-minute session is still inside it, and is not;
* the third session's pointer names another host, which sweeps its own runs;
* the fourth has a live registration, held the way
  ``follower_registration`` judges one, so it is not swept whatever its ages
  say -- its lease is unwritten, its registration file is eleven minutes old
  and its own run is as old as the swept session's;
* the fifth never armed a follower at all and dispatched two minutes ago, so it
  is not swept: the grace window is measured from its own newest pointer;
* the sixth never armed a follower either and dispatched eleven minutes ago, so
  it is swept;
* the seventh and eighth carry a registration released two hours ago -- the
  file a release leaves behind -- with the seventh dispatching two minutes ago
  and the eighth eleven, so the released stamp does not sweep the session that
  has just acted and does sweep the one that has not.
"""

from __future__ import annotations

import fcntl
import os
import socket
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import pytest

from reckon.crew import runs
from reckon.crew.follower_registration import (
    _write_watch_record,
    follower_lock_path,
    renew_follower_lease,
)
from reckon.crew.recovery_watch import watch_ticker
from reckon.crew.resumption import writer_sweep_status
from reckon.crew.runs import (
    WATCHER_SWEEP_WRITER,
    _write_json,
    pointer_path,
    run_dir,
)
from tests.test_follow_sweep_is_session_scoped import _absent_pid
from tests.test_review_lane_follows_its_run import (  # noqa: F401 - fixtures
    OWNING_BACKEND,
    _scoring_pointer,
    isolated_project,
)

PROJECT = "sample"
SWEPT_SESSION = "session-unfollowed"
LATE_SESSION = "session-rearming"
ELSEWHERE_SESSION = "session-elsewhere"
ATTENDED_SESSION = "session-attended"
FRESH_SESSION = "session-fresh-dispatch"
STALE_SESSION = "session-stale-dispatch"
RELEASED_FRESH_SESSION = "session-released-fresh"
RELEASED_STALE_SESSION = "session-released-stale"
FOREIGN_HOST = "another-login-node"
LONG_AGO = 11 * 60
WITHIN_GRACE = 2 * 60
RELEASED_AGE = 2 * 60 * 60
REFUSED_STREAM = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "backends"
    / "codex-usage-limit.jsonl"
)


class _LoopEndedError(Exception):
    """Raised from a fake sleeper to end a watch loop after its pass."""


def _ago(seconds: float) -> str:
    """An ISO-8601 UTC instant ``seconds`` before now."""
    return datetime.fromtimestamp(time.time() - seconds, tz=UTC).isoformat()


def _age_run(run_id: str, *, seconds: float) -> None:
    """Move every file a run left behind to ``seconds`` before now.

    The run's evidence is aged together -- stream, pointer and manifest -- so
    the one clock that reads them (stream, then pointer, then creation) answers
    with the same age whichever branch it takes.
    """
    when = time.time() - seconds
    directory = run_dir(run_id)
    if directory.is_dir():
        for path in [directory, *directory.rglob("*")]:
            with suppress(OSError):
                os.utime(path, (when, when))
    pointer = pointer_path(run_id)
    with suppress(OSError):
        os.utime(pointer, (when, when))


def _lapsed_run(
    run_id: str,
    *,
    repo: Path,
    session: str,
    launcher_host: str | None = None,
    idle_seconds_ago: float | None = None,
) -> None:
    """A pointer for a run a provider refusal stopped, owned by one session.

    ``idle_seconds_ago`` dates the run's own last act -- the stamp it carries
    and the stream, pointer and manifest it left -- which is what a session is
    measured by when its follower has not been seen recently.
    """
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_bytes(REFUSED_STREAM.read_bytes())
    tree = directory / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text("node: node-a\nstatus: blocked\n", encoding="utf-8")
    dispatched_at = (
        "2026-09-03T09:00:00Z" if idle_seconds_ago is None else _ago(idle_seconds_ago)
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "role": "implement",
        "session": session,
        "created_at": dispatched_at,
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "phase": "working",
        "launcher_host": launcher_host or socket.gethostname(),
        "pid": _absent_pid(),
        "node": {
            "id": run_id.rsplit("-", 1)[-1],
            "plan": "plan-a",
            "section": "§7",
            "time_budget": "30m",
            "write_paths": ["reckon/one.py"],
        },
    }
    path = pointer_path(run_id)
    _write_json(path, record)
    if idle_seconds_ago is not None:
        _age_run(run_id, seconds=idle_seconds_ago)


def _hold_a_live_registration(
    project: str, session: str, *, age_seconds: float
) -> IO[bytes]:
    """Register a follower that reads live, with an aged file behind it.

    The advisory lock is held for as long as the returned handle is open, and
    the process it names is this one, so ``follower_registration`` judges the
    registration live. Its lease is left unwritten and the registration file's
    mtime is moved into the past, so every age beside it says the opposite.
    """
    path = follower_lock_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    _write_watch_record(
        handle,
        {
            "project": project,
            "session": session,
            "pid": os.getpid(),
            "parent_pid": os.getpid(),
            "delivery": "stream",
            "started_at": _ago(age_seconds),
        },
    )
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    when = time.time() - age_seconds
    os.utime(path, (when, when))
    return handle


def _release_a_registration(project: str, session: str, *, age_seconds: float) -> None:
    """Leave the file a released registration keeps, stamped in the past.

    A release drops the advisory lock and keeps the file, so the registration
    reads ``released`` and its mtime is the only stamp beside it -- the stamp a
    session that arms a follower again after a release would otherwise be
    measured by.
    """
    path = follower_lock_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        _write_watch_record(
            handle,
            {
                "project": project,
                "session": session,
                "pid": _absent_pid(),
                "delivery": "stream",
                "started_at": _ago(age_seconds),
            },
        )
    when = time.time() - age_seconds
    os.utime(path, (when, when))


def _stub_review_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record which runs the review reflex offers a dispatch to."""
    offered: list[str] = []

    def record(pointer, **_kwargs):
        offered.append(str(pointer.get("run_id") or ""))
        return {
            "run_id": pointer.get("run_id"),
            "dispatched": True,
            "review_run_id": "r-review-of-" + str(pointer.get("run_id")),
        }

    monkeypatch.setattr(
        "reckon.crew.recovery_review_acceptance.dispatch_review_for_run", record
    )
    return offered


def _stub_spawn(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for the supervisor a resume leaves through, recording the run."""
    launched: list[str] = []

    def spawn(plan, *, log_path, prompt_path, **_kwargs):
        launched.append(Path(log_path).parent.name)
        return _absent_pid()

    monkeypatch.setattr("reckon.crew.resumption._spawn", spawn)
    return launched


def _arrange(config_home, repo, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Eight sessions: three past the grace window, five that are not.

    The unattended session holds a lapsed hold to resume and a finished run to
    review; so does the session nobody ever registered, and the session whose
    registration was released. The re-arming session holds a run the same pass
    must leave alone, the third names another host, the fourth is attended by a
    live registration, and the fifth and sixth dispatched too recently for the
    grace window to have run out -- one with no registration at all and one
    with a registration released two hours ago. Each session left alone holds a
    lapsed run, so a sweep that wrongly took one would show up as a launch. The
    fourth is left alone by its live registration and nothing else: its lease is
    unwritten, its registration file is eleven minutes old and its own run is as
    old as the swept session's, so a pass that stopped asking about liveness
    would sweep it.
    """
    offered = _stub_review_dispatch(monkeypatch)
    launched = _stub_spawn(monkeypatch)
    _lapsed_run("r-lapsed", repo=repo, session=SWEPT_SESSION)
    _scoring_pointer(
        config_home, repo, "r-scoring", backend=OWNING_BACKEND, session=SWEPT_SESSION
    )
    _lapsed_run("r-late-scoring", repo=repo, session=LATE_SESSION)
    _lapsed_run(
        "r-foreign", repo=repo, session=ELSEWHERE_SESSION, launcher_host=FOREIGN_HOST
    )
    _lapsed_run(
        "r-attended", repo=repo, session=ATTENDED_SESSION, idle_seconds_ago=LONG_AGO
    )
    _lapsed_run(
        "r-fresh", repo=repo, session=FRESH_SESSION, idle_seconds_ago=WITHIN_GRACE
    )
    _lapsed_run("r-stale", repo=repo, session=STALE_SESSION, idle_seconds_ago=LONG_AGO)
    _lapsed_run(
        "r-released-fresh",
        repo=repo,
        session=RELEASED_FRESH_SESSION,
        idle_seconds_ago=WITHIN_GRACE,
    )
    _lapsed_run(
        "r-released-stale",
        repo=repo,
        session=RELEASED_STALE_SESSION,
        idle_seconds_ago=LONG_AGO,
    )
    now_seconds = time.time()
    renew_follower_lease(PROJECT, SWEPT_SESSION, now=now_seconds - LONG_AGO)
    renew_follower_lease(PROJECT, LATE_SESSION, now=now_seconds - 9 * 60)
    held = _hold_a_live_registration(PROJECT, ATTENDED_SESSION, age_seconds=LONG_AGO)
    _release_a_registration(PROJECT, RELEASED_FRESH_SESSION, age_seconds=RELEASED_AGE)
    _release_a_registration(PROJECT, RELEASED_STALE_SESSION, age_seconds=RELEASED_AGE)
    # The swept session's own evidence is as old as its lapsed lease, so the
    # pass has nothing recent of its own to read it as attended by.
    _age_run("r-lapsed", seconds=LONG_AGO)
    _age_run("r-scoring", seconds=LONG_AGO)
    return offered, launched, held


def _end(_interval: float) -> None:
    """A sleeper that ends the loop rather than waiting on it."""
    raise _LoopEndedError


def _sweep_through_watch(project: str) -> dict:
    """One pass of the single-event watcher, ending as soon as it reports."""
    try:
        return runs.watch(project, exit_on_empty=True, poll_interval=0.0, sleeper=_end)
    except _LoopEndedError:
        return {}


def _sweep_through_ticker(project: str) -> list[dict]:
    """One pass of the streaming ticker, ending at its first sleep."""
    events: list[dict] = []
    with suppress(_LoopEndedError):
        events.extend(watch_ticker(project, poll_interval=0.0, sleeper=_end))
    return events


@pytest.mark.parametrize("driver", ["single-event", "streaming"])
def test_the_watcher_sweeps_only_the_sessions_nobody_follows(
    isolated_project, monkeypatch: pytest.MonkeyPatch, driver: str
) -> None:
    """Either watcher sweeps the two unattended sessions and leaves the rest."""
    config_home, repo = isolated_project
    offered, launched, held = _arrange(config_home, repo, monkeypatch)
    assert not held.closed, "the attended session's registration was released"
    # The premise of the case: the attended registration really does read live,
    # so it is the liveness predicate and not an age that leaves it alone.
    attended = runs.follower_state(PROJECT, ATTENDED_SESSION)
    assert attended["live"], attended
    # The other premise: the two released sessions really do carry the file a
    # release leaves, two hours old, so it is the session's own freshness and
    # not the registration that separates the two cases below.
    released = runs.follower_state(PROJECT, RELEASED_FRESH_SESSION)
    assert released["released"] and not released["registered"], released
    assert released["live"] is False, released

    if driver == "single-event":
        event = _sweep_through_watch(PROJECT)
        assert event.get("project") == PROJECT, event
    else:
        events = _sweep_through_ticker(PROJECT)
        assert events, "the tick published no row for a fleet holding live runs"

    # The three sessions past the grace window were resumed -- including the one
    # whose registration was released two hours ago and whose own newest run is
    # eleven minutes old -- and none of the runs of the five sessions left alone
    # was touched.
    assert sorted(launched) == ["r-lapsed", "r-released-stale", "r-stale"], launched
    # The reflex reviews a session's runs only for the session handed to it: the
    # unattended session's scoring run is offered, and the re-arming session's
    # is not.
    assert offered == ["r-scoring"], offered
    written = writer_sweep_status(PROJECT, WATCHER_SWEEP_WRITER)
    assert written is not None, "the watcher's sweep wrote no writer record"
    assert written["key"] == "watcher"
    # The status is keyed by writer rather than accumulated across the sessions
    # one pass sweeps, so it carries the last session handed to the sweep -- one
    # run considered, and one lapsed hold resumed out of it. What each session
    # contributed is on ``launched`` above.
    assert written["checked"] == 1, written
    assert written["resumed"] == 1, written
