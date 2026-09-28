"""The pre-flight reports runs remaining, and refuses a wave projected past it.

A coordinator committing a wave needs one figure it does not have today: how
many more runs this wallet's remaining week affords, and whether the wave about
to open fits inside it. The figure is the group's remaining seven-day window
divided by the mean per-run quota cost over the trailing seven days, keyed on the
declared budget group and role, and it is unmeasured -- never zero -- when the
week's cost cannot be measured, because a zero would refuse every wave and read
as a measurement rather than an absence.

A receipt's weekly figure is the *level* the window stood at when the run was
harvested -- a stock. The run's own cost is a *flow*: the rise in that level
across a reset window, divided by the intervals it spans -- the receipts bound one
fewer gap of spend than there are receipts, because the first receipt is the level
the rise starts from. Pricing the level as though it were the cost would read a
nearly full window as a nearly spent budget and throttle every metered wave, and
dividing the rise by the receipt count would overstate the runs remaining by
n/(n-1); the cases below pin the flow, not the level, and pin the interval divisor
against the receipt count.

The cases build synthetic run records under a temporary ledger and hand the group
a synthetic window reading, so the figure is arithmetic rather than a stroke of
this host's clock. Every assertion reads the emitted payload, which is what a
coordinator routes on, rather than a rendered summary string.

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

# The receipt rows of two consecutive weekly windows, distinguished by their own
# reset boundary: a rise is priced within one, never across.
CURRENT_RESET = "2026-09-26T00:00:00Z"
PREVIOUS_RESET = "2026-09-19T00:00:00Z"

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
    resets_at: str = CURRENT_RESET,
    window_minutes: int = WEEKLY_WINDOW_MINUTES,
) -> dict:
    """One committed run record, carrying its weekly receipt row."""
    windows = []
    if used_percent is not None:
        windows.append(
            {
                "window_minutes": window_minutes,
                "used_percent": used_percent,
                "resets_at": resets_at,
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


def _series(
    root: Path,
    *,
    count: int,
    start_pct: float,
    end_pct: float,
    oldest_hours_ago: float,
    resets_at: str = CURRENT_RESET,
    role: str = "implement",
    backend: str = "sol-a",
    run_id_prefix: str = "r",
) -> None:
    """Commit ``count`` receipts rising linearly from ``start_pct`` to ``end_pct``.

    The series is oldest-first, each receipt one hour after the last, so the
    earliest-to-latest rise is exactly ``end_pct - start_pct`` over ``count - 1``
    intervals, and its reset boundary is the caller's. This is the flow the
    estimator must price, and the shape a level-based reading mistakes for a spent
    budget.
    """
    step = 0.0 if count < 2 else (end_pct - start_pct) / (count - 1)
    _write_runs(
        root,
        [
            _run_doc(
                run_id=f"{run_id_prefix}-{index}",
                role=role,
                backend=backend,
                used_percent=start_pct + step * index,
                completed_at=NOW - timedelta(hours=oldest_hours_ago - index),
                resets_at=resets_at,
            )
            for index in range(count)
        ],
    )


def _runway(report: list[dict], group: str = "sol") -> dict:
    found = [entry for entry in report if entry["group"] == group]
    assert len(found) == 1, f"expected exactly one entry for {group!r}, got {found}"
    return found[0]["bar"]["runway"]


def _bar(report: list[dict], group: str = "sol") -> dict:
    found = [entry for entry in report if entry["group"] == group]
    assert len(found) == 1, f"expected exactly one entry for {group!r}, got {found}"
    return found[0]["bar"]


# ── The figure: the remaining week over the mean per-run cost ────────────────


def test_the_figure_is_the_rise_over_the_runs_that_produced_it(tmp_path: Path) -> None:
    """A rise from 10% to 18% over 12 receipts prices 8/11 of a point a run.

    The window's level, 18%, is where the week stands -- not what a run cost. The
    cost is the rise, divided by the intervals it spans: 12 receipts bound 11 gaps
    of spend, because the first receipt is the 10% the rise starts from. So the
    figure is (100 - 18) divided by 8/11.
    """
    _series(tmp_path, count=12, start_pct=10.0, end_pct=18.0, oldest_hours_ago=30.0)

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.10)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["state"] == budget.OBSERVED
    assert runway["mean_run_cost_pct"] == pytest.approx(8.0 / 11.0)
    assert runway["priced_runs"] == 12
    assert runway["remaining_pct"] == pytest.approx(90.0)
    assert runway["runs_remaining"] == pytest.approx(90.0 / (8.0 / 11.0))


def test_a_two_receipt_window_prices_one_interval_not_two(tmp_path: Path) -> None:
    """Two receipts bound one gap, so the per-run cost is the whole rise.

    Dividing the rise by the receipt count instead of the interval count would
    halve this cost and double the runs remaining -- exactly the 2x error the
    interval divisor exists to prevent, on the smallest window that measures.
    """
    _series(tmp_path, count=2, start_pct=10.0, end_pct=18.0, oldest_hours_ago=5.0)

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.10)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["priced_runs"] == 2
    assert runway["mean_run_cost_pct"] == pytest.approx(8.0 / 1.0)
    # 90% remaining at 8 points a run is 11.25 runs; the receipt-count divisor
    # would have said 22.5, twice as many.
    assert runway["runs_remaining"] == pytest.approx(90.0 / 8.0)


def test_a_nearly_full_window_does_not_report_few_runs(tmp_path: Path) -> None:
    """A high level with no rise is unmeasured, never a nearly spent budget.

    This is the defect the flow estimator exists to prevent: pricing the level as
    the cost would read twelve runs that each moved the window a hair as though
    each had spent 18% of the week, and report a fraction of a run remaining.
    """
    _series(tmp_path, count=12, start_pct=18.0, end_pct=18.0, oldest_hours_ago=30.0)

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.10)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["state"] == budget.UNKNOWN
    assert runway["runs_remaining"] is None
    assert runway["reason"]
    # The window itself was measured, so the remaining week is still carried.
    assert runway["remaining_pct"] == pytest.approx(90.0)


def test_a_window_that_reset_mid_series_prices_the_current_window(
    tmp_path: Path,
) -> None:
    """Receipts from a reset window price their own rise, not the current level.

    Five receipts in the previous window sat flat at 90% -- a nearly full window
    that moved not at all -- and four in the current window rose 10% to 13%. Only
    the current window's rise is priced, so the per-run cost is 3/3 over the three
    intervals those four receipts bound; a level-based reading would have taken the
    90% level for a cost and reported a fraction of a run.
    """
    _series(
        tmp_path,
        count=5,
        start_pct=90.0,
        end_pct=90.0,
        oldest_hours_ago=40.0,
        resets_at=PREVIOUS_RESET,
        run_id_prefix="old",
    )
    _series(
        tmp_path,
        count=4,
        start_pct=10.0,
        end_pct=13.0,
        oldest_hours_ago=8.0,
        resets_at=CURRENT_RESET,
        run_id_prefix="new",
    )

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.10)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["state"] == budget.OBSERVED
    assert runway["mean_run_cost_pct"] == pytest.approx(3.0 / 3.0)
    assert runway["priced_runs"] == 4


def test_the_figure_carries_the_age_of_both_its_inputs(tmp_path: Path) -> None:
    """The window's age and the mean's own observation age both ride the figure."""
    _series(tmp_path, count=2, start_pct=10.0, end_pct=12.0, oldest_hours_ago=3.0)

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.40, age_seconds=120.0)},
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
    """One wallet, one remaining window, and a run count per role.

    The wallet is one cumulative level line, so its receipts rise monotonically
    however the roles alternate; each role's own receipts then rise by their own
    span, and both roles draw on the same remaining week.
    """
    roles = ["implement", "review"]
    _write_runs(
        tmp_path,
        [
            _run_doc(
                run_id=f"r-{index}",
                role=roles[index % 2],
                backend="sol-a",
                used_percent=10.0 + 2.0 * index,
                completed_at=NOW - timedelta(hours=10.0 - index),
            )
            for index in range(6)
        ],
    )

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.10)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    # The whole wallet rose 10 points over 6 receipts, i.e. 5 intervals.
    assert runway["mean_run_cost_pct"] == pytest.approx(10.0 / 5.0)
    assert runway["priced_runs"] == 6
    assert sorted(runway["by_role"]) == ["implement", "review"]
    # implement drew 10, 14, 18 -- a rise of 8 over its 2 intervals; review drew
    # 12, 16, 20 -- the same rise over its own 2.
    assert runway["by_role"]["implement"]["mean_run_cost_pct"] == pytest.approx(
        8.0 / 2.0
    )
    assert runway["by_role"]["implement"]["runs_remaining"] == pytest.approx(
        90.0 / (8.0 / 2.0)
    )
    assert runway["by_role"]["review"]["priced_runs"] == 3
    assert runway["by_role"]["review"]["runs_remaining"] == pytest.approx(
        90.0 / (8.0 / 2.0)
    )


def test_a_run_from_beyond_the_trailing_week_is_not_priced(tmp_path: Path) -> None:
    """A receipt outside the horizon describes a window that has already reset."""
    _series(tmp_path, count=2, start_pct=10.0, end_pct=12.0, oldest_hours_ago=8.0)
    _series(
        tmp_path,
        count=2,
        start_pct=60.0,
        end_pct=80.0,
        oldest_hours_ago=8 * 24.0,
        resets_at=PREVIOUS_RESET,
        run_id_prefix="stale",
    )

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.10)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["priced_runs"] == 2
    assert runway["mean_run_cost_pct"] == pytest.approx(2.0 / 1.0)


# ── Unmeasured, never zero ──────────────────────────────────────────────────


def test_a_single_priced_receipt_is_unmeasured(tmp_path: Path) -> None:
    """One receipt measures no rise, so the cost is an absence, not a level."""
    _write_runs(
        tmp_path,
        [
            _run_doc(
                run_id="r-solo",
                role="implement",
                backend="sol-a",
                used_percent=20.0,
                completed_at=NOW - timedelta(hours=3),
            )
        ],
    )

    runway = _runway(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.10)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["state"] == budget.UNKNOWN
    assert runway["runs_remaining"] is None
    assert runway["mean_run_cost_pct"] is None
    assert runway["priced_runs"] == 0
    assert runway["reason"]
    # The window itself was measured, so it is carried even though nothing divides
    # it.
    assert runway["remaining_pct"] == pytest.approx(90.0)


def test_a_run_with_no_weekly_receipt_is_not_priced(tmp_path: Path) -> None:
    """A receipt naming no weekly row prices nothing rather than a zero."""
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
            windows={"sol-a": _reading(0.10, 0.10)},
            records=_rows(tmp_path),
            now=NOW,
        )
    )

    assert runway["state"] == budget.UNKNOWN
    assert runway["runs_remaining"] is None


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


def test_a_wave_inside_the_window_is_admitted(tmp_path: Path) -> None:
    """A wave of 20 at 8/11 of a point each projects 14.5%, well inside 90%."""
    _series(tmp_path, count=12, start_pct=10.0, end_pct=18.0, oldest_hours_ago=30.0)

    bar = _bar(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.10)},
            records=_rows(tmp_path),
            ready=[
                {"name": f"open-{index}", "group": "sol", "score": 0.9}
                for index in range(20)
            ],
            now=NOW,
        )
    )

    assert bar["refusal"] is None
    assert len(bar["admitted"]) == 20
    assert bar["runway"]["runs_remaining"] == pytest.approx(90.0 / (8.0 / 11.0))


def test_a_wave_projected_past_the_reset_is_refused(tmp_path: Path) -> None:
    """10% of the week left at 8/11 a run is 13.75 runs; 20 nodes project past it."""
    _series(tmp_path, count=12, start_pct=10.0, end_pct=18.0, oldest_hours_ago=30.0)

    bar = _bar(
        budget.group_pace(
            CONFIG,
            windows={"sol-a": _reading(0.10, 0.90)},
            records=_rows(tmp_path),
            ready=[
                {"name": f"open-{index}", "group": "sol", "score": 0.9}
                for index in range(20)
            ],
            now=NOW,
        )
    )

    refusal = bar["refusal"]
    assert refusal is not None
    assert refusal["refused"] is True
    assert refusal["runs_remaining"] == pytest.approx(10.0 / (8.0 / 11.0))
    # The refusal names the runs remaining and the reset time, because those are
    # the two figures that let a coordinator size the wave and time its retry.
    assert "13.75 runs remain" in refusal["reason"]
    reset = _iso(NOW + timedelta(hours=100.0))
    assert reset in refusal["reason"]
    assert refusal["resets_at"] == reset
    # A refused wave admits nobody, and the nodes that would have been admitted
    # are held rather than silently dropped.
    assert bar["admitted"] == []
    assert len(bar["held"]) == 20
    assert len(refusal["withheld"]) == 20


def test_preflight_reads_the_records_from_the_ledger_it_is_given(
    tmp_path: Path,
) -> None:
    """The figure is derived from the committed records, not from an injected set."""
    _series(tmp_path, count=12, start_pct=10.0, end_pct=18.0, oldest_hours_ago=30.0)

    report = budget.preflight(
        PROJECT,
        CONFIG,
        root=tmp_path,
        windows={"sol-a": _reading(0.10, 0.90)},
        ready=[
            {"name": f"open-{index}", "group": "sol", "score": 0.9}
            for index in range(20)
        ],
        now=NOW,
    )

    entry = next(item for item in report["groups"] if item["group"] == "sol")
    assert entry["bar"]["runway"]["priced_runs"] == 12
    assert entry["bar"]["refusal"] is not None
    assert "runs remain before the weekly reset at" in entry["bar"]["refusal"]["reason"]
