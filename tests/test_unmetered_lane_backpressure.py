"""An unmetered lane's exhausted retry stream is backpressure, not a spent budget.

The local lane reports a full consumer on ``system/api_retry`` records carrying
``error: "rate_limit"`` and ``error_status: 429``, and the stream cannot
distinguish that kind of 429 from a spent metered consumer — every sampled
retry record carries the identical field set. The only discriminator available
is the backend's meteredness: an unmetered backend has no metered window at
all, so its retry exhaustion cannot be budget exhaustion by construction.

Recording the unmetered shape as a budget refusal converts a transient capacity
condition into a metered-budget hold on recovery: the refusal gated ``crew
resume`` on the dead run for the evidence shelf life even though the lane was
merely full, and ``crew observe`` on a served live run could not lift it. The
block is therefore recorded as lane backpressure instead — it names the retry
count, says the lane refused, and a dispatch fence may hold new work on it, but
it carries none of the metered-budget semantics (``refusal`` stays false,
``headroom`` stays unknown) that gate an existing run's resume.

The falsifiers both directions are pinned here. A metered backend keeps the
budget refusal on the identical wire shape and keeps the reset time on a
usage-limit refusal, because that path is what still holds a wave correctly.
And a run that merely retried and recovered produces no refusal of either kind.
"""

from __future__ import annotations

import json

from reckon import _backends


def _observe(
    events: list[dict],
    *,
    backend_name: str,
    command: str = "clive",
):
    """Observe a synthetic stream exactly as a run record's reader would."""
    return _backends.observe_stream(
        backend_name=backend_name,
        backend={"launch": "cli", "command": command},
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


# ── The unmetered shape is backpressure ──────────────────────────────────────


def test_unmetered_retry_exhaustion_records_backpressure_not_a_budget_refusal():
    """A dead local-lane run reads as lane backpressure, never as spent budget.

    The recorded message carries no recognised usage/spend phrase and the lane
    has no metered window to spend, so the refusal must carry no metered-budget
    semantics: a resume resolver that keys on ``refusal`` and the exhausted
    threshold must not hold the dead run.
    """
    events = [_retry() for _ in range(15)]
    events.append(_result(is_error=True))

    observation = _observe(events, backend_name="clive")

    assert observation.exit_status == "error"
    assert observation.budget["refusal"] is False
    assert observation.budget["lane_backpressure"] is True
    assert observation.budget["headroom"] == "unknown"
    assert observation.budget["threshold_status"] not in {"exhausted", "rejected"}
    assert _backends.budget_exhausted(observation.budget) is None
    assert "15" in observation.budget["detail"]
    assert "the lane is exhausted" not in observation.budget["detail"]
    assert observation.phase == "failed"


def test_clive_glm_shares_the_unmetered_discriminator():
    """Both local lane names record the identical backpressure shape.

    ``clive-glm`` is a backend name whose route runs the ``clive`` command, so
    the command selects the dialect while the configured name drives the
    meteredness discriminator.
    """
    events = [_retry() for _ in range(10)]
    events.append(_result(is_error=True))

    observation = _observe(events, backend_name="clive-glm")

    assert observation.budget["refusal"] is False
    assert observation.budget["lane_backpressure"] is True
    assert _backends.budget_exhausted(observation.budget) is None


# ── Falsifier: the metered lane keeps its refusal ────────────────────────────


def test_metered_retry_exhaustion_still_records_a_budget_refusal():
    """The identical wire shape on a metered backend stays a spent-lane refusal.

    The account that pays per token can genuinely exhaust its window where an
    unmetered lane cannot, so the retry exhaustion keeps the budget refusal on
    ``claude`` — a fix that flattened this would remove the protection that
    holds a wave before it dispatches into a spent lane.
    """
    events = [_retry() for _ in range(10)]
    events.append(_result(is_error=True))

    observation = _observe(events, backend_name="claude")

    assert observation.budget["refusal"] is True
    assert observation.budget["headroom"] == "known"
    assert observation.budget.get("lane_backpressure") is not True
    assert _backends.budget_exhausted(observation.budget) is True
    assert "the lane is exhausted" in observation.budget["detail"]
    assert observation.phase == "blocked"


def test_metered_usage_limit_refusal_keeps_its_reset_time():
    """A metered usage refusal still names the moment the limit lifts.

    The prose-refusal path is untouched by the retry discriminator. Even with a
    retry-bearing stream, the recognised usage-limit phrase wins and keeps its
    reset time, because a stated reset is the stronger evidence and the one a
    wave fences on.
    """
    events = [_retry() for _ in range(6)]
    events.append(
        _result(
            is_error=True,
            message=(
                "You've hit your usage limit. Please try again at Sep 10, 2026, 8:00 PM"
            ),
        )
    )

    observation = _observe(events, backend_name="claude")

    assert observation.budget["refusal"] is True
    assert observation.budget["resets_at"] is not None
    assert observation.budget.get("lane_backpressure") is not True
    assert observation.phase == "blocked"


# ── Falsifier: retrying and recovering is no refusal of either kind ──────────


def test_unmetered_retries_that_recover_hold_nothing_of_either_kind():
    """A busy local lane that completes after retries must not be read as spent.

    Measured runs completed with seven retries each and committed real work; a
    reading that held them would stall the fleet on every busy lane, so the
    recovered run records neither a budget refusal nor a backpressure marker.
    """
    events = [_retry() for _ in range(7)]
    events.append(_result(is_error=False))

    observation = _observe(events, backend_name="clive")

    assert observation.phase == "complete"
    assert observation.exit_status == "ok"
    assert observation.budget["refusal"] is False
    assert observation.budget.get("lane_backpressure") is not True
    assert _backends.budget_exhausted(observation.budget) is None


def test_unmetered_retries_in_flight_hold_nothing_of_either_kind():
    """A mid-retry-burst stream without a terminal result never holds.

    Death is the process table's call, not the observer's; the count is still
    surfaced so the run does not report that it carries no rate-limit signal.
    """
    events = [_retry() for _ in range(14)]

    observation = _observe(events, backend_name="clive")

    assert observation.terminal is False
    assert observation.phase == "working"
    assert observation.budget["refusal"] is False
    assert observation.budget.get("lane_backpressure") is not True
    assert _backends.budget_exhausted(observation.budget) is None
    assert "14" in observation.budget["detail"]


def test_overload_retries_are_capacity_on_the_local_lane_too():
    """A 529 overload retry is not backpressure on the local lane either.

    Only the rate-limit-shaped 429 retry opens the lane-refused marker; an
    overload is server capacity on any lane and opens neither a refusal nor a
    backpressure hold.
    """
    events = [_retry(cause="overloaded", status=529) for _ in range(9)]
    events.append(_result(is_error=True, message="server overloaded"))

    observation = _observe(events, backend_name="clive")

    assert observation.exit_status == "error"
    assert observation.budget["refusal"] is False
    assert observation.budget.get("lane_backpressure") is not True
    assert observation.budget["headroom"] == "unknown"
