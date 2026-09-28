"""The lane reading a dispatch carries says what the lane is doing and how fast.

The reading is composed once per dispatch and rides in the payload beside the
routing decision, so a coordinator weighing whether to send the next node to
this lane reads the lane's own load and rate without a second call. Two things
have to hold for that to be worth carrying. The counts must stay apart: a lane
serving seven requests while three wait is not a lane serving ten. And a figure
the document did not publish must read as unknown rather than as zero, because
a zero is a measurement and an unpublished count is not -- a lane nobody is
using and a lane nobody can measure must not read alike.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from tests import test_dispatch_names_its_backend as harness

pytest_plugins = ("tests.test_dispatch_names_its_backend",)

# The lane's own names for the two populations, and the rate it achieved across
# the generating one. Figures are chosen to be unambiguous: the mean and the
# aggregate differ, and neither equals the run count they were divided by.
GENERATING = 7
WAITING = 3
MEAN_TOKENS_PER_SECOND = 1.44
AGGREGATE_TOKENS_PER_SECOND = 53.2
RUNS = 37


def _stamp(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    lane_document: Path,
):
    """Dispatch with the lane document the test wrote, through the CLI payload."""
    config = copy.deepcopy(harness.CONFIG)
    config["backends"]["beta"]["lane_document"] = str(lane_document)
    monkeypatch.setattr(
        cli_module, "_resolved_flight", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_args, **_kwargs: None
    )
    result = CliRunner().invoke(
        cli_module.main,
        [*harness._arguments(repo, node=node), "--backend", "beta"],
    )
    return harness._payload(result), result


def _write_lane_document(config_home: Path, **extra: object) -> Path:
    """Publish a lane document inside the temporary config home the run resolves."""
    path = config_home / "lane-reading.json"
    path.write_text(json.dumps(extra), encoding="utf-8")
    return path


def test_the_payload_carries_the_lane_counts_and_its_achieved_rate(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = dispatch_repo.parent / "config"
    assert config_home.is_dir(), "the temporary config home the fixture built"
    reading_stamp = _stamp(10)
    rate_stamp = _stamp(90)
    lane_path = _write_lane_document(
        config_home,
        running=GENERATING,
        waiting=WAITING,
        headroom=4.0,
        mean_context=120000.0,
        observed_at=reading_stamp,
        throughput={
            "mean_tokens_per_second": MEAN_TOKENS_PER_SECOND,
            "aggregate_tokens_per_second": AGGREGATE_TOKENS_PER_SECOND,
            "runs": RUNS,
            "observed_at": rate_stamp,
        },
    )

    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="carries-the-counts", lane_document=lane_path
    )

    assert result.exit_code == 0
    reading = payload["lane_reading"]
    assert reading["state"] == "fresh"
    # Generating and waiting arrive as two figures rather than one load total,
    # which is what lets a coordinator see a lane serving while others queue.
    assert reading["generating"] == GENERATING
    assert reading["waiting"] == WAITING
    assert reading["generating"] != reading["waiting"]
    assert reading["observed_at"] == reading_stamp

    rate = reading["throughput"]
    assert rate["state"] == "measured"
    assert rate["mean_tokens_per_second"] == MEAN_TOKENS_PER_SECOND
    assert rate["aggregate_tokens_per_second"] == AGGREGATE_TOKENS_PER_SECOND
    # The denominator travels with the derived figure: 1.44 tok/s is the mean
    # over 37 runs, not over whatever population a reader assumes.
    assert rate["runs"] == RUNS
    # The vintage travels with the figure too, and it is the rate's own window
    # rather than the reading's stamp. Both stamps are set far enough apart that
    # the two cannot be confused however slow this dispatch is.
    assert rate["observed_at"] == rate_stamp
    assert 80 <= rate["age_seconds"] <= 240
    assert rate["age_seconds"] > reading["age_seconds"] + 30


def test_a_count_the_document_omits_reads_unknown_and_never_zero(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = dispatch_repo.parent / "config"
    lane_path = _write_lane_document(
        config_home,
        running=GENERATING,
        headroom=4.0,
        mean_context=120000.0,
        observed_at=_stamp(5),
    )

    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="omits-the-queue", lane_document=lane_path
    )

    assert result.exit_code == 0
    reading = payload["lane_reading"]
    assert reading["state"] == "fresh"
    # The published count is still measured and still carried; the absent one is
    # withheld. A zero here would report a measured empty queue on a document
    # that said nothing about its queue at all.
    assert reading["generating"] == GENERATING
    assert reading["waiting"] == "unknown"
    assert reading["waiting"] != 0
    # A document publishing no rate is not a lane achieving zero.
    assert reading["throughput"]["state"] == "unknown"
    assert reading["throughput"]["mean_tokens_per_second"] == "unknown"
    assert reading["throughput"]["runs"] == "unknown"


def test_a_rate_without_its_own_stamp_is_dated_by_the_reading_that_carries_it(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = dispatch_repo.parent / "config"
    reading_stamp = _stamp(20)
    lane_path = _write_lane_document(
        config_home,
        running=GENERATING,
        waiting=WAITING,
        headroom=4.0,
        mean_context=120000.0,
        observed_at=reading_stamp,
        throughput={
            "mean_tokens_per_second": MEAN_TOKENS_PER_SECOND,
            "aggregate_tokens_per_second": AGGREGATE_TOKENS_PER_SECOND,
            "runs": RUNS,
        },
    )

    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="undated-rate", lane_document=lane_path
    )

    assert result.exit_code == 0
    reading = payload["lane_reading"]
    rate = reading["throughput"]
    # One publication writes both, so a block carrying no stamp of its own is
    # described by the reading it arrived in rather than left undated.
    assert rate["state"] == "measured"
    assert rate["observed_at"] == reading_stamp
    assert rate["age_seconds"] == reading["age_seconds"]
