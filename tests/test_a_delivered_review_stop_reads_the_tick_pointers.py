"""The delivered-review stop shares the producer's live-pointer reading."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import Mock, call

from reckon.crew import recovery_review_delivery, recovery_watch, review, runs
from tests.test_a_producer_tick_reads_live_pointers_once import (
    tick as tick,  # noqa: PLC0414
)
from tests.test_review_plan_command import project as project  # noqa: PLC0414


def test_delivered_reviews_share_the_ticks_single_live_read(tick, monkeypatch):
    listing, _publish, _resolve, _dispatch, _producer, _compose = tick
    delivered_at = time.time() - 3600
    expected = []
    for index in range(35, 40):
        run_id = f"r-author-{index:02}"
        source_id = f"r-author-{index - 35:02}"
        pointer = runs.read_pointer(run_id)
        record_path = Path(
            review.run_review_record_paths(
                runs.run_dir(source_id), source_id, reviewed_head_sha="a" * 40
            )[-1]
        )
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record = review.parse_review(
            "\n".join(
                f"SCORE {dimension}: 18" for dimension in review.REVIEW_DIMENSIONS
            )
        )
        record.update(reviewed_head_sha="a" * 40, review_run_id=run_id)
        record_path.write_text(json.dumps(record))
        manifest = runs.run_dir(run_id) / "manifest.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("status: complete\ncommits: abc123\n")
        stream = runs.run_dir(run_id) / "stream.jsonl"
        stream.write_text('{"type":"turn.completed"}\n')
        for path in (record_path, manifest):
            os.utime(path, (delivered_at, delivered_at))
        os.utime(stream, (delivered_at + 600, delivered_at + 600))
        pointer.update(
            role="review",
            pid=900000 + index,
            pid_start_time=f"start-{index}",
            manifest_path=str(manifest),
            log_path=str(stream),
        )
        pointer["node"].update(
            id=f"review-of-author-{index - 35}", write_paths=[str(record_path)]
        )
        runs._write_json(runs.pointer_path(run_id), pointer)
        expected.append(pointer)

    # Count every imported binding, including aliases and the delivery resolver,
    # so a nested read cannot hide behind a different module's imported name.
    original = listing._mock_wraps
    bound = []
    for module_name, module in list(sys.modules.items()):
        if module is None or not module_name.startswith("reckon"):
            continue
        for name, value in list(vars(module).items()):
            if value is original or value is listing:
                monkeypatch.setattr(module, name, listing)
                bound.append(f"{module_name}.{name}")
    assert "reckon.crew.recovery_review_delivery.list_live" in bound
    assert len(runs._list_live_records(project="sample")) == 40
    # A valid delivery is the positive control for the stop's record reader.
    assert all(recovery_review_delivery.review_delivered(row) for row in expected)
    listing.reset_mock()
    signals = Mock()
    ticker = recovery_watch.watch_ticker("sample", poll_interval=0, signal_run=signals)
    try:
        events = [next(ticker) for _ in range(40)]
    finally:
        ticker.close()
    print(
        json.dumps(
            {
                "live_pointers": 40,
                "delivered_reviews": len(expected),
                "list_live_calls": listing.call_count,
                "signals": signals.call_count,
                "patched_bindings": bound,
            },
            sort_keys=True,
        )
    )
    assert len(events) == 40
    by_run = {event["run_id"]: event for event in events}
    for index in range(35, 40):
        event = by_run[f"r-author-{index:02}"]
        assert event["reviewed_run_id"] == f"r-author-{index - 35:02}"
        assert event["reviewed_node"] == f"author-{index - 35}"
    assert signals.call_args_list == [
        call(row["pid"], row["pid_start_time"]) for row in expected
    ]
    for row in expected:
        stopped = runs.read_pointer(row["run_id"])
        assert stopped["phase"] == "stopped"
        assert (
            stopped["ended_after_delivery"]["record_path"]
            == row["node"]["write_paths"][0]
        )
    assert listing.call_count == 1
