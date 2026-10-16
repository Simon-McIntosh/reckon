# ruff: noqa: F811 - fixtures imported beside the cases that use them.
"""The project watcher sweeps a session nobody is following.

Once a follower's sweep acts only on the session it serves, a run whose session
armed no follower has no sweeper: its lapsed hold is never resumed and its
finished run is never reviewed by the reflex. The project watcher fills that gap
on its tick, sweeping each session with live runs that has had no live follower
for the grace window and passing that session to ``sweep`` explicitly. Without
the explicit session the reflex reads no filter and reviews every session's
runs, so the same tick that sweeps one session must leave the others alone.
"""

from __future__ import annotations

import socket
from datetime import timedelta
from pathlib import Path

import pytest

from reckon.crew.follower_registration import renew_follower_lease
from reckon.crew.resumption import writer_sweep_status
from reckon.crew.runs import (
    WATCHER_SWEEP_GRACE_SECONDS,
    WATCHER_SWEEP_WRITER,
    _write_json,
    pointer_path,
    run_dir,
    sweep_unfollowed_sessions,
)
from tests.test_follow_sweep_is_session_scoped import (
    _absent_pid,
    _clock_at,
    _Launcher,
    _stated_reset,
)
from tests.test_review_lane_follows_its_run import (  # noqa: F401 - fixtures
    CONFIG,
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


def _lapsed_run(
    run_id: str, *, repo: Path, session: str, launcher_host: str | None = None
) -> None:
    """A pointer for a run a provider refusal stopped, owned by one session."""
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_bytes(REFUSED_STREAM.read_bytes())
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


def test_the_watcher_sweeps_only_the_session_unattended_past_the_grace_window(
    isolated_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An eleven-minute session is swept; a nine-minute one and a foreign one are not."""
    config_home, repo = isolated_project
    after = _stated_reset() + timedelta(minutes=1)
    _clock_at(monkeypatch, after)
    offered = _stub_review_dispatch(monkeypatch)

    # The swept session: a lapsed hold to resume and a finished run to review.
    _lapsed_run("r-lapsed", repo=repo, session=SWEPT_SESSION)
    _scoring_pointer(
        config_home, repo, "r-scoring", backend=OWNING_BACKEND, session=SWEPT_SESSION
    )
    # The session re-arming inside the grace window: its scoring run must be left.
    _scoring_pointer(
        config_home,
        repo,
        "r-late-scoring",
        backend=OWNING_BACKEND,
        session=LATE_SESSION,
    )
    # A session whose pointer names another host: left for that host to sweep.
    _lapsed_run(
        "r-foreign", repo=repo, session=ELSEWHERE_SESSION, launcher_host=FOREIGN_HOST
    )

    now_seconds = after.timestamp()
    renew_follower_lease(PROJECT, SWEPT_SESSION, now=now_seconds - 11 * 60)
    renew_follower_lease(PROJECT, LATE_SESSION, now=now_seconds - 9 * 60)

    launcher = _Launcher()
    report = sweep_unfollowed_sessions(
        PROJECT, launcher=launcher, config=CONFIG, now=after
    )

    assert [item["session"] for item in report["swept"]] == [SWEPT_SESSION]
    skipped = {item["session"]: item["reason"] for item in report["skipped"]}
    assert skipped == {
        LATE_SESSION: "follower-rearm-within-grace",
        ELSEWHERE_SESSION: "pointer-names-another-host",
    }

    resumed = [entry["run_id"] for entry in report["swept"][0]["resumed"]]
    assert resumed == ["r-lapsed"], report["swept"][0]
    assert len(launcher.calls) == 1, "the lapsed run was not handed to the launcher"

    # The review reflex was confined to the swept session: the re-arming
    # session's scoring run is never offered, which is the whole failure the
    # explicit session prevents.
    assert offered == ["r-scoring"], offered

    # The sweep records the watcher as its writer, so a reader can tell the
    # watcher's pass apart from an anonymous other caller.
    written = writer_sweep_status(PROJECT, WATCHER_SWEEP_WRITER)
    assert written is not None, "the watcher's sweep wrote no writer record"
    assert written["key"] == "watcher"

    assert WATCHER_SWEEP_GRACE_SECONDS == 600.0
