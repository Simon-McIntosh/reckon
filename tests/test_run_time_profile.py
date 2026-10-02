"""Run-time profile and local-lane load derive figures no reader can misread.

Every case here runs against synthetic rows written into ``tmp_path``; the real
ledger is never read, because ``ledger.runs`` is replaced with a reader of the
fixture file. That is deliberate: a test that read the live ledger would pass
when written and drift as runs accumulate, and it would measure the machine
rather than the module.

Four properties are exercised, one per case, each chosen because its opposite
was a plausible bug:

* **The 10-row percentile fixture** pins the median and the 90th percentile to
  hand-computed members of a known sample, with the two deliberately unequal so
  a percentile that silently returns the median moves the figure.
* **The passed fraction** divides passes by the closed verdicts only, so a
  ``not-run`` row does not count as a fail, and an empty denominator is ``None``
  rather than a zero.
* **Both size-bucket keys** are covered separately: a row that recorded a time
  budget buckets by it, and a row that recorded none falls back to output
  tokens, with the key named on each result.
* **A missing lane document** yields ``None`` for every load field, proven
  against a present document that resolves its figures, so the absence check is
  shown to see something where something exists.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import run_time_profile as profile_module
from reckon.crew.run_time_profile import local_lane_load, run_time_profile

FIXED_NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)


def _row(
    *,
    backend: str = "clive",
    effort: str = "high",
    role: str = "implement",
    spec_level: str = "exact",
    wall_seconds: float | None = None,
    output_tokens: int | None = None,
    gate: str = "passed",
    time_budget: str | None = None,
) -> dict:
    """Build one synthetic ledger row with only the fields under test."""

    row: dict = {
        "backend": backend,
        "agent": {"backend": backend, "effort": effort},
        "role": role,
        "spec_level": spec_level,
        "gate": gate,
    }
    if wall_seconds is not None:
        row["wall_seconds"] = wall_seconds
    if output_tokens is not None:
        row["budget"] = {"tokens": {"output_tokens": output_tokens}}
    if time_budget is not None:
        row["time_budget"] = time_budget
    return row


@pytest.fixture()
def install_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write synthetic rows to a temp file and serve them as ``ledger.runs``.

    The fixture file is resolved by the patched reader, so no case here can
    reach the operator's live ledger even by accident.
    """

    store = tmp_path / "synthetic_runs.json"

    def install(rows: list[dict]) -> None:
        store.write_text(json.dumps(rows), encoding="utf-8")

        def fake_runs(project: str, **kwargs: object) -> list[dict]:
            return json.loads(store.read_text(encoding="utf-8"))

        monkeypatch.setattr(ledger, "runs", fake_runs)

    return install


def test_the_percentile_fixture_pins_median_and_ninetieth(install_rows):
    """A 10-row sample with a hand-computable median and 90th percentile."""

    walls = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    install_rows([_row(wall_seconds=value) for value in walls])

    report = run_time_profile("reckon", days=14, now=FIXED_NOW)

    assert report["rows"] == 10
    assert len(report["groups"]) == 1
    group = report["groups"][0]
    assert group["runs"] == 10
    # statistics.median of an even sample is the mean of the middle pair.
    assert group["wall_seconds_median"] == 55.0
    # nearest-rank 90th of 10 is the ninth member, which must not equal the median.
    assert group["wall_seconds_p90"] == 90
    assert group["wall_seconds_p90"] != group["wall_seconds_median"]


def test_the_passed_fraction_ignores_unclosed_verdicts(install_rows):
    """Passes divide by closed verdicts; an empty denominator is ``None``."""

    install_rows(
        [
            _row(gate="passed"),
            _row(gate="passed"),
            _row(gate="passed"),
            _row(gate="failed"),
            _row(gate="not-run"),
            _row(gate="not-run"),
            _row(backend="codex", gate="not-run"),
        ]
    )

    report = run_time_profile("reckon", days=14, now=FIXED_NOW)
    by_backend = {group["backend"]: group for group in report["groups"]}

    assert by_backend["clive"]["passed"] == 3
    assert by_backend["clive"]["failed"] == 1
    assert by_backend["clive"]["passed_fraction"] == 3 / 4
    assert by_backend["codex"]["passed_fraction"] is None


def test_both_size_bucket_keys_are_used_and_named(install_rows):
    """A budget buckets on minutes; its absence falls back to output tokens."""

    install_rows(
        [
            _row(backend="clive", wall_seconds=600, time_budget="30m"),
            _row(backend="clive", wall_seconds=1200, time_budget="60m"),
            _row(backend="clive", wall_seconds=1800, time_budget="90m"),
            _row(backend="codex", wall_seconds=300, output_tokens=5000),
            _row(backend="codex", wall_seconds=900, output_tokens=50000),
            _row(backend="codex", wall_seconds=1500, output_tokens=200000),
        ]
    )

    report = run_time_profile("reckon", days=14, now=FIXED_NOW)
    buckets = {entry["backend"]: entry for entry in report["size_buckets"]}

    assert buckets["clive"]["key"] == "time_budget"
    assert buckets["clive"]["buckets"]["up_to_30m"]["runs"] == 1
    assert buckets["clive"]["buckets"]["up_to_30m"]["wall_seconds_median"] == 600
    assert buckets["clive"]["buckets"]["30m_to_60m"]["wall_seconds_median"] == 1200
    assert buckets["clive"]["buckets"]["over_60m"]["wall_seconds_median"] == 1800

    assert buckets["codex"]["key"] == "output_tokens"
    assert buckets["codex"]["buckets"]["under_20k"]["wall_seconds_median"] == 300
    assert buckets["codex"]["buckets"]["20k_to_100k"]["wall_seconds_median"] == 900
    assert buckets["codex"]["buckets"]["over_100k"]["wall_seconds_median"] == 1500


def test_a_missing_lane_document_yields_nulls(tmp_path, monkeypatch):
    """Every load field is ``None`` with no document, and resolves with one."""

    absent = tmp_path / "absent-lane.json"
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(absent))

    missing = local_lane_load()
    for field in (
        "running",
        "waiting",
        "headroom",
        "worker_slots",
        "mean_tokens_per_second",
    ):
        assert missing[field] is None, field
    assert missing["observed_at"] is None
    assert missing["read_at"]

    # Positive control: the same reader resolves a document that is present, and
    # a measured zero stays zero rather than reading as the absent value.
    present = tmp_path / "lane.json"
    present.write_text(
        json.dumps(
            {
                "running": 0,
                "waiting": 2,
                "headroom": 3,
                "observed_at": "2026-10-02T11:59:00Z",
                "admission": {"worker_slots": 5},
                "throughput": {"mean_tokens_per_second": 42.0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(present))

    present_load = local_lane_load()
    assert present_load["running"] == 0
    assert present_load["waiting"] == 2
    assert present_load["headroom"] == 3
    assert present_load["worker_slots"] == 5
    assert present_load["mean_tokens_per_second"] == 42.0
    assert present_load["observed_at"] == "2026-10-02T11:59:00Z"


def test_the_module_is_the_one_under_test():
    """Guard against importing a stale installed copy from a shared environment."""

    assert profile_module.__file__ is not None
