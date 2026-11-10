"""Every review lookup in a producer tick shares the store's file identities."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from contextlib import suppress
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_memo, recovery_watch, review, runs
from tests.helpers.git import git
from tests.test_a_producer_tick_reads_live_pointers_once import (
    tick as tick,  # noqa: PLC0414
)
from tests.test_review_plan_command import project as project  # noqa: PLC0414


class TickFinishedError(Exception):
    """Stop after the producer has completed its next tick."""


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
