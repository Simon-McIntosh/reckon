"""Contract tests for the worker-backends reference.

The reference records what measurement established about the local lane so a
later reader cannot quietly lose it: that the concurrency ceiling belongs to
the deployment and is discovered, that an unmetered lane's 429 is backpressure
rather than exhaustion, that stderr carries no status, and that one manifest
label names two distinct failures. These tests assert the claims are present in
the document. They assert no figure, because the claims mean exactly that the
deployment's numbers move and must be discovered.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def normalized(text: str) -> str:
    return " ".join(text.split())


def reference() -> str:
    """The worker-backends reference, read verbatim from the repository."""
    return normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "worker-backends.md"
        ).read_text()
    )


def test_ceiling_belongs_to_the_deployment_and_is_discovered_never_assumed() -> None:
    """The reference records that the lane's ceiling is the deployment's and
    that reckon carries none, and states the live-runs-versus-simultaneous-
    requests unit mismatch — without pinning a figure for any of it."""
    text = reference()
    assert "the deployment" in text
    assert "discovered, never assumed" in text
    assert "reckon carries none" in text
    assert "coarse bound" in text
    assert "holds open" in text
    assert "not a model of the serving queue" in text
    assert "live runs" in text
    assert "simultaneous requests" in text
    assert "not aligned" in text


def test_an_unmetered_lane_429_is_backpressure_not_exhaustion() -> None:
    """The unmetered reading has no reset time, clears when a slot frees, and
    classifies the run as blocked and resumable rather than abandoned; the
    metered reading carries a reset and the discriminator is declared
    meteredness, because on the wire the two are identical."""
    text = reference()
    assert "backpressure, not exhaustion" in text
    assert "unmetered" in text
    assert "no reset time" in text
    assert "blocked and resumable" in text
    assert "abandoned" in text
    assert "declared meteredness" in text
    assert "reset moment" in text


def test_both_lane_readings_key_on_the_structured_retry_record() -> None:
    """The refusal sentence is a strict and unreliable subset of the record,
    so the classification keys on the structured retry record — and the rule
    is stated both where backpressure is explained and again where the two
    manifest failures are separated, because that is where it does its work."""
    text = reference()
    assert text.count("structured retry record") >= 2
    assert "never on the refusal sentence" in text
    assert "unreliable subset" in text


def test_stderr_carries_no_status_for_the_lane_on_any_outcome() -> None:
    """The one conspicuous stderr line, an unrecognized-model complaint naming
    the served model, appears on healthy runs too, so the status surface is
    the stream rather than the log."""
    text = reference()
    assert "stderr" in text
    assert "no status" in text
    assert "unrecognized-model" in text
    assert "healthy" in text
    assert "stream.jsonl" in text


def test_one_label_names_two_distinct_failures() -> None:
    """The manifest label covers retry exhaustion mid-turn and a completed turn
    with the deliverable written and only the manifest missing; the two
    separate on the retry count and the presence of a terminal turn record."""
    text = reference()
    assert re.search(r"process gone without a complete manifest", text, re.IGNORECASE)
    assert "two distinct failures" in text
    assert "retry exhaustion mid-turn" in text
    assert "terminal turn record" in text
    assert "retry count" in text


def test_the_reference_records_that_it_records_no_ceiling_figure() -> None:
    """The claim is that the number is the deployment's, moves, and must be
    discovered — so the reference states that it keeps no number rather than
    pinning one. A figure written down would falsify the claim itself."""
    text = reference()
    assert "records no number" in text
    assert "moves" in text
    assert "discover" in text
