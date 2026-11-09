"""A producer shares file identities within a tick and rereads them next tick."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

import pytest

from reckon.crew import recovery_memo, recovery_watch, review
from tests.helpers.git import git
from tests.test_a_producer_tick_reads_live_pointers_once import (
    tick as tick,  # noqa: PLC0414
)
from tests.test_review_plan_command import project as project  # noqa: PLC0414


class TickFinishedError(Exception):
    """Stop the synthetic producer after its next quiet tick."""


@pytest.fixture
def reviewed_tick(tick, project, monkeypatch):
    _home, repo, _plan = project
    committed = review.committed_review_root("sample")
    assert committed is not None
    paths = {}
    for index in range(40):
        run_id = f"r-author-{index:02}"
        path = committed / review.COMMITTED_RUN_DIRNAME / run_id / "review.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "project": "sample",
                    "reviewed_run_id": run_id,
                    "review_run_id": f"review-of-{run_id}",
                    "status": "parsed",
                    "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
                    "absent": [],
                    "total": 18 * len(review.REVIEW_DIMENSIONS),
                }
            )
        )
        paths[run_id] = path
    git(repo, "add", "docs/state/sample/reviews")
    git(
        repo,
        "commit",
        "-q",
        "-m",
        "test: seed run reviews",
        "-m",
        "Give every synthetic live run a committed review record.",
    )
    counts = Counter()
    original = recovery_memo._review_input_identities

    def counted(record):
        run_id = record["run_id"]
        counts[run_id] += 1
        identities = original(record)
        assert str(paths[run_id]) in identities, (
            "the counter must see committed records"
        )
        return identities

    bindings = []
    for module in list(sys.modules.values()):
        if module is not None and module.__name__.startswith("reckon."):
            for name, value in list(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, name, counted)
                    bindings.append(f"{module.__name__}.{name}")
    assert "reckon.crew.recovery_memo._review_input_identities" in bindings
    keys = {}
    reuse_key = recovery_watch._snapshot_reuse_key

    def remember_key(record):
        key = reuse_key(record)
        keys[record["run_id"]] = key
        return key

    monkeypatch.setattr(recovery_watch, "_snapshot_reuse_key", remember_key)
    return counts, keys, paths, bindings


def test_one_producer_tick_computes_review_inputs_once_per_run(reviewed_tick):
    counts, _keys, paths, bindings = reviewed_tick
    stream = recovery_watch.watch_ticker("sample", poll_interval=0)
    try:
        events = [next(stream) for _ in paths]
    finally:
        stream.close()
    print(json.dumps({"calls": dict(counts), "bindings": bindings}, sort_keys=True))
    assert {event["run_id"] for event in events} == set(paths)
    assert counts == dict.fromkeys(paths, 1)


def test_an_in_place_review_rewrite_changes_the_next_tick_key(reviewed_tick):
    counts, keys, paths, _bindings = reviewed_tick

    def stop(_interval):
        raise TickFinishedError

    stream = recovery_watch.watch_ticker("sample", poll_interval=0, sleeper=stop)
    try:
        for _ in paths:
            next(stream)
        before_keys = dict(keys)
        before_counts = counts.copy()
        path = paths["r-author-00"]
        directory_identity = recovery_memo._directory_identity(path.parent)
        inode = path.stat().st_ino
        payload = json.loads(path.read_text())
        payload["note"] = "rewritten in place"
        path.write_text(json.dumps(payload))
        stamp = path.stat()
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
        assert path.stat().st_ino == inode
        assert recovery_memo._directory_identity(path.parent) == directory_identity
        with pytest.raises(TickFinishedError):
            next(stream)
    finally:
        stream.close()
    delta = counts - before_counts
    changed = {run_id for run_id in paths if keys[run_id] != before_keys[run_id]}
    print(
        json.dumps({"second_tick_calls": dict(delta), "changed_keys": sorted(changed)})
    )
    assert delta == dict.fromkeys(paths, 1)
    assert changed == {"r-author-00"}
