"""A stale published subscription window is refreshed through the account probe."""

from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import paid_lanes, window_reading

NOW = datetime(2026, 10, 7, 5, 30, tzinfo=UTC)
BACKENDS = {
    name: {
        "launch": "cli",
        "command": "codex",
        "budget_check": True,
        "budget_group": "codex-sub",
    }
    for name in ("codex", "codex-astra", "codex-luna")
}


def _sources(age_seconds=7200):
    observed = NOW - timedelta(seconds=age_seconds)
    reading = window_reading.WindowReading(
        figures=(
            window_reading.WindowFigure(
                period="seven_day",
                utilisation=0.85,
                observed_at=observed,
                age_seconds=age_seconds,
                resets_at=(NOW + timedelta(days=1)).isoformat(),
            ),
        ),
        observed_at=observed,
    )
    return {name: [paid_lanes.Candidate("rollout", reading)] for name in BACKENDS}


def _answer(_probe):
    return {
        "id": 2,
        "result": {
            "rateLimits": {
                "primary": {
                    "usedPercent": 0.0,
                    "windowDurationMins": 10080,
                    "resetsAt": int((NOW + timedelta(days=7)).timestamp()),
                }
            }
        },
    }


def test_stale_subscription_window_is_refreshed_once_for_its_account(tmp_path):
    calls = []

    def probe(request):
        calls.append(request)
        return _answer(request)

    path = tmp_path / "paid-lanes.json"
    document = paid_lanes.publish_document(
        {"backends": BACKENDS},
        _sources(),
        path=path,
        moment=NOW,
        probe_runner=probe,
        local_lane={"state": "unknown"},
    )
    assert len(calls) == 1
    for name in BACKENDS:
        week = document["accounts"][name]["windows"]["seven_day"]
        assert week["utilisation"] == 0.0
        assert week["source"] == "probe"
        assert week["observed_at"] == NOW.isoformat()
        assert datetime.fromisoformat(week["resets_at"]) == NOW + timedelta(days=7)
        assert week["stale"] is False
    assert paid_lanes.read_document(path) == document


def test_second_publish_within_horizon_keeps_probe_reading(tmp_path):
    calls = []

    def probe(request):
        calls.append(request)
        return _answer(request)

    path = tmp_path / "paid-lanes.json"
    paid_lanes.publish_document(
        {"backends": BACKENDS},
        _sources(),
        path=path,
        moment=NOW,
        probe_runner=probe,
        local_lane={"state": "unknown"},
    )
    later = paid_lanes.publish_document(
        {"backends": BACKENDS},
        _sources(),
        path=path,
        moment=NOW + timedelta(minutes=5),
        probe_runner=probe,
        local_lane={"state": "unknown"},
    )
    assert len(calls) == 1
    assert later["accounts"]["codex"]["windows"]["seven_day"]["source"] == "probe"


def test_failed_probe_keeps_aged_reading_and_records_reason(tmp_path):
    calls = []

    def probe(request):
        calls.append(request)

    path = tmp_path / "paid-lanes.json"
    document = paid_lanes.publish_document(
        {"backends": BACKENDS},
        _sources(),
        path=path,
        moment=NOW,
        probe_runner=probe,
        local_lane={"state": "unknown"},
    )
    assert len(calls) == 1
    for name in BACKENDS:
        entry = document["accounts"][name]
        week = entry["windows"]["seven_day"]
        assert (
            week["utilisation"],
            week["source"],
            week["age_seconds"],
            week["stale"],
        ) == (0.85, "rollout", 7200.0, True)
        assert "no answer" in entry["probe"]["failure"]
    again = paid_lanes.publish_document(
        {"backends": BACKENDS},
        _sources(),
        path=path,
        moment=NOW + timedelta(minutes=5),
        probe_runner=probe,
        local_lane={"state": "unknown"},
    )
    assert len(calls) == 1
    assert "no answer" in again["accounts"]["codex"]["probe"]["failure"]


def test_fresh_window_never_probes(tmp_path):
    def forbidden(_request):
        raise AssertionError("fresh account must not be probed")

    document = paid_lanes.publish_document(
        {"backends": BACKENDS},
        _sources(age_seconds=30),
        path=tmp_path / "paid-lanes.json",
        moment=NOW,
        probe_runner=forbidden,
        local_lane={"state": "unknown"},
    )
    assert document["accounts"]["codex"]["windows"]["seven_day"]["source"] == "rollout"


def test_probe_claim_survives_an_interrupted_publish(tmp_path):
    path = tmp_path / "paid-lanes.json"

    def interrupted(_request):
        assert paid_lanes.read_document(path)["accounts"]["codex"]["probe"] == {
            "attempted_at": NOW.isoformat(),
            "failure": "probe interrupted before a reading was published",
        }
        raise RuntimeError("publisher stopped mid-probe")

    with pytest.raises(RuntimeError, match="stopped mid-probe"):
        paid_lanes.publish_document(
            {"backends": BACKENDS},
            _sources(),
            path=path,
            moment=NOW,
            probe_runner=interrupted,
            local_lane={"state": "unknown"},
        )
    resumed = paid_lanes.publish_document(
        {"backends": BACKENDS},
        _sources(),
        path=path,
        moment=NOW + timedelta(minutes=5),
        probe_runner=interrupted,
        local_lane={"state": "unknown"},
    )
    assert resumed["accounts"]["codex"]["windows"]["seven_day"]["stale"]
    assert "interrupted" in resumed["accounts"]["codex"]["probe"]["failure"]
