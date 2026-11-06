"""A worker past its soft budget is told so, and the run is not ended.

The time budget is a soft limit: the response to an overrun is to message the
worker, never to signal it. The message has to exist somewhere durable, because
a headless worker cannot be interrupted — it reads its own run directory at its
next checkpoint — and the verdict has to survive promotion, which deletes the
live pointer holding the only other copy.

Both halves are held here. A run past the limit gets exactly one message in its
own directory, with its process, phase and session untouched; a run inside the
limit gets none, because a limit that always speaks is filtered like any other
noise. The committed row carries the same figure the message names, so a census
reading the ledger and a worker reading its inbox are looking at one fact.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew import promotion, recovery_stream, recovery_watch
from reckon.crew.runs import _write_json, pointer_path, read_pointer, run_dir

# The instant every run here was launched, so an elapsed span is a supplied
# number rather than a measurement of how long the test took.
START = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)

# A short allowance keeps the arithmetic readable: the soft limit is 1.5x, so
# 900 seconds is exactly the threshold and 901 is past it.
BUDGET = "600s"
SOFT_LIMIT_SECONDS = 900


def _moment(seconds: int) -> float:
    return (START + timedelta(seconds=seconds)).timestamp()


def _pointer(run_id: str) -> dict:
    record = {
        "run_id": run_id,
        "project": "sample",
        "backend": "clive",
        "agent": {"model": "a-model"},
        "node": {
            "id": f"node-{run_id}",
            "plan": "sample",
            "time_budget": BUDGET,
        },
        "phase": "working",
        "process_alive": True,
        "launcher_host": socket.gethostname(),
        "session": "a-session",
        "created_at": START.isoformat(),
        "attempt_started_at": START.isoformat(),
        "throughput": {"generated_tokens": 4_000},
    }
    _write_json(pointer_path(run_id), record)
    return record


def _messages(run_id: str) -> list[Path]:
    home = run_dir(run_id)
    if not home.is_dir():
        return []
    return sorted(home.glob("*.md"))


def test_a_run_past_its_soft_limit_is_messaged_once(monkeypatch) -> None:
    run_id = "r-past-soft-limit"
    record = _pointer(run_id)
    signalled: list[tuple] = []
    monkeypatch.setattr(
        recovery_watch, "_signal_process_group", lambda *a, **k: signalled.append(a)
    )
    monkeypatch.setattr(
        recovery_stream, "_signal_process_group", lambda *a, **k: signalled.append(a)
    )

    path = recovery_watch.notify_budget_overrun(record, now_seconds=_moment(1_200))

    assert path is not None, "a run 2x its budget is told"
    assert path == recovery_watch.budget_notice_path(run_id)
    assert _messages(run_id) == [path], "one message, not one per poll"
    text = path.read_text(encoding="utf-8")
    assert "600s" in text, "the message names the allowance it passed"
    assert "1200s" in text and "600s past" in text, (
        "the message names the measured elapsed and the overrun"
    )
    assert "manifest" in text.lower()

    stored = read_pointer(run_id)
    assert stored["process_alive"] is True, "the run keeps its process"
    assert stored["phase"] == "working", "the run keeps its phase"
    assert stored["budget_notice_path"] == str(path)

    assert signalled == [], "a soft limit never signals a worker"


def test_a_second_pass_does_not_send_a_second_message() -> None:
    run_id = "r-told-once"
    record = _pointer(run_id)

    first = recovery_watch.notify_budget_overrun(record, now_seconds=_moment(1_200))
    second = recovery_watch.notify_budget_overrun(record, now_seconds=_moment(1_400))

    assert first is not None
    assert second is None, "a run already told is not told again"
    assert _messages(run_id) == [first]


def test_a_run_inside_its_soft_limit_is_silent() -> None:
    run_id = "r-inside-limit"
    record = _pointer(run_id)

    assert (
        recovery_watch.notify_budget_overrun(record, now_seconds=_moment(899)) is None
    )
    assert (
        recovery_watch.notify_budget_overrun(record, now_seconds=_moment(900)) is None
    )
    assert _messages(run_id) == [], "a run inside the limit receives nothing"
    assert read_pointer(run_id).get("budget_notice_path") is None


def test_a_finished_run_is_not_messaged() -> None:
    run_id = "r-finished"
    record = {**_pointer(run_id), "process_alive": False}

    assert (
        recovery_watch.notify_budget_overrun(record, now_seconds=_moment(1_200)) is None
    )
    assert _messages(run_id) == []


def test_the_committed_row_carries_the_figure_the_message_named() -> None:
    run_id = "r-ledger-match"
    record = _pointer(run_id)
    path = recovery_watch.notify_budget_overrun(record, now_seconds=_moment(1_200))
    assert path is not None
    stored = read_pointer(run_id)

    facts = promotion._budget_overrun_facts(
        stored, finished=(START + timedelta(seconds=1_200)).isoformat()
    )

    assert facts["budget_overrun"] is True
    assert facts["budget_overrun_seconds"] == 600, "1200s elapsed against 600s"
    assert facts["budget_overrun_notice"] == str(path), (
        "the row names the message the worker received"
    )


def test_the_measurement_follows_a_token_budget_over_the_wall_clock() -> None:
    """A slow lane inside its token budget is not an overrun, however long it took."""
    record = _pointer("r-token-budget")
    record["node"]["token_budget"] = 5_000
    record["throughput"]["generated_tokens"] = 4_000

    assert (
        recovery_watch.notify_budget_overrun(record, now_seconds=_moment(5_000)) is None
    )
    assert _messages("r-token-budget") == [], (
        "inside the token budget is not an overrun"
    )

    record["throughput"]["generated_tokens"] = 6_000
    path = recovery_watch.notify_budget_overrun(record, now_seconds=_moment(5_000))

    assert path is not None, "past the token allowance the run is told"
    assert "6000 generated tokens" in path.read_text(encoding="utf-8")
