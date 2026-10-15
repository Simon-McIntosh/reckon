"""A follower on another node is judged by the lease it renews, not its lock.

``flock`` is node-local on this shared home, so the lock a follower holds on its
own node reads free from the producer's node, and the producer then publishes no
snapshot for the session and dispatches no review. This module pins the repair:
a registration naming another host is judged by a lease the follower renews
beside its registration, and the lease decides the whole verdict — a fresh lease
is registered, live and not released, a stale or missing one has gone.

The fixture synthesises the fleet under a temporary configuration home. The real
configuration home is asserted untouched.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import follower_registration, obligation_snapshot, runs
from tests.helpers.git import git as _git

PROJECT = "lease-foreign-fixture"
SESSION_HERE = "session-on-this-host"
SESSION_FOREIGN = "session-on-another-host"
FOREIGN_HOST = "98dci4-clu-9901"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# A pid no process can hold: the recorded pid is dead in this host's table by
# construction, so a passing read cannot be this host's process table answering.
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
        ("commit", "-q", "-m", "test: seed the lease fixture"),
    ):
        _git(root, *arguments)
    mounts.write_text(json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8")
    return {"home": config_home, "repo": root}


def _write_foreign_registration(
    project: str, session: str, *, host: str = FOREIGN_HOST, pid: int = DEAD_LOCAL_PID
) -> None:
    """Write a registration naming another host, its advisory lock left free."""
    path = runs.follower_lock_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        runs._write_watch_record(
            handle,
            {
                "project": project,
                "session": session,
                "pid": pid,
                "pid_start_time": "0",
                "parent_pid": pid,
                "delivery": "stream",
                "scope": {},
                "started_at": "2026-10-15T00:00:00Z",
                "host": host,
            },
        )


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


def test_a_follower_on_another_node_is_registered_live_and_published(
    fleet: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A free lock and a fresh lease reads live, and the producer publishes.

    The registration names another host and holds no lock here, which is the
    state the producer's host meets. The lease is fresh, so the session reads
    registered, live and not released, and the producer's sweep publishes a
    snapshot for it.
    """
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", "600")
    _write_pointer(fleet, "r-foreign-lease", session=SESSION_FOREIGN)
    _write_foreign_registration(PROJECT, SESSION_FOREIGN)
    follower_registration.renew_follower_lease(PROJECT, SESSION_FOREIGN)

    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired is True
        _wait_for_sweep()

    state = runs.follower_state(PROJECT, SESSION_FOREIGN)
    assert state["registered"] is True, state
    assert state["live"] is True, state
    assert state["released"] is False, state
    assert (
        runs.watch_state(PROJECT, session=SESSION_FOREIGN)["session_follower_released"]
        is False
    )
    published = obligation_snapshot.read_snapshot(PROJECT, SESSION_FOREIGN)
    assert published is not None, (
        "the producer publishes for a session whose follower is on another node"
    )


def test_a_foreign_registration_past_its_lease_window_reads_gone(
    fleet: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renewal older than the window is a follower that has gone, unpublished.

    With the renewal moved past the producer's own window, the registration
    reads gone and the producer's sweep publishes no snapshot for its session.
    """
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", "600")
    _write_pointer(fleet, "r-stale-lease", session=SESSION_FOREIGN)
    _write_foreign_registration(PROJECT, SESSION_FOREIGN)
    follower_registration.renew_follower_lease(
        PROJECT, SESSION_FOREIGN, now=time.time() - 100000.0
    )

    state = runs.follower_state(PROJECT, SESSION_FOREIGN)
    assert state["registered"] is False, state
    assert state["live"] is False, state
    assert state["released"] is True, state

    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired is True
        _wait_for_sweep()

    assert obligation_snapshot.read_snapshot(PROJECT, SESSION_FOREIGN) is None


def test_a_missing_lease_reads_gone_even_with_a_record_present(
    fleet: dict[str, Path],
) -> None:
    """A record with no lease beside it is a foreign follower that has stopped."""
    _write_foreign_registration(PROJECT, SESSION_FOREIGN)
    state = runs.follower_state(PROJECT, SESSION_FOREIGN)
    assert state["foreign"] is True, state
    assert state["registered"] is False, state
    assert state["live"] is False, state
    assert state["released"] is True, state


def test_a_same_host_registration_with_a_free_lock_still_reads_gone(
    fleet: dict[str, Path],
) -> None:
    """A registration on this host keeps its lock and pid probes, not a lease.

    A fresh lease beside a same-host record changes nothing: the lock is what
    answers a registration this host issued, and a free lock is a follower that
    has gone.
    """
    path = runs.follower_lock_path(PROJECT, SESSION_HERE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        runs._write_watch_record(
            handle,
            {
                "project": PROJECT,
                "session": SESSION_HERE,
                "pid": DEAD_LOCAL_PID,
                "pid_start_time": "0",
                "parent_pid": DEAD_LOCAL_PID,
                "delivery": "stream",
                "scope": {},
                "started_at": "2026-10-15T00:00:00Z",
                "host": socket.gethostname(),
            },
        )
    follower_registration.renew_follower_lease(PROJECT, SESSION_HERE)

    state = runs.follower_state(PROJECT, SESSION_HERE)
    assert state["foreign"] is False, state
    assert state["registered"] is False, state
    assert state["live"] is False, state


def test_a_renewal_is_never_read_empty_while_a_reader_polls(
    fleet: dict[str, Path],
) -> None:
    """A reader on another node never sees a half-written or empty lease.

    The lease is published by a rename, so a reader either sees the previous
    record or the new one. A writer renewing in a loop and a reader polling from
    a second thread must therefore never find the lease empty or unparseable.
    """
    _write_foreign_registration(PROJECT, SESSION_FOREIGN)
    lock_path = runs.follower_lock_path(PROJECT, SESSION_FOREIGN)
    follower_registration.renew_follower_lease(PROJECT, SESSION_FOREIGN, now=1000.0)

    stop = threading.Event()
    errors: list[str] = []

    def renew_loop() -> None:
        tick = 1000.0
        while not stop.is_set():
            tick += 1.0
            follower_registration.renew_follower_lease(
                PROJECT, SESSION_FOREIGN, now=tick
            )

    writer = threading.Thread(target=renew_loop, daemon=True)
    writer.start()
    try:
        for _ in range(2000):
            renewed = follower_registration._follower_lease_renewed_at(lock_path)
            if renewed is None:
                errors.append("the lease read empty")
                break
    finally:
        stop.set()
        writer.join(timeout=2.0)

    assert errors == []


def test_a_same_node_registration_holding_its_lock_reads_live_after_a_renewal(
    fleet: dict[str, Path],
) -> None:
    """Renewing the lease does not disturb the lock judgement for this host.

    A registration this host issued and still holds reads live on its lock, and
    a renewal written beside it leaves that verdict untouched.
    """
    with runs.follower_registration(PROJECT, SESSION_HERE, delivery="stream"):
        follower_registration.renew_follower_lease(PROJECT, SESSION_HERE)
        state = runs.follower_state(PROJECT, SESSION_HERE)

    assert state["foreign"] is False, state
    assert state["registered"] is True, state
    assert state["live"] is True, state
    assert state["released"] is False, state
    lease = follower_registration.follower_lease_path(PROJECT, SESSION_HERE)
    assert lease.is_file()


def test_the_real_crew_home_is_untouched(fleet: dict[str, Path]) -> None:
    """The fixture writes only under its temporary home."""
    real_home = Path.home() / ".config" / "reckon"
    before = sorted(p.name for p in real_home.iterdir()) if real_home.is_dir() else []
    _write_foreign_registration(PROJECT, SESSION_FOREIGN)
    follower_registration.renew_follower_lease(PROJECT, SESSION_FOREIGN)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired is True
        _wait_for_sweep()
    after = sorted(p.name for p in real_home.iterdir()) if real_home.is_dir() else []
    assert after == before
    assert obligation_snapshot.snapshot_dir(PROJECT).is_relative_to(fleet["home"])
