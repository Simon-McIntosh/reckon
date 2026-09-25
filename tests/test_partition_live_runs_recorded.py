from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import mcp_views
from reckon.crew import recovery, runs

PROJECT = "recorded-runs-sample"


def _pointer(run_id: str, plan: str) -> dict:
    return {
        "project": PROJECT,
        "run_id": run_id,
        "member": f"member-{run_id}",
        "node": {"plan": plan, "section": "implementation"},
    }


def _write_events(home: Path, *events: dict) -> None:
    stream = runs.watch_stream_path(PROJECT)
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def test_recorded_classification_is_used_without_observing_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    pointer = _pointer("run-recorded", "active-plan")
    _write_events(
        tmp_path,
        {
            "event": "baseline",
            "project": PROJECT,
            "run_id": pointer["run_id"],
            "to_state": "blocked",
            "recovery_classification": "interrupted",
            "detail": "worker process ended",
            "next_action": "resume the retained session",
        },
    )

    def no_observation(*_args, **_kwargs):
        pytest.fail("a recorded run must not be observed again")

    monkeypatch.setattr(recovery, "classify_pointer", no_observation)

    in_flight, interrupted = mcp_views.partition_live_runs(PROJECT, [pointer])

    assert in_flight == {}
    assert interrupted == {
        "active-plan": [
            {
                "run_id": "run-recorded",
                "member": "member-run-recorded",
                "section": "implementation",
                "started_at": "",
                "reason": "worker process ended",
                "next_action": "resume the retained session",
            }
        ]
    }


def test_only_unrecorded_runs_use_the_live_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    recorded = _pointer("run-recorded", "active-plan")
    unrecorded = _pointer("run-unrecorded", "other-plan")
    _write_events(
        tmp_path,
        {
            "event": "baseline",
            "project": PROJECT,
            "run_id": recorded["run_id"],
            "to_state": "working",
            "recovery_classification": "running",
        },
    )
    observed: list[str] = []

    def classify(pointer: dict) -> dict:
        observed.append(pointer["run_id"])
        return {
            "classification": "interrupted",
            "detail": "no process remains",
            "next_action": "redispatch",
        }

    monkeypatch.setattr(recovery, "classify_pointer", classify)

    in_flight, interrupted = mcp_views.partition_live_runs(
        PROJECT, [recorded, unrecorded]
    )

    assert observed == ["run-unrecorded"]
    assert [row["run_id"] for row in in_flight["active-plan"]] == ["run-recorded"]
    assert [row["run_id"] for row in interrupted["other-plan"]] == ["run-unrecorded"]
