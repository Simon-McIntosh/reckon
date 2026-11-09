"""Promotion leaves pointer access available during checks and waiter release."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import (
    promotion,
    promotion_records,
    queue_admission,
    resumption,
    runs,
    station,
    station_store,
)
from tests import test_a_review_run_promotes_itself as review_fixture


@pytest.fixture
def delivered_review(isolated_reckon_home: Path, tmp_path: Path):
    repository = review_fixture.repository.__wrapped__(isolated_reckon_home, tmp_path)
    review_fixture._write_review_pointer(
        repository,
        tmp_path,
        worker_pid=None,
        record_path=review_fixture._store_review_record(),
    )
    review_fixture._write_exit_record()
    first = runs.read_pointer(review_fixture.REVIEW_RUN_ID)
    first.update(session="session-fixture", launcher_host=socket.gethostname())
    runs._write_json(runs.pointer_path(review_fixture.REVIEW_RUN_ID), first)
    runs._write_json(
        runs.pointer_path("r-waiter"),
        {
            "run_id": "r-waiter",
            "project": review_fixture.PROJECT,
            "repo": str(repository),
            "phase": "awaiting-upstream",
            "session": "session-fixture",
            "launcher_host": socket.gethostname(),
            "waits": [review_fixture.PLAN + "#s2"],
            "node": {
                "id": "waiter",
                "plan": review_fixture.PLAN,
                "section": "s3",
                "role": "implement",
            },
        },
    )
    return repository


@pytest.mark.parametrize("stage", ["checks", "waiter-release"])
def test_a_station_sweep_leaves_the_pointer_lock_available(
    delivered_review: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    held = threading.Event()
    release = threading.Event()
    acquired = threading.Event()
    sweeps: list[threading.Thread] = []
    active: set[tuple[int, str]] = set()
    acquisitions: list[tuple[str, str]] = []
    original_lock = runs._pointer_lock
    original_check = promotion._require_commit_for_changed_manifest
    original_move = queue_admission._resolve_and_move

    @contextmanager
    def observe_lock(run_id):
        with original_lock(run_id):
            key = (threading.get_ident(), run_id)
            active.add(key)
            acquisitions.append((threading.current_thread().name, run_id))
            try:
                yield
            finally:
                active.remove(key)

    def hold():
        held.set()
        assert release.wait(30), "the promotion barrier was never released"

    def check(*args, **kwargs):
        if stage == "checks":
            hold()
        return original_check(*args, **kwargs)

    def move(pointer, **kwargs):
        if stage == "waiter-release" and pointer["run_id"] == "r-waiter":
            hold()
        return original_move(pointer, **kwargs)

    def start(run):
        thread = threading.Thread(target=run, name="station-sweep")
        sweeps.append(thread)
        thread.start()

    def acquire():
        with runs._pointer_lock(review_fixture.REVIEW_RUN_ID):
            acquired.set()

    monkeypatch.setattr(runs, "_pointer_lock", observe_lock)
    monkeypatch.setattr(promotion, "_pointer_lock", observe_lock)
    monkeypatch.setattr(resumption, "_pointer_lock", observe_lock)
    monkeypatch.setattr(promotion, "_require_commit_for_changed_manifest", check)
    monkeypatch.setattr(queue_admission, "_resolve_and_move", move)
    store = station_store.StationStore(tmp_path / "station")
    node = station.Station(
        store,
        home=runs.crew_home(),
        projects=dict,
        sweeps=station.SweepBeat(start=start),
    )
    contender = threading.Thread(target=acquire, name="pointer-contender")
    try:
        node.tick(now=datetime(2026, 11, 9, 12, tzinfo=UTC))
        assert held.wait(30), "the sweep never reached the promotion barrier"
        assert ("station-sweep", review_fixture.REVIEW_RUN_ID) in acquisitions
        started = time.monotonic()
        contender.start()
        assert acquired.wait(5), "pointer acquisition timed out after five seconds"
        elapsed = time.monotonic() - started
        assert elapsed < 1, f"pointer acquisition took {elapsed:.3f} seconds"
        print(f"{stage}: pointer acquisition {elapsed:.6f} seconds")
    finally:
        release.set()
        for thread in sweeps:
            thread.join()
        if contender.ident is not None:
            contender.join()
        store.close()
    assert sweeps, "the station started no sweep thread"
    assert not active
    assert not runs.pointer_path(review_fixture.REVIEW_RUN_ID).exists()


def test_a_resumed_pointer_is_not_removed_by_an_earlier_promotion(
    delivered_review: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = promotion._commit_landing_writes

    def resume_after_commit(**kwargs):
        receipt = original(**kwargs)
        # Another host's flock does not exclude this atomic pointer publication.
        run_id = review_fixture.REVIEW_RUN_ID
        current = runs.read_pointer(run_id)
        runs._write_json(
            runs.pointer_path(run_id),
            {**current, "attempt": 2, "phase": "working", "pid": 12345},
        )
        return receipt

    monkeypatch.setattr(promotion, "_commit_landing_writes", resume_after_commit)
    with pytest.raises(promotion.CrewError, match=r"changed.*promotion"):
        promotion.complete(
            review_fixture.REVIEW_RUN_ID,
            gate="not-run",
            root=delivered_review,
            promoted_by="review-self",
        )
    current = runs.read_pointer(review_fixture.REVIEW_RUN_ID)
    assert current["attempt"] == 2
    assert current["pid"] == 12345


def test_a_local_promotion_holds_its_own_serialisation_lock(
    delivered_review: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    original = promotion._require_commit_for_changed_manifest
    checked = []

    def check(*args, **kwargs):
        path = (
            runs.crew_home()
            / "locks"
            / f"promotion-{review_fixture.REVIEW_RUN_ID}.lock"
        )
        with path.open("a+b") as handle, pytest.raises(BlockingIOError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        checked.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(promotion, "_require_commit_for_changed_manifest", check)
    promotion.complete(
        review_fixture.REVIEW_RUN_ID,
        gate="not-run",
        root=delivered_review,
        promoted_by="review-self",
    )
    assert checked
    with promotion._promotion_lock(review_fixture.REVIEW_RUN_ID):
        pass


def test_withdrawal_releases_workspace_without_holding_the_pointer_lock(
    isolated_reckon_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-withdrawn"
    runs._write_json(runs.pointer_path(run_id), {"run_id": run_id})
    acquired = threading.Event()
    threads = []

    def release(run_id, record):
        def acquire():
            with runs._pointer_lock(run_id):
                acquired.set()

        thread = threading.Thread(target=acquire)
        threads.append(thread)
        thread.start()
        assert acquired.wait(1), "withdrawal held the pointer lock during cleanup"
        return {}

    monkeypatch.setattr(promotion_records, "_release_after_promotion", release)
    try:
        result = promotion.complete(run_id, gate="not-run")
    finally:
        for thread in threads:
            thread.join()
    assert result["withdrawn"]
    assert result["pointer_removed"]


def test_a_suite_refusal_preserves_concurrent_pointer_updates(
    delivered_review: Path,
) -> None:
    run_id = review_fixture.REVIEW_RUN_ID
    record = runs.read_pointer(run_id)
    record["suite_command"] = "pytest {node_test_files}"
    record["node"] = {**record["node"], "write_paths": ["tests/test_pointer.py"]}
    runs._mutate_pointer(
        run_id, lambda current: {**current, "probe_observation": "fresh"}
    )
    with pytest.raises(promotion.ledger.SuiteDeltaError):
        promotion._evaluate_suite_delta(run_id, record, waiver_reason="")
    current = runs.read_pointer(run_id)
    assert current["probe_observation"] == "fresh"
    assert current["suite_delta_refusal"]["status"] == "refused"
