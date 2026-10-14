"""A follower registered on another host is judged from a fact that travels.

A login-node producer unit found the project's seat held on a compute node and
its own hook still printed "no producer": the producer publishes a session's
obligations snapshot only for the follower registrations that read ``live``, and
``live`` probed the *producer host's* process table for the follower's recorded
pids. A registration written on another host names a pid issued by a kernel this
host cannot query, so its absence here was read as death and the session was
dropped from publication. This module pins the repair: a registration carries
the host it was written on, a registration naming another host is never put to
this host's process table, and its liveness is the one fact that crosses the
shared home -- the advisory lock the follower holds for as long as it delivers.

The fixture synthesises the fleet under a temporary configuration home and
drives the producer's sweeps explicitly. A foreign registration is written by
holding its lock here and stamping another host into the record, which is the
state a reader on the producer's host meets; the second consumer is the review
reflex, which reads the same ``follower_state`` the producer's snapshot
selection does. The real configuration home is asserted untouched.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import (
    obligation_snapshot,
    recovery_watch,
    runs,
    watch_unit,
)
from reckon.hooks import coordinator_obligations
from tests.helpers.git import git as _git

PROJECT = "snapshot-foreign-fixture"
SESSION_HERE = "session-on-this-host"
SESSION_FOREIGN = "session-on-another-host"
FOREIGN_HOST = "98dci4-clu-9900"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# A pid no process can be holding: the maximum Linux pid value is below this, so
# the recorded pid is dead in this host's table by construction. A foreign
# registration naming it must still read live, which is the whole point -- the
# local table is not consulted for a pid another kernel issued.
DEAD_LOCAL_PID = 2_147_483_647


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A synthesised project under a temporary configuration home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    mounts = config_home / "mounts.json"
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the foreign-session fixture"),
    ):
        _git(root, *arguments)
    mounts.write_text(json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8")
    return {"home": config_home, "repo": root}


@contextlib.contextmanager
def _foreign_registration(
    project: str, session: str, *, host: str = FOREIGN_HOST, pid: int = DEAD_LOCAL_PID
) -> Iterator[dict[str, Any]]:
    """Hold a registration whose record names another host, as a follower there wrote it.

    The lock is held by this process for the block, which is the fact the
    producer's host reads across the shared home; the pid is a dead one so a
    passing read cannot be the local table answering.
    """
    import fcntl

    path = runs.follower_lock_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    record = {
        "project": project,
        "session": session,
        "pid": pid,
        "pid_start_time": "0",
        "parent_pid": pid,
        "delivery": "stream",
        "scope": {},
        "started_at": "2026-10-14T00:00:00Z",
        "host": host,
    }
    runs._write_watch_record(handle, record)
    try:
        yield record
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _write_pointer(
    fleet: dict[str, Path], run_id: str, *, session: str, status: str = "complete"
) -> None:
    """Write one live pointer and the manifest its delivery is read from."""
    root = fleet["repo"]
    manifest = fleet["home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(root, "rev-parse", "HEAD")
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: [{head}]\n", encoding="utf-8"
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "repo": str(root),
            "worktree": str(root),
            "base_sha": head,
            "process_alive": False,
            "phase": "complete",
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": run_id,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _wait_for_sweep() -> None:
    """Wait for the producer's sweep thread to finish publishing."""
    for _ in range(600):
        producer = runs._WATCH_STREAM_PRODUCERS.get(PROJECT)
        thread = None if producer is None else producer.sweep_thread
        if thread is None:
            return
        thread.join(timeout=0.1)
    raise AssertionError("the sweep did not settle")


def test_a_follower_on_another_host_is_published_and_the_hook_reads_it(
    fleet: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Both sessions get a fresh snapshot, and the second session's hook reads one.

    A follower on this host and one on another are registered against one
    producer. The producer publishes a snapshot for each because each reads
    live; the hook run as the second session finds its snapshot fresh and prints
    the checklist rather than the no-producer line.
    """
    _write_pointer(fleet, "r-foreign-scoring", session=SESSION_FOREIGN)
    with (
        _foreign_registration(PROJECT, SESSION_FOREIGN),
        runs.follower_registration(PROJECT, SESSION_HERE, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        _wait_for_sweep()
        here = obligation_snapshot.read_snapshot(PROJECT, SESSION_HERE)
        foreign = obligation_snapshot.read_snapshot(PROJECT, SESSION_FOREIGN)
        assert here is not None, "the producer publishes for the local session"
        assert foreign is not None, (
            "the producer publishes for the session whose follower is on another host"
        )
        assert (
            obligation_snapshot.freshness(foreign, project=PROJECT)
            == obligation_snapshot.FRESH
        )

        capsys.readouterr()
        coordinator_obligations._prompt(
            {
                "session_id": SESSION_FOREIGN,
                "cwd": str(fleet["repo"]),
                "hook_event_name": "UserPromptSubmit",
            }
        )
        emitted = capsys.readouterr().out

    assert coordinator_obligations.AUTHORITY_LINE in emitted, emitted
    assert "no producer" not in emitted, emitted


def test_the_review_reflex_dispatches_for_a_foreign_session(
    fleet: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scoring run whose session's follower is elsewhere gets its review.

    The reflex's own guard is the same ``runs.follower_state`` the producer
    reads, so the fix reaches it without a second rule: with the foreign
    registration held, the run is dispatched rather than reported awaiting its
    coordinator.
    """
    pointer = {
        "run_id": "r-scoring",
        "project": PROJECT,
        "session": SESSION_FOREIGN,
        "repo": str(fleet["repo"]),
        "worktree": str(fleet["repo"]),
    }
    monkeypatch.setattr(recovery_watch, "list_live", lambda: [pointer])
    monkeypatch.setattr(
        recovery_watch,
        "classify_pointer",
        lambda p: {"run_id": p["run_id"], "classification": "scoring"},
    )
    monkeypatch.setattr(
        recovery_watch, "_derive_missing_manifest", lambda p, config=None: p
    )
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda run_id, config=None: pointer,
    )
    launches: list[str] = []

    def launch(record: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        launches.append(record["run_id"])
        return {
            "run_id": record["run_id"],
            "dispatched": True,
            "review_run_id": "rev-1",
        }

    monkeypatch.setattr(recovery_watch, "dispatch_review_for_run", launch)

    with _foreign_registration(PROJECT, SESSION_FOREIGN):
        report = recovery_watch.recover(project=PROJECT, dispatch_reviews=True)

    assert launches == ["r-scoring"], report
    assert report["reviews_awaiting_coordinator"] == [], report
    assert report["reviews_dispatched"] == ["rev-1"], report


def test_a_registration_records_the_host_it_was_written_on(
    fleet: dict[str, Path],
) -> None:
    """The registration carries its host, spelled as a producer seat spells one."""
    with runs.follower_registration(PROJECT, SESSION_HERE, delivery="stream"):
        state = runs.follower_state(PROJECT, SESSION_HERE)

    assert state["live"] is True
    assert state["foreign"] is False
    assert state["host"] == socket.gethostname()
    record = state["follower"]
    # One host judgement serves seats and followers alike: the row reads the
    # registration through the same helper a producer seat is read through, so
    # the two can never answer differently about one host.
    assert watch_unit._seat_host(record) == socket.gethostname()
    assert watch_unit._seat_host(record) == runs._seat_host(record)


def test_a_foreign_registration_is_judged_by_its_lock_not_this_hosts_table(
    fleet: dict[str, Path],
) -> None:
    """A pid this host cannot see is unknown, not dead; the lock decides.

    The record names a pid no process holds, so a judgement that consulted the
    local table would read it dead. The held lock is the fact that crosses the
    shared home, so the registration reads live; when the lock is released the
    same record reads gone.
    """
    assert runs.follower_state(PROJECT, SESSION_FOREIGN)["registered"] is False

    with _foreign_registration(PROJECT, SESSION_FOREIGN):
        held = runs.follower_state(PROJECT, SESSION_FOREIGN)
        assert held["foreign"] is True
        assert held["host"] == FOREIGN_HOST
        # The row's host judgement is the seat judgement, not a second one.
        assert held["host"] == watch_unit._seat_host(held["follower"])
        assert held["foreign"] is watch_unit._seat_names_a_foreign_host(
            held["follower"]
        )
        assert held["live"] is True, held
        # The producer's selection and the reflex's guard read this one value.
        rows = {row["session"]: row for row in runs.list_followers(PROJECT)}
        assert rows[SESSION_FOREIGN]["live"] is True
        assert (
            watch_unit.watch_state(PROJECT, session=SESSION_FOREIGN)["session_attached"]
            is True
        )

    gone = runs.follower_state(PROJECT, SESSION_FOREIGN)
    assert gone["foreign"] is True
    assert gone["registered"] is False
    assert gone["released"] is True
    assert gone["live"] is False, gone


def test_the_real_crew_home_is_untouched(fleet: dict[str, Path]) -> None:
    """The fixture writes only under its temporary home."""
    real_home = Path.home() / ".config" / "reckon"
    before = sorted(p.name for p in real_home.iterdir()) if real_home.is_dir() else []
    _write_pointer(fleet, "r-untouched", session=SESSION_FOREIGN)
    with (
        _foreign_registration(PROJECT, SESSION_FOREIGN),
        runs.follower_registration(PROJECT, SESSION_HERE, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        _wait_for_sweep()
    after = sorted(p.name for p in real_home.iterdir()) if real_home.is_dir() else []
    assert after == before
    assert obligation_snapshot.snapshot_dir(PROJECT).is_relative_to(fleet["home"])
