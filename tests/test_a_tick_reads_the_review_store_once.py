"""Every review lookup in a producer tick shares the store's file identities."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from reckon import file_memo, flight, ledger
from reckon.crew import (
    model_catalogue,
    plan_review,
    recovery,
    recovery_memo,
    recovery_review_acceptance,
    recovery_review_dispatch,
    recovery_watch,
    resumption,
    review,
    runs,
)
from tests.helpers.git import git
from tests.test_review_plan_command import project as project  # noqa: PLC0414


class TickFinishedError(Exception):
    """Stop after the producer has completed its next tick."""


@pytest.fixture
def tick(project, monkeypatch):
    """A producer tick over a synthetic crew home, driven one tick at a time.

    The producer's own readers are replaced so the tick measures the review
    store's reads alone: every other sweep that would reach a service, a model
    catalogue or the network answers empty. The fixture is built here rather
    than shared, so this measurement keeps its own harness and a reader can see
    every input the counted tick was given.
    """
    _home, repo, plan = project
    edited = time.time() - 11 * 60
    os.utime(plan, (edited, edited))
    monkeypatch.setattr(runs, "PRODUCER_TICK_SECONDS", 120.0, raising=False)
    created = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    for index in range(40):
        run_id = f"r-author-{index:02}"
        runs._write_json(
            runs.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": "sample",
                "repo": str(repo),
                "session": f"author-{index % 4}",
                "phase": "working",
                "created_at": created,
                "node": {"id": f"author-{index}", "plan": "fixture"},
            },
        )
        stale = time.time() - 3600
        os.utime(runs.pointer_path(run_id), (stale, stale))
    producer = runs._WatchStreamProducer(
        path=runs.watch_stream_path("sample"), known={}, stall_window="10m"
    )
    # Obligations publish on their own thread; the synchronous tick is measured
    # here with that independent writer already holding its sweep lock.
    producer.sweep_lock.acquire()
    monkeypatch.setitem(runs._WATCH_STREAM_PRODUCERS, "sample", producer)

    @contextmanager
    def registration(*args, **kwargs):
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
    monkeypatch.setattr(
        plan_review, "_project_plan_paths", lambda _: ([("fixture", plan)], [])
    )
    compose = Mock(wraps=plan_review.compose_owed_plan_reviews)
    monkeypatch.setattr(plan_review, "compose_owed_plan_reviews", compose)
    dispatch = Mock(return_value={"run_id": "r-composed", "dispatched": True})
    monkeypatch.setattr(recovery, "dispatch_review_for_run", dispatch)

    listing = Mock(wraps=runs.list_live)
    for module in (
        runs,
        recovery_watch,
        resumption,
        recovery_review_dispatch,
        recovery_review_acceptance,
    ):
        monkeypatch.setattr(module, "list_live", listing)
    publish = Mock(wraps=runs._publish_watch_stream)
    monkeypatch.setattr(runs, "_publish_watch_stream", publish)
    resolve = Mock(wraps=flight.resolve)
    monkeypatch.setattr(flight, "resolve", resolve)
    return listing, publish, resolve, dispatch, producer, compose


@pytest.fixture
def reviewed_store(tick, project, monkeypatch):
    _home, repo, _plan = project
    head = git(repo, "rev-parse", "HEAD").strip()
    directory = review.review_store_root() / "sample"
    directory.mkdir(parents=True)
    paths = {}
    for index in range(40):
        run_id = f"r-author-{index:02}"
        # Both correctly filed and misfiled records must use the same listing.
        name = run_id if index % 2 else f"reviewer-{index:02}"
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "project": "sample",
                    "reviewed_run_id": run_id,
                    "review_run_id": f"review-of-{run_id}",
                    "reviewed_head_sha": head,
                    "status": "parsed",
                    "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
                    "absent": [],
                    "total": 18 * len(review.REVIEW_DIMENSIONS),
                }
            )
        )
        pointer = recovery.read_pointer(run_id)
        run_directory = runs.run_dir(run_id)
        run_directory.mkdir(parents=True, exist_ok=True)
        manifest = run_directory / "manifest.md"
        log = run_directory / "worker.jsonl"
        log.write_text("")
        pointer["log_path"] = str(log)
        manifest.write_text("status: complete\ncommits: []\noutside_scope: none\n")
        pointer["manifest_path"] = str(manifest)
        runs._write_json(runs.pointer_path(run_id), pointer)
        paths[run_id] = path
    counts = Counter()
    original_scan = os.scandir

    def counted_scan(path):
        if not isinstance(path, int) and Path(path) == directory:
            counts["enumerations"] += 1
        return original_scan(path)

    original_stat = os.stat
    original_path_stat = Path.stat

    def count_stat(kind, path):
        if not isinstance(path, int):
            candidate = Path(os.fsdecode(path))
            if candidate.parent == directory and candidate.suffix == ".json":
                counts[f"stat:{kind}:{candidate.name}"] += 1

    def counted_stat(path, *args, **kwargs):
        count_stat("os", path)
        return original_stat(path, *args, **kwargs)

    def counted_path_stat(path, *args, **kwargs):
        count_stat("path", path)
        return original_path_stat(path, *args, **kwargs)

    original_read = review.read_review
    bindings = []
    observed = {}

    def counted_read(project, run_id, **kwargs):
        counts[f"read:{run_id}"] += 1
        result = original_read(project, run_id, **kwargs)
        if result is not None:
            observed[run_id] = result
        return result

    for module in list(sys.modules.values()):
        if module is None or not (
            module.__name__.startswith("reckon.") or module.__name__ in {"os", "posix"}
        ):
            continue
        for name, value in list(vars(module).items()):
            if value is original_scan:
                monkeypatch.setattr(module, name, counted_scan)
                bindings.append(f"{module.__name__}.{name}")
            elif value is original_stat:
                monkeypatch.setattr(module, name, counted_stat)
                bindings.append(f"{module.__name__}.{name}")
            elif value is original_path_stat:
                monkeypatch.setattr(module, name, counted_path_stat)
                bindings.append(f"{module.__name__}.{name}")
            elif value is original_read:
                monkeypatch.setattr(module, name, counted_read)
                bindings.append(f"{module.__name__}.{name}")
    monkeypatch.setattr(Path, "stat", counted_path_stat)
    bindings.append("pathlib.Path.stat")
    control = paths["r-author-00"]
    control.stat()
    os.stat(control)
    assert counts[f"stat:path:{control.name}"] == 1
    assert counts[f"stat:os:{control.name}"] == 2
    print(json.dumps({"stat_positive_control": dict(counts)}))
    counts.clear()
    assert "os.stat" in bindings
    assert "pathlib.Path.stat" in bindings
    assert "reckon.crew.review.read_review" in bindings
    assert "os.scandir" in bindings
    return paths, counts, bindings, observed


def _ticks(paths, counts, change=None):
    def after_tick(_interval):
        raise TickFinishedError

    counts.clear()
    stream = recovery_watch.watch_ticker("sample", poll_interval=0, sleeper=after_tick)
    try:
        events = [next(stream) for _ in paths]
        assert {event["run_id"] for event in events} == set(paths)
        first = dict(counts)
        if change:
            change()
        counts.clear()
        with suppress(TickFinishedError):
            next(stream)
        second = dict(counts)
    finally:
        stream.close()
    print(json.dumps({"ticks": [first, second]}, sort_keys=True))
    assert len(paths) == 40
    return first, second


def test_one_listing_answers_every_run_and_an_unchanged_tick_reads_no_review(
    reviewed_store,
):
    paths, counts, bindings, _observed = reviewed_store
    first, second = _ticks(paths, counts)
    print(json.dumps({"bindings": bindings}))
    assert first == {
        "enumerations": 1,
        **dict.fromkeys((f"read:{run_id}" for run_id in paths), 1),
    }, "the instrument must see every run's review exactly once on the first tick"
    assert second == {"enumerations": 1}


def test_an_unchanged_tick_stats_no_review_file_per_run(reviewed_store):
    paths, counts, bindings, _observed = reviewed_store
    _first, second = _ticks(paths, counts)
    per_run_stats = {
        key: value for key, value in second.items() if key.startswith("stat:")
    }
    print(
        json.dumps(
            {"stat_bindings": bindings, "unchanged_per_run_stats": per_run_stats}
        )
    )
    assert not per_run_stats, (
        "the unchanged tick must use the store snapshot's stat readings"
    )


def test_an_in_place_rewrite_is_read_on_the_next_tick(reviewed_store):
    paths, counts, _bindings, observed = reviewed_store
    run_id = "r-author-00"
    path = paths[run_id]

    def rewrite():
        inode = path.stat().st_ino
        directory_identity = recovery_memo._directory_identity(path.parent)
        record = json.loads(path.read_text())
        record["scores"]["goal_fidelity"] = 5
        path.write_text(json.dumps(record))
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        assert path.stat().st_ino == inode
        assert recovery_memo._directory_identity(path.parent) == directory_identity

    first, second = _ticks(paths, counts, rewrite)
    assert first["enumerations"] == 1
    assert second == {"enumerations": 1, f"read:{run_id}": 1}
    assert observed[run_id]["scores"]["goal_fidelity"] == 5


def test_an_atomic_replacement_rereads_only_its_run(reviewed_store):
    paths, counts, _bindings, observed = reviewed_store
    run_id = "r-author-01"
    path = paths[run_id]

    def replace():
        record = json.loads(path.read_text())
        record["scores"]["goal_fidelity"] = 6
        replacement = path.with_suffix(".pending")
        replacement.write_text(json.dumps(record))
        replacement.replace(path)

    first, second = _ticks(paths, counts, replace)
    assert first["enumerations"] == 1
    assert second == {"enumerations": 1, f"read:{run_id}": 1}
    assert observed[run_id]["scores"]["goal_fidelity"] == 6


@pytest.mark.parametrize("changed_input", ["manifest", "gate_log"])
def test_annotation_inputs_change_the_next_tick_answer(reviewed_store, changed_input):
    paths, counts, _bindings, observed = reviewed_store
    run_id = "r-author-00"
    directory = runs.run_dir(run_id)
    manifest = directory / "manifest.md"
    base_log = directory / "base.log"
    head_log = directory / "head.log"
    failed_log = directory / "failed.log"
    failure_id = "tests/test_gate.py::test_signal"
    clean = "1 passed\n"
    failed = f"FAILED {failure_id} - AssertionError\n1 failed\n"
    base_log.write_text(clean)
    head_log.write_text(clean)
    failed_log.write_text(failed)

    def manifest_text(head_name):
        return (
            "status: complete\ncommits: []\noutside_scope: none\n"
            f"baseline_suite: {json.dumps({'log_path': base_log.name})}\n"
            f"after_suite: {json.dumps({'log_path': head_name})}\n"
        )

    manifest.write_text(manifest_text(head_log.name))
    record_identity = recovery_memo._file_identity(paths[run_id])

    def rewrite():
        assert observed[run_id]["added_failure_count"] == 0
        assert observed[run_id]["total"] == 18 * len(review.REVIEW_DIMENSIONS)
        if changed_input == "manifest":
            manifest.write_text(manifest_text(failed_log.name))
        else:
            head_log.write_text(failed)
        assert recovery_memo._file_identity(paths[run_id]) == record_identity

    _first, second = _ticks(paths, counts, rewrite)
    assert second == {"enumerations": 1, f"read:{run_id}": 1}
    assert observed[run_id]["added_failure_count"] == 1
    assert observed[run_id]["added_failure_ids"] == [failure_id]
    assert observed[run_id]["total"] == review.ADDED_FAILURES_TOTAL_CAP


def test_a_live_runs_growing_stream_reads_no_review(reviewed_store):
    """A worker's stream is not an input of its stored-review answer.

    A live run appends to its own stream every tick. That file is an input of
    the run's liveness reading, not of the review stored against it, so a tick
    whose only move is the stream must answer every run's review from the
    previous tick's read.
    """
    paths, counts, _bindings, _observed = reviewed_store
    logs = {run_id: runs.run_dir(run_id) / "worker.jsonl" for run_id in paths}

    def append_streams():
        for index, log in enumerate(logs.values()):
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "text", "n": index}) + "\n")

    first, second = _ticks(paths, counts, append_streams)
    assert first == {
        "enumerations": 1,
        **dict.fromkeys((f"read:{run_id}" for run_id in paths), 1),
    }
    assert second == {"enumerations": 1}


def test_two_threads_missing_at_once_raise_nothing(reviewed_store, monkeypatch):
    """The review reflex reads on its own thread, beside the tick's own read.

    Two readers that miss at the same instant must both answer and leave the
    store usable. The holding is the store's locked LRU rather than a
    dictionary whose eviction is a check followed by an act, so a reader that
    arrives while another is evicting cannot raise out of a producer tick.
    """
    paths, _counts, _bindings, _observed = reviewed_store
    run_id = "r-author-00"
    # A cache only two entries deep makes every store in the race evict.
    monkeypatch.setattr(file_memo, "_MAX_ENTRIES", 2)
    readers = 8
    barrier = threading.Barrier(readers, timeout=30)
    answers = {}
    failures = []

    def read(index):
        def compute():
            barrier.wait()
            return {"index": index}

        try:
            answers[index] = recovery_memo._read_review_once(
                {"project": "sample", "run_id": run_id},
                ("head", f"head-{index}"),
                compute,
            )
        except BaseException as exc:  # noqa: BLE001 - the whole point of the case
            failures.append(exc)

    threads = [threading.Thread(target=read, args=(index,)) for index in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert not failures, failures
    assert len(paths) == 40
    assert answers == {index: {"index": index} for index in range(readers)}

    def refuse():
        raise AssertionError("a held answer must be served without recomputing")

    def recompute():
        return {"recomputed": True}

    monkeypatch.setattr(file_memo, "_MAX_ENTRIES", 64)
    later = recovery_memo._read_review_once(
        {"project": "sample", "run_id": run_id}, ("head", "head-later"), recompute
    )
    assert later == {"recomputed": True}
    assert recovery_memo._read_review_once(
        {"project": "sample", "run_id": run_id}, ("head", "head-later"), refuse
    ) == {"recomputed": True}, (
        "the store must serve what the race stored without recomputing"
    )
