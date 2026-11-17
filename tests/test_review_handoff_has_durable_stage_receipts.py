"""Delivered handoffs survive retries and missing instants stay unknown."""

from __future__ import annotations

import importlib
import itertools
import json
import os
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, crew_follow_commands
from reckon.crew import (
    dispatch_launch,
    recovery_liveness,
    recovery_review_delivery,
    recovery_review_dispatch,
    recovery_watch,
    review,
    runs,
)
from reckon.crew import promotion_requests as requests
from reckon.crew import stage_receipts as receipts


@pytest.fixture
def clock(monkeypatch):
    ticks = itertools.count()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=next(ticks))

    monkeypatch.setattr(receipts, "datetime", Clock)
    monkeypatch.setattr(requests, "_now", lambda: Clock.now(UTC).isoformat())
    return Clock


def _delivery(run_id):
    return recovery_watch._watch_transition(
        "sample",
        kind="transition",
        snapshot={"run_id": run_id, "node": "fixture", "session": "owner"},
        previous="working",
        current="completed_unpromoted",
        counts={"working": 0, "blocked": 0, "unpromoted": 1},
        spend_runs=[],
        rate_statuses={},
    )


def _pointer(run_id):
    pointer = {
        "run_id": run_id,
        "phase": "complete",
        "project": "sample",
        "session": "owner",
        "node": {"id": "fixture"},
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _direct_review(run_id, clock, *, reviewer_id=None, body_updates=None):
    reviewer_id = reviewer_id or f"r-20260101T000000000000-review-{run_id}"
    head = "a" * 40
    path = Path(
        review.run_review_record_paths(
            runs.run_dir(run_id), run_id, reviewed_head_sha=head
        )[-1]
    )
    manifest = runs.run_dir(reviewer_id) / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("status: complete\n")
    pointer = {
        "run_id": reviewer_id,
        "project": "sample",
        "session": "owner",
        "role": "review",
        "launch": "in-harness",
        "phase": "complete",
        "created_at": clock.now(UTC).isoformat(),
        "manifest_path": str(manifest),
        "node": {
            "id": f"review-of-{run_id}",
            "role": "review",
            "time_budget": "10m",
            "write_paths": [str(path)],
        },
    }
    runs._write_json(runs.pointer_path(reviewer_id), pointer)
    body = {
        "status": "parsed",
        "project": "sample",
        "reviewed_run_id": run_id,
        "review_run_id": reviewer_id,
        "reviewed_base_sha": "b" * 40,
        "reviewed_head_sha": head,
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 20),
    }
    body.update(body_updates or {})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    written = clock.now(UTC).timestamp()
    os.utime(path, (written, written))
    os.utime(manifest, (written + 0.25, written + 0.25))
    return pointer, path, body, written


def _observe_review(pointer, clock):
    return recovery_watch._compute_watch_snapshot(
        pointer,
        moment=clock.now(UTC).timestamp(),
        stall_seconds=300,
        pointers=[pointer],
    )


def _lifecycle(run_id, tmp_path, monkeypatch):
    pointer = _pointer(run_id)
    event = _delivery(run_id)
    receipts.note_delivery(event, shown=True)
    # Reach the real compose boundary without launching an external worker.
    monkeypatch.setattr(
        recovery_review_dispatch, "_list_write_grants_in_brief", lambda *_: None
    )
    report = recovery_review_dispatch._dispatch_composed_review(
        pointer,
        {"project": "", "head": "a" * 40},
        config=None,
        launcher=None,
        allow_unreconciled_runs=True,
        prefer_local=True,
    )
    assert not report["dispatched"]
    reviewer = {
        "run_id": receipts.read_receipts(run_id)[-1]["attempt"],
        "reviewed_run_id": run_id,
    }
    receipts.note_review(reviewer, "review_admitted")
    receipts.note_review(reviewer, "review_started")
    reviewer, _, _, _ = _direct_review(
        run_id, clock=receipts.datetime, reviewer_id=reviewer["run_id"]
    )
    _observe_review(reviewer, receipts.datetime)
    refused = requests.request_promotion(run_id)
    service = requests.PromotionService(
        execute=lambda _: {"ok": False, "reason": "review requires a repair"}
    )
    service._run(refused)
    assert requests.read_promotion(run_id)["error"] == "review requires a repair"
    prefix = receipts.receipt_path(run_id).read_bytes()
    retry = requests.request_promotion(run_id, retry=True)
    service.execute = lambda _: {"ok": True, "steps": {"land": {"ok": True}}}
    service._run(retry)
    assert requests.read_promotion(run_id)["phase"] == requests.PROMOTED
    assert receipts.receipt_path(run_id).read_bytes().startswith(prefix)
    return receipts.read_receipts(run_id)


def test_delivery_review_refusal_retry_and_landing_keep_every_instant(
    clock, tmp_path, monkeypatch
):
    rows = _lifecycle("r-ordered", tmp_path, monkeypatch)
    assert [row["stage"] for row in rows] == [
        "delivery_recorded",
        "delivery_shown",
        "review_requested",
        "review_admitted",
        "review_started",
        "review_record_written",
        "promotion_requested",
        "promotion_attempt",
        "promotion_outcome",
        "promotion_requested",
        "promotion_attempt",
        "promotion_outcome",
        "promotion_settled",
        "landing",
    ]
    assert [row["at"] for row in rows] == sorted(row["at"] for row in rows)
    outcomes = [row for row in rows if row["stage"] == "promotion_outcome"]
    assert outcomes[0]["outcome"] == requests.REFUSED
    assert outcomes[0]["reason"] == "review requires a repair"
    assert outcomes[1]["outcome"] == requests.PROMOTED
    assert len({row["attempt"] for row in outcomes}) == 2


def test_ten_run_census_leaves_no_interval_unattributed(clock, tmp_path, monkeypatch):
    run_ids = [f"r-census-{index}" for index in range(10)]
    for run_id in run_ids:
        _lifecycle(run_id, tmp_path, monkeypatch)
    census = receipts.stage_census(run_ids)
    assert census["unread_runs"] == []
    assert census["unknown_seconds"] == 0, census
    assert len(census["intervals"]) == 130
    total = 0
    for run_id in run_ids:
        rows = receipts.read_receipts(run_id)
        duration = (
            datetime.fromisoformat(rows[-1]["at"])
            - datetime.fromisoformat(rows[0]["at"])
        ).total_seconds()
        intervals = [row for row in census["intervals"] if row["run_id"] == run_id]
        assert sum(row["seconds"] for row in intervals) == duration > 0
        assert all(
            left["end"] == right["start"]
            for left, right in itertools.pairwise(intervals)
        )
        total += duration
    assert sum(census["seconds_by_stage"].values()) == total
    print(f"10 runs; 130 disjoint intervals; {total:g} seconds; 0 unknown seconds")


def test_a_suppressed_receipt_reports_unknown_time(clock, tmp_path, monkeypatch):
    original = receipts.record_stage

    def suppress(run_id, stage, **kwargs):
        if stage == "review_admitted":
            return True
        return original(run_id, stage, **kwargs)

    monkeypatch.setattr(receipts, "record_stage", suppress)
    _lifecycle("r-missing", tmp_path, monkeypatch)
    census = receipts.stage_census(["r-missing"])
    assert census["unknown_seconds"] > 0
    assert any(row["stage"] == "unknown" for row in census["intervals"])


def test_fifty_attempts_fit_and_never_rewrite_a_receipt(clock):
    run_id = "r-many-attempts"
    for index in range(50):
        request = {
            "run_id": run_id,
            "request_id": f"{index:032x}",
            "attempt_id": f"{index:032x}",
        }
        receipts.record_stage(
            run_id, "promotion_requested", attempt=request["request_id"]
        )
        receipts.record_stage(
            run_id, "promotion_attempt", attempt=request["attempt_id"]
        )
        receipts.note_promotion_outcome(
            request, requests.REFUSED, {"error": "refused: " + "é" * 200}
        )
    rows = receipts.read_receipts(run_id)
    assert len(rows) == 150
    assert not any(row["stage"] == "overflow" for row in rows)
    assert all(
        row["reason"].endswith("…")
        for row in rows
        if row["stage"] == "promotion_outcome"
    )
    path = receipts.receipt_path(run_id)
    assert path.stat().st_size < 16_000
    print(f"50 attempts; {len(rows)} receipts; {path.stat().st_size} bytes")
    prefix = path.read_bytes()
    for index in range(50, 500):
        receipts.record_stage(run_id, "promotion_attempt", attempt=str(index))
    assert path.read_bytes().startswith(prefix)
    assert path.stat().st_size <= receipts.MAX_BYTES
    assert receipts.read_receipts(run_id)[-1]["stage"] == "overflow"


def test_replayed_delivery_preserves_its_first_instant(clock):
    event = _delivery("r-replayed")
    before = receipts.receipt_path("r-replayed").read_bytes()
    receipts.note_delivery(event)
    assert receipts.receipt_path("r-replayed").read_bytes() == before
    assert len(receipts.read_receipts("r-replayed")) == 1
    receipts.note_delivery({**event, "to_state": "working"}, shown=True)
    assert len(receipts.read_receipts("r-replayed")) == 1


def test_receipts_survive_a_request_rewrite_and_removal(clock):
    _pointer("r-durable")
    request = requests.request_promotion("r-durable")
    requests._finish(request, requests.REFUSED, error="review incomplete")
    before = receipts.receipt_path("r-durable").read_bytes()
    requests._request_path("r-durable").write_text(json.dumps({"overwritten": True}))
    requests._request_path("r-durable").unlink()
    assert receipts.receipt_path("r-durable").read_bytes() == before
    assert receipts.read_receipts("r-durable")[-1]["reason"] == "review incomplete"


def test_an_interrupted_append_is_unknown(clock):
    receipts.record_stage("r-interrupted", "delivery_recorded")
    with receipts.receipt_path("r-interrupted").open("ab") as handle:
        handle.write(b"[123,")
    receipts.record_stage("r-interrupted", "delivery_shown")
    census = receipts.stage_census(["r-interrupted"])
    assert census["unknown_seconds"] > 0


def test_reading_an_absent_run_reports_it_unread():
    assert receipts.read_receipts("r-absent") == []
    assert receipts.stage_census(["r-absent"])["unread_runs"] == ["r-absent"]


def test_late_display_does_not_make_a_complete_history_unknown(clock):
    run_id = "r-late-display"
    receipts.record_stage(run_id, "delivery_recorded")
    receipts.record_stage(run_id, "review_requested", attempt="reviewer")
    receipts.record_stage(run_id, "review_admitted", attempt="reviewer")
    receipts.record_stage(run_id, "delivery_shown")
    receipts.record_stage(run_id, "review_started", attempt="reviewer")
    receipts.record_stage(run_id, "review_record_written", attempt="reviewer")
    census = receipts.stage_census([run_id])
    assert census["unknown_seconds"] == 0
    assert census["intervals"][3]["stage"] == "review_admitted"


def test_saturation_accounts_for_the_tail_as_unknown(clock):
    run_id = "r-saturated-tail"
    receipts.record_stage(run_id, "delivery_recorded")
    receipts.record_stage(run_id, "delivery_shown")
    receipts.record_stage(run_id, "overflow")
    until = clock.now(UTC).isoformat()
    census = receipts.stage_census([run_id], until=until)
    assert census["unknown_seconds"] > 0
    assert census["intervals"][-1]["stage"] == "unknown"
    assert census["intervals"][0]["stage"] == "delivery_recorded"


@pytest.mark.parametrize("json_output", [False, True])
def test_only_a_delivery_printed_to_its_own_pane_is_shown(
    clock, monkeypatch, json_output
):
    event = _delivery("r-pane")
    monkeypatch.setattr(
        crew_follow_commands, "_follow_watch_lines", lambda *a, **k: iter([dict(event)])
    )
    monkeypatch.setattr(
        crew_follow_commands, "_hand_off_from_follower", lambda *_: False
    )
    monkeypatch.setattr(
        crew_follow_commands, "_station_subscriber_for", lambda *a, **k: None
    )
    arguments = [
        "crew",
        "follow",
        "--project",
        "sample",
        "--session",
        "owner",
        "--no-color",
    ]
    if json_output:
        arguments.append("--json")
    result = CliRunner().invoke(cli.main, arguments)
    assert result.exit_code == 0, result.output
    stages = [row["stage"] for row in receipts.read_receipts("r-pane")]
    assert ("delivery_shown" in stages) is not json_output


def test_worker_start_is_recorded_at_the_actual_spawn(clock, tmp_path, monkeypatch):
    source = "r-spawn-source"
    reviewer = "r-review-worker"
    pointer = {
        "run_id": reviewer,
        "project": "sample",
        "node": {"id": f"review-of-{source}"},
    }
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Review the fixture.")
    spec = {
        "run_id": reviewer,
        "run_directory": str(tmp_path),
        "plan": {"argv": ["fixture-worker"]},
        "prompt_path": str(prompt),
        "log_path": str(tmp_path / "stream.jsonl"),
        "stderr_path": str(tmp_path / "stderr.log"),
    }
    monkeypatch.setattr(dispatch_launch, "read_pointer", lambda _: pointer)
    monkeypatch.setattr(dispatch_launch, "_pointer_lock", lambda _: nullcontext())
    monkeypatch.setattr(
        recovery_liveness,
        "live_worker_reading",
        lambda _: recovery_liveness.LivenessReading("unknown", False),
    )
    monkeypatch.setattr(
        dispatch_launch, "held_launch_reservation", lambda *a, **k: None
    )
    monkeypatch.setattr(
        dispatch_launch, "_worker_runtime_environment", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        dispatch_launch,
        "step_launch_environment",
        lambda environment, *a, **k: environment,
    )
    monkeypatch.setattr(
        dispatch_launch,
        "_worker_process_environment",
        lambda environment, **k: environment,
    )
    monkeypatch.setattr(dispatch_launch, "_worker_record_writer", lambda _: None)
    monkeypatch.setattr(
        dispatch_launch,
        "_spawn_worker_retrying_a_vanished_bind",
        lambda spawn, **k: spawn(),
    )
    monkeypatch.setattr(
        dispatch_launch.subprocess,
        "Popen",
        lambda *a, **k: SimpleNamespace(pid=123, returncode=None),
    )
    monkeypatch.setattr(dispatch_launch.oom_priority, "claim_worker", lambda _: None)
    assert dispatch_launch._supervisor_spawn_worker(spec) == 123
    assert [row["stage"] for row in receipts.read_receipts(source)] == [
        "review_admitted",
        "review_started",
    ]


def test_long_review_ids_remain_distinct_at_their_head_suffix(clock):
    prefix = "r-" + "review-source-" * 12
    receipts.record_stage("r-long-review", "review_started", attempt=prefix + "a" * 40)
    receipts.record_stage("r-long-review", "review_started", attempt=prefix + "b" * 40)
    rows = receipts.read_receipts("r-long-review")
    assert len(rows) == 2
    assert rows[0]["attempt"] != rows[1]["attempt"]


def test_supervisor_records_delivery_without_waiting_for_the_monitor(
    clock, tmp_path, monkeypatch
):
    from reckon.crew import recovery_review_subject

    run_id = "r-supervisor-delivery"
    pointer = _pointer(run_id)
    manifest = tmp_path / "manifest.md"
    manifest.write_text("status: complete\n")
    pointer["manifest_path"] = str(manifest)
    runs._write_json(runs.pointer_path(run_id), pointer)
    monkeypatch.setattr(
        recovery_review_subject, "write_delivered_run_review_brief", lambda _: None
    )
    dispatch_launch._publish_stored_phase({"run_id": run_id}, ended=True)
    assert [row["stage"] for row in receipts.read_receipts(run_id)] == [
        "delivery_recorded"
    ]
    before = receipts.receipt_path(run_id).read_bytes()
    _delivery(run_id)
    assert receipts.receipt_path(run_id).read_bytes() == before


def test_a_direct_review_file_is_dated_before_later_filing(clock, tmp_path):
    run_id = "r-direct-delivery"
    _pointer(run_id)
    _delivery(run_id)
    pointer, path, body, written = _direct_review(run_id, clock)
    delivered = recovery_review_delivery.review_delivered(pointer)
    assert delivered is not None
    assert delivered["record_mtime"] == written
    for _ in range(30):
        clock.now(UTC)
    _observe_review(pointer, clock)
    rows = [
        row
        for row in receipts.read_receipts(run_id)
        if row["stage"] == "review_record_written"
    ]
    assert len(rows) == 1, rows
    assert datetime.fromisoformat(rows[0]["at"]).timestamp() == written
    assert rows[0]["attempt"] == pointer["run_id"]
    assert path.read_text() == json.dumps(body)
    before = receipts.receipt_path(run_id).read_bytes()
    review._write_record(tmp_path / "filed-copy.json", body)
    _observe_review(pointer, clock)
    assert receipts.receipt_path(run_id).read_bytes() == before


def test_filing_direct_json_keeps_its_original_write_instant(clock, monkeypatch):
    run_id = "r-direct-filing"
    _pointer(run_id)
    _delivery(run_id)
    pointer, _, _, written = _direct_review(run_id, clock)
    monkeypatch.setattr(review, "unresolved_reviewed_revision", lambda _: None)
    for _ in range(40):
        clock.now(UTC)
    filed = review.file_delivered_run_review_report(
        pointer, run_directory=runs.run_dir(run_id)
    )
    assert filed["filed"] and filed["refused"] == [], filed
    rows = [
        row
        for row in receipts.read_receipts(run_id)
        if row["stage"] == "review_record_written"
    ]
    assert len(rows) == 1
    assert datetime.fromisoformat(rows[0]["at"]).timestamp() == written


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "unparsed"},
        {"scores": {}},
        {"review_run_id": "r-another-review"},
        {"reviewed_head_sha": "c" * 40},
    ],
)
def test_an_incomplete_or_foreign_direct_file_stamps_no_completion(clock, updates):
    run_id = "r-invalid-direct-file"
    _pointer(run_id)
    _delivery(run_id)
    pointer, _, _, _ = _direct_review(run_id, clock, body_updates=updates)
    assert recovery_review_delivery.review_delivered(pointer) is None
    _observe_review(pointer, clock)
    assert not any(
        row["stage"] == "review_record_written"
        for row in receipts.read_receipts(run_id)
    )


@pytest.mark.parametrize("suppress_request", [False, True])
def test_a_second_review_without_its_request_has_unknown_time(clock, suppress_request):
    run_id = "r-review-rounds"
    receipts.record_stage(run_id, "delivery_recorded")
    receipts.record_stage(run_id, "delivery_shown")
    receipts.record_stage(run_id, "review_requested", attempt="reviewer-a")
    for stage in ("review_admitted", "review_started", "review_record_written"):
        receipts.record_stage(run_id, stage, attempt="reviewer-a")
    if not suppress_request:
        receipts.record_stage(run_id, "review_requested", attempt="reviewer-b")
    for stage in ("review_admitted", "review_started", "review_record_written"):
        receipts.record_stage(run_id, stage, attempt="reviewer-b")
    census = receipts.stage_census([run_id])
    if suppress_request:
        assert census["unknown_seconds"] > 0, census
        assert census["intervals"][5]["stage"] == "unknown"
    else:
        assert census["unknown_seconds"] == 0, census


@pytest.mark.parametrize("launch_path", ["direct", "roster-full", "producer-gone"])
def test_the_review_request_identity_reaches_dispatch_and_queued_admission(
    clock, tmp_path, monkeypatch, launch_path
):
    from reckon.crew.dispatch import RosterFull
    from reckon.crew.node import WatcherRequired

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    config = {"local_backend": "local", "backends": {"local": {"launch": "cli"}}}
    monkeypatch.setattr(
        recovery_review_dispatch, "_resolved_review_config", lambda *_: config
    )
    monkeypatch.setattr(
        recovery_review_dispatch, "_list_write_grants_in_brief", lambda *_: None
    )
    monkeypatch.setattr(
        recovery_review_dispatch,
        "_reroute_held_review",
        lambda *_, **__: {"backend": "local"},
    )
    monkeypatch.setattr(
        recovery_review_dispatch, "_release_delivered_review_claim", lambda *_, **__: []
    )
    monkeypatch.setattr(
        recovery_review_dispatch, "record_re_review_prediction", lambda *_: None
    )
    dispatched = []

    def dispatch(**kwargs):
        dispatched.append(kwargs["run_id"])
        if launch_path == "roster-full":
            raise RosterFull("occupied", occupancy=1, limit=1, occupying=["other"])
        if launch_path == "producer-gone":
            raise WatcherRequired(
                "sample",
                {"watcher_live": False, "ensure_line": "reckon crew watch"},
                session="owner",
            )
        return {"run_id": kwargs["run_id"]}

    monkeypatch.setattr(dispatch_module, "dispatch", dispatch)
    source = _pointer("r-dispatch-review-identity")
    source["repo"] = str(tmp_path)
    receipts.note_delivery(_delivery(source["run_id"]), shown=True)
    fields = {
        "project": "sample",
        "node_id": "review-identity",
        "goal": "review",
        "plan": "",
        "section": "",
        "brief": "",
        "done_when": "review written",
        "write_paths": [str(tmp_path / "review.json")],
        "time_budget": "10m",
        "session": "owner",
        "head": "a" * 40,
    }
    identities = []
    for _ in range(2):
        report = recovery_review_dispatch._dispatch_composed_review(
            source,
            fields,
            config=config,
            launcher=None,
            allow_unreconciled_runs=True,
            prefer_local=True,
        )
        reviewer_id = report.get("review_run_id") or report["queued"]["run_id"]
        identities.append(reviewer_id)
        request = receipts.read_receipts(source["run_id"])[-1]
        assert request["stage"] == "review_requested"
        assert request["attempt"] == dispatched[-1] == reviewer_id
        if launch_path != "direct":
            reviewer = runs.read_pointer(reviewer_id)
            assert reviewer["phase"] == "queued"
            reviewer["phase"] = "complete"
            runs._write_json(runs.pointer_path(reviewer_id), reviewer)
        reviewer = {"run_id": reviewer_id, "reviewed_run_id": source["run_id"]}
        for stage in ("review_admitted", "review_started", "review_record_written"):
            receipts.note_review(reviewer, stage)
    assert identities[0] != identities[1]
    assert receipts.stage_census([source["run_id"]])["unknown_seconds"] == 0


def test_a_waiting_attempt_keeps_its_receipt_when_readiness_changes_with_slots_full(
    clock, monkeypatch
):
    run_id = "r-waiting-attempt"
    _pointer(run_id)
    monkeypatch.setattr(requests, "_review_self_waits", lambda _: (False, ""))
    request = requests.request_promotion(run_id, kind="review-self")
    service = requests.PromotionService(
        width=1,
        execute=lambda _: {"waiting": True, "reason": "review record is still writing"},
    )
    service._run(request)
    parked = requests.read_promotion(run_id)
    assert parked["phase"] == requests.WAITING
    assert parked["waiting_reason"] == "review record is still writing"
    assert runs.read_pointer(run_id)["promotion_status"] == requests.WAITING
    outcomes = [
        row
        for row in receipts.read_receipts(run_id)
        if row["stage"] == "promotion_outcome"
    ]
    assert len(outcomes) == 1
    assert outcomes[0]["attempt"] == parked["attempt_id"]
    assert outcomes[0]["outcome"] == requests.WAITING
    assert outcomes[0]["reason"] == parked["waiting_reason"]
    journal = receipts.receipt_path(run_id).read_bytes()
    slot = service._claim("promotion-slot-0")
    assert slot is not None
    try:
        assert service.tick() == []
        ready = requests.read_promotion(run_id)
        assert ready["phase"] == requests.REQUESTED
        assert ready["request_id"] == request["request_id"]
        assert ready["attempt_id"] == parked["attempt_id"]
        assert ready["waiting_reason"] == ""
        assert ready["checked_at"] != parked["checked_at"]
        assert receipts.receipt_path(run_id).read_bytes() == journal
    finally:
        service._release(slot)
