"""A live writer is ended before its receipt is folded, not read past it.

A prompt promotion that finds the run's process still alive was, until now,
folding a completion taken from the file mtime and only afterwards ending the
writer that was still appending to its stream — so the terminal record with the
run's own timing and token figures landed up to many seconds after the row was
written. These tests pin the ordering: the writer this promotion is about to
end is ended first, its stream settle is then bounded, and the fold is re-read
so the terminal record is folded instead of the mtime. A run that has no
terminal record, an already-terminal quiet run, and a run this promotion would
not signal keep their prior behaviour.
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
    repository: Path,
    run_id: str,
    stream: Path,
    manifest: Path,
    *,
    pid: int | None = None,
) -> None:
    """A complete passed pointer whose stream a test writes and settles."""
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\ncommits: none\n",
        encoding="utf-8",
    )
    stream.parent.mkdir(parents=True, exist_ok=True)
    record = {
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
    }
    if pid is not None:
        record["pid"] = pid
        record["pid_start_time"] = "start"
    _write_json(pointer_path(run_id), record)


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


def test_a_live_writer_is_ended_first_and_its_receipt_is_folded(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured defect: a writer still appending at promotion is read past.

    The stream is partial when promotion arrives and the process is alive, so
    promotion would normally fold the mtime; the writer does not finish its
    terminal record until it is released. The promotion must end the writer
    before the fold — which it was about to do in its release step anyway —
    and then fold the terminal record its shutdown writes.
    """
    run_id = "r-20260906T110000000000-live-writer"
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_terminal_stream_pointer(repository, run_id, stream, manifest, pid=4242)
    stream.write_text(PARTIAL_BODY, encoding="utf-8")  # no terminal record yet

    state = {"alive": True, "signalled": 0}

    def fake_alive(pid):
        return state["alive"]

    def release_when_signalled(pid: int, started_at) -> None:
        # The release signal is what makes the harness write its terminal
        # record: on shutdown it appends the receipt that promotion must fold.
        state["signalled"] += 1
        state["alive"] = False
        with stream.open("a", encoding="utf-8") as handle:
            handle.write(_terminal_lines())

    monkeypatch.setattr(promotion, "process_alive", fake_alive)
    monkeypatch.setattr(promotion, "_signal_process_group", release_when_signalled)
    _fast_settle(monkeypatch)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the run's own receipt was folded",
        root=repository,
    )

    assert state["signalled"] == 1
    run = promoted["record"]
    assert run["completed_at_source"] == "terminal_event"
    assert run["worker_seconds_source"] == "stream_events"
    assert run["throughput"]["generated_tokens"] == 40
    assert run["throughput"]["peak_input_tokens"] == 1002
    assert run["session_id"] == "sess-1"
    # The release reports the writer it ended before the fold rather than a
    # process that merely went away between the two steps.
    assert promoted["release"]["process_signalled"] is True
    assert promoted["release"]["process_withheld"] == (
        "process ended by promotion before the fold"
    )


def test_a_live_writer_that_never_emits_a_receipt_keeps_the_fallback(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose shutdown produces no terminal record still promotes.

    The writer is ended and settled exactly as in the receipt case, but nothing
    terminal ever lands, so the mtime fallback must survive with its source
    recorded — and the whole turn must stay bounded.
    """
    run_id = "r-20260906T110100000000-no-receipt"
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_terminal_stream_pointer(repository, run_id, stream, manifest, pid=4242)
    stream.write_text(PARTIAL_BODY, encoding="utf-8")

    state = {"alive": True}

    def fake_alive(pid):
        return state["alive"]

    def signal_without_receipt(pid: int, started_at) -> None:
        # The writer ends on the signal but never writes a terminal record, as
        # a killed or hard-exiting harness never does.
        state["alive"] = False

    monkeypatch.setattr(promotion, "process_alive", fake_alive)
    monkeypatch.setattr(promotion, "_signal_process_group", signal_without_receipt)
    _fast_settle(monkeypatch)

    began = time.monotonic()
    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="no receipt, fallback survives",
        root=repository,
    )
    elapsed = time.monotonic() - began

    assert elapsed < 5.0
    run = promoted["record"]
    assert run["completed_at_source"] == "stream_mtime"
    assert run["worker_seconds_source"] == "wall_fallback"
    assert run.get("throughput") is None
    assert promoted["release"]["process_signalled"] is True


def test_an_already_terminal_quiet_run_is_unchanged(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that finished before promotion behaves exactly as before.

    The ordinary promotion — process exited, terminal record long on disk — pays
    no settle beyond a single quiet check and still folds the run's own record.
    """
    run_id = "r-20260906T110200000000-quiet"
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_terminal_stream_pointer(repository, run_id, stream, manifest)
    stream.write_text(PARTIAL_BODY, encoding="utf-8")
    with stream.open("a", encoding="utf-8") as handle:
        handle.write(_terminal_lines())
    aged = time.time() - 10.0
    os.utime(stream, (aged, aged))

    reads = {"count": 0}
    real_newest = promotion._newest_stream_mtime

    def count_reads(paths: list[Path]) -> float:
        reads["count"] += 1
        return real_newest(paths)

    monkeypatch.setattr(promotion, "_newest_stream_mtime", count_reads)
    monkeypatch.setattr(promotion, "process_alive", lambda pid: False)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="quiet stream, no new settle",
        root=repository,
    )

    # One read: the already-quiet check returns without waiting, exactly the
    # behaviour a closed writer gets today.
    assert reads["count"] == 1
    run = promoted["record"]
    assert run["completed_at_source"] == "terminal_event"
    assert run["throughput"]["generated_tokens"] == 40
    assert run["session_id"] == "sess-1"


def test_a_writer_that_never_stops_cannot_hang_the_folded_promotion(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that never quiesces cannot hold a live-writer fold open.

    The writer is ended, but a truncated stream — or one whose final record
    never lands — must not extend the promotion past the settle ceiling; the
    mtime fallback is what folds.
    """
    run_id = "r-20260906T110300000000-never-quiet"
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_terminal_stream_pointer(repository, run_id, stream, manifest, pid=4242)
    stream.write_text(PARTIAL_BODY, encoding="utf-8")

    state = {"alive": True}
    stop = threading.Event()

    def fake_alive(pid):
        return state["alive"]

    def signal_then_keep_writing(pid: int, started_at) -> None:
        # The writer is ended yet something keeps appending to its stream, so
        # the settle never observes quiescence before the ceiling.
        state["alive"] = False

    monkeypatch.setattr(promotion, "process_alive", fake_alive)
    monkeypatch.setattr(promotion, "_signal_process_group", signal_then_keep_writing)
    monkeypatch.setattr(promotion, "_STREAM_SETTLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(promotion, "_STREAM_SETTLE_QUIESCENCE_SECONDS", 0.05)
    monkeypatch.setattr(promotion, "_STREAM_SETTLE_MAX_SECONDS", 0.5)

    def endless_writer() -> None:
        while not stop.is_set():
            with stream.open("a", encoding="utf-8") as handle:
                handle.write('{"type": "assistant"}\n')
            time.sleep(0.01)

    thread = threading.Thread(target=endless_writer)
    thread.start()
    try:
        began = time.monotonic()
        promoted = crew.complete(
            run_id,
            gate="passed",
            outcome="endless stream stayed bounded",
            root=repository,
        )
        elapsed = time.monotonic() - began
    finally:
        stop.set()
        thread.join(timeout=5.0)

    assert elapsed < 2.0
    assert not thread.is_alive()
    assert promoted["record"]["completed_at_source"] == "stream_mtime"


def test_a_writer_the_release_would_not_signal_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a promotion that will end its writer ends it before the fold.

    The release's own signal is gated on a fresh terminal manifest and a live
    process; the pre-release must share that gate exactly, so a run with no
    manifest — a recovery case, not a cleanup one — is never ended early.
    """
    stream = tmp_path / "stream.jsonl"
    stream.write_text(PARTIAL_BODY, encoding="utf-8")
    manifest = tmp_path / "manifests" / "r-null-release.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: incomplete\n", encoding="utf-8")
    record = {
        "launch": "cli",
        "backend": "claude",
        "argv": ["claude", "-p"],
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "pid": 4242,
        "pid_start_time": "start",
        "manifest_baseline_mtime_ns": 1,
    }

    signalled: list[int] = []
    monkeypatch.setattr(promotion, "process_alive", lambda pid: True)
    monkeypatch.setattr(
        promotion,
        "_signal_process_group",
        lambda pid, started_at: signalled.append(pid),
    )

    ended = promotion._end_live_writer_for_settle(record)

    assert ended is False
    assert signalled == []


def test_the_pre_release_gate_is_the_release_gates_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-release fires exactly when the release step would have signalled.

    A live cli run with a fresh terminal manifest is ended before the fold; a
    dead process is not signalled (nothing to end); a non-cli launch is not
    ended, because the settle never applies to it. The last is the guarantee
    that promotion never ends a process its release would keep.
    """
    stream = tmp_path / "stream.jsonl"
    stream.write_text(PARTIAL_BODY, encoding="utf-8")
    manifest = tmp_path / "manifests" / "r-gate.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    signalled: list[int] = []
    monkeypatch.setattr(
        promotion,
        "_signal_process_group",
        lambda pid, started_at: signalled.append(pid),
    )

    base = {
        "launch": "cli",
        "backend": "claude",
        "argv": ["claude", "-p"],
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "pid": 4242,
        "pid_start_time": "start",
    }

    monkeypatch.setattr(promotion, "process_alive", lambda pid: True)
    assert promotion._end_live_writer_for_settle(base) is True
    assert signalled == [4242]

    monkeypatch.setattr(promotion, "process_alive", lambda pid: False)
    assert promotion._end_live_writer_for_settle(base) is False
    assert signalled == [4242]

    monkeypatch.setattr(promotion, "process_alive", lambda pid: True)
    non_cli = {**base, "launch": "in-harness"}
    assert promotion._end_live_writer_for_settle(non_cli) is False
    assert signalled == [4242]
