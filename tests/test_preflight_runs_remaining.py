"""The pre-flight reports runs remaining, and refuses a wave projected past it.

A coordinator committing a wave needs one figure it does not have today: how
many more runs this wallet's remaining week affords, and whether the wave about
to open fits inside it. The figure is the group's remaining seven-day window
divided by the mean per-run quota cost over the trailing seven days, keyed on the
declared budget group and role, and it is unmeasured -- never zero -- when fewer
than one priced run exists, because a zero would refuse every wave and read as a
measurement rather than an absence.

The cases below build synthetic run records under a temporary ledger and hand the
group a synthetic window reading, so the figure is arithmetic rather than a
stroke of this host's clock. Every assertion reads the emitted payload, which is
what a coordinator routes on, rather than a rendered summary string.

The refusal is exercised in both directions: a wave whose projected cost exceeds
the remaining window is refused with the runs-remaining figure and the reset time
in its text, and a wave inside the window is admitted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import budget, ledger
from reckon.crew import window_reading

PROJECT = "demo"

# A fixed instant, so every age, every receipt stamp and every reset stamp is
# arithmetic rather than a stroke of the clock. A test reading the wall clock
# here would pass on the day it was written and drift afterwards.
NOW = datetime(2026, 9, 21, 18, 0, 0, tzinfo=UTC)

# Two lanes sharing one declared wallet, so the figure is counted once per group
# rather than once per lane.
CONFIG = {
    "default_backend": "sol-a",
    "backends": {
        "sol-a": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "budget_group": "sol",
        },
        "sol-b": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "budget_group": "sol",
        },
    },
    "roles": {"implement": {}, "review": {}},
    "budget": {"utilisation_ceiling_pct": 100, "exhausted_statuses": []},
}

# The weekly receipt row every priced run carries, in the unit the receipt
# writes: whole percent of the seven-day window.
WEEKLY_WINDOW_MINUTES = 10080


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _reading(
    five_hour: float,
    seven_day: float,
    *,
    age_seconds: float = 5.0,
    week_reset_in_hours: float = 100.0,
) -> window_reading.WindowReading:
    """One member's window report, aged exactly ``age_seconds`` against ``NOW``."""
    observed = NOW - timedelta(seconds=age_seconds)
    figures = (
        window_reading.WindowFigure(
            period="five_hour",
            utilisation=five_hour,
            observed_at=observed,
            age_seconds=age_seconds,
            resets_at=_iso(NOW + timedelta(hours=1.0)),
        ),
        window_reading.WindowFigure(
            period="seven_day",
            utilisation=seven_day,
            observed_at=observed,
            age_seconds=age_seconds,
            resets_at=_iso(NOW + timedelta(hours=week_reset_in_hours)),
        ),
    )
    return window_reading.WindowReading(
        figures=figures, observed_at=observed, age_seconds=age_seconds
    )


def _run_doc(
    *,
    run_id: str,
    role: str,
    backend: str,
    used_percent: float | None,
    completed_at: datetime,
    window_minutes: int = WEEKLY_WINDOW_MINUTES,
) -> dict:
    """One committed run record, priced by the weekly row of its own receipt."""
    windows = []
    if used_percent is not None:
        windows.append(
            {
                "window_minutes": window_minutes,
                "used_percent": used_percent,
                "resets_at": 1790869124,
                "observed_at": _iso(completed_at),
            }
        )
    return {
        "run_id": run_id,
        "role": role,
        "backend": backend,
        "completed_at": _iso(completed_at),
        "lane_receipt": {
            "observed_at": _iso(completed_at),
            "quota_windows": windows,
        },
    }


def _write_runs(root: Path, records: list[dict]) -> None:
    """Commit synthetic run records under the ledger this project reads."""
    run_dir = root / "docs" / "state" / PROJECT / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    for record in records:
        (run_dir / f"{record['run_id']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )


def _rows(root: Path) -> list[dict]:
    """The committed run records the ledger under ``root`` holds."""
    return ledger.runs(PROJECT, root)


def _runway(report: list[dict], group: str = "sol") -> dict:
    found = [entry for entry in report if entry["group"] == group]
    assert len(found) == 1, f"expected exactly one entry for {group!r}, got {found}"
    return found[0]["bar"]["runway"]


def _bar(report: list[dict], group: str = "sol") -> dict:
    found = [entry for entry in report if entry["group"] == group]
    assert len(found) == 1, f"expected exactly one entry for {group!r}, got {found}"
    return found[0]["bar"]


def _four_implement_runs(
    root: Path, *, cost: float = 20.0, hours_ago: float = 6.0
) -> None:
    _write_runs(
        root,
        [
            _run_doc(
                run_id=f"r-{index}",
                role="implement",
                backend="sol-a",
                used_percent=cost,
                completed_at=NOW - timedelta(hours=hours_ago + index),
            )
            for index in range(4)
        ],
    )


# ── The figure: the remaining week over the mean per-run cost ────────────────


def test_the_figure_is_the_remaining_window_over_the_mean_run_cost(
    tmp_path: Path,
) -> None:
    """40% of the week left at 20% a run is two runs, from the payload."""
    _four_implement_runs(tmp_path)

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.60)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["state"] == budget.OBSERVED
    assert runway["remaining_pct"] == pytest.approx(40.0)
    assert runway["mean_run_cost_pct"] == pytest.approx(20.0)
    assert runway["priced_runs"] == 4
    assert runway["runs_remaining"] == pytest.approx(2.0)


def test_the_figure_carries_the_age_of_both_its_inputs(tmp_path: Path) -> None:
    """The window's age and the mean's own observation age both ride the figure."""
    _write_runs(
        tmp_path,
        [
            _run_doc(
                run_id="r-new",
                role="implement",
                backend="sol-a",
                used_percent=20.0,
                completed_at=NOW - timedelta(hours=2),
            ),
            _run_doc(
                run_id="r-old",
                role="implement",
                backend="sol-a",
                used_percent=20.0,
                completed_at=NOW - timedelta(hours=30),
            ),
        ],
    )

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.60, age_seconds=120.0)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["age_seconds"] == pytest.approx(120.0)
    assert runway["observed_at"] == _iso(NOW - timedelta(seconds=120))
    assert runway["mean_age_seconds"] == pytest.approx(2 * 3600.0)
    assert runway["mean_observed_at"] == _iso(NOW - timedelta(hours=2))
    # The quantisation of a whole-percent receipt rides the figure rather than
    # being dropped, so a reader never mistakes the aggregate for an exact count.
    assert runway["quantisation_pct"] == pytest.approx(1.0)


def test_the_figure_is_reported_per_role_and_per_group(tmp_path: Path) -> None:
    """One wallet, one remaining window, and a run count per role."""
    _write_runs(
        tmp_path,
        [
            _run_doc(
                run_id="r-impl-1",
                role="implement",
                backend="sol-a",
                used_percent=30.0,
                completed_at=NOW - timedelta(hours=3),
            ),
            _run_doc(
                run_id="r-impl-2",
                role="implement",
                backend="sol-b",
                used_percent=30.0,
                completed_at=NOW - timedelta(hours=4),
            ),
            _run_doc(
                run_id="r-review-1",
                role="review",
                backend="sol-b",
                used_percent=10.0,
                completed_at=NOW - timedelta(hours=5),
            ),
        ],
    )

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.60)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["mean_run_cost_pct"] == pytest.approx(70.0 / 3.0)
    assert sorted(runway["by_role"]) == ["implement", "review"]
    assert runway["by_role"]["implement"]["priced_runs"] == 2
    assert runway["by_role"]["implement"]["mean_run_cost_pct"] == pytest.approx(30.0)
    assert runway["by_role"]["implement"]["runs_remaining"] == pytest.approx(
        40.0 / 30.0
    )
    assert runway["by_role"]["review"]["runs_remaining"] == pytest.approx(4.0)


def test_a_run_from_beyond_the_trailing_week_is_not_priced(tmp_path: Path) -> None:
    """A receipt outside the horizon describes a window that has already reset."""
    _write_runs(
        tmp_path,
        [
            _run_doc(
                run_id="r-fresh",
                role="implement",
                backend="sol-a",
                used_percent=20.0,
                completed_at=NOW - timedelta(hours=12),
            ),
            _run_doc(
                run_id="r-stale",
                role="implement",
                backend="sol-a",
                used_percent=80.0,
                completed_at=NOW - timedelta(days=8),
            ),
        ],
    )

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.60)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["priced_runs"] == 1
    assert runway["mean_run_cost_pct"] == pytest.approx(20.0)


# ── Unmeasured, never zero ──────────────────────────────────────────────────


def test_no_priced_run_reports_unmeasured_never_zero(tmp_path: Path) -> None:
    """Nothing priced is an explicit absence, not a zero that refuses everything."""
    _write_runs(
        tmp_path,
        [
            _run_doc(
                run_id="r-unpriced",
                role="implement",
                backend="sol-a",
                used_percent=None,
                completed_at=NOW - timedelta(hours=3),
            )
        ],
    )

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.60)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["state"] == budget.UNKNOWN
    assert runway["runs_remaining"] is None
    assert runway["mean_run_cost_pct"] is None
    assert runway["priced_runs"] == 0
    assert runway["reason"]
    # The window *was* measured, so it is carried even though nothing divides it.
    assert runway["remaining_pct"] == pytest.approx(40.0)


def test_a_group_whose_week_could_not_be_read_reports_unmeasured() -> None:
    """With no seven-day clock there is no remaining window to divide."""
    runway = _runway(budget.group_pace(CONFIG, windows={}, now=NOW))

    assert runway["state"] == budget.UNKNOWN
    assert runway["runs_remaining"] is None
    assert runway["remaining_pct"] is None
    assert runway["reason"]


def test_an_unmeasured_runway_refuses_nothing() -> None:
    """Absence of a signal never holds a wave, exactly as it never holds a lane."""
    bar = _bar(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.60)},
            ready=[{"name": "open-node", "group": "sol", "score": 0.9}],
            now=NOW,
        )
    )

    assert bar["refusal"] is None
    assert [entry["name"] for entry in bar["admitted"]] == ["open-node"]


# ── The refusal ─────────────────────────────────────────────────────────────


def test_a_wave_projected_past_the_reset_is_refused(tmp_path: Path) -> None:
    """90% spent leaves 10%; one run at 15% projects past it, so the wave is held."""
    _write_runs(
        tmp_path,
        [
            _run_doc(
                run_id=f"r-{index}",
                role="implement",
                backend="sol-a",
                used_percent=15.0,
                completed_at=NOW - timedelta(hours=2 + index),
            )
            for index in range(3)
        ],
    )

    bar = _bar(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.90)},
            records=_rows(tmp_path),
            ready=[{"name": "open-node", "group": "sol", "score": 0.9}],
            now=NOW,
        )
    )

    refusal = bar["refusal"]
    assert refusal is not None
    assert refusal["refused"] is True
    assert refusal["runs_remaining"] == pytest.approx(2.0 / 3.0)
    # The refusal names the runs remaining and the reset time, because those are
    # the two figures that let a coordinator size the wave and time its retry.
    assert "0.67 runs remain" in refusal["reason"]
    reset = _iso(NOW + timedelta(hours=100.0))
    assert reset in refusal["reason"]
    assert refusal["resets_at"] == reset
    # A refused wave admits nobody, and the node that would have been admitted is
    # held rather than silently dropped.
    assert bar["admitted"] == []
    assert "open-node" in bar["held"]
    assert refusal["withheld"] == ["open-node"]


def test_a_wave_inside_the_window_is_admitted(tmp_path: Path) -> None:
    """10% spent leaves 90%; one run at 15% fits, so the wave opens."""
    _write_runs(
        tmp_path,
        [
            _run_doc(
                run_id="r-0",
                role="implement",
                backend="sol-a",
                used_percent=15.0,
                completed_at=NOW - timedelta(hours=2),
            )
        ],
    )

    bar = _bar(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.10)},
            records=_rows(tmp_path),
            ready=[{"name": "open-node", "group": "sol", "score": 0.9}],
            now=NOW,
        )
    )

    assert bar["refusal"] is None
    assert [entry["name"] for entry in bar["admitted"]] == ["open-node"]
    runway = bar["runway"]
    assert runway["runs_remaining"] == pytest.approx(6.0)


def test_preflight_reads_the_records_from_the_ledger_it_is_given(
    tmp_path: Path,
) -> None:
    """The figure is derived from the committed records, not from an injected set."""
    _write_runs(
        tmp_path,
        [
            _run_doc(
                run_id=f"r-{index}",
                role="implement",
                backend="sol-a",
                used_percent=15.0,
                completed_at=NOW - timedelta(hours=2 + index),
            )
            for index in range(2)
        ],
    )

    report = budget.preflight(
        PROJECT,
        CONFIG,
        root=tmp_path,
        windows={"sol-a": _reading(0.10, 0.90)},
        ready=[{"name": "open-node", "group": "sol", "score": 0.9}],
        now=NOW,
    )

    entry = next(item for item in report["groups"] if item["group"] == "sol")
    assert entry["bar"]["runway"]["priced_runs"] == 2
    assert entry["bar"]["refusal"] is not None
    assert "runs remain before the weekly reset at" in entry["bar"]["refusal"]["reason"]
