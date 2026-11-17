"""A replay fixes its cohort before comparing the promotion drain."""

import json
import threading

import pytest

from reckon.crew import promotion_requests, runs
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


def test_real_replay_exercises_guards_and_a_positive_control(tmp_path):
    crew = tmp_path / "source-crew"
    request = {
        **_request("r-unavailable", START),
        "project": "missing",
        "kind": "complete",
        "options": {"gate": "passed"},
        "priority_override": None,
    }
    _write(crew / "promotion-requests" / "run.json", request)
    data = collect(crew, START, END)
    before = {
        p.relative_to(crew): p.read_bytes() for p in crew.rglob("*") if p.is_file()
    }
    result = replay(
        data,
        sources={"projects": {}, "heads": {}, "sha256": "fixture"},
        assets=tmp_path,
        scratch_root=tmp_path,
    )
    assert result["positive_control"]["phase"] == "promoted"
    assert result["requests"] == 1
    assert result["verdicts"] == {"promotion-refused": 1}
    assert result["successful_promotions_per_hour"] == 0
    assert {
        p.relative_to(crew): p.read_bytes() for p in crew.rglob("*") if p.is_file()
    } == before


def test_empty_real_replay_reports_no_rate(tmp_path):
    with pytest.raises(ValueError, match="no rate"):
        replay(collect(tmp_path / "empty", START, END), sources={}, assets=tmp_path)


def _queue_for_repository(run_id, repository, *, role="implement"):
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "repo": str(repository),
            "role": role,
            "project": "burst-fixture",
            "session": "burst-session",
            "node": {"id": run_id, "role": role},
            "phase": "complete",
        },
    )
    return promotion_requests.request_promotion(run_id)


def _write_branch(repository):
    from reckon.crew import promotion_scope

    acquire = getattr(promotion_scope, "_acquire_branch_write", None)
    if acquire is not None:
        acquire(repository)


def test_a_commitless_promotion_finishes_beside_a_landing(tmp_path):
    first, report = "r-held-landing", "r-report"
    _queue_for_repository(first, tmp_path / "repo")
    _queue_for_repository(report, tmp_path / "repo", role="review")
    landed, release, reported = threading.Event(), threading.Event(), threading.Event()

    def execute(request):
        if request["run_id"] == first:
            _write_branch(tmp_path / "repo")
            landed.set()
            assert release.wait(5), "the fixture did not release its landing"
        else:
            reported.set()
        return {"ok": True, "record": {"commits": []}}

    service = promotion_requests.PromotionService(width=1, execute=execute)
    try:
        service.tick()
        assert landed.wait(2)
        service.tick()
        assert reported.wait(0.5), "commit-less promotion waited behind a landing"
        assert not release.is_set()
    finally:
        release.set()
        service.begin_drain()
        assert service.wait_for_in_flight(10) == []


def test_two_landings_on_one_branch_never_overlap(tmp_path):
    first, second = "r-first-landing", "r-second-landing"
    _queue_for_repository(first, tmp_path / "repo")
    _queue_for_repository(second, tmp_path / "repo")
    began, release, overlapped = threading.Event(), threading.Event(), threading.Event()

    def execute(request):
        if request["run_id"] == first:
            _write_branch(tmp_path / "repo")
            began.set()
            assert release.wait(5), "the fixture did not release its landing"
        else:
            assert began.wait(2)
            _write_branch(tmp_path / "repo")
            if not release.is_set():
                overlapped.set()
        return {"ok": True, "record": {"commits": []}}

    service = promotion_requests.PromotionService(width=2, execute=execute)
    try:
        service.tick()
        assert began.wait(2)
        assert not overlapped.wait(0.5), (
            "two landing executors overlapped on one branch"
        )
    finally:
        release.set()
        service.begin_drain()
        assert service.wait_for_in_flight(10) == []


def test_landings_for_different_repositories_do_not_wait(tmp_path):
    first, second = "r-first-repository", "r-other-repository"
    _queue_for_repository(first, tmp_path / "one")
    _queue_for_repository(second, tmp_path / "two")
    began, release, concurrent = threading.Event(), threading.Event(), threading.Event()

    def execute(request):
        if request["run_id"] == first:
            _write_branch(tmp_path / "one")
            began.set()
            assert release.wait(5), "the fixture did not release its landing"
        else:
            _write_branch(tmp_path / "two")
            concurrent.set()
        return {"ok": True, "record": {"commits": []}}

    service = promotion_requests.PromotionService(width=1, execute=execute)
    try:
        service.tick()
        assert began.wait(2)
        service.tick()
        assert concurrent.wait(0.5), "another repository waited behind a landing"
    finally:
        release.set()
        service.begin_drain()
        assert service.wait_for_in_flight(10) == []
