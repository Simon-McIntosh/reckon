"""Ledger readings share one decode while live observations stay fresh."""

import json
from unittest.mock import Mock

import pytest

from reckon import budget, crew, file_memo, ledger

PROJECT = "sample"


def _row(run_id="first", *, utilisation=25, stamp="2030-01-01T10:00:00Z"):
    return {
        "run_id": run_id,
        "backend": "alpha",
        "completed_at": stamp,
        "budget": {
            "headroom": "known",
            "utilisation_pct": utilisation,
            "windows": {"five_hour": {"utilisation": utilisation / 100}},
        },
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "cache"))
    root = tmp_path / "repo"
    root.mkdir()
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    ledger.append_run(PROJECT, _row(), root=root)
    file_memo.clear()
    counted = Mock(wraps=budget.ledger.load)
    monkeypatch.setattr(budget.ledger, "load", counted)
    yield root, counted
    file_memo.clear()


def _latest(root, **kwargs):
    return budget.latest_recorded(PROJECT, root=root, **kwargs)["alpha"]


def test_unchanged_ledger_loads_once(store):
    root, counted = store
    assert _latest(root).budget["utilisation_pct"] == 25
    assert counted.call_count == 1
    assert _latest(root).budget["utilisation_pct"] == 25
    assert counted.call_count == 1


def test_promoted_row_changes_the_next_answer(store):
    root, counted = store
    assert _latest(root).record_id == "first"
    ledger.append_run(
        PROJECT,
        _row("second", utilisation=60, stamp="2030-01-01T11:00:00Z"),
        root=root,
    )
    reading = _latest(root)
    assert reading.record_id == "second"
    assert reading.budget["utilisation_pct"] == 60
    assert counted.call_count == 2


def test_live_pointer_is_read_without_loading_the_ledger(store):
    root, counted = store
    assert _latest(root).source == "ledger"
    pointer = {
        **_row("live", utilisation=70),
        "project": PROJECT,
        "observed_at": "2030-01-01T12:00:00Z",
    }
    path = crew.live_dir() / "live.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer))
    reading = _latest(root)
    assert reading.source == "live-run"
    assert reading.record_id == "live"
    assert reading.budget["utilisation_pct"] == 70
    assert counted.call_count == 1


def test_mutating_a_returned_budget_leaves_the_next_read_unchanged(store):
    root, counted = store
    reading = _latest(root)
    reading.budget["utilisation_pct"] = 99
    reading.budget["windows"]["five_hour"]["utilisation"] = 0.99
    again = _latest(root)
    assert again.budget["utilisation_pct"] == 25
    assert again.budget["windows"]["five_hour"]["utilisation"] == 0.25
    assert counted.call_count == 1


def test_generation_change_invalidates_an_in_place_edit(store):
    root, counted = store
    assert _latest(root).budget["utilisation_pct"] == 25
    path = ledger.run_path(PROJECT, "first", root)
    path.write_text(ledger.serialize_run(_row(utilisation=45)))
    ledger._bump_generation(PROJECT)
    assert _latest(root).budget["utilisation_pct"] == 45
    assert counted.call_count == 2


def test_a_missing_generation_does_not_cache_readings(store):
    root, counted = store
    ledger.ledger_generation_path(PROJECT).unlink()
    assert _latest(root).budget["utilisation_pct"] == 25
    assert _latest(root).budget["utilisation_pct"] == 25
    assert counted.call_count == 2


def test_explicit_records_are_not_shadowed_by_the_cached_ledger(store):
    root, counted = store
    assert _latest(root).budget["utilisation_pct"] == 25
    assert _latest(root, records=[_row(utilisation=80)]).budget["utilisation_pct"] == 80
    assert counted.call_count == 2
    assert counted.call_args.kwargs["headers_only"]
    assert _latest(root).budget["utilisation_pct"] == 25
    assert counted.call_count == 2


def test_config_changes_are_reflected_in_readings(store):
    root, _counted = store
    config = {"backends": {"alpha": {"budget_check": False}}}
    assert not _latest(root, config=config).surface_opt_in
    config["backends"]["alpha"]["budget_check"] = True
    assert _latest(root, config=config).surface_opt_in


def test_a_failed_load_does_not_cache_an_empty_answer(store):
    root, counted = store
    real_load = counted._mock_wraps
    counted.side_effect = [
        ledger.LedgerError("temporarily unreadable"),
        real_load(PROJECT, root),
    ]
    assert not budget.latest_recorded(PROJECT, root=root)
    assert _latest(root).budget["utilisation_pct"] == 25
    assert counted.call_count == 2


def test_a_run_added_without_a_generation_bump_is_seen(store):
    root, counted = store
    assert _latest(root).record_id == "first"
    ledger.run_path(PROJECT, "external", root).write_text(
        ledger.serialize_run(
            _row("external", utilisation=50, stamp="2030-01-01T11:00:00Z")
        )
    )
    assert _latest(root).record_id == "external"
    assert counted.call_count == 2


def test_an_aggregate_change_without_a_generation_bump_is_seen(store):
    root, counted = store
    assert _latest(root).record_id == "first"
    path = ledger.ledger_path(PROJECT, root)
    envelope = json.loads(path.read_text())
    envelope["data"]["runs"].append(
        _row("aggregate", utilisation=50, stamp="2030-01-01T11:00:00Z")
    )
    path.write_text(json.dumps(envelope))
    assert _latest(root).record_id == "aggregate"
    assert counted.call_count == 2
