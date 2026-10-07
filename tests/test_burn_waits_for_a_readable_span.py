"""A fresh quota reset must not project a full-window burn from a thin sample."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew.picker import snapshot
from tests.test_picker_dispatch_pace import _candidates, _config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    monkeypatch.setattr(snapshot.budget._backends, "probe_budget", lambda **_k: {})


def _record(*, used_percent: float, elapsed: timedelta) -> dict:
    observed = datetime.now(UTC)
    reset = observed + timedelta(days=7) - elapsed
    return {
        "run_id": "sample-run",
        "backend": "codex",
        "completed_at": observed.isoformat(),
        "lane_receipt": {
            "quota_state": "measured",
            "observed_at": observed.isoformat(),
            "quota_windows": [
                {
                    "window_minutes": 10_080,
                    "used_percent": used_percent,
                    "resets_at": int(reset.timestamp()),
                    "observed_at": observed.isoformat(),
                }
            ],
        },
    }


def test_a_new_week_carries_position_without_a_burn_or_pace_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record(used_percent=1.0, elapsed=timedelta(minutes=11))
    config = _config()
    rows = [record]
    view = snapshot.budget_view("demo", config, tmp_path, rows, cached_only=True)
    candidate = _candidates(monkeypatch, tmp_path, config, rows, view)["codex"]
    allowance = view["groups"][0]["allowance"]

    assert allowance["burn_multiple"] is None
    assert allowance["effective_limit"] is None
    assert "window too young to project" in allowance["reason"]
    assert candidate["burn_multiple"] is None
    assert candidate["pace_allowance"] is None
    assert candidate["utilisation_pct"] == pytest.approx(1.0)
    assert candidate["days_to_reset"] == pytest.approx(7, abs=0.01)
    assert candidate["resets_at"] is not None


def test_a_mature_week_projects_at_the_existing_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record(used_percent=6.0, elapsed=timedelta(hours=12))
    config = _config()
    rows = [record]
    view = snapshot.budget_view("demo", config, tmp_path, rows, cached_only=True)
    candidate = _candidates(monkeypatch, tmp_path, config, rows, view)["codex"]
    allowance = view["groups"][0]["allowance"]
    elapsed_fraction = 12 / (7 * 24)

    assert allowance["burn_multiple"] == pytest.approx(
        0.06 / elapsed_fraction, rel=0.001
    )
    assert allowance["effective_limit"] == pytest.approx(
        elapsed_fraction * 1.1, rel=0.001
    )
    assert candidate["burn_multiple"] == pytest.approx(
        0.06 / elapsed_fraction, rel=0.001
    )
    assert candidate["pace_allowance"] == pytest.approx(
        elapsed_fraction * 1.1, rel=0.001
    )
