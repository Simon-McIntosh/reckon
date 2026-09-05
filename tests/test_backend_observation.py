"""What a run record can say about a worker's speed and its backend's headroom.

Every case here reads a stream recorded from a real invocation
(``tests/fixtures/backends/``) rather than starting a model. That is the only
way these two signals can be tested at all: a generation rate needs a run that
actually generated, and a quota refusal needs an account that was actually
spent, and neither is reproducible on demand.

The two halves fail in opposite directions, so they are asserted separately. A
missing rate is merely uninformative. A misread refusal is worse than missing:
reading an ordinary failure as exhaustion holds every later wave on evidence
that was never a measurement, and reading a real refusal as silence is what let
a pre-flight report a clear backend for six days while it was exhausted.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from reckon import _backends

FIXTURES = Path(__file__).parent / "fixtures" / "backends"

CLAUDE = {"launch": "cli", "command": "claude"}
CODEX = {"launch": "cli", "command": "codex"}


def observe(fixture: str, backend: dict[str, str], **kwargs: object):
    """Observe a recorded stream exactly as a run record's reader would."""
    return _backends.observe_log(
        backend_name="probe",
        backend=backend,
        log_path=FIXTURES / fixture,
        **kwargs,
    )


# ── Throughput ──────────────────────────────────────────────────────────────


def test_generation_rate_is_measured_over_the_span_the_stream_reports():
    """Tokens over the reported inference span, not over the wall clock."""
    throughput = observe("claude-worked-turn.jsonl", CLAUDE).throughput

    assert throughput["generated_tokens"] == 62888
    assert throughput["generation_seconds"] == 664.405
    expected = round(62888 / 664.405, 2)
    assert throughput["tokens_per_second"] == expected


def test_machine_seconds_is_reported_apart_from_generation():
    """The node's own suite must not be charged to the model's speed.

    Two thirds of this run was inference and the rest was the worker waiting on
    its tools, so the wall rate is materially lower than the generation rate.
    Collapsing the two would report this model as a third slower than it is.

    This fixture's own events carry no timestamps (elided, see the fixtures'
    README), so the machine span here falls back to elapsed minus the
    backend's reported inference span — the same arithmetic a stream that does
    carry timestamps replaces with a direct measurement; see
    ``test_generation_and_machine_seconds_are_measured_from_event_timestamps``.
    """
    throughput = observe("claude-worked-turn.jsonl", CLAUDE).throughput

    assert throughput["elapsed_seconds"] == 886.713
    assert throughput["machine_seconds"] == round(886.713 - 664.405, 3)
    assert throughput["wall_tokens_per_second"] == round(62888 / 886.713, 2)
    assert throughput["wall_tokens_per_second"] < throughput["tokens_per_second"]


def _assistant_event(timestamp: str, *, tool_use: bool = False) -> dict:
    """One assistant turn at a stated moment, optionally requesting a tool."""
    block = {"type": "tool_use", "name": "probe"} if tool_use else {"type": "text", "text": "…"}
    return {
        "type": "assistant",
        "session_id": "sess-synthetic",
        "timestamp": timestamp,
        "message": {"content": [block]},
    }


def _result_event(*, duration_ms: float, output_tokens: int) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": duration_ms,
        "usage": {"output_tokens": output_tokens},
    }


def _observe_synthetic(events: list[dict]):
    return _backends.observe_stream(
        backend_name="probe",
        backend=CLAUDE,
        lines=[json.dumps(event) for event in events],
    )


def test_generation_and_machine_seconds_are_measured_from_event_timestamps():
    """A long tool span is read straight from the stream's own timestamps.

    No backend-reported inference span is supplied at all here — this is the
    plan's point: nothing needs the harness's own totals when the stream
    already carries a timestamp per event, so the split between waiting on
    the machine and the model's own time comes from the stream alone.
    """
    events = [
        _assistant_event("2026-01-01T00:00:00.000Z"),
        _assistant_event("2026-01-01T00:00:02.000Z", tool_use=True),
        {
            "type": "user",
            "timestamp": "2026-01-01T00:10:02.000Z",
            "message": {"content": [{"type": "tool_result"}]},
        },
        _assistant_event("2026-01-01T00:10:05.500Z"),
        _result_event(duration_ms=605_500, output_tokens=1000),
    ]

    throughput = _observe_synthetic(events).throughput

    assert throughput["machine_seconds"] == 600.0
    assert throughput["generation_seconds"] == 5.5
    assert throughput["elapsed_seconds"] == 605.5
    assert (
        abs(
            (throughput["generation_seconds"] + throughput["machine_seconds"])
            - throughput["elapsed_seconds"]
        )
        < 0.01
    )
    assert throughput["tokens_per_second"] == round(1000 / 5.5, 2)


def test_generation_and_machine_seconds_with_no_tool_span():
    """A stream that never calls a tool has a real, measured machine span of zero.

    Zero is a measurement here, not the absence of one — every gap in this
    stream is generation, so machine seconds is exactly 0.0 rather than
    unknown.
    """
    events = [
        _assistant_event("2026-01-01T00:00:00.000Z"),
        _assistant_event("2026-01-01T00:00:04.250Z"),
        _result_event(duration_ms=4_250, output_tokens=50),
    ]

    throughput = _observe_synthetic(events).throughput

    assert throughput["machine_seconds"] == 0.0
    assert throughput["generation_seconds"] == 4.25
    assert throughput["elapsed_seconds"] == 4.25


def test_generation_and_machine_seconds_are_unknown_without_usable_timestamps():
    """Absence of a timestamp signal is never read as a measurement of zero.

    This recorded stream's events carry no timestamps at all (verified by the
    fixture itself, not assumed), and no span is supplied by the caller
    either, so neither figure has a basis and both stay unknown.
    """
    throughput = observe("codex-turn.jsonl", CODEX).throughput

    assert throughput["generation_seconds"] is None
    assert throughput["machine_seconds"] is None


def test_peak_input_is_reported_against_the_declared_budget():
    """The largest single prompt, against the window it was sent into."""
    throughput = observe("claude-worked-turn.jsonl", CLAUDE).throughput

    assert throughput["peak_input_tokens"] == 242912
    assert throughput["input_budget_tokens"] == 1_000_000
    assert throughput["input_utilisation_pct"] == round(100 * 242912 / 1_000_000, 1)


def test_peak_input_counts_the_cached_part_of_a_prompt():
    """A cached segment still occupies the window.

    The uncached input on every request of this run is two tokens. Reading that
    field alone reports a two-token prompt for a request carrying a quarter of a
    million, which would say a run at its ceiling had all its room left.
    """
    throughput = observe("claude-worked-turn.jsonl", CLAUDE).throughput

    assert throughput["peak_input_tokens"] > 200_000


def test_a_span_supplied_by_the_caller_rates_a_stream_that_reports_none():
    """One harness times its turn; the other does not, and is rated anyway."""
    throughput = observe("codex-turn.jsonl", CODEX, elapsed_seconds=100.0).throughput

    assert throughput["generated_tokens"] == 5
    assert throughput["elapsed_seconds"] == 100.0
    assert throughput["wall_tokens_per_second"] == 0.05
    # No inference span was reported, so none is claimed.
    assert throughput["generation_seconds"] is None
    assert throughput["tokens_per_second"] is None


def test_an_unrated_stream_says_so_rather_than_reporting_zero():
    """No span and no completed turn is unknown, never a rate of nothing."""
    throughput = observe("codex-failed-turn.jsonl", CODEX).throughput

    assert throughput["tokens_per_second"] is None
    assert throughput["wall_tokens_per_second"] is None
    assert throughput["detail"]


# ── Quota refusal ───────────────────────────────────────────────────────────


def test_a_usage_limit_refusal_is_read_as_exhausted_headroom():
    """A refusal states headroom in prose; folding it to unknown loses it."""
    observation = observe("codex-usage-limit.jsonl", CODEX)

    assert observation.exit_status == "error"
    assert _backends.budget_exhausted(observation.budget) is True
    assert observation.budget["headroom"] == "known"
    assert observation.budget["threshold_status"] == "exhausted"


def test_a_refusal_carries_the_moment_the_hold_lifts():
    """Without the reset a hold has no end, so it must survive the parse.

    The recorded moment is local wall clock with no zone, so the assertion is on
    the wall clock — stamping it as UTC would move the expiry by this machine's
    offset, releasing a wave early or holding it late.
    """
    budget = observe("codex-usage-limit.jsonl", CODEX).budget

    resets_at = budget["resets_at"]
    assert resets_at is not None
    moment = datetime.fromisoformat(resets_at)
    assert (moment.year, moment.month, moment.day) == (2026, 9, 8)
    assert (moment.hour, moment.minute) == (7, 38)
    assert moment.tzinfo is not None


def test_a_stream_with_no_rate_limit_signal_stays_unknown():
    """Absence of evidence is never read as exhaustion."""
    budget = observe("codex-turn.jsonl", CODEX).budget

    assert budget["headroom"] == "unknown"
    assert _backends.budget_exhausted(budget) is None


def test_an_ordinary_failed_turn_is_not_read_as_exhaustion():
    """This turn failed on an unusable model id, with budget to spare."""
    observation = observe("codex-failed-turn.jsonl", CODEX)

    assert observation.exit_status == "error"
    assert observation.budget["headroom"] == "unknown"
    assert _backends.budget_exhausted(observation.budget) is None


def test_a_refusal_message_without_a_reset_is_still_a_refusal():
    """The limit is the measurement; the reset only bounds how long it holds."""
    budget = _backends.refusal_budget("You've hit your usage limit.")

    assert budget is not None
    assert _backends.budget_exhausted(budget) is True
    assert budget["resets_at"] is None


def test_a_context_overflow_is_not_a_quota_refusal():
    """Both end the turn; only one says anything about the account."""
    assert (
        _backends.refusal_budget(
            "Codex ran out of room in the model's context window. Start a new "
            "thread or clear earlier history before retrying."
        )
        is None
    )


# ── Rate-limit windows ───────────────────────────────────────────────────────


def _claude_rate_limit_info(fixture: str) -> dict:
    """The first ``rate_limit_event``'s payload from a recorded stream."""
    for line in (FIXTURES / fixture).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        if event.get("type") == "rate_limit_event":
            return event["rate_limit_info"]
    raise AssertionError(f"no rate_limit_event in {fixture}")


def test_a_metered_window_yields_its_own_utilisation_and_reset():
    """The gating figure is the window's, not the account's calendar position."""
    info = _claude_rate_limit_info("claude-worked-turn.jsonl")
    dialect = _backends.dialect_for(CLAUDE)

    block = dialect._budget(info)

    assert block["headroom"] == "known"
    assert block["utilisation_pct"] == 61.0
    assert block["rate_limit_type"] == "five_hour"
    assert block["resets_at"] == _backends._epoch_to_iso(
        info["unifiedWindows"]["five_hour"]["resetsAt"]
    )


def test_the_account_overage_figure_never_reaches_utilisation_pct():
    """A month-scale spend counter must never be read as a rate-limit percent.

    Measured on a serving lane: the account figure read utilization 1.21
    (121%) while the five-hour window it was 54 turns into read 0.05 (5%).
    Reading the account figure as a percentage would report a lane at 121%
    utilisation that was in fact five percent through its actual window, and
    the account figure's own reset sits on a calendar-month boundary rather
    than the window's much nearer one.
    """
    info = {
        "status": "allowed_warning",
        "rateLimitType": "overage",
        "resetsAt": 1790812800,
        "surpassedThreshold": 1,
        "utilization": 1.21,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.05, "resetsAt": 1788447600},
        },
    }
    dialect = _backends.dialect_for(CLAUDE)

    block = dialect._budget(info)

    assert block["utilisation_pct"] == 5.0
    assert block["utilisation_pct"] != 1.21
    assert block["rate_limit_type"] == "five_hour"
    assert block["resets_at"] == _backends._epoch_to_iso(1788447600)
    assert block["resets_at"] != _backends._epoch_to_iso(1790812800)


def test_an_event_with_no_unified_windows_reports_unknown_headroom():
    """The account fraction is never a fallback when the window figure is absent."""
    info = _claude_rate_limit_info("claude-turn.jsonl")
    assert "unifiedWindows" not in info
    dialect = _backends.dialect_for(CLAUDE)

    block = dialect._budget(info)

    assert block["headroom"] == "unknown"
    assert block["utilisation_pct"] is None


# ── The record ──────────────────────────────────────────────────────────────


def test_both_signals_reach_the_serialised_record():
    """A signal a run record cannot carry is a signal nobody reads."""
    record = observe("claude-worked-turn.jsonl", CLAUDE).as_dict()

    assert record["throughput"]["tokens_per_second"] > 0
    assert record["throughput"]["input_utilisation_pct"] > 0
    assert record["budget"]["headroom"] in {"known", "unknown"}


def test_a_stream_that_has_not_started_reports_neither_signal_as_zero():
    """An absent log is a run with nothing measured, not a stalled one."""
    observation = _backends.observe_log(
        backend_name="probe",
        backend=CODEX,
        log_path=FIXTURES / "no-such-stream.jsonl",
    )

    assert observation.phase == "starting"
    assert observation.throughput["tokens_per_second"] is None
    assert observation.budget["headroom"] == "unknown"
