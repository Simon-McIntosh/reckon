"""The local lane writes its exhaustion as retries, not as a rate-limit event.

The claude-flavoured run stream carries budget headroom in a
``rate_limit_event`` record. The lane served by ``clive`` never emits one: it
reports a spent consumer as ``system/api_retry`` records carrying
``error: "rate_limit"`` and ``error_status: 429``, until either a terminal
``result`` with ``is_error: true`` or a process that dies before any terminal
event. Before this dialect read retries, a run that died of rate limiting was
recorded with ``headroom: "unknown"``, ``refusal: false`` and the detail "no
rate-limit event in the stream yet" — indistinguishable from a lane that was
never measured, so the next wave dispatched into the lane that had just shed
its workers.

The verdict is the terminal shape, never the retry count: busy lanes carry
rate-limit retries and complete with work committed (measured: successful runs
carried seven retries), so a count alone matches both a healthy run and a dead
one. A refusal is recorded only when rate-limit retries end in an error result.
"""

from __future__ import annotations

import json

from reckon import _backends

CLIVE = {"launch": "cli", "command": "clive"}
CLAUDE = {"launch": "cli", "command": "claude"}


def _observe(events: list[dict], backend: dict[str, str] = CLIVE):
    """Observe a synthetic stream exactly as a run record's reader would."""
    return _backends.observe_stream(
        backend_name=backend["command"],
        backend=backend,
        lines=[json.dumps(event) for event in events],
    )


def _retry(*, cause: str = "rate_limit", status: int = 429) -> dict:
    """One ``system/api_retry`` record in the recorded lane's own shape."""
    return {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 1,
        "max_retries": 10,
        "retry_delay_ms": 5000,
        "error": cause,
        "error_status": status,
        "session_id": "sess-local",
    }


def _result(*, is_error: bool, message: str = "") -> dict:
    """One terminal ``result`` event; the error text defaults to a lane shape."""
    if is_error and not message:
        message = "API Error: Request rejected (429) · consumer queue full"
    return {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": message,
        "api_error_status": 429 if is_error else 200,
    }


# ── The exhaustion shape ─────────────────────────────────────────────────────


def test_rate_limit_retries_ending_in_an_error_result_report_a_refusal():
    """The exhaustion shape: many retries, then a terminal error result.

    The recorded message carries no recognised usage/spend phrase, which is
    exactly the run that previously fell through to "no rate-limit event in the
    stream yet" despite having died on the lane's own rate-limit signal.
    """
    events = [_retry() for _ in range(15)]
    events.append(_result(is_error=True))

    observation = _observe(events)

    assert observation.exit_status == "error"
    assert observation.budget["refusal"] is True
    assert observation.budget["headroom"] == "known"
    assert _backends.budget_exhausted(observation.budget) is True
    assert "rate-limit" in observation.budget["detail"]
    assert "no rate-limit event in the stream yet" not in observation.budget["detail"]
    assert observation.phase == "blocked"


def test_claude_retries_speak_the_same_wire_language_as_the_local_lane():
    """``claude`` and ``clive`` share a dialect, so the signal is not a lane quirk.

    The backend command selects the dialect; both names must refuse on the same
    exhaustion shape, so renaming the lane in configuration cannot lose the
    signal the observer now reads.
    """
    events = [_retry() for _ in range(10)]
    events.append(_result(is_error=True))

    clive_observation = _observe(events, CLIVE)
    claude_observation = _observe(events, CLAUDE)

    assert clive_observation.budget["refusal"] is True
    assert claude_observation.budget["refusal"] is True


# ── The falsifier: retrying is not refusing ──────────────────────────────────


def test_rate_limit_retries_ending_in_success_hold_nothing():
    """A busy lane completes with retries carried, never reading as spent.

    Two runs that completed normally tonight each carried seven rate-limit
    retries and committed real work; a reading that treated the count alone as
    exhaustion would hold the whole fleet on every busy lane.
    """
    events = [_retry() for _ in range(7)]
    events.append(_result(is_error=False))

    observation = _observe(events)

    assert observation.phase == "complete"
    assert observation.exit_status == "ok"
    assert observation.budget["refusal"] is False
    assert _backends.budget_exhausted(observation.budget) is None


def test_retries_with_no_terminal_result_report_facts_not_a_verdict():
    """A mid-flight stream (retries, no result event) never holds.

    A busy lane mid-retry-burst is indistinguishable from this by stream alone;
    death is the process table's call, not the observer's. The count is still
    surfaced, so the run does not report that it carries no rate-limit signal.
    """
    events = [_retry() for _ in range(14)]

    observation = _observe(events)

    assert observation.terminal is False
    assert observation.phase == "working"
    assert observation.budget["refusal"] is False
    assert _backends.budget_exhausted(observation.budget) is None
    assert "14 rate-limit retries" in observation.budget["detail"]
    assert "no rate-limit event in the stream yet" not in observation.budget["detail"]


def test_retries_for_server_overload_are_not_a_rate_limit_refusal():
    """A 529 overload retry is capacity, not a spent lane.

    The lane's own record vocabulary distinguishes the causes; a death after
    overload retries must not record a rate-limit refusal it never carried.
    """
    events = [_retry(cause="overloaded", status=529) for _ in range(9)]
    events.append(_result(is_error=True, message="server overloaded"))

    observation = _observe(events)

    assert observation.exit_status == "error"
    assert observation.budget["refusal"] is False
    assert observation.budget["headroom"] == "unknown"


# ── The unchanged cases ──────────────────────────────────────────────────────


def test_a_stream_with_no_retries_is_unchanged():
    """Absence of the retry signal changes nothing about the default reading."""
    events = [_result(is_error=False)]

    observation = _observe(events)

    assert observation.budget["detail"] == "no rate-limit event in the stream yet"
    assert observation.budget["headroom"] == "unknown"
    assert observation.budget["refusal"] is False


def test_an_ordinary_failed_turn_without_retries_is_not_a_refusal():
    """A failed turn on another cause (a bad model) is not a spent lane."""
    events = [
        _result(
            is_error=True,
            message="There's an issue with the selected model (no-such-model-xyz).",
        )
    ]

    observation = _observe(events)

    assert observation.exit_status == "error"
    assert observation.phase == "failed"
    assert observation.budget["refusal"] is False
    assert _backends.budget_exhausted(observation.budget) is None


def test_the_rate_limit_event_path_is_unchanged():
    """A ``rate_limit_event`` stream still reads its own windows.

    The claude-harness budget path is untouched by the retry reading; a stream
    carrying the event reports its window utilisation rather than a refusal.
    """
    events = [
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed_warning",
                "rateLimitType": "overage",
                "resetsAt": 1790812800,
                "surpassedThreshold": 1,
                "utilization": 1.21,
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.05, "resetsAt": 1788447600},
                },
            },
        },
        _result(is_error=False),
    ]

    observation = _observe(events)

    assert observation.budget["headroom"] == "known"
    assert observation.budget["utilisation_pct"] == 5.0
    assert observation.budget["refusal"] is False
    assert _backends.budget_exhausted(observation.budget) is False
