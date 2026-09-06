"""Waiting on time or on one's own job is paused, not blocked.

A run that is waiting on something that will lift itself — its own submitted
job, a rate-limit window that resets, a bounded peer read, a budget hold that
ages out — needs no person, so it must not read as blocked, which names a stop
that demands a reader. The discriminator is exactly who lifts it: time or the
run's own job lifts it -> paused; a person or another session must act ->
blocked. So a NEEDS-HELP question of the manifest and a metered usage or spend
limit keep blocking, while the lane's own window, a parked background job and
a bounded wait pause.

The bounds the change is held to: every paused verdict names what will lift
it, so a paused run is never the bucket a forgotten run sits in, and the
closure fence counts a paused pointer as unreconciled, because a paused row is
precisely the one nobody is woken for.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew import recovery
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move every crew pointer into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


# ── Helpers ────────────────────────────────────────────────────────────────


def _write_stream(tmp_path: Path, name: str, events: list[dict[str, Any]]) -> Path:
    """Write one synthetic run stream beside the record that names it."""
    stream = tmp_path / "streams" / f"{name}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text("".join(json.dumps(event) + "\n" for event in events))
    return stream


def _init() -> dict[str, Any]:
    """The session-carrying event every real run stream opens with."""
    return {"type": "system", "subtype": "init", "session_id": "sess-1"}


def _assistant_tool(name: str, command: str) -> dict[str, Any]:
    """One assistant event carrying a single tool call, as a live stream ends."""
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": name, "input": {"command": command}}
            ]
        },
    }


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    stream: Path,
    alive: bool | None,
    phase: str,
    manifest: str | None = None,
    final_message: str | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A cli pointer reading one synthetic stream with an optional manifest."""
    manifest_path = tmp_path / "manifests" / f"{run_id}.md"
    if manifest is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(manifest)
    stderr = tmp_path / f"{run_id}.stderr.log"
    stderr.write_text("")
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "30m"},
        "backend": "claude",
        "launch": "cli",
        "argv": ["claude"],
        "log_path": str(stream),
        "stderr_path": str(stderr),
        "manifest_path": str(manifest_path),
        "phase": phase,
        "process_alive": alive,
        "session": "s",
    }
    if final_message is not None:
        record["final_message"] = final_message
    if budget is not None:
        record["budget"] = budget
    return record


def _quiet(stream: Path, *, now: float, age: float = 300.0) -> None:
    """Backdate a stream so the run reads as quiet past any short stall window."""
    os.utime(stream, (now - age, now - age))


def _classify(pointer: dict[str, Any]) -> dict[str, Any]:
    return recovery.classify_pointer(pointer, now_seconds=time.time())


# ── Blocked stays blocked: the falsifiers ───────────────────────────────────


def test_a_needs_help_manifest_still_reads_blocked_and_names_its_question(
    tmp_path,
) -> None:
    stream = _write_stream(tmp_path, "needs-help", [_init()])
    manifest = (
        "node: r-needs-help\n"
        "status: blocked\n"
        "blockers: |\n"
        "  NEEDS-HELP: how should I proceed with the missing fixture?\n"
        "  tried: set gates.enforce to off; validation rejected the boolean\n"
        "  options: spell the value disabled; or coerce the boolean\n"
        "  leaning: spell it disabled, because the coercion would be invisible\n"
        "  cost-if-wrong: the generated schema and its committed JSON regenerate\n"
    )
    pointer = _pointer(
        tmp_path,
        "r-needs-help",
        stream=stream,
        alive=False,
        phase="blocked",
        manifest=manifest,
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert row["needs_help_complete"] is True
    assert row["marker"] == "?"
    assert "how should I proceed with the missing fixture" in row["detail"]
    assert row["next_action"].startswith("reckon crew resume")


def test_a_metered_usage_limit_refusal_with_a_reset_still_reads_blocked(
    tmp_path,
) -> None:
    stream = _write_stream(tmp_path, "usage-limit", [_init()])
    pointer = _pointer(
        tmp_path,
        "r-usage-limit",
        stream=stream,
        alive=False,
        phase="blocked",
        budget={
            "headroom": "known",
            "utilisation_pct": 100.0,
            "rate_limit_type": "usage-limit",
            "resets_at": "2026-09-06T12:00:00+00:00",
            "threshold_status": "exhausted",
            "refusal": True,
            "detail": "backend refused the turn: the account's usage-limit is reached",
        },
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert "usage-limit" in row["detail"]
    assert "reset 2026-09-06" in row["detail"]
    assert row["classification"] != "paused"


def test_a_metered_spend_limit_refusal_with_a_reset_still_reads_blocked(
    tmp_path,
) -> None:
    stream = _write_stream(tmp_path, "spend-limit", [_init()])
    pointer = _pointer(
        tmp_path,
        "r-spend-limit",
        stream=stream,
        alive=False,
        phase="blocked",
        budget={
            "headroom": "known",
            "utilisation_pct": 100.0,
            "rate_limit_type": "spend-limit",
            "resets_at": "2026-09-06T12:00:00+00:00",
            "threshold_status": "exhausted",
            "refusal": True,
            "detail": "backend refused the turn: the account's spend-limit is reached",
        },
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert "spend-limit" in row["detail"]


def test_a_genuinely_hung_live_process_with_no_bounded_wait_still_stalls(
    tmp_path,
) -> None:
    # The falsifier that keeps the stalled arm honest: a quiet stream whose last
    # tool call is ordinary work with no wait of its own is a hang, not a pause.
    now = time.time()
    stream = _write_stream(
        tmp_path,
        "hung",
        [_init(), _assistant_tool("Bash", "analyse the diagnostic dump")],
    )
    _quiet(stream, now=now)
    pointer = _pointer(tmp_path, "r-hung", stream=stream, alive=True, phase="working")

    snapshot = recovery._watch_snapshot(pointer, moment=now, stall_seconds=60)

    assert snapshot["state"] == "stalled"
    assert "quiet" in snapshot["detail"]


# ── Paused names what lifts it, and is never a block ───────────────────────


def test_a_rejected_rate_limit_window_reads_paused_with_its_reset_as_wake(
    tmp_path,
) -> None:
    # The clearest who-lifts-it case: the provider rejected the turn on a
    # window and named the moment the window turns over, so time lifts the hold
    # and nobody has to act. The paused verdict must name that reset.
    stream = _write_stream(
        tmp_path,
        "rejected-window",
        [
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "unifiedWindows": {
                        "five_hour": {
                            "utilization": 1.2,
                            "resetsAt": time.time() + 3600,
                            "windowDurationMins": 300,
                        }
                    },
                },
            }
        ],
    )
    pointer = _pointer(
        tmp_path, "r-rejected-window", stream=stream, alive=False, phase="working"
    )

    row = _classify(pointer)

    assert row["classification"] == "paused"
    assert row["classification"] != "blocked"
    assert "window" in row["detail"]
    assert "resumes" in row["detail"] or "resets" in row["detail"]


def test_a_rate_limit_refusal_with_no_expiry_stays_blocked(tmp_path) -> None:
    # Paused is reserved for waits with a wake that actually acts: the recovery
    # sweep auto-resumes only runs classified blocked, so a retry exhaustion
    # refusal with an unknown reset must keep reading blocked (its lift is the
    # sweep's lane re-probe, gated on blocked) rather than pausing into a wait
    # nothing resumes. This is the boundary that keeps paused from becoming the
    # bucket a forgotten run sits in.
    stream = _write_stream(tmp_path, "retry-refusal", [_init()])
    pointer = _pointer(
        tmp_path,
        "r-rate-limit",
        stream=stream,
        alive=False,
        phase="blocked",
        budget={
            "headroom": "known",
            "utilisation_pct": 100.0,
            "rate_limit_type": "rate-limit",
            "resets_at": "unknown",
            "threshold_status": "exhausted",
            "refusal": True,
            "detail": "the run died after 9 rate-limit retries; the lane is exhausted",
        },
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert "rate-limit" in row["detail"]
    assert row["classification"] != "paused"


def test_a_dead_process_parked_on_its_own_job_with_committed_work_reads_paused(
    tmp_path,
) -> None:
    # The parked run: the worker's last turn said it was waiting on background
    # work before finalizing the manifest, the in-progress manifest names
    # committed work, and the run is resumable when that work ends — nothing a
    # person has to do, so it pauses with the wake spelled out.
    stream = _write_stream(
        tmp_path,
        "parked",
        [
            {
                "type": "result",
                "is_error": False,
                "subtype": "success",
                "result": (
                    "Waiting for the background `tests/standard_names` suite run "
                    "to complete before finalizing the manifest."
                ),
            }
        ],
    )
    manifest = (
        "node: r-parked\n"
        "status: in-progress\n"
        "commits: 13185ff\n"
        "changed_paths: reckon/crew/recovery.py\n"
    )
    pointer = _pointer(
        tmp_path,
        "r-parked",
        stream=stream,
        alive=False,
        phase="complete",
        manifest=manifest,
    )

    row = _classify(pointer)

    assert row["classification"] == "paused"
    assert row["classification"] != "blocked"
    assert "committed work is safe" in row["detail"]
    assert "resumes when the background work" in row["detail"]


def test_the_same_parked_shape_without_committed_work_stays_blocked(tmp_path) -> None:
    # The boundary the discriminator must not blur: a background wait with
    # nothing committed is not a parked run with safe work — a reader should
    # still decide, so it keeps reading blocked.
    stream = _write_stream(
        tmp_path,
        "parked-nothing",
        [
            {
                "type": "result",
                "is_error": False,
                "subtype": "success",
                "result": (
                    "Waiting for the background `tests/standard_names` suite run "
                    "to complete before finalizing the manifest."
                ),
            }
        ],
    )
    pointer = _pointer(
        tmp_path, "r-parked-nothing", stream=stream, alive=False, phase="complete"
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert "waiting on background work" in row["detail"]


def test_a_quiet_alive_run_sleeping_in_a_poll_loop_reads_paused_not_stalled(
    tmp_path,
) -> None:
    # The run that fired the wrong stalled verdict: alive, quiet because it sits
    # in a long foreground poll sleeping in a loop while its compute-node job
    # runs. The bounded sleep means it wakes itself, so it must not read as a
    # hang, and it must not read as blocked either.
    now = time.time()
    stream = _write_stream(
        tmp_path,
        "poll-loop",
        [
            _init(),
            _assistant_tool(
                "Bash", "while :; do scheduler-status --job 7788; sleep 60; done"
            ),
        ],
    )
    _quiet(stream, now=now)
    pointer = _pointer(
        tmp_path, "r-poll-loop", stream=stream, alive=True, phase="working"
    )

    snapshot = recovery._watch_snapshot(pointer, moment=now, stall_seconds=60)

    assert snapshot["state"] == "waiting"
    assert snapshot["state"] not in recovery.FLEET_BLOCKED_STATES
    assert "sleep" in snapshot["detail"]
    # The classifier itself still reads the live process as running; it is the
    # monitor verb that says paused-family rather than stalled.
    assert _classify(pointer)["classification"] == "running"


def test_a_quiet_alive_run_in_a_peer_channel_bounded_read_reads_paused(
    tmp_path,
) -> None:
    # The peer-channel wait is the bounded read the dispatch surface itself
    # provides: a run parked on a peer question with a bounded read is waiting,
    # not hung, and not blocked.
    now = time.time()
    stream = _write_stream(
        tmp_path,
        "peer-read",
        [
            _init(),
            _assistant_tool(
                "Bash",
                "python -c 'from reckon.crew.dispatch import _peer_command; "
                "raise SystemExit(_peer_command())' peer-read --run r-x "
                "--question-id q-1 --wait 300",
            ),
        ],
    )
    _quiet(stream, now=now)
    pointer = _pointer(
        tmp_path, "r-peer-read", stream=stream, alive=True, phase="working"
    )

    snapshot = recovery._watch_snapshot(pointer, moment=now, stall_seconds=60)

    assert snapshot["state"] == "waiting"
    assert snapshot["state"] not in recovery.FLEET_BLOCKED_STATES
    assert "peer-channel" in snapshot["detail"]


def test_a_quiet_alive_run_in_a_rate_limit_retry_loop_reads_paused_not_stalled(
    tmp_path,
) -> None:
    # A live run throttled in a retry loop is not hung: the lane's window
    # resets on its own and the retry loop keeps the session alive, so a quiet
    # pause in it reads as paused-family, never as the "stalled" a hang earns.
    # The existing reading that a live retrying run classifies as running is
    # preserved — only the monitor verb changes, and only once it is quiet.
    now = time.time()
    retry = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 1,
        "max_retries": 10,
        "retry_delay_ms": 5000,
        "error": "rate_limit",
        "error_status": 429,
        "session_id": "sess-1",
    }
    stream = _write_stream(
        tmp_path,
        "retry-loop",
        [_init(), *[retry for _ in range(6)]],
    )
    _quiet(stream, now=now)
    pointer = _pointer(
        tmp_path, "r-retry-loop", stream=stream, alive=True, phase="working"
    )

    snapshot = recovery._watch_snapshot(pointer, moment=now, stall_seconds=60)

    assert snapshot["state"] == "waiting"
    assert snapshot["state"] not in recovery.FLEET_BLOCKED_STATES
    assert "retry loop" in snapshot["detail"] or "rate-limit" in snapshot["detail"]
    assert _classify(pointer)["classification"] == "running"


def test_a_live_run_with_a_rejected_window_stays_running_and_waits_not_stalls(
    tmp_path,
) -> None:
    # A live process is not paused on a rejected window — it is still alive and
    # may recover on its own — so it keeps reading running. Only once it goes
    # quiet does the stall gate name that rejected window as a wait rather than
    # a hang, so the monitor shows waiting-family and never "stalled".
    now = time.time()
    stream = _write_stream(
        tmp_path,
        "live-rejected",
        [
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "unifiedWindows": {
                        "five_hour": {
                            "utilization": 1.2,
                            "resetsAt": time.time() + 3600,
                            "windowDurationMins": 300,
                        }
                    },
                },
            }
        ],
    )
    _quiet(stream, now=now)
    pointer = _pointer(
        tmp_path, "r-live-rejected", stream=stream, alive=True, phase="working"
    )

    row = _classify(pointer)
    snapshot = recovery._watch_snapshot(pointer, moment=now, stall_seconds=60)

    assert row["classification"] == "running"
    assert snapshot["state"] == "waiting"
    assert snapshot["state"] not in recovery.FLEET_BLOCKED_STATES
    assert "rejected" in snapshot["detail"]


def test_only_a_live_process_sitting_in_a_wait_reads_paused(tmp_path) -> None:
    # The liveness gate on the pause correction: a dead process whose stream ends
    # in a sleep is not sitting in that sleep, it died after it — so it must not
    # read as a paused/waiting family run and vanish from the follower's report.
    now = time.time()
    stream = _write_stream(
        tmp_path, "dead-slept", [_init(), _assistant_tool("Bash", "sleep 300")]
    )
    _quiet(stream, now=now)
    pointer = _pointer(
        tmp_path, "r-dead-slept", stream=stream, alive=False, phase="working"
    )

    row = _classify(pointer)
    snapshot = recovery._watch_snapshot(pointer, moment=now, stall_seconds=60)

    assert row["classification"] == "abandoned"
    assert snapshot["state"] == "abandoned"
    assert snapshot["state"] != "waiting"
    # The dead run is still reported as stalled by the follower's own loop
    # (which gates the pause correction on liveness), so it cannot be lost.
    assert recovery._stall_wait_reason(pointer) is not None


def test_every_paused_verdict_names_what_will_lift_it(tmp_path) -> None:
    # The binding condition the coordinator who kept blocked insisted on: a
    # paused verdict is only safe if it names the wake, so the run cannot be
    # forgotten in a calm bucket a reader skips.
    scenarios = [
        (
            "rejected window",
            [
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "unifiedWindows": {
                            "five_hour": {
                                "utilization": 1.2,
                                "resetsAt": time.time() + 3600,
                                "windowDurationMins": 300,
                            }
                        },
                    },
                }
            ],
            None,
            None,
        ),
        (
            "parked on job",
            [
                {
                    "type": "result",
                    "is_error": False,
                    "subtype": "success",
                    "result": (
                        "Waiting for the background `tests/standard_names` suite "
                        "run to complete before finalizing the manifest."
                    ),
                }
            ],
            "node: r-parked\nstatus: in-progress\ncommits: 13185ff\n",
            None,
        ),
    ]
    wake_markers = re.compile(
        r"resumes|resets|lifts|recovers|when .+ (?:ends|recovers)|wake", re.IGNORECASE
    )

    for index, (name, events, manifest, budget) in enumerate(scenarios):
        run_id = f"r-paused-{index}"
        stream = _write_stream(tmp_path, run_id, events)
        pointer = _pointer(
            tmp_path,
            run_id,
            stream=stream,
            alive=False,
            phase="working",
            manifest=manifest,
            budget=budget,
        )
        row = _classify(pointer)
        assert row["classification"] == "paused", f"{name}: {row['classification']}"
        assert wake_markers.search(row["detail"]), (
            f"{name}: verdict names no wake: {row['detail']!r}"
        )


def test_drain_counts_a_paused_pointer_as_unreconciled(home, tmp_path) -> None:
    # The closure fence is the second half of the binding condition: a paused
    # pointer is unreconciled, because nobody is woken for it and the drain is
    # what stops it being a forgotten run. A still-working disposition must not
    # excuse it, exactly as it excuses only a genuinely running run.
    stream = _write_stream(
        tmp_path,
        "drain-paused",
        [
            {
                "type": "result",
                "is_error": False,
                "subtype": "success",
                "result": (
                    "Waiting for the background `tests/standard_names` suite "
                    "run to complete before finalizing the manifest."
                ),
            }
        ],
    )
    manifest = "node: r-drain-paused\nstatus: in-progress\ncommits: 13185ff\n"
    pointer = _pointer(
        tmp_path,
        "r-drain-paused",
        stream=stream,
        alive=False,
        phase="complete",
        manifest=manifest,
    )
    _write_json(pointer_path("r-drain-paused"), pointer)

    report = crew.drain(PROJECT)
    assert report["unreconciled_runs"] == 1
    assert report["runs"][0]["classification"] == "paused"
    assert report["runs"][0]["unreconciled"] is True
    assert report["runs"][0]["disposition_valid"] is False

    crew.record_run_disposition("r-drain-paused", "still-working", project=PROJECT)
    still = crew.drain(PROJECT)
    assert still["runs"][0]["classification"] == "paused"
    assert still["runs"][0]["unreconciled"] is True
    assert still["unreconciled_runs"] == 1
