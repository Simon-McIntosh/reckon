"""Run-time profile and local-lane load derive figures no reader can misread.

Every case here runs against synthetic rows written into ``tmp_path``; the real
ledger is never read, because ``ledger.runs`` is replaced with a reader of the
fixture file. That is deliberate: a test that read the live ledger would pass
when written and drift as runs accumulate, and it would measure the machine
rather than the module.

Each case exists because its opposite was a plausible bug:

* **The 10-row percentile fixture** pins the median and the 90th percentile to
  hand-computed members of a known sample, with the two deliberately unequal so
  a percentile that silently returns the median moves the figure.
* **The passed fraction** divides passes by the closed verdicts only, so a
  ``not-run`` row does not count as a fail, and an empty denominator is ``None``
  rather than a zero.
* **Both size-bucket keys** are covered separately: a row that recorded a time
  budget buckets by it, and a row that recorded none falls back to output
  tokens, with the key named on each result.
* **The window's upper bound** drops a row completing after ``until``, so a
  promotion landing after the window closes cannot be counted.
* **A size bucket** counts every member row in ``runs`` and its timed subset in
  ``timed_runs``, and a backend whose rows all lack a wall time still appears
  against a null median rather than being dropped.
* **The module identity** is checked to resolve inside the checkout under test,
  so a stale copy from a shared environment or another tree is refused.
* **A missing lane document** yields ``None`` for every load field, proven
  against a present document that resolves its figures, so the absence check is
  shown to see something where something exists.
* **The router's observed window** gates worker slots: a zero, negative or
  absent ``observed_seconds`` carries no slot figure, while a positive one
  carries the published figure, so a slot allowance resting on no history is
  not read as a real capacity. The gate reads the allowance's structured
  ``rests_on_observed_window`` field, and a case rewrites the human-readable
  source label to show the gate does not match its text.
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
    completed_at: str | None = None,
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
    if completed_at is not None:
        row["completed_at"] = completed_at
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
                # A positive observation window is what makes the router's own
                # slot figure trustworthy; without it the figure reads as absent.
                "admission": {"worker_slots": 5, "observed_seconds": 600},
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


@pytest.mark.parametrize(
    "observed_seconds",
    [
        pytest.param(0, id="zero"),
        pytest.param(-5, id="negative"),
        pytest.param(None, id="absent"),
    ],
)
def test_worker_slots_rest_on_an_observed_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed_seconds: int | None
):
    """A slot figure resting on no observed window reads as absent, not zero.

    The router averages its slot arithmetic over the window it reports as
    ``observed_seconds``. A zero, a negative figure and an absent figure each
    state that no window has been observed, so the published slot figure is not
    carried: the dispatcher's own allowance reader applies that rule once and
    falls back to headroom, which needs no history, and the load reading reuses
    that decision rather than restating the window test. Headroom is unaffected,
    so the two fields are shown to move independently.
    """

    admission: dict = {"worker_slots": 5}
    if observed_seconds is not None:
        admission["observed_seconds"] = observed_seconds
    document = {"running": 4, "headroom": 3, "admission": admission}
    path = tmp_path / "lane.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(path))

    load = local_lane_load()

    assert load["worker_slots"] is None
    assert load["headroom"] == 3


def test_a_positive_window_carries_the_published_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A positive observation window carries the router's published figure."""

    document = {
        "running": 4,
        "headroom": 3,
        "admission": {"worker_slots": 5, "observed_seconds": 600},
    }
    path = tmp_path / "lane.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(path))

    load = local_lane_load()

    assert load["worker_slots"] == 5


def test_the_slot_gate_reads_the_window_flag_not_the_source_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rewording every source label leaves the gate on the structured flag.

    The allowance carries a human-readable ``source`` beside the structured
    ``rests_on_observed_window`` field. The load reading must decide which slot
    figures to trust from that field alone. The wrapper below rewrites the label
    to a sentence carrying no slot wording at all: a gate that matched the
    label's text would drop a figure that the flag says is real.
    """

    real = profile_module._lane_worker_allowance

    def reworded(document, *, session):
        decision = dict(real(document, session=session))
        decision["source"] = "a reworded label carrying no slot wording"
        return decision

    monkeypatch.setattr(profile_module, "_lane_worker_allowance", reworded)

    trusted = tmp_path / "trusted.json"
    trusted.write_text(
        json.dumps(
            {
                "running": 4,
                "headroom": 3,
                "admission": {"worker_slots": 5, "observed_seconds": 600},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(trusted))
    assert local_lane_load()["worker_slots"] == 5

    untrusted = tmp_path / "untrusted.json"
    untrusted.write_text(
        json.dumps(
            {
                "running": 4,
                "headroom": 3,
                "admission": {"worker_slots": 5, "observed_seconds": 0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(untrusted))
    assert local_lane_load()["worker_slots"] is None


def test_a_slot_labelled_source_without_the_window_flag_carries_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A label that reads like a slot figure is not the gate; the flag is.

    A gate matching the substring ``worker slots`` in the source label would
    carry a figure whose structured flag says it rests on no observed window.
    Here the label reads like the global slot figure while the flag is false,
    and the load reading drops the figure.
    """

    real = profile_module._lane_worker_allowance

    def mislabelled(document, *, session):
        decision = dict(real(document, session=session))
        decision["source"] = "the global worker slots"
        decision["rests_on_observed_window"] = False
        return decision

    monkeypatch.setattr(profile_module, "_lane_worker_allowance", mislabelled)

    path = tmp_path / "lane.json"
    path.write_text(
        json.dumps(
            {
                "running": 4,
                "headroom": 3,
                "admission": {"worker_slots": 5, "observed_seconds": 0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(path))

    assert local_lane_load()["worker_slots"] is None


def test_the_module_is_the_one_under_test():
    """Guard against importing a stale installed copy from a shared environment.

    The module must resolve inside the repository root of the checkout under
    test. An import that lands in a shared environment or another tree's
    checkout would otherwise pass a mere ``__file__ is not None`` check while
    running code from a different revision.
    """

    repo_root = Path(__file__).resolve().parents[1]
    module_path = Path(profile_module.__file__ or "").resolve()
    assert module_path.is_relative_to(repo_root), (
        f"run_time_profile imported from {module_path}, outside {repo_root}"
    )


def test_a_row_completing_after_the_window_is_excluded(install_rows):
    """A completion stamp after ``until`` keeps the row out of every table.

    ``now`` is fixed, so the window is deterministic; the only difference
    between the two rows is that the second completed after the bound.
    """

    install_rows(
        [
            _row(
                backend="clive",
                wall_seconds=100,
                time_budget="10m",
                completed_at="2026-10-02T11:00:00Z",
            ),
            _row(
                backend="clive",
                wall_seconds=200,
                time_budget="10m",
                completed_at="2026-10-02T12:30:00Z",
            ),
        ]
    )

    report = run_time_profile("reckon", days=14, now=FIXED_NOW)

    assert report["rows"] == 1
    assert len(report["groups"]) == 1
    assert report["groups"][0]["runs"] == 1
    assert report["groups"][0]["wall_seconds_median"] == 100
    bucket = report["size_buckets"][0]["buckets"]["up_to_30m"]
    assert bucket["runs"] == 1
    assert bucket["timed_runs"] == 1
    assert bucket["wall_seconds_median"] == 100


def test_size_buckets_count_runs_and_timed_runs(install_rows):
    """A bucket counts every member row, and its timed subset separately.

    ``clive`` has one timed and one untimed row in the same bucket, so its
    ``runs`` is two and its ``timed_runs`` one; ``codex`` has only untimed
    rows, so it still appears with a null median rather than being dropped.
    """

    install_rows(
        [
            _row(backend="clive", wall_seconds=600, time_budget="10m"),
            _row(backend="clive", wall_seconds=None, time_budget="10m"),
            _row(backend="codex", wall_seconds=None, time_budget="10m"),
        ]
    )

    report = run_time_profile("reckon", days=14, now=FIXED_NOW)
    buckets = {entry["backend"]: entry for entry in report["size_buckets"]}

    clive = buckets["clive"]["buckets"]["up_to_30m"]
    assert clive["runs"] == 2
    assert clive["timed_runs"] == 1
    assert clive["wall_seconds_median"] == 600

    codex = buckets["codex"]["buckets"]["up_to_30m"]
    assert codex["runs"] == 1
    assert codex["timed_runs"] == 0
    assert codex["wall_seconds_median"] is None

    # The bucket ``runs`` now means what the group table's ``runs`` means.
    by_group = {group["backend"]: group for group in report["groups"]}
    assert by_group["clive"]["runs"] == 2
    assert by_group["codex"]["runs"] == 1
