"""Tests for replaying committed pace rows as an independent report."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import pace


def _replay_project(root: Path, project: str, **kwargs):
    from reckon.crew.pace_replay import replay_project

    return replay_project(root, project, **kwargs)


def _render_report(report):
    from reckon.crew.pace_replay import render_report

    return render_report(report)


def _instant(hours_from_start: float) -> str:
    start = datetime(2026, 9, 22, 8, tzinfo=UTC)
    return (start + timedelta(hours=hours_from_start)).isoformat()


def _row(
    *,
    node: str,
    role: str,
    backend: str,
    local: bool,
    utilisation: float,
    elapsed: float,
    hold: dict | None = None,
) -> dict:
    recorded_at = _instant(elapsed)
    reset_at = datetime.fromisoformat(recorded_at) + timedelta(
        hours=pace.WEEK_HOURS - elapsed
    )
    policy = pace.PacePolicy(drain_lead_hours=12.0, pace_multiple=1.1)
    reading = pace.GroupReading(
        group="shared",
        utilisation=utilisation,
        elapsed_hours=elapsed,
    )
    allowance = pace.allowance_for_group(reading, pace=policy).as_dict()
    pace_row = {
        "lane": backend,
        "node": node,
        "score": 0.4,
        "recorded_at": recorded_at,
        "policy": {
            "drain_lead_hours": policy.drain_lead_hours,
            "pace_multiple": policy.pace_multiple,
        },
        "hold": hold,
        "group": "shared",
        "state": "observed",
        "source": "recorded_windows",
        "member": backend,
        "clocks": {
            "five_hour": {
                "period": "five_hour",
                "state": "observed",
                "utilisation": 0.2,
                "observed_at": recorded_at,
                "resets_at": _instant(elapsed + 5.0),
                "age_seconds": 0.0,
            },
            "seven_day": {
                "period": "seven_day",
                "state": "observed",
                "utilisation": utilisation,
                "observed_at": recorded_at,
                "resets_at": reset_at.isoformat(),
                "age_seconds": 0.0,
            },
        },
        "allowance": allowance,
        "bar": {
            "name": node,
            "score": 0.4,
            "state": "observed",
            "verdict": "send-metered",
            "window_fill": 0.2,
        },
        "reason": None,
    }
    return {
        "run_id": f"run-{node}",
        "role": role,
        "backend": backend,
        "local": local,
        "pace": pace_row,
    }


def _held_row() -> dict:
    return _row(
        node="held-review",
        role="review",
        backend="codex",
        local=False,
        utilisation=0.95,
        elapsed=96.0,
        hold={
            "backend": "codex",
            "held": True,
            "effective_ceiling_pct": 92.0,
            "state": {"utilisation_pct": 95.0},
            "reason": "95.0% exceeds 92.0% ceiling",
        },
    )


@pytest.fixture()
def committed_week(tmp_path: Path) -> Path:
    run_dir = tmp_path / "docs" / "state" / "sample" / "runs"
    run_dir.mkdir(parents=True)
    records = [
        _row(
            node="build-early",
            role="implement",
            backend="codex",
            local=False,
            utilisation=0.1,
            elapsed=24.0,
        ),
        _row(
            node="build-late",
            role="implement",
            backend="clive",
            local=True,
            utilisation=0.6,
            elapsed=120.0,
        ),
        _held_row(),
    ]
    for record in records:
        (run_dir / f"{record['run_id']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    (run_dir / "without-pace.json").write_text(
        json.dumps({"run_id": "without-pace"}), encoding="utf-8"
    )
    return tmp_path


def test_report_module_is_available_at_head():
    assert importlib.util.find_spec("reckon.crew.pace_replay") is not None


def test_committed_week_replays_allowances_holds_and_work_split(committed_week):
    report = _replay_project(committed_week, "sample")

    assert report["ok"] is True, report["text"]
    assert report["row_count"] == 3, report
    assert report["allowances"] == {
        "checked": 3,
        "matched": 3,
        "mismatches": [],
        "all_match": True,
    }
    assert report["holds"] == {
        "checked": 3,
        "matched": 3,
        "mismatches": [],
        "all_match": True,
    }
    assert report["split_by_class"] == {
        "implement": {"local": 1, "metered": 1, "total": 2},
        "review": {"local": 0, "metered": 1, "total": 1},
    }
    assert "implement: local=1 metered=1 total=2" in report["text"]
    assert "review: local=0 metered=1 total=1" in report["text"]


def test_mistuned_lead_is_detected_from_rows_alone(committed_week):
    report = _replay_project(
        committed_week,
        "sample",
        drain_lead_hours=24.0,
    )

    assert report["mistuned"] == {
        "requested": True,
        "candidate": 24.0,
        "detected": True,
        "mismatches": ["run-build-early", "run-build-late", "run-held-review"],
    }
    assert report["allowances"]["all_match"] is False
    assert "mistuned drain_lead_hours=24.0: detected" in report["text"]


def test_a_changed_recorded_allowance_or_hold_is_reported(committed_week):
    path = (
        committed_week / "docs" / "state" / "sample" / "runs" / "run-build-early.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    record["pace"]["allowance"]["derived"] = 99.0
    path.write_text(json.dumps(record), encoding="utf-8")

    report = _replay_project(committed_week, "sample")

    assert report["ok"] is False, report
    assert report["allowances"]["mismatches"] == ["run-build-early"]


def test_render_report_accepts_a_replay_result(committed_week):
    report = _replay_project(committed_week, "sample")

    assert _render_report(report) == report["text"]
    assert report["text"].splitlines()[:4] == [
        "rows: 3",
        "allowances: 3/3 reproduced (ok)",
        "holds: 3/3 reproduced (ok)",
        "split by class:",
    ]
