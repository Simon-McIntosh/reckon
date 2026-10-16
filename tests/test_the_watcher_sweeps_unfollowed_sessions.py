# ruff: noqa: F811 - fixtures imported beside the cases that use them.
"""The project watcher sweeps a session nobody is following.

Once a follower's sweep acts only on the session it serves, a run whose session
armed no follower has no sweeper: its lapsed hold is never resumed and its
finished run is never reviewed by the reflex. The project watcher fills that gap
on each pass it makes, and the two loops a watcher reaches the fleet through --
the single-event ``runs.watch`` a hand-run watch takes and the streaming ticker
a registered producer runs -- both call the one sweep. Each loop is driven here
over the same three-session fleet: a session unattended past the grace window is
swept, with the session passed to ``sweep`` explicitly, and a session inside the
window or owned by another host is left alone.
"""

from __future__ import annotations

import socket
import time
from contextlib import suppress
from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.crew.follower_registration import renew_follower_lease
from reckon.crew.recovery_watch import watch_ticker
from reckon.crew.resumption import writer_sweep_status
from reckon.crew.runs import (
    WATCHER_SWEEP_GRACE_SECONDS,
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
FOREIGN_HOST = "another-login-node"
REFUSED_STREAM = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "backends"
    / "codex-usage-limit.jsonl"
)


class _LoopEndedError(Exception):
    """Raised from a fake sleeper to end a watch loop after its pass."""


def _lapsed_run(
    run_id: str, *, repo: Path, session: str, launcher_host: str | None = None
) -> None:
    """A pointer for a run a provider refusal stopped, owned by one session."""
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_bytes(REFUSED_STREAM.read_bytes())
    tree = directory / "tree"
    tree = directory / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text("node: node-a\nstatus: blocked\n", encoding="utf-8")
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
        "created_at": "2026-09-03T09:00:00Z",
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
    _write_json(pointer_path(run_id), record)


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


def _arrange(config_home, repo, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Three sessions: one unattended past the grace window, two that are not.

    The swept session holds a lapsed hold to resume and a finished run to
    review; the re-arming session holds a scoring run that the same pass must
    leave alone; the third names another host, which sweeps its own runs.
    """
    offered = _stub_review_dispatch(monkeypatch)
    launched = _stub_spawn(monkeypatch)
    _lapsed_run("r-lapsed", repo=repo, session=SWEPT_SESSION)
    _scoring_pointer(
        config_home, repo, "r-scoring", backend=OWNING_BACKEND, session=SWEPT_SESSION
    )
    _scoring_pointer(
        config_home,
        repo,
        "r-late-scoring",
        backend=OWNING_BACKEND,
        session=LATE_SESSION,
    )
    _lapsed_run(
        "r-foreign", repo=repo, session=ELSEWHERE_SESSION, launcher_host=FOREIGN_HOST
    )
    now_seconds = time.time()
    renew_follower_lease(PROJECT, SWEPT_SESSION, now=now_seconds - 11 * 60)
    renew_follower_lease(PROJECT, LATE_SESSION, now=now_seconds - 9 * 60)
    return offered, launched


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


def _assert_only_the_unattended_session_was_swept(
    offered: list[str], launched: list[str]
) -> None:
    """The eleven-minute session was swept; the other two were left alone."""
    assert launched == ["r-lapsed"], launched
    assert offered == ["r-scoring"], offered
    written = writer_sweep_status(PROJECT, WATCHER_SWEEP_WRITER)
    assert written is not None, "the watcher's sweep wrote no writer record"
    assert written["key"] == "watcher"
    assert written["resumed"] == 1, written
    # One run was considered: only the unattended session's were handed to the
    # sweep at all, and the other two sessions' runs are outside that count.
    assert written["checked"] == 1, written


def test_the_single_event_watcher_sweeps_the_unattended_session(
    isolated_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pass of ``runs.watch`` sweeps the eleven-minute session alone."""
    config_home, repo = isolated_project
    offered, launched = _arrange(config_home, repo, monkeypatch)

    event = _sweep_through_watch(PROJECT)

    _assert_only_the_unattended_session_was_swept(offered, launched)
    assert event.get("project") == PROJECT, event
    assert WATCHER_SWEEP_GRACE_SECONDS == 600.0


def test_the_following_loop_sweeps_the_unattended_session(
    isolated_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One tick of the streaming producer sweeps the same session, alone."""
    config_home, repo = isolated_project
    offered, launched = _arrange(config_home, repo, monkeypatch)

    events = _sweep_through_ticker(PROJECT)

    _assert_only_the_unattended_session_was_swept(offered, launched)
    assert events, "the tick published no row for a fleet holding live runs"
