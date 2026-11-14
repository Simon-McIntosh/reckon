"""A quiet fleet's seat holder keeps its sessions' snapshots fresh, and says who wrote a stale one.

A session's snapshot carries an age, so publishing a fresh one becomes due on
the clock. The producer's other two triggers both read a document: a pointer
transition, and a file under the project's state or plan trees. A fleet that is
simply quiet moves neither, and the crew home's fingerprint the ticker gates its
fleet listing on moves no faster, so the obligations sweep was reachable only
through a change. A healthy seat holder therefore went on drawing rows while
its sessions' snapshots aged out of the freshness window, and the hook could
report only the age of the reading, never who held the seat.

These cases pin both halves: a beat on a quiet fleet republishes the snapshot
within one producer interval without listing the fleet again, and a snapshot
that is still stale names the seat holder's pid, host and last write. A third
case holds the crew home's fingerprint still so the change gate opens once and
shuts, and shows a later beat reaching the publisher anyway. The negative
control removes the beat's own publication: the same beats then leave the
snapshot where it was, ageing past the freshness window, so the first case
cannot pass on some other trigger.

Every case synthesises its fleet under a temporary configuration home and
drives the ticker in this process, so no case reads or writes the operator's
own state.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from reckon.crew import (
    ledger,
    model_catalogue,
    obligation_snapshot,
    plan_review,
    recovery,
    recovery_review_acceptance,
    recovery_review_dispatch,
    recovery_watch,
    resumption,
    runs,
)
from tests.helpers.git import git as _git

PROJECT = "obligation-freshness-fixture"
SESSION = "obligation-freshness-session"
FRESHNESS = obligation_snapshot.FRESHNESS_WINDOW_SECONDS
FLOOR = obligation_snapshot.FLOOR_TICK_SECONDS


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A synthesised project, its mount, and one live pointer."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the obligations fixture"),
    ):
        _git(root, *arguments)
    (root / "reckon").mkdir()
    (root / "reckon" / "fixture_runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "reckon/fixture_runtime.py")
    _git(root, "commit", "-q", "-m", "test: seed a runtime source")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _write_pointer(root, config_home, "r-quiet", phase="working", status="working")
    return {"home": config_home, "repo": root}


def _write_pointer(
    root: Path, home: Path, run_id: str, *, phase: str, status: str
) -> None:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(root, "rev-parse", "HEAD")
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(root),
            "worktree": str(root),
            "base_sha": head,
            "process_alive": False,
            "phase": phase,
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


def _publishing_sweep(
    project: str,
    *,
    sessions: Any,
    producer: Any = None,
    stream_offset: int = 0,
    transition_fired: bool = False,
    state_dirs: Any = (),
    now: Any = None,
) -> list[Path]:
    """Publish one snapshot per session, standing in for the derivation.

    The case is about the clock reaching the publisher, not about what the
    derivation computes, so the slice is a minimal document written through the
    module's own writer. Every trigger above it — the ticker's beat, the
    producer's thread, the live-follower filter — runs for real, and the file
    the hook reads is a real snapshot whose age the case can assert on.
    """
    instant = datetime.now(tz=UTC) if now is None else now
    written: list[Path] = []
    for session in sessions:
        document = {
            "computed_at": instant.isoformat(),
            "stream_offset": stream_offset,
            "producer": dict(producer or {}),
            "obligations": [],
        }
        written.append(obligation_snapshot.write_snapshot(project, session, document))
    return written


class _BeatStreamClosedError(Exception):
    """End a synthetic producer after its last beat, as a real seat's stop would."""


def _beats(count: int):
    """A sleeper that lets ``count`` beats pass and then ends the producer."""
    seen = [0]

    def sleeper(_interval: float) -> None:
        seen[0] += 1
        if seen[0] >= count:
            raise _BeatStreamClosedError

    return sleeper


def _wait_for_sweep() -> None:
    """Wait for the producer's sweep thread to finish publishing."""
    for _ in range(600):
        producer = runs._WATCH_STREAM_PRODUCERS.get(PROJECT)
        thread = None if producer is None else producer.sweep_thread
        if thread is None:
            return
        thread.join(timeout=0.1)
    raise AssertionError("the sweep did not settle")


def _quiet_ticker(monkeypatch: pytest.MonkeyPatch):
    """A ticker whose fleet stays still, so only the clock can trigger a publish."""
    producer = runs._WatchStreamProducer(
        path=runs.watch_stream_path(PROJECT), known={}, stall_window="10m"
    )
    monkeypatch.setitem(runs._WATCH_STREAM_PRODUCERS, PROJECT, producer)

    @contextmanager
    def registration(*args: Any, **kwargs: Any):
        yield True, {}

    monkeypatch.setattr(recovery_watch, "_watch_registration", registration)
    monkeypatch.setattr(recovery_watch, "_refuse_unresolvable_watch", lambda _: None)
    monkeypatch.setattr(runs, "renew_watch_host_lease", lambda _: True)
    monkeypatch.setattr(
        recovery_watch, "update_watch_registration", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        recovery_watch.quota_weight, "backend_rate_statuses", lambda **kw: {}
    )
    monkeypatch.setattr(model_catalogue, "check_model_catalogue", lambda _: None)
    monkeypatch.setattr(recovery_watch, "_sweep_lift_rows", lambda *a: {})
    monkeypatch.setattr(recovery_watch, "_sweep_reset_rows", lambda *a: set())
    monkeypatch.setattr(recovery_watch, "_sweep_gate_rows", lambda *a: {})
    monkeypatch.setattr(ledger, "read_records", lambda *a, **kw: ([], 0))
    monkeypatch.setattr(plan_review, "list_plan_reviews", lambda *a, **kw: [])
    monkeypatch.setattr(plan_review, "_project_plan_paths", lambda _: ([], []))
    monkeypatch.setattr(plan_review, "compose_owed_plan_reviews", lambda *a, **kw: [])
    monkeypatch.setattr(recovery, "dispatch_review_for_run", lambda *a, **kw: {})
    monkeypatch.setattr(
        recovery_review_acceptance, "classify_pointer", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        runs, "list_followers", lambda project: [{"session": SESSION, "live": True}]
    )
    monkeypatch.setattr(obligation_snapshot, "sweep", _publishing_sweep)
    monkeypatch.setattr(runs, "sweep_unfollowed_sessions", lambda *a, **kw: [])
    # The clock block's own cadence is zero, so every beat is a clock beat and
    # the case does not wait thirty real seconds for the second one.
    monkeypatch.setattr(recovery_watch, "WATCHER_SWEEP_INTERVAL_SECONDS", 0.0)
    # The fleet listing is pinned: the case is about what happens when nothing
    # moves, so a listing here would be the trigger it means to exclude.
    records = runs._list_live_records(project=PROJECT)
    listing = Mock(return_value=records)
    for module in (
        runs,
        recovery_watch,
        resumption,
        recovery_review_dispatch,
        recovery_review_acceptance,
    ):
        monkeypatch.setattr(module, "list_live", listing)
    return listing


def _age_the_snapshot(session: str, *, seconds: float) -> None:
    """Rewrite one session's snapshot as published ``seconds`` ago."""
    document = obligation_snapshot.read_snapshot(PROJECT, session)
    assert document is not None, "the fixture's first beat must publish a baseline"
    document["computed_at"] = (
        datetime.now(tz=UTC) - timedelta(seconds=seconds)
    ).isoformat()
    obligation_snapshot.write_snapshot(PROJECT, session, document)
    stale = time.time() - seconds
    os.utime(obligation_snapshot.snapshot_path(PROJECT, session), (stale, stale))


def _age_seconds(session: str) -> float:
    document = obligation_snapshot.read_snapshot(PROJECT, session)
    assert document is not None
    age = obligation_snapshot.snapshot_age_seconds(document)
    assert age is not None
    return float(age)


def test_a_quiet_beat_republishes_the_session_snapshot(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still fleet's seat holder refreshes the snapshot on its own clock.

    The fleet listing is pinned to a fixed answer, so the change gate opens once
    and never again; the snapshot can then only be refreshed by the beat itself.
    The age asserted is the document's own, which is what the hook reads.
    """
    listing = _quiet_ticker(monkeypatch)
    with runs.follower_registration(PROJECT, SESSION, delivery="stream"):
        stream = recovery_watch.watch_ticker(
            PROJECT, poll_interval=0, sleeper=_beats(8)
        )
        try:
            next(stream)
            _wait_for_sweep()
            _age_the_snapshot(SESSION, seconds=FRESHNESS + 60)
            assert _age_seconds(SESSION) > FRESHNESS

            with pytest.raises(_BeatStreamClosedError):
                list(stream)
            _wait_for_sweep()
        finally:
            stream.close()

        assert listing.call_count <= 1, (
            "the fleet listing must not be what refreshes a quiet beat"
        )

    assert _age_seconds(SESSION) <= FRESHNESS, (
        "a quiet beat must republish within one producer interval"
    )


def test_the_negative_control_leaves_the_snapshot_ageing(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the beat's publication removed, the same beats publish nothing.

    This is the control for the case above: the only difference is the repair
    itself, so the snapshot's age holding still is what proves the first case
    measured the repair rather than some other trigger.
    """
    monkeypatch.setattr(runs, "publish_obligations_on_beat", lambda project: False)
    _quiet_ticker(monkeypatch)
    with runs.follower_registration(PROJECT, SESSION, delivery="stream"):
        stream = recovery_watch.watch_ticker(
            PROJECT, poll_interval=0, sleeper=_beats(8)
        )
        try:
            next(stream)
            _wait_for_sweep()
            # The control removes the beat's publication, so a quiet first
            # beat publishes nothing at all; the baseline it ages is written
            # here, through the module's own writer, exactly as a producer's
            # first sweep would.
            _publishing_sweep(PROJECT, sessions=[SESSION])
            _age_the_snapshot(SESSION, seconds=FRESHNESS + 60)
            with pytest.raises(_BeatStreamClosedError):
                list(stream)
            _wait_for_sweep()
        finally:
            stream.close()

    assert _age_seconds(SESSION) > FRESHNESS, (
        "with the repair removed the snapshot must age past the interval"
    )


def test_the_publisher_is_reached_on_a_beat_the_change_gate_skips(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged change token must not stand between a beat and the publish.

    The gate that lists the fleet opens only when the crew home's fingerprint
    moves, which a quiet fleet never does. Pinning that fingerprint to one value
    makes the case the reported condition exactly: the first beat opens the gate
    and every later beat is skipped by it. The second beat must still reach the
    publisher, and what it publishes must be a snapshot newer than the one the
    case aged before it.
    """
    listing = _quiet_ticker(monkeypatch)
    monkeypatch.setattr(
        recovery_watch, "_crew_home_change_token", lambda project, pointers: "still"
    )
    published: list[str] = []
    starts_sweep = runs.publish_obligations_on_beat

    def counted(project: str) -> bool:
        published.append(project)
        return starts_sweep(project)

    monkeypatch.setattr(runs, "publish_obligations_on_beat", counted)

    with runs.follower_registration(PROJECT, SESSION, delivery="stream"):
        stream = recovery_watch.watch_ticker(
            PROJECT, poll_interval=0, sleeper=_beats(3)
        )
        try:
            next(stream)
            _wait_for_sweep()
            at_first_beat = len(published)
            assert at_first_beat == 1, "the first beat reaches the publisher once"

            _age_the_snapshot(SESSION, seconds=FRESHNESS + 60)
            assert _age_seconds(SESSION) > FRESHNESS

            with pytest.raises(_BeatStreamClosedError):
                list(stream)
            _wait_for_sweep()
        finally:
            stream.close()

    assert len(published) > at_first_beat, (
        "a beat the change gate skips must still reach the publisher"
    )
    assert listing.call_count == 1, (
        "the gate must have opened once and then stayed shut, or the case is not quiet"
    )
    assert _age_seconds(SESSION) <= FRESHNESS, (
        "the publish on the later beat must refresh the snapshot"
    )


def _plant_seat_record(*, pid: int, host: str) -> None:
    path = runs.watch_registration_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "pid": pid,
                "host": host,
                "lease_renewed_at": time.time(),
                "stall_window": "10m",
            }
        ),
        encoding="utf-8",
    )


def test_a_stale_snapshot_names_the_seat_holder_and_its_last_write(
    fleet: dict[str, Any],
) -> None:
    """The stale reading names the pid and host that hold the seat, and the write.

    A coordinator reading that its snapshot is hours old needs to know whose
    write it was reading: its own seat holder on this host, or a producer
    elsewhere. The line states the holder and the age of the last write, which
    is the snapshot's own computed instant.
    """
    from reckon.hooks.coordinator_obligations import not_fresh_line

    _plant_seat_record(pid=4242, host="seat-holder-node.invalid")
    document = {
        "computed_at": (datetime.now(tz=UTC) - timedelta(seconds=8308)).isoformat(),
        "stream_offset": 0,
        "producer": {"pid": 4242},
        "obligations": [],
    }

    line = not_fresh_line(
        obligation_snapshot.STALE_SNAPSHOT,
        project=PROJECT,
        session=SESSION,
        document=document,
    )

    assert "the snapshot is 8308s old" in line, line
    assert "pid 4242" in line, line
    assert "seat-holder-node.invalid" in line, line
    assert "last wrote it" in line, line
    assert "8308s ago" in line, line


def test_a_stale_snapshot_with_no_seat_record_names_no_holder(
    fleet: dict[str, Any],
) -> None:
    """With no registration to read, the line says only what it knows."""
    from reckon.hooks.coordinator_obligations import not_fresh_line

    document = {
        "computed_at": (datetime.now(tz=UTC) - timedelta(seconds=900)).isoformat(),
        "stream_offset": 0,
        "producer": {"pid": 4242},
        "obligations": [],
    }

    line = not_fresh_line(
        obligation_snapshot.STALE_SNAPSHOT,
        project=PROJECT,
        session=SESSION,
        document=document,
    )

    assert "the snapshot is 900s old" in line, line
    assert "seat holder" not in line, line
