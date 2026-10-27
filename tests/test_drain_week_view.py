"""The drain reports queue progress from request and landing records."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from reckon import mcp
from reckon.crew import runs


@pytest.fixture
def queue_home(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(runs, "_drain_plan_states", lambda _project: [])
    monkeypatch.setattr(runs, "_unloaded_sections", lambda *_args: {})
    return home


def _request(number: int, phase: str, plan: str, now: datetime) -> dict:
    stamp = (now - timedelta(hours=2)).isoformat()
    request = {
        "run_id": f"r-{number:03d}",
        "project": "week-project",
        "node": {"plan": plan, "section": f"section-{number}"},
        "phase": phase,
        "created_at": stamp,
        "queued_at": stamp,
    }
    if phase == "held":
        request["queue_hold"] = {
            "started_at": stamp,
            "reason": "plan-review-owed" if number % 2 else "operator",
        }
    if phase in {"working", "complete", "failed"}:
        request["launched_at"] = (now - timedelta(hours=3)).isoformat()
    return request


def test_week_view_counts_each_request_and_uses_the_last_admission_verdict(
    queue_home, monkeypatch
) -> None:
    now = datetime.now(UTC)
    phases = (
        ["awaiting-upstream"] * 12
        + ["queued"] * 18
        + ["held"] * 10
        + ["blocked-upstream"] * 8
        + ["working"] * 12
        + ["complete"] * 10
        + ["failed"] * 10
    )
    requests = [
        _request(number, phase, "north" if number < 80 else "south", now)
        for number, phase in enumerate(phases + phases[:40])
    ]
    assert len(requests) == 120
    for request in requests:
        runs._write_json(runs.pointer_path(request["run_id"]), request)
    verdict_log = runs.crew_home() / "admission-verdicts.jsonl"
    verdict_log.write_text(
        json.dumps({"pointers": [{"run_id": "r-012", "verdict": "held-by-gate"}]})
        + "\n"
        + json.dumps(
            {"pointers": [{"run_id": "r-012", "verdict": "held-for-allowance"}]}
        )
        + "\n"
    )
    recent = (now - timedelta(hours=4)).isoformat()
    old = (now - timedelta(days=2)).isoformat()
    landings = [
        {"run_id": f"landed-{number}", "plan": "north", "completed_at": recent}
        for number in range(5)
    ] + [
        {"run_id": "landed-old", "plan": "north", "completed_at": old},
        {"run_id": "landed-south", "plan": "south", "completed_at": recent},
    ]
    monkeypatch.setattr(
        "reckon.ledger.load", lambda *_args, **_kwargs: ({"runs": landings}, 1)
    )

    week = runs.drain("week-project")["week"]
    assert week["plans"]["north"]["phases"] == {
        "awaiting-upstream": 12,
        "queued": 18,
        "held": 10,
        "blocked-upstream": 8,
        "launched": 22,
        "landed": 6,
        "failed": 10,
    }
    assert week["plans"]["north"]["held_reasons"] == {
        "operator": 5,
        "plan-review-owed": 5,
    }
    assert week["plans"]["north"]["launches_24h"] == 32
    assert week["plans"]["north"]["landings_24h"] == 5
    assert week["plans"]["north"]["finish_estimate_days"] == pytest.approx(80 / 5)
    assert week["plans"]["south"]["phases"] == {
        "awaiting-upstream": 12,
        "queued": 18,
        "held": 10,
        "blocked-upstream": 0,
        "launched": 0,
        "landed": 1,
        "failed": 0,
    }
    assert week["plans"]["south"]["landings_24h"] == 1
    assert week["plans"]["south"]["finish_estimate_days"] == 40
    assert week["project"]["phases"] == {
        "awaiting-upstream": 24,
        "queued": 36,
        "held": 20,
        "blocked-upstream": 8,
        "launched": 22,
        "landed": 7,
        "failed": 10,
    }
    assert week["project"]["landings_24h"] == 6
    assert {row["run_id"]: row["last_verdict"] for row in week["project"]["stuck"]}[
        "r-012"
    ] == "held-for-allowance"
    assert not any(
        row["phase"] == "awaiting-upstream" for row in week["project"]["stuck"]
    )


def test_recent_launch_of_a_landed_request_uses_the_worker_stamp(
    queue_home, monkeypatch
) -> None:
    now = datetime.now(UTC)
    queued = (now - timedelta(days=2)).isoformat()
    launched = (now - timedelta(hours=2)).isoformat()
    completed = (now - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(
        "reckon.ledger.load",
        lambda *_args, **_kwargs: (
            {
                "runs": [
                    {
                        "run_id": "r-landed",
                        "plan": "north",
                        "dispatched_at": queued,
                        "completed_at": completed,
                        "worker_exit": {"launched_at": launched},
                    }
                ]
            },
            1,
        ),
    )

    week = runs.drain("week-project")["week"]
    assert week["project"]["launches_24h"] == 1
    assert week["plans"]["north"]["launches_24h"] == 1
    assert week["project"]["landings_24h"] == 1


def test_zero_landing_rate_has_no_finish_estimate(queue_home, monkeypatch) -> None:
    now = datetime.now(UTC)
    runs._write_json(runs.pointer_path("r-only"), _request(0, "queued", "north", now))
    monkeypatch.setattr("reckon.ledger.load", lambda *_a, **_k: ({"runs": []}, 0))
    assert runs.drain("week-project")["week"]["project"]["finish_estimate_days"] is None


def test_mcp_drain_reads_five_hundred_pointers_inside_deadline(
    queue_home,
) -> None:
    now = datetime.now(UTC)
    for number in range(500):
        request = _request(number, "queued", "north", now)
        runs._write_json(runs.pointer_path(request["run_id"]), request)
    for _arm in ("cold", "warm"):
        started = time.monotonic()
        result = mcp._crew("week-project", view="drain")
        elapsed = time.monotonic() - started
        assert result["week"]["project"]["phases"]["queued"] == 500
        print(f"{_arm} MCP drain: {elapsed:.3f} s for 500 pointers")
        assert elapsed < 30, f"MCP drain took {elapsed:.3f} s"
