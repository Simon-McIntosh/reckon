"""The lane reading a dispatch carries says what the lane is doing and how fast.

The reading is composed once per dispatch and rides in the payload beside the
routing decision, so a coordinator weighing whether to send the next node to
this lane reads the lane's own load and rate without a second call. Two things
have to hold for that to be worth carrying. The counts must stay apart: a lane
serving seven requests while three wait is not a lane serving ten. And a figure
the document did not publish must read as unknown rather than as zero, because
a zero is a measurement and an unpublished count is not -- a lane nobody is
using and a lane nobody can measure must not read alike. The counts have one
owner: the payload carries what the lane-document reader resolved, so the
dispatch holds no knowledge of the names the document spells them with.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_dispatch_commands
from reckon.crew import lane_document as lane_document_module
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

# The generating count has been published under two names, and a document
# carrying both must resolve to the one that names it directly rather than to
# the lane's current spelling of the same population, which is the fallback.
ALIAS_GENERATING = 11
RENAMED_GENERATING = 5


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
        crew_dispatch_commands, "_resolved_flight", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_args, **_kwargs: None
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


def test_a_document_naming_its_generating_count_under_the_alias_is_read(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generating count is published under two names; both are read.

    The lane's current document spells the count ``running``, so the other name
    is the one no fixture reaches and the one an edit to the reader's key list
    can drop without anything else going red. A document spelling it the other
    way is dispatched here, alongside the current name carrying a different
    figure, and the reading must arrive measured -- not unknown for an
    unpublished count -- and must resolve to the name that states the
    population directly.
    """
    config_home = dispatch_repo.parent / "config"
    lane_path = _write_lane_document(
        config_home,
        generating=ALIAS_GENERATING,
        running=RENAMED_GENERATING,
        waiting=WAITING,
        headroom=4.0,
        mean_context=120000.0,
        observed_at=_stamp(5),
    )

    payload, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="names-the-count-under-its-other-name",
        lane_document=lane_path,
    )

    assert result.exit_code == 0
    reading = payload["lane_reading"]
    assert reading["state"] == "fresh"
    assert reading["generating"] == ALIAS_GENERATING
    assert reading["generating"] != RENAMED_GENERATING
    assert reading["generating"] != "unknown"
    assert reading["waiting"] == WAITING


def test_the_document_reader_returns_the_counts_the_payload_carries(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One owner, and the fixture's own statement as the expectation.

    Every document this file's fixtures publish is dispatched and then read a
    second time, straight from the bytes on disk, through the lane-document
    reader. Each side is compared against the counts the fixture itself
    states rather than against the other side: a comparison of the payload
    with a second reading of the same document resolves to itself, so a
    regression inside the reader moves both sides and the case stays green.
    An omitted count must arrive as unknown on both sides, never as a zero.
    """
    config_home = dispatch_repo.parent / "config"
    for index, (document, expected) in enumerate(_fixture_documents()):
        lane_path = _write_lane_document(config_home, **document)
        payload, result = _invoke(
            dispatch_repo,
            monkeypatch,
            node=f"reader-and-payload-agree-{index}",
            lane_document=lane_path,
        )

        assert result.exit_code == 0, result.output
        reading = payload["lane_reading"]
        assert reading["state"] == "fresh", document
        written = json.loads(lane_path.read_text(encoding="utf-8"))
        assert lane_document_module.read_lane_counts(written) == expected, document
        assert {
            "generating": reading["generating"],
            "waiting": reading["waiting"],
        } == expected, document


def test_the_dispatch_carries_what_the_document_readers_resolved(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatch spells no document key: the readers' answers ride through.

    Both halves of the carried reading -- the two counts and the stamped
    figures beside them -- are composed from what the lane-document readers
    resolved. A sentinel returned by a reader arrives in the payload
    unchanged -- the stamp included -- which is what a dispatch holding a
    second reading of the document's own keys could not do.
    """
    config_home = dispatch_repo.parent / "config"
    lane_path = _write_lane_document(
        config_home,
        running=GENERATING,
        waiting=WAITING,
        headroom=4.0,
        mean_context=120000.0,
        observed_at=_stamp(5),
    )
    sentinel_stamp = _stamp(23)
    sentinel_counts = {"generating": GENERATING + 100, "waiting": WAITING + 100}
    sentinel_fields = {
        "detail": "",
        "observed_at": sentinel_stamp,
        "mean_context": MEAN_TOKENS_PER_SECOND,
        "binding_observed": "sentinel binding",
        "shelf_life_seconds": 600.0,
    }
    monkeypatch.setattr(
        lane_document_module,
        "read_lane_counts",
        lambda *_a, **_k: dict(sentinel_counts),
    )
    monkeypatch.setattr(
        lane_document_module,
        "read_lane_reading_fields",
        lambda *_a, **_k: dict(sentinel_fields),
    )

    payload, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="carries-what-the-readers-resolved",
        lane_document=lane_path,
    )

    assert result.exit_code == 0
    reading = payload["lane_reading"]
    assert reading["generating"] == sentinel_counts["generating"]
    assert reading["waiting"] == sentinel_counts["waiting"]
    assert reading["observed_at"] == sentinel_stamp
    assert reading["mean_context"] == MEAN_TOKENS_PER_SECOND
    assert reading["binding_observed"] == "sentinel binding"
    assert reading["suggested_shelf_life_seconds"] == 600.0
    # The reading is dated by the stamp the reader returned, not the document's
    # own: the document publishes a stamp 5 seconds old and the sentinel's is
    # 23, so only the reader's answer gives the second.
    assert 20 <= reading["age_seconds"] <= 120


def test_the_reading_fields_reader_names_the_shape_before_the_stamp() -> None:
    """An object-shaped failure is not reported as a stamp-shaped one.

    The reader resolves the document's names, so the shape it was handed and
    the stamp it did not find are different faults and must not share one
    message: a caller told a list carried no parseable stamp would go looking
    for a key in something that is not a document, and from the message alone
    it could not tell the two apart at all.
    """
    shaped = lane_document_module.read_lane_reading_fields(["not", "a", "document"])
    assert shaped["observed_at"] is None
    assert "not a JSON object" in shaped["detail"]
    assert "observed_at" not in shaped["detail"]

    unstamped = lane_document_module.read_lane_reading_fields({"running": GENERATING})
    assert unstamped["observed_at"] is None
    assert "observed_at" in unstamped["detail"]
    assert "not a JSON object" not in unstamped["detail"]


def _fixture_documents() -> list[tuple[dict, dict]]:
    """The lane documents this file's fixtures write, with the counts each states.

    The expected counts sit beside each document as literals rather than being
    read back out of it, so both the payload and the reader's own answer are
    held against the fixture's statement: a reader regression that moved both
    sides would otherwise resolve to itself and could not fail.

    Stamped on each call: a reading's own age decides whether it is carried at
    all, so a document written once could not be dispatched twice.
    """
    return [
        # Both counts, and a rate carrying the window it was measured over.
        (
            {
                "running": GENERATING,
                "waiting": WAITING,
                "headroom": 4.0,
                "mean_context": 120000.0,
                "observed_at": _stamp(10),
                "throughput": {
                    "mean_tokens_per_second": MEAN_TOKENS_PER_SECOND,
                    "aggregate_tokens_per_second": AGGREGATE_TOKENS_PER_SECOND,
                    "runs": RUNS,
                    "observed_at": _stamp(90),
                },
            },
            {"generating": GENERATING, "waiting": WAITING},
        ),
        # A document publishing one count and omitting the other, and stating
        # no rate at all: the absent count is unknown, never a measured zero.
        (
            {
                "running": GENERATING,
                "headroom": 4.0,
                "mean_context": 120000.0,
                "observed_at": _stamp(5),
            },
            {"generating": GENERATING, "waiting": "unknown"},
        ),
        # A rate carrying no window of its own, dated by the reading that
        # carries it.
        (
            {
                "running": GENERATING,
                "waiting": WAITING,
                "headroom": 4.0,
                "mean_context": 120000.0,
                "observed_at": _stamp(20),
                "throughput": {
                    "mean_tokens_per_second": MEAN_TOKENS_PER_SECOND,
                    "aggregate_tokens_per_second": AGGREGATE_TOKENS_PER_SECOND,
                    "runs": RUNS,
                },
            },
            {"generating": GENERATING, "waiting": WAITING},
        ),
        # The count under its other name, with the current name present too:
        # the name that states the population directly wins over the fallback.
        (
            {
                "generating": ALIAS_GENERATING,
                "running": RENAMED_GENERATING,
                "waiting": WAITING,
                "headroom": 4.0,
                "mean_context": 120000.0,
                "observed_at": _stamp(5),
            },
            {"generating": ALIAS_GENERATING, "waiting": WAITING},
        ),
    ]
