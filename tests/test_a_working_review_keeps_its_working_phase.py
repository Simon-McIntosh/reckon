"""A review keeps working until its worker and manifest have finished."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion_requests as requests
from reckon.crew.runs import pointer_path, read_pointer
from tests import test_review_promotion_waits_for_its_record as fixtures

repository = fixtures.repository


@pytest.mark.parametrize("manifest_status", ["in-progress", "complete"])
def test_a_working_review_keeps_its_phase_until_the_same_request_promotes(
    repository: Path, tmp_path: Path, manifest_status: str
) -> None:
    run_id = fixtures.FIRST_RUN_ID
    reviewed_run_id = fixtures.FIRST_REVIEWED_RUN_ID
    fixtures._write_pointer(repository, tmp_path, run_id, reviewed_run_id, pid=None)
    manifest = Path(read_pointer(run_id)["manifest_path"])
    manifest.write_text(f"status: {manifest_status}\n", encoding="utf-8")
    stream = tmp_path / "reviewer.jsonl"
    finish = tmp_path / "finish"
    script = (
        "import pathlib, sys, time\n"
        "stream, finish = map(pathlib.Path, sys.argv[1:])\n"
        "deadline = time.monotonic() + 60\n"
        "with stream.open('a') as output:\n"
        "    while not finish.exists():\n"
        '        output.write(\'{"type":"progress"}\\n\')\n'
        "        output.flush()\n"
        "        if time.monotonic() > deadline: sys.exit(2)\n"
        "        time.sleep(0.01)\n"
    )
    worker = subprocess.Popen([sys.executable, "-c", script, str(stream), str(finish)])
    pointer = read_pointer(run_id)
    pointer.update(pid=worker.pid, log_path=str(stream))
    requests.runs._write_json(pointer_path(run_id), pointer)
    service = requests.PromotionService(width=1)
    if manifest_status == "complete":
        fixtures._store_record(run_id, reviewed_run_id)
    try:
        deadline = time.monotonic() + 10
        while not stream.exists() or stream.stat().st_size == 0:
            assert worker.poll() is None
            assert time.monotonic() < deadline, "reviewer wrote no progress"
            time.sleep(0.01)
        observed_size = stream.stat().st_size

        requested = requests.request_promotion(run_id, kind="review-self")
        assert worker.poll() is None
        assert read_pointer(run_id)["phase"] == "working"
        assert "started_at" not in requests.read_promotion(run_id)
        assert service.tick() == []
        waiting = requests.read_promotion(run_id)
        assert waiting["phase"] == requests.WAITING
        assert waiting["request_id"] == requested["request_id"]
        assert "started_at" not in waiting
        assert read_pointer(run_id)["phase"] == "working"
        assert ledger.runs(fixtures.PROJECT, root=repository) == []
        duplicate = requests.request_promotion(run_id, kind="review-self")
        assert duplicate["duplicate"] is True
        assert duplicate["request_id"] == requested["request_id"]

        while stream.stat().st_size <= observed_size:
            assert worker.poll() is None
            assert time.monotonic() < deadline, "reviewer stopped writing progress"
            time.sleep(0.01)
        finish.write_text("finish\n", encoding="utf-8")
        assert worker.wait(timeout=10) == 0
        fixtures._store_record(run_id, reviewed_run_id)
        fixtures._write_exit_record(run_id)
        if manifest_status == "in-progress":
            assert service.tick() == []
            assert read_pointer(run_id)["phase"] == "working"
            assert "started_at" not in requests.read_promotion(run_id)
        manifest.write_text("status: complete\n", encoding="utf-8")

        assert service.tick() == [run_id]
        service.settle(timeout=60)
        receipt = requests.read_promotion(run_id)
        assert receipt["phase"] == requests.PROMOTED, receipt
        assert receipt["request_id"] == requested["request_id"]
        assert not pointer_path(run_id).exists()
        [row] = ledger.runs(fixtures.PROJECT, root=repository)
        assert row["promoted_by"] == "review-self"
        print(
            json.dumps(
                {
                    "manifest_status": manifest_status,
                    "live_phase": "working",
                    "promotion_started_while_live": False,
                    "same_request_promoted": True,
                }
            )
        )
    finally:
        finish.write_text("finish\n", encoding="utf-8")
        worker.wait(timeout=10)
        service.settle(timeout=60)
