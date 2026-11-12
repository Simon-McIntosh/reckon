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
    ledger._bump_generation(PROJECT)
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
    load = Mock(wraps=ledger.load)
    monkeypatch.setattr(ledger, "load", load)
    profile = lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    raw = [
        call for call in load.call_args_list if call.kwargs.get("use_index") is False
    ]
    print(f"raw_profile_fallbacks={len(raw)}")
    assert len(raw) == 1
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


def test_folds_preserve_window_statistics(history):
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


@pytest.mark.parametrize("change", ["rewrite", "delete", "aggregate"])
def test_changed_history_refuses_a_fold_and_builds_once(history, monkeypatch, change):
    ledger.append_run(PROJECT, _row(50))
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    target = ledger.run_path(PROJECT, "run-50")
    if change == "rewrite":
        target.write_text(ledger.serialize_run(_row(50, wall_seconds=9999)))
    elif change == "delete":
        target.unlink()
    else:
        path = ledger.ledger_path(PROJECT)
        envelope = json.loads(path.read_text())
        envelope["data"]["runs"][0]["wall_seconds"] = 9999
        path.write_text(json.dumps(envelope))
    ledger._bump_generation(PROJECT)
    build = Mock(wraps=lane_context.run_time_profile)
    monkeypatch.setattr(lane_context, "run_time_profile", build)
    actual = lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    assert build.call_count == 1
    expected = build(PROJECT, now=NOW)
    assert {key: actual[key] for key in expected} == expected


def test_a_generation_move_during_a_fold_does_not_publish_mixed_inputs(
    history, monkeypatch
):
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    before = history.read_bytes()
    ledger.append_run(PROJECT, _row(50))
    real_inputs = lane_context._indexed_profile_inputs

    def moved(*args, **kwargs):
        inputs = real_inputs(*args, **kwargs)
        ledger._bump_generation(PROJECT)
        return inputs

    monkeypatch.setattr(lane_context, "_indexed_profile_inputs", moved)
    assert lane_context.refresh_run_time_profile(PROJECT, now=NOW)["rows"] == 51
    assert history.read_bytes() == before


def _fold_path(history):
    return history.with_name(f".{PROJECT}-fold-inputs.json")


def test_picks_never_decode_the_fold_inputs(history, monkeypatch):
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    fold_path = _fold_path(history)
    assert fold_path.exists(), "fold inputs need their own file"
    fold_text = fold_path.read_text()
    loads = lane_context.json.loads
    decoded = []

    def counted(value, *args, **kwargs):
        if value == fold_text:
            decoded.append("fold-inputs")
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(lane_context.json, "loads", counted)
    assert capabilities._read_pick_entry(fold_path) is not None
    assert decoded == ["fold-inputs"], "the instrument must see the sidecar read"
    decoded.clear()
    ledger.append_run(PROJECT, _row(50))
    lane_context._PROFILE_CACHE.clear()
    profile, age = lane_context._cached_run_time_profile(PROJECT, now=NOW)
    assert profile["rows"] == 50
    assert age == 0
    assert decoded == []
    assert "fold_inputs" not in profile


def test_served_profile_size_is_independent_of_history(history):
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    before = history.stat().st_size
    aggregate = ledger.ledger_path(PROJECT)
    envelope = json.loads(aggregate.read_text())
    envelope["data"]["runs"].extend(
        _row(index, node={"brief": "x" * 2048}) for index in range(50, 550)
    )
    aggregate.write_text(json.dumps(envelope))
    ledger._bump_generation(PROJECT)
    profile = lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    after = history.stat().st_size
    print(f"served_profile_bytes={before}->{after}; rows={profile['rows']}")
    assert profile["rows"] == 550
    assert after <= before + 64, "served profile grew with individual run samples"
    assert _fold_path(history).stat().st_size > 1_000_000
    assert "fold_inputs" not in capabilities._read_pick_entry(history)["value"]


def test_fold_samples_are_bounded_by_the_window_and_one_forward_day(history):
    for index, completed in enumerate(
        [NOW - timedelta(days=15), NOW + timedelta(hours=1), NOW + timedelta(days=2)],
        start=50,
    ):
        ledger.append_run(PROJECT, _row(index, completed_at=completed.isoformat()))
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    entry = capabilities._read_pick_entry(_fold_path(history))
    assert entry is not None
    assert entry["key"] == capabilities._read_pick_entry(history)["key"]
    rows = entry["value"]["rows"]
    assert len(rows) == 51
    assert {row["run_id"] for row in rows} & {"run-50", "run-51", "run-52"} == {
        "run-51"
    }


@pytest.mark.parametrize("missing", ["absent", "generation", "projection"])
def test_raw_file_fallback_is_not_a_cold_or_unusable_cache_route(
    history, monkeypatch, missing
):
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    if missing == "absent":
        history.unlink()
    elif missing == "generation":
        ledger.ledger_generation_path(PROJECT).unlink()
    else:
        monkeypatch.setattr(lane_context, "_indexed_profile_inputs", lambda *a: None)
    load = Mock(wraps=ledger.load)
    monkeypatch.setattr(ledger, "load", load)
    assert lane_context.refresh_run_time_profile(PROJECT, now=NOW)["rows"] == 50
    raw = [
        call for call in load.call_args_list if call.kwargs.get("use_index") is False
    ]
    assert raw == []


def test_profile_owner_accepts_supplied_rows_without_a_ledger_read(
    history, monkeypatch
):
    expected = lane_context.run_time_profile(PROJECT, now=NOW)
    rows = [
        *ledger.runs(PROJECT),
        _row(50, completed_at=(NOW - timedelta(days=15)).isoformat()),
        _row(51, completed_at=(NOW + timedelta(days=1)).isoformat()),
    ]
    monkeypatch.setattr(ledger, "runs", Mock(side_effect=AssertionError("ledger read")))
    actual = lane_context.run_time_profile(PROJECT, now=NOW, rows=rows)
    assert actual == expected
    ledger.runs.assert_not_called()


@pytest.mark.parametrize("damage", ["stamp", "date", "missing"])
def test_fold_inputs_must_match_the_served_stamp_and_date(history, monkeypatch, damage):
    expected = lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    assert expected["rows"] == 50
    path = _fold_path(history)
    if damage == "missing":
        path.unlink()
    else:
        entry = json.loads(path.read_text())
        entry["value"]["rows"] = entry["value"]["rows"][:25]
        if damage == "stamp":
            entry["key"] = "unrelated-source"
        else:
            entry["value"]["date"] = (NOW - timedelta(days=1)).date().isoformat()
        path.write_text(json.dumps(entry))
    build = Mock(wraps=lane_context.run_time_profile)
    monkeypatch.setattr(lane_context, "run_time_profile", build)
    actual = lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    assert actual == expected
    assert capabilities._read_pick_entry(history)["value"] == expected
    build.assert_called_once_with(PROJECT, now=NOW)


def test_fold_inputs_do_not_overwrite_another_projects_served_profile(history):
    other = history.with_name(f"{PROJECT}-fold-inputs.json")
    capabilities._write_pick_entry(
        other,
        {
            "version": capabilities.PICK_INPUT_CACHE_VERSION,
            "key": "other-project",
            "value": {"rows": 7, "persisted_at": NOW.isoformat()},
        },
    )
    before = other.read_bytes()
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    assert other.read_bytes() == before


def test_an_index_recovery_refreshes_the_profile_without_a_raw_scan(
    history, monkeypatch
):
    lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_index_record_written_document", lambda *a: None)
        ledger.append_run(PROJECT, _row(50))
    indexed = ledger._indexed_data
    attempts = []

    def briefly_refused(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise OSError("fixture index writer is briefly busy")
        return indexed(*args, **kwargs)

    monkeypatch.setattr(ledger, "_indexed_data", briefly_refused)
    load = Mock(wraps=ledger.load)
    monkeypatch.setattr(ledger, "load", load)
    actual = lane_context.refresh_run_time_profile(PROJECT, now=NOW)
    assert len(attempts) >= 2
    assert actual["rows"] == 51
    assert not any(
        call.kwargs.get("use_index") is False for call in load.call_args_list
    )
