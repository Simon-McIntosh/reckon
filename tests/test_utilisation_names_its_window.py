"""A reported utilisation names the window it was divided by, or says unknown.

A percentage whose denominator is unstated is not a reading: the recorded
figure is only meaningful when the reader can see exactly which window it was
divided by. The window a utilisation is divided by resolves from the
configured lane window — the figure that actually enforces a ceiling — never
from whatever window the client happened to announce. Two live figures on the
same lane disagreeing by about sixteen percent, and every recorded utilisation
splitting that difference by dividing over the announced one, is the measured
failure this file guards.

The same strictness applies at the percentage boundary. A figure the source
describes as a percentage must actually be on a percentage's scale: a
sub-one value is a ratio wearing a percentage's name, records as a plausible
low utilisation, and is refused rather than half-recorded.
"""

from __future__ import annotations

import json

from reckon import _backends

CLAUDE = {"launch": "cli", "command": "claude"}

PEAK_INPUT = 145_000
ANNOUNCED_WINDOW = 200_000
CONFIGURED_WINDOW = 172_800


def _claude_events(*, context_window: int | None) -> list[str]:
    """One assistant request plus one completed result for a window reading."""
    assistant = {
        "type": "assistant",
        "session_id": "sess-window",
        "message": {
            "usage": {"input_tokens": PEAK_INPUT},
            "content": [{"type": "text", "text": "work"}],
        },
    }
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 10_000,
        "result": "done",
        "modelUsage": {
            "model-a": {
                "inputTokens": PEAK_INPUT,
                "outputTokens": 100,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "contextWindow": context_window,
            }
        },
        "usage": {"output_tokens": 100},
    }
    return [json.dumps(event) for event in (assistant, result)]


def _observe(*, context_window: int | None, usable_input_window: int | None):
    backend = dict(CLAUDE)
    if usable_input_window is not None:
        backend["usable_input_window"] = usable_input_window
    return _backends.observe_stream(
        backend_name="probe",
        backend=backend,
        lines=_claude_events(context_window=context_window),
    )


def test_a_reported_utilisation_is_divided_by_the_window_it_names():
    """The denominator the reader can see is the one the figure was divided over.

    With no configured lane window the stream's own declared window is the only
    window a reader is told the run was measured against, so it stays the
    recorded basis — but the reading must still say so beside the figure.
    """
    throughput = _observe(
        context_window=ANNOUNCED_WINDOW, usable_input_window=None
    ).throughput

    assert throughput["input_budget_tokens"] == ANNOUNCED_WINDOW
    assert throughput["input_utilisation_pct"] == round(
        100 * PEAK_INPUT / ANNOUNCED_WINDOW, 1
    )
    assert f"divided by the stream window {ANNOUNCED_WINDOW}" in throughput["detail"]


def test_the_configured_lane_window_wins_over_an_announced_conflict():
    """A conflicting announced window is never preferred, never averaged with.

    The client announces 200,000 while the configured lane enforces 172,800.
    The reading must divide by the configured figure — the authority — exactly;
    splitting the difference would flatter the reading and is refused.
    """
    throughput = _observe(
        context_window=ANNOUNCED_WINDOW, usable_input_window=CONFIGURED_WINDOW
    ).throughput

    announced_pct = round(100 * PEAK_INPUT / ANNOUNCED_WINDOW, 1)
    pinned_pct = round(100 * PEAK_INPUT / CONFIGURED_WINDOW, 1)
    mid_window = (ANNOUNCED_WINDOW + CONFIGURED_WINDOW) / 2

    assert throughput["input_budget_tokens"] == CONFIGURED_WINDOW
    assert throughput["input_utilisation_pct"] == pinned_pct
    # never split the difference
    assert throughput["input_utilisation_pct"] != round(
        100 * PEAK_INPUT / mid_window, 1
    )
    # never silently prefer the announced one
    assert throughput["input_utilisation_pct"] != announced_pct
    assert pinned_pct > announced_pct
    assert (
        f"divided by the configured lane window {CONFIGURED_WINDOW}"
        in throughput["detail"]
    )


def test_an_unresolvable_window_leaves_the_utilisation_unknown():
    """No window anywhere is unknown, never a pleasing constant.

    Neither the stream nor the configured lane declares a window, so a
    percentage has no denominator to be divided by. The reading must report
    unknown rather than fabricate a division over zero, one, or any figure.
    """
    throughput = _observe(context_window=None, usable_input_window=None).throughput

    assert throughput["input_budget_tokens"] is None
    assert throughput["input_utilisation_pct"] is None
    assert "no window resolved, so utilisation is unknown" in throughput["detail"]


def test_a_ratio_where_a_percentage_is_expected_is_refused():
    """0.63 is a ratio, not a percentage, and must not record as a low figure.

    Recorded verbatim the sub-one value reads as a plausible 0.63% of the
    window — a quiet failure that runs toward headroom. The strict read
    refuses the whole answer instead.
    """
    dialect = _backends.dialect_for({"command": "codex"})
    answer = {
        "result": {
            "rateLimits": {
                "primary": {
                    "usedPercent": 0.63,
                    "windowDurationMins": 300,
                    "resetsAt": 1_790_000_000,
                },
                "secondary": {
                    "usedPercent": 41,
                    "windowDurationMins": 10_080,
                    "resetsAt": 1_790_600_000,
                },
            }
        }
    }

    budget = dialect.read_probe(answer)

    assert budget["headroom"] == "unknown"
    assert budget["utilisation_pct"] is None
    assert "ratio where a percentage is expected" in budget["detail"]


def test_whole_number_percentages_still_parse_into_a_reading():
    """The strict parse refuses only the ratio-shaped figure, not the scale."""
    dialect = _backends.dialect_for({"command": "codex"})
    answer = {
        "result": {
            "rateLimits": {
                "primary": {
                    "usedPercent": 41,
                    "windowDurationMins": 300,
                    "resetsAt": 1_790_000_000,
                },
                "secondary": {
                    "usedPercent": 82,
                    "windowDurationMins": 10_080,
                    "resetsAt": 1_790_600_000,
                },
            }
        }
    }

    budget = dialect.read_probe(answer)

    assert budget["headroom"] == "known"
    assert budget["utilisation_pct"] == 82.0
