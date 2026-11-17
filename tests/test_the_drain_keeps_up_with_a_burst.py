"""A replay fixes its cohort before comparing the promotion drain."""

import json

import pytest

from scripts.replay_promotion_drain import collect, replay, validate_dataset

START = "2026-11-16T21:00:00Z"
END = "2026-11-16T22:30:00Z"


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _request(run_id, stamp):
    return {"run_id": run_id, "request_id": run_id, "requested_at": stamp}


def test_selection_uses_request_time_and_keeps_retained_settlements(tmp_path):
    crew = tmp_path / "crew"
    for name, stamp in (
        ("before", "2026-11-16T20:59:59Z"),
        ("lower", START),
        ("middle", "2026-11-16T22:00:00+00:00"),
        ("upper", END),
    ):
        _write(
            crew / "promotion-requests" / f"{name}.json.settled", _request(name, stamp)
        )
    result = collect(crew, START, END)
    assert result["header"]["count"] == 2
    assert [r["request"]["run_id"] for r in validate_dataset(result)] == [
        "lower",
        "middle",
    ]


def test_retained_request_supersedes_the_same_historical_request(tmp_path):
    crew, census = tmp_path / "crew", tmp_path / "census"
    request = _request("run", START)
    _write(census / "sources.json", {"requests": {"run": request}})
    _write(crew / "promotion-requests" / "run.json", {**request, "phase": "promoted"})
    result = collect(crew, START, END, census)
    assert result["header"]["count"] == 1
    assert validate_dataset(result)[0]["request"]["phase"] == "promoted"


def test_distinct_attempts_of_one_run_remain_distinct(tmp_path):
    crew, census = tmp_path / "crew", tmp_path / "census"
    request = _request("run", START)
    _write(census / "sources.json", {"requests": {"run": request}})
    _write(crew / "promotion-requests" / "run.json", {**request, "request_id": "retry"})
    assert collect(crew, START, END, census)["header"]["count"] == 2


def test_archived_run_records_and_historical_pointer_are_copied(tmp_path):
    crew, census = tmp_path / "crew", tmp_path / "census"
    _write(
        census / "sources.json",
        {
            "requests": {"run": _request("run", START)},
            "pointers": {"run": {"run_id": "run", "repo": "/fixture"}},
        },
    )
    manifest = crew / "archive" / "runs" / "run" / "manifest.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("status: complete\n")
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    [row] = validate_dataset(collect(crew, START, END, census))
    assert row["pointer"]["repo"] == "/fixture"
    assert row["run_files"]["manifest.md"] == "status: complete\n"
    after = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_empty_request_directory_reports_no_rate(tmp_path):
    data = collect(tmp_path / "empty", START, END)
    assert data["header"]["count"] == 0
    with pytest.raises(ValueError, match="no rate"):
        validate_dataset(data)


def test_changed_copied_set_is_refused(tmp_path):
    crew = tmp_path / "crew"
    _write(crew / "promotion-requests" / "run.json", _request("run", START))
    data = collect(crew, START, END)
    data["rows"][0]["request"]["run_id"] = "changed"
    with pytest.raises(ValueError, match="digest"):
        validate_dataset(data)


def test_trace_replay_preserves_each_recorded_verdict_and_names_censoring(
    tmp_path, monkeypatch
):
    from reckon.crew import promotion_requests

    monkeypatch.setattr(promotion_requests, "REFILL_POLL_SECONDS", 0.01)
    crew = tmp_path / "source-crew"
    for phase in (
        "promoted",
        "promotion-refused",
        "promotion-failed",
        "promotion-deferred",
    ):
        request = {
            **_request(phase, START),
            "phase": phase,
            "kind": "complete",
            "started_at": START,
            "finished_at": START,
            "priority_override": None,
        }
        _write(crew / "promotion-requests" / f"{phase}.json", request)
    _write(crew / "promotion-requests" / "unknown.json", _request("unknown", START))
    data = collect(crew, START, END)
    before = {
        p.relative_to(crew): p.read_bytes() for p in crew.rglob("*") if p.is_file()
    }
    result = replay(data)
    assert result["verdicts_match"] is True
    assert result["timed_requests"] == 4
    assert result["unmeasured_requests"] == 1
    assert result["verdicts"] == {
        "promoted": 1,
        "promotion-refused": 1,
        "promotion-failed": 1,
        "promotion-deferred": 1,
    }
    assert result["successful_promotions_per_hour"] > 0
    assert {
        p.relative_to(crew): p.read_bytes() for p in crew.rglob("*") if p.is_file()
    } == before


def test_trace_without_service_receipts_cannot_report_a_rate(tmp_path):
    crew = tmp_path / "crew"
    _write(crew / "promotion-requests" / "run.json", _request("run", START))
    with pytest.raises(ValueError, match="no rate"):
        replay(collect(crew, START, END))
