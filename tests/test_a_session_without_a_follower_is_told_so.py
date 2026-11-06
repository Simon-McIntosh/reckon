"""A session whose follower has gone is told so, not left a frozen duty.

A session's snapshot was refreshed only while a follower delivered for it, and
the station removed the session's stored row without touching the file the
producer had last written. A coordinator went on reading a duty that said "the
review reflex owns this dispatch", on exactly the session where no reflex would
run again.

Three properties are measured here against a temporary crew home:

* a session whose registration is released, with a delivered run owing a
  review, has its snapshot rewritten on the next publish -- the rewritten one
  carries no reflex claim and names the review dispatch command;
* a later publish with the session still absent leaves that file's mtime
  unchanged, while the export path does run;
* a session whose follower is live keeps today's text, so the rewrite is the
  departure's own record rather than a change to the ordinary reading.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, runs, station_obligations, station_store
from tests.helpers.git import git as _git

PROJECT = "follower-departure-fixture"
SESSION = "follower-departure-session"
RUN_ID = "run-follower-departure"
NODE_ID = "follower-departure-node"
REFLEX_CLAIM = "the review reflex owns this dispatch"
STARTED = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home every path in this module resolves through."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    return root


@pytest.fixture()
def repository(tmp_path: Path, config_home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the follower-departure fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@contextlib.contextmanager
def _armed_follower():
    """One live session registration, held for the life of the block."""
    with runs.follower_registration(PROJECT, SESSION, delivery="stream"):
        yield


def _delivered_run_owing_a_review(repository: Path, tmp_path: Path) -> None:
    """Record one complete run at the tree's head with no stored review."""
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(repository, "rev-parse", "HEAD")
    manifest.write_text(
        f"node: {NODE_ID}\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "process_alive": False,
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": NODE_ID,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _producer() -> dict[str, object]:
    return {
        "pid": os.getpid(),
        "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
        "started_at": STARTED.isoformat(),
        "code_stamp": obligation_snapshot.source_code_stamp(),
    }


def _publish_producer_snapshot(now: datetime) -> None:
    """Write the per-session file the way the producer's sweep does."""
    obligation_snapshot.sweep(
        PROJECT,
        sessions=[SESSION],
        producer=_producer(),
        stream_offset=0,
        now=now,
    )


def _snapshot_text(project: str = PROJECT, session: str = SESSION) -> str:
    document = obligation_snapshot.read_snapshot(project, session)
    assert document is not None, "the session has no snapshot to read"
    return json.dumps(document)


def _review_missing_rows(document: dict) -> list[dict]:
    return [
        row
        for row in document.get("obligations") or ()
        if str(row.get("kind") or "") == "review-missing"
    ]


def test_a_released_follower_leaves_a_snapshot_that_does_not_defer(
    repository: Path, tmp_path: Path, config_home: Path, runtime_root: Path
) -> None:
    """The departure is published, so the frozen reflex claim cannot stand.

    The live arm reproduces the reported state first: with the follower armed,
    the producer's sweep writes a review-missing duty that defers to the
    reflex. Releasing the registration and publishing again must replace that
    with a snapshot naming the command to run.
    """
    _delivered_run_owing_a_review(repository, tmp_path)
    store = station_store.StationStore(runtime_root)
    store.connect()

    with _armed_follower():
        station_obligations.refresh(store, project=PROJECT, changed=True, now=STARTED)
        _publish_producer_snapshot(STARTED)
        live_document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
        assert live_document is not None
        assert REFLEX_CLAIM in _snapshot_text()
        assert [row["kind"] for row in _review_missing_rows(live_document)] == [
            "review-missing"
        ]

    departed = STARTED + timedelta(seconds=30)
    station_obligations.publish(
        store, project=PROJECT, home=config_home, changed=True, now=departed
    )

    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None
    written = json.dumps(document)
    assert REFLEX_CLAIM not in written
    follower = document.get("follower") or {}
    assert follower.get("live") is False
    assert "no follower is delivering" in str(follower.get("statement") or "")
    rows = _review_missing_rows(document)
    assert [row["run_id"] for row in rows] == [RUN_ID]
    for row in rows:
        assert row.get("review_owner") in (None, "")
        command = str(row.get("next_command") or "")
        assert command.startswith("reckon crew dispatch")
        assert "--role review" in command


def test_a_second_publish_leaves_the_absent_sessions_file_exactly_as_written(
    repository: Path, tmp_path: Path, config_home: Path, runtime_root: Path
) -> None:
    """The rewrite is the departure's, not every tick's.

    The second publish is made after the export's minimum interval and after
    the heartbeat refresh window, so the export path does run -- proven by the
    station's own export being rewritten -- and the session's snapshot still
    does not move.
    """
    _delivered_run_owing_a_review(repository, tmp_path)
    store = station_store.StationStore(runtime_root)
    store.connect()

    with _armed_follower():
        station_obligations.refresh(store, project=PROJECT, changed=True, now=STARTED)
        _publish_producer_snapshot(STARTED)

    departed = STARTED + timedelta(seconds=30)
    station_obligations.publish(
        store, project=PROJECT, home=config_home, changed=True, now=departed
    )
    session_path = obligation_snapshot.snapshot_path(PROJECT, SESSION)
    before = session_path.stat()

    later = departed + timedelta(
        seconds=station_obligations.EXPORT_HEARTBEAT_REFRESH_SECONDS
        + station_obligations.EXPORT_MIN_INTERVAL_SECONDS
        + 5
    )
    store.heartbeat(now=later)
    station_snapshot = obligation_snapshot.station_snapshot_path(PROJECT, home=config_home)
    station_before = station_snapshot.stat() if station_snapshot.exists() else None
    published = station_obligations.publish(
        store, project=PROJECT, home=config_home, changed=True, now=later
    )
    assert published == station_snapshot, "the export path did not run"
    station_after = station_snapshot.stat()
    assert station_before is None or station_after.st_mtime_ns != station_before.st_mtime_ns

    after = session_path.stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)


def test_a_live_follower_keeps_the_reflex_duty(
    repository: Path, tmp_path: Path, config_home: Path, runtime_root: Path
) -> None:
    """While a follower delivers, the duty still names the reflex, as today."""
    _delivered_run_owing_a_review(repository, tmp_path)
    store = station_store.StationStore(runtime_root)
    store.connect()

    with _armed_follower():
        station_obligations.refresh(store, project=PROJECT, changed=True, now=STARTED)
        _publish_producer_snapshot(STARTED)
        document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
        assert document is not None
        rows = _review_missing_rows(document)
        assert [row["run_id"] for row in rows] == [RUN_ID]
        assert rows[0].get("review_owner") == "reflex"
        assert "follower" not in document

    # The same text still stands on the next tick while the follower is there.
    with _armed_follower():
        station_obligations.refresh(
            store, project=PROJECT, changed=True, now=STARTED + timedelta(seconds=30)
        )
        assert REFLEX_CLAIM in _snapshot_text()