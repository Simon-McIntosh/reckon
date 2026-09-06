"""Promotion folds a finished run's own terminal record, not the file mtime.

A worker writes its manifest before the harness reaches its terminal turn
record, so a prompt promotion can read the stream in that gap and take its
completion from a file mtime — losing the run's own timing and token figures
from the ledger. These tests pin the bounded settle that closes the gap: once
the run's process has exited, promotion waits out the stream's tail and
re-reads, while a live process, an already-settled stream, and a stream that
never produces a terminal record keep their prior behaviour.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _stamp(seconds_ago: int) -> str:
    moment = datetime.now(tz=UTC) - timedelta(seconds=seconds_ago)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_terminal_stream_pointer(
    repository: Path, run_id: str, stream: Path, manifest: Path
) -> None:
    """A complete passed pointer whose stream a test writes and settles."""
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\ncommits: none\n",
        encoding="utf-8",
    )
    stream.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "cli",
            "role": "implement",
            "member": "worker-a",
            "backend": "claude",
            "argv": ["claude", "-p"],
            "created_at": _stamp(120),
            "manifest_path": str(manifest),
            "log_path": str(stream),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "35m",
                "write_paths": [],
            },
        },
    )


# A stream as a prompt promotion reads it before the writer finishes: events
# carry no timestamps, so the terminal record the run will write is not yet
# present and a file mtime is currently the only completion source.
PARTIAL_BODY = (
    '{"type": "thread.started", "thread_id": "th-1"}\n{"type": "turn.started"}\n'
)


def _terminal_lines() -> str:
    """The tail a finished harness appends: a timestamped turn and its result.

    The result carries the token figures — a completed-token total on the
    model usage and a peak prompt spanning cached input — that the mtime
    fallback would otherwise lose from the ledger.
    """
    timestamp = (
        datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    return "".join(
        json.dumps(event) + "\n"
        for event in (
            {
                "type": "assistant",
                "session_id": "sess-1",
                "timestamp": timestamp,
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "type": "message",
                    "content": [{"type": "text", "text": "elided"}],
                    "usage": {
                        "input_tokens": 2,
                        "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
            {
                "type": "result",
                "session_id": "sess-1",
                "is_error": False,
                "result": "done",
                "duration_ms": 5000,
                "duration_api_ms": 3000,
                "num_turns": 1,
                "modelUsage": {
                    "claude-opus-5": {"outputTokens": 40, "contextWindow": 1000000}
                },
                "usage": {"input_tokens": 2, "output_tokens": 40},
            },
        )
    )


def _fast_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the settle's windows so the suite does not pay production timing."""
    monkeypatch.setattr(promotion, "_STREAM_SETTLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(promotion, "_STREAM_SETTLE_QUIESCENCE_SECONDS", 0.05)
    monkeypatch.setattr(promotion, "_STREAM_SETTLE_MAX_SECONDS", 5.0)


def test_a_terminal_record_landing_after_exit_is_folded_into_the_row(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tail that lands while promotion settles is the run's own record, not mtime.

    The stream is partial — no timestamps, no terminal result — when promotion
    reads it, and the harness appends its terminal turn record only once the
    settle has begun observing. Promotion must wait the tail out and fold the
    completion from the terminal stream event, with the run's token figures on
    the row.
    """
    run_id = "r-20260906T100000000000-node-a"
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_terminal_stream_pointer(repository, run_id, stream, manifest)
    stream.write_text(PARTIAL_BODY, encoding="utf-8")

    real_newest = promotion._newest_stream_mtime
    # The harness that wrote the manifest finishes its terminal record at a
    # moment the settle's file-touching drives, so the append is exactly one
    # quiet observation after promotion first reads the stream. The first call
    # also reports a freshly written stream, so the settle's already-quiet
    # check cannot conclude the tail is over before it has begun.
    reads = {"count": 0}

    def writer_finishes_after_first_observation(paths: list[Path]) -> float:
        reads["count"] += 1
        if reads["count"] == 1:
            os.utime(stream, None)
        elif reads["count"] == 2:
            with stream.open("a", encoding="utf-8") as handle:
                handle.write(_terminal_lines())
        return real_newest(paths)

    monkeypatch.setattr(
        promotion, "_newest_stream_mtime", writer_finishes_after_first_observation
    )
    monkeypatch.setattr(promotion, "process_alive", lambda pid: False)
    _fast_settle(monkeypatch)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the node delivered its terminal record",
        root=repository,
    )

    assert reads["count"] >= 3
    run = promoted["record"]
    assert run["completed_at_source"] == "terminal_event"
    assert run["worker_seconds_source"] == "stream_events"
    assert run["throughput"]["generated_tokens"] == 40
    assert run["throughput"]["peak_input_tokens"] == 1002
    assert run["session_id"] == "sess-1"


def test_an_already_quiet_stream_costs_no_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream whose terminal record already landed returns without waiting.

    The ordinary promotion — the writer finished and its record is on disk long
    before the coordinator arrives — must not pay the settle: the already-quiet
    check returns after a single read, and the run's own record is still folded.
    """
    stream = tmp_path / "stream.jsonl"
    stream.write_text(PARTIAL_BODY, encoding="utf-8")
    with stream.open("a", encoding="utf-8") as handle:
        handle.write(_terminal_lines())
    aged = time.time() - 10.0
    os.utime(stream, (aged, aged))
    record = {
        "launch": "cli",
        "backend": "claude",
        "argv": ["claude", "-p"],
        "log_path": str(stream),
    }

    reads = {"count": 0}
    real_newest = promotion._newest_stream_mtime

    def count_reads(paths: list[Path]) -> float:
        reads["count"] += 1
        return real_newest(paths)

    monkeypatch.setattr(promotion, "_newest_stream_mtime", count_reads)
    monkeypatch.setattr(promotion, "process_alive", lambda pid: False)

    observed = promotion._promotion_terminal_observation(record)

    assert reads["count"] == 1
    assert observed.completion_source == "terminal_event"
    assert observed.throughput["generated_tokens"] == 40


def test_a_stream_that_never_receives_a_terminal_record_keeps_the_mtime_fallback(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose stream genuinely ends without a terminal event still promotes.

    Killed runs never write one, so the mtime fallback must survive, with its
    source recorded on the row exactly as before rather than mislabelled.
    """
    run_id = "r-20260906T100100000000-node-a"
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_terminal_stream_pointer(repository, run_id, stream, manifest)
    stream.write_text(PARTIAL_BODY, encoding="utf-8")
    os.utime(stream, None)

    monkeypatch.setattr(promotion, "process_alive", lambda pid: False)
    _fast_settle(monkeypatch)

    began = time.monotonic()
    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the node recorded its outcome",
        root=repository,
    )
    elapsed = time.monotonic() - began

    # The settle is bounded: a stream with no terminal record cannot extend a
    # promotion past the ceiling.
    assert elapsed < 5.0
    run = promoted["record"]
    assert run["completed_at_source"] == "stream_mtime"
    assert run["worker_seconds_source"] == "wall_fallback"
    # No terminal record, so no measured rate was stored on the row at all.
    assert run.get("throughput") is None


def test_a_live_run_process_is_not_settled_or_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process still alive is never waited on; its observation is unchanged.

    A live run's stream is legitimately mid-write, so promotion must not block
    on it — the settle is engaged only once the writer has exited.
    """
    stream = tmp_path / "stream.jsonl"
    stream.write_text(PARTIAL_BODY, encoding="utf-8")
    record = {
        "launch": "cli",
        "backend": "claude",
        "argv": ["claude", "-p"],
        "log_path": str(stream),
    }

    settle_engaged: list[tuple] = []
    monkeypatch.setattr(promotion, "process_alive", lambda pid: True)
    monkeypatch.setattr(
        promotion,
        "_wait_out_stream_tail",
        lambda paths: settle_engaged.append(tuple(paths)),
    )

    observed = promotion._promotion_terminal_observation(record)
    unmodified = promotion._terminal_stream_data(record)

    assert settle_engaged == []
    assert observed.completed_at == unmodified.completed_at
    assert observed.completion_source == unmodified.completion_source
    assert observed.worker_seconds == unmodified.worker_seconds


def test_a_dead_run_process_engages_the_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle is the behaviour a closed writer triggers, and only it."""
    stream = tmp_path / "stream.jsonl"
    stream.write_text(PARTIAL_BODY, encoding="utf-8")
    record = {
        "launch": "cli",
        "backend": "claude",
        "argv": ["claude", "-p"],
        "log_path": str(stream),
    }

    settle_engaged: list[tuple] = []
    monkeypatch.setattr(promotion, "process_alive", lambda pid: False)
    monkeypatch.setattr(
        promotion,
        "_wait_out_stream_tail",
        lambda paths: settle_engaged.append(tuple(paths)),
    )

    promotion._promotion_terminal_observation(record)

    assert settle_engaged == [(Path(str(stream)),)]


def test_the_settle_is_bounded_for_a_stream_that_never_quiesces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that keeps appending cannot extend the settle past its ceiling.

    A truncated or still-advancing stream must not be able to hang a
    promotion: the wait returns at the bounded maximum and the fallback state
    is what the caller folds.
    """
    stream = tmp_path / "stream.jsonl"
    stream.write_text(PARTIAL_BODY, encoding="utf-8")

    monkeypatch.setattr(promotion, "_STREAM_SETTLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(promotion, "_STREAM_SETTLE_QUIESCENCE_SECONDS", 0.05)
    monkeypatch.setattr(promotion, "_STREAM_SETTLE_MAX_SECONDS", 0.5)

    stop = threading.Event()

    def endless_writer() -> None:
        while not stop.is_set():
            with stream.open("a", encoding="utf-8") as handle:
                handle.write("""{"type": "assistant"}\n""")
            time.sleep(0.01)

    thread = threading.Thread(target=endless_writer)
    thread.start()
    try:
        began = time.monotonic()
        promotion._wait_out_stream_tail([stream])
        elapsed = time.monotonic() - began
    finally:
        stop.set()
        thread.join(timeout=5.0)

    assert elapsed < 2.0
    assert not thread.is_alive()
