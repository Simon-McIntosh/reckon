"""A promotion folds indexed inputs without decoding retained full payloads."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from reckon import capabilities, ledger
from reckon.crew.picker import lane_context

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
PROJECT = "profile-fold-fixture"


def _row(index, **fields):
    return {
        "run_id": f"run-{index}",
        "backend": "fixture-backend",
        "agent": {"effort": "standard"},
        "role": "implement",
        "spec_level": "guided",
        "completed_at": (NOW - timedelta(minutes=1)).isoformat(),
        "wall_seconds": index + 1,
        "time_budget": "30m",
        "gate": "passed",
        "budget": {"tokens": {"output_tokens": 100 + index}},
        "full_payload": "retained narrative absent from picker inputs",
        **fields,
    }


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("RECKON_RUN_TIME_PROFILE_CACHE", str(tmp_path / "profiles"))
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(docs)}))
    path = ledger.ledger_path(PROJECT)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "data": {
                    "_version": 1,
                    "members": [],
                    "holds": [],
                    "runs": [_row(index) for index in range(50)],
                }
            }
        )
    )
    lane_context._PROFILE_CACHE.clear()
    yield tmp_path / "profiles" / f"{PROJECT}.json"
    lane_context._PROFILE_CACHE.clear()


def _decode_counter(monkeypatch):
    original = ledger.json.loads
    decoded = []

    def counted(value, *args, **kwargs):
        result = original(value, *args, **kwargs)
        if isinstance(result, dict) and "full_payload" in result:
            decoded.append(result["run_id"])
        return result

    monkeypatch.setattr(ledger.json, "loads", counted)
    return decoded


def _refuse_index(*args, **kwargs):
    raise OSError("fixture refuses index refresh during promotion")


@pytest.mark.parametrize("refused", [False, True])
def test_refresh_decodes_no_full_payload_and_persists_the_appended_row(
    history, monkeypatch, refused
):
    decoded = _decode_counter(monkeypatch)
    first = lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    assert first["rows"] == 50
    assert len(decoded) >= 50, "the instrument must see a known full build"
    ledger.append_run(PROJECT, _row(50))
    if refused:
        monkeypatch.setattr(ledger, "_indexed_data", _refuse_index)
    decoded.clear()
    lane_context._PROFILE_CACHE.clear()
    profile = lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    persisted = capabilities._read_pick_entry(history)["value"]
    print(f"full_payload_decodes={len(decoded)} rows={profile['rows']}")
    assert profile["rows"] == persisted["rows"] == 51
    assert decoded == [], f"refresh decoded {len(decoded)} full run payloads"
    assert persisted["groups"][0]["runs"] == 51
    assert persisted["groups"][0]["wall_seconds_median"] == 26


def test_a_refused_index_build_can_serve_a_snapshot_missing_the_append(
    history, monkeypatch
):
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_index_record_written_document", lambda *a: None)
        ledger.append_run(PROJECT, _row(50))
    monkeypatch.setattr(ledger, "_indexed_data", _refuse_index)
    fallback = Mock(wraps=ledger._last_good_index_data)
    monkeypatch.setattr(ledger, "_last_good_index_data", fallback)
    profile = lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    print(f"last_good_reads={fallback.call_count} rows={profile['rows']}")
    assert fallback.call_count >= 1
    assert profile["rows"] == 51


@pytest.mark.parametrize("damage", ["missing", "corrupt", "mismatched"])
def test_an_unusable_stored_profile_gets_one_full_build(history, monkeypatch, damage):
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    if damage == "missing":
        history.unlink()
    elif damage == "corrupt":
        history.write_text("{")
    else:
        entry = json.loads(history.read_text())
        entry["key"] = "unrelated-ledger-stamp"
        history.write_text(json.dumps(entry))
    ledger.append_run(PROJECT, _row(50))
    build = Mock(wraps=lane_context.run_time_profile)
    monkeypatch.setattr(lane_context, "run_time_profile", build)
    decoded = _decode_counter(monkeypatch)
    profile = lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    assert build.call_count == 1
    assert len(decoded) >= 51
    assert profile["rows"] == 51


def test_folds_preserve_window_statistics_and_reject_rewritten_history(history):
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    for index, row in enumerate(
        [
            _row(50, wall_seconds=None, gate="failed", time_budget=None),
            _row(51, completed_at=(NOW + timedelta(minutes=1)).isoformat()),
            _row(52, completed_at=(NOW - timedelta(days=15)).isoformat()),
        ]
    ):
        ledger.append_run(PROJECT, row)
        moment = NOW + timedelta(seconds=index)
        actual = dict(lane_context.refresh_run_time_profile(PROJECT, now=moment))
        expected = lane_context.run_time_profile(PROJECT, now=moment)
        assert {key: actual[key] for key in expected} == expected
    later = NOW + timedelta(minutes=2)
    actual = lane_context.refresh_run_time_profile(PROJECT, now=later)
    expected = lane_context.run_time_profile(PROJECT, now=later)
    assert {key: actual[key] for key in expected} == expected
