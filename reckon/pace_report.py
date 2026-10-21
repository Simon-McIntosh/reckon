"""The pace report, the pace row and the pace hold over a group's allowance.

A budget group has one allowance, computed in :mod:`reckon.crew.pace` from the
group's policy and the readings its backends publish. This module is what a
reader and a dispatcher consult about that allowance: the pace report a
pre-flight names, the row a dispatch records, and the hold that refuses work
when the group is running ahead of its own pace.

The family lives here rather than in :mod:`reckon.crew.pace` because
:func:`group_pace` reads the readings :mod:`reckon.budget` assembles, and
``reckon.budget`` imports :mod:`reckon.crew.pace` at module level, so hosting it
there would close an import cycle. The dependency therefore runs one way at
module level: ``reckon.budget`` imports this module, and this module reaches
back into ``reckon.budget`` inside the functions that need its clocks,
constants and readings. Each such name is resolved where it is used rather than
at module level.

The crew package is reached the same way, for a second reason: importing it here
would run :mod:`reckon.crew`'s package initialiser, which loads
:mod:`reckon.budget`, so a first import of this module would run against a
partially initialised ``reckon.budget``. Resolving those names inside the
functions that use them keeps importing this module free of the crew graph. The
names read only in annotations are imported under ``TYPE_CHECKING``, which costs
nothing at run time.

The lift-reading helpers here read the lift records that
:mod:`reckon.crew.budget_lift` owns. They duplicate none of it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - annotation use only
    from reckon.crew import window_reading


def _elapsed_hours(clock: Mapping[str, Any], *, moment: datetime) -> float | None:
    """How far into the week a clock stands, or ``None`` if it cannot say.

    The remaining time comes from the window's own reset stamp, so the elapsed
    figure is measured from the same origin as the weekly clock the allowance
    divides. A clock with no readable reset cannot place itself in the week, and
    an invented elapsed time would move the allowance as much as a real one.
    """
    from reckon.budget import (
        _parse_stamp,
    )
    from reckon.crew import pace as pace_module

    resets_at = _parse_stamp(clock.get("resets_at"))
    if resets_at is None:
        return None
    remaining = (resets_at - moment).total_seconds() / 3600.0
    return max(0.0, pace_module.WEEK_HOURS - remaining)


def _unknown_allowance(
    group: str, reason: str, *, utilisation: float | None = None
) -> dict[str, Any]:
    """A group's allowance where none could be derived, as an explicit absence.

    Every derived field is ``None`` rather than zero, so an allowance nobody
    could compute is not mistaken for one that came back empty. A zero allowance
    is a real and load-bearing value — a spent week earns exactly that — and the
    two must stay distinguishable in the payload.

    ``utilisation`` is carried through when a reading supplied one, because that
    figure *was* measured: a week spent past its pace but whose reset stamp could
    not be placed is a different report from a week nothing reached, and the
    reason string says which. Only the derivation is withheld, and a withheld
    derivation is never rendered as a zero.
    """
    from reckon.budget import (
        UNKNOWN,
    )

    allowance: dict[str, Any] = {"group": group, "state": UNKNOWN, "reason": reason}
    allowance.update(
        {
            "utilisation": utilisation,
            "elapsed_hours": None,
            "drain_hours": None,
            "remaining_budget": None,
            "remaining_windows": None,
            "pace_multiple": None,
            "derived": None,
            "provider_ceiling": None,
            "effective_limit": None,
            "limited_by": None,
            "window_minutes": None,
            "elapsed_fraction": None,
            "burn_multiple": None,
            "observed_at": None,
            "resets_at": None,
        }
    )
    return allowance


def _scale_for_banked_reset(
    allowance: dict[str, Any], *, credit: float
) -> dict[str, Any]:
    """Count a banked reset as one extra full window of allowance.

    A banked reset adds a whole extra window of budget on top of what remains,
    so the same reading admits more: the derived allowance scales up by the
    extra window and the burn, which measures spend against the budget that is
    actually available, scales down by the same factor. The credit is capped at
    one window — a group carries at most one banked reset.

    A group with no banked reset is returned byte for byte as it was derived:
    ``reset_available`` and ``reset_factor`` are added only when a reset is
    counted, so the allowance a group without one carries is unchanged and stays
    equal to the allowance a replay recomputes from the same row. ``reset_factor``
    is the exact figure the burn was divided by, recorded so a reader needing the
    number rather than the flag reads it from one place.
    """
    credit = max(0.0, min(1.0, float(credit)))
    if credit > 0:
        allowance["reset_available"] = True
        factor = 1.0 + credit
        # The factor is recorded beside the flag so the reader that needs the
        # numeric scaling — the pace hold's release instant — reads it from here
        # rather than keeping a second copy of the constant that could drift
        # from the one the burn was actually divided by.
        allowance["reset_factor"] = factor
        derived = allowance.get("derived")
        if isinstance(derived, (int, float)) and not isinstance(derived, bool):
            scaled = float(derived) * factor
            allowance["derived"] = scaled
            ceiling = allowance.get("provider_ceiling")
            if ceiling is not None and float(ceiling) < scaled:
                allowance["effective_limit"] = float(ceiling)
                allowance["limited_by"] = "ceiling"
            else:
                allowance["effective_limit"] = scaled
                allowance["limited_by"] = "allowance"
        burn = allowance.get("burn_multiple")
        if isinstance(burn, (int, float)) and not isinstance(burn, bool):
            allowance["burn_multiple"] = float(burn) / factor
        remaining = allowance.get("remaining_budget")
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
            allowance["remaining_budget"] = float(remaining) + credit
    return allowance


def _group_allowance(
    group: str,
    reading: window_reading.WindowReading,
    clocks: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    moment: datetime,
) -> dict[str, Any]:
    """Derive a group's allowance from the operative window it reported.

    A reading with both historical clocks keeps the next-five-hour derivation.
    A primary-only reading has no shorter burst window to divide through, so its
    allowance is the elapsed fraction of its own reported period, leaned by the
    configured pace multiple. Both paths carry the operative length and reset,
    so a reader can reproduce the choice without knowing a period-name table.
    """
    from reckon.budget import (
        CLOCK_FIVE_HOUR,
        CLOCK_SEVEN_DAY,
        OBSERVED,
        _burn_floor_reason,
        _parse_stamp,
    )
    from reckon.crew import budget_reset
    from reckon.crew import pace as pace_module

    operative = _operative_window(reading)
    if operative is None or operative.window_minutes is None:
        return _unknown_allowance(
            group, "the group's operative window was not read, so nothing divides"
        )
    reset = _parse_stamp(operative.resets_at)
    if reset is None:
        return _unknown_allowance(
            group,
            "the group's operative window carries no readable reset, so it cannot "
            "be placed in its period",
            utilisation=operative.utilisation,
        )
    window_hours = operative.window_minutes / 60.0
    remaining_hours = (reset - moment).total_seconds() / 3600.0
    elapsed = max(0.0, window_hours - remaining_hours)
    elapsed_fraction = min(1.0, elapsed / window_hours)
    multiple = pace_module.policy(config).pace_multiple
    burn = (
        None
        if elapsed_fraction <= 0
        else float(operative.utilisation) / elapsed_fraction
    )
    floor_reason = _burn_floor_reason(
        float(operative.utilisation) * 100.0, elapsed_fraction
    )

    # A banked reset is read from the durable flag before the allowance is
    # derived, so the pace and the hold that reads it both count the extra
    # allowance covers. Nothing is tracked for a group that never carried one,
    # so this is a read for every other group.
    reset_state = budget_reset.observe(
        group,
        resets_at=operative.resets_at,
        window_minutes=operative.window_minutes,
        moment=moment,
        utilisation=float(operative.utilisation),
    )
    available = bool(reset_state.get("available"))
    credit = 1.0 if available else 0.0

    if floor_reason is not None:
        allowance = _unknown_allowance(
            group, floor_reason, utilisation=float(operative.utilisation)
        )
        allowance.update(
            state=OBSERVED,
            elapsed_hours=elapsed,
            drain_hours=window_hours,
            remaining_budget=max(0.0, 1.0 - float(operative.utilisation)),
            pace_multiple=float(multiple),
            window_minutes=operative.window_minutes,
            elapsed_fraction=elapsed_fraction,
            observed_at=operative.observed_at.isoformat(),
            resets_at=operative.resets_at,
        )
        return _scale_for_banked_reset(allowance, credit=credit)

    # Accounts that publish both clocks keep the established next-window
    # derivation. A primary-only account has no shorter burst ceiling to divide
    # through, so its safe cumulative limit is the elapsed share of the window.
    if reading.figure(CLOCK_FIVE_HOUR) is None:
        derived = min(1.0, elapsed_fraction * float(multiple))
        return _scale_for_banked_reset(
            {
                "group": group,
                "state": OBSERVED,
                "reason": None,
                "utilisation": float(operative.utilisation),
                "elapsed_hours": elapsed,
                "drain_hours": window_hours,
                "remaining_budget": max(0.0, 1.0 - float(operative.utilisation)),
                "remaining_windows": None,
                "pace_multiple": float(multiple),
                "derived": derived,
                "provider_ceiling": None,
                "effective_limit": derived,
                "limited_by": "allowance",
                "window_minutes": operative.window_minutes,
                "elapsed_fraction": elapsed_fraction,
                "burn_multiple": burn,
                "observed_at": operative.observed_at.isoformat(),
                "resets_at": operative.resets_at,
            },
            credit=credit,
        )

    week = clocks[CLOCK_SEVEN_DAY]
    if week["state"] != OBSERVED:
        return _unknown_allowance(
            group, "the group's weekly clock was not read, so nothing divides"
        )
    elapsed = _elapsed_hours(week, moment=moment)
    if elapsed is None:
        return _unknown_allowance(
            group,
            "the group's weekly clock carries no readable reset, so it cannot "
            "be placed in the week",
            utilisation=week["utilisation"],
        )
    reading = pace_module.GroupReading(
        group=group,
        utilisation=week["utilisation"],
        elapsed_hours=elapsed,
    )
    allowance = pace_module.allowance_for_group(reading, config=config).as_dict()
    allowance.update(
        {
            "state": OBSERVED,
            "reason": None,
            "window_minutes": operative.window_minutes,
            "elapsed_fraction": elapsed_fraction,
            "burn_multiple": burn,
            "observed_at": operative.observed_at.isoformat(),
            "resets_at": operative.resets_at,
        }
    )
    return _scale_for_banked_reset(allowance, credit=credit)


def _operative_window(
    reading: window_reading.WindowReading,
) -> window_reading.WindowFigure | None:
    """Return the longest provider-reported quota window in one reading."""
    from reckon.budget import (
        CLOCK_FIVE_HOUR,
        CLOCK_SEVEN_DAY,
        WEEKLY_WINDOW_MINUTES,
    )

    candidates = [
        figure
        for figure in reading.figures
        if figure.window_minutes is not None and figure.window_minutes > 0
    ]
    if candidates:
        return max(candidates, key=lambda figure: figure.window_minutes or 0)
    fallback = reading.figure(CLOCK_SEVEN_DAY)
    if fallback is not None:
        return replace(fallback, window_minutes=WEEKLY_WINDOW_MINUTES)
    fallback = reading.figure(CLOCK_FIVE_HOUR)
    return None if fallback is None else replace(fallback, window_minutes=300)


def _priced_receipt(row: Mapping[str, Any]) -> tuple[float, Any] | None:
    """A run's weekly receipt as ``(used_percent, resets_at)``, or ``None``.

    The figure is the level the seven-day window stood at when the run was
    harvested, in the whole percent the receipt writes, and the reset boundary
    that level belongs to. A receipt naming no weekly row, or one whose figure is
    not a number, prices nothing rather than a zero.
    """
    from reckon.budget import (
        WEEKLY_WINDOW_MINUTES,
    )

    receipt = row.get("lane_receipt")
    if not isinstance(receipt, Mapping):
        return None
    windows = receipt.get("quota_windows")
    if not isinstance(windows, list):
        return None
    for window in windows:
        if not isinstance(window, Mapping):
            continue
        if window.get("window_minutes") != WEEKLY_WINDOW_MINUTES:
            continue
        used = window.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            return None
        return float(used), window.get("resets_at")
    return None


def _priced_runs(
    rows: Iterable[Mapping[str, Any]],
    members: Iterable[str],
    *,
    moment: datetime,
) -> list[tuple[str, str, datetime, float, Any]]:
    """Every priced run of this wallet's members inside the trailing week.

    One tuple per run: its backend, its role, the moment its receipt was observed,
    the weekly level the receipt recorded, and the reset boundary that level
    belongs to. Only a run whose backend declares the wallet is read, and only one
    whose own durable stamp places it inside the trailing week -- a receipt from
    outside the horizon measures a window that has already reset. The stamp is the
    record's own, read through the one reader that names it, so every surface that
    ages a run ages it the same way.
    """
    from reckon.budget import (
        RUNWAY_TRAILING_DAYS,
        _run_backend,
        _run_observed_at,
    )

    horizon = moment - timedelta(days=RUNWAY_TRAILING_DAYS)
    allowed = set(members)
    priced: list[tuple[str, str, datetime, float, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        backend = _run_backend(row)
        if backend not in allowed:
            continue
        receipt = _priced_receipt(row)
        if receipt is None:
            continue
        observed = _run_observed_at(row)
        if observed is None or observed < horizon:
            continue
        used, resets = receipt
        key = resets if isinstance(resets, (str, int, float)) else repr(resets)
        priced.append((backend, str(row.get("role") or ""), observed, used, key))
    return priced


def _run_cost_flow(
    priced: Iterable[tuple[str, str, datetime, float, Any]],
) -> tuple[float, int, datetime] | None:
    """The mean per-run cost as a rise within one reset window of one wallet.

    A receipt's seven-day figure is the level the window stood at when the run was
    harvested -- a stock, not what the run itself spent. Two receipts that share a
    reset boundary differ by the quota spent between them, so the flow of one
    window is the rise from its earliest to its latest receipt, and summing the
    rises per window gives the wallet's total rise. Because a window is keyed by
    its own reset boundary, a window that reset inside the trailing week
    contributes its own rise and never its level against the current window's --
    the current window's receipts alone price the current week.

    The rise is divided by the number of *intervals* it spans, not the number of
    receipts: ``n`` receipts bound ``n - 1`` gaps of spend, because the first
    receipt is the level the rise starts *from*, not a run the rise paid for.
    Dividing by ``n`` would understate the per-run cost -- by ``n / (n - 1)``,
    exactly a factor of two on a two-receipt window -- and so overstate the runs
    remaining, which is the direction that weakens the refusal. The receipts are
    still counted, so a reader sees how many priced runs fed the figure.

    A window holding fewer than two receipts bounds no interval, and a window
    whose level did not move prices nothing, so neither contributes. That is what
    keeps the figure honest: a window's level divided by another level would read
    a nearly full window as a nearly spent budget and throttle every metered wave.
    ``None`` means no window measured a rise, which is an absence and never a zero
    cost or an infinite count.
    """
    by_window: dict[Any, list[tuple[datetime, float]]] = {}
    for _backend, _role, observed, used, resets in priced:
        by_window.setdefault(resets, []).append((observed, used))
    total_rise = 0.0
    total_intervals = 0
    priced_receipts = 0
    newest: datetime | None = None
    for samples in by_window.values():
        if len(samples) < 2:
            continue
        samples.sort(key=lambda item: item[0])
        rise = samples[-1][1] - samples[0][1]
        if rise <= 0.0:
            continue
        total_rise += rise
        total_intervals += len(samples) - 1
        priced_receipts += len(samples)
        if newest is None or samples[-1][0] > newest:
            newest = samples[-1][0]
    if total_intervals == 0 or total_rise <= 0.0 or newest is None:
        return None
    return total_rise / total_intervals, priced_receipts, newest


def _unmeasured_runway(
    group: str,
    reason: str,
    *,
    remaining_pct: float | None = None,
    week: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A runway no figure could be derived for, as an explicit absence.

    ``runs_remaining`` and the mean cost are ``None`` rather than zero: zero runs
    remaining reads as a spent week and refuses every wave, which is the same
    substitution this module refuses for a hold and for a pace. The remaining
    window is carried when it could be read, because the window *was* measured
    even when nothing divides it, and the reason string says which input was
    missing.
    """
    from reckon.budget import (
        UNKNOWN,
    )

    return {
        "group": group,
        "state": UNKNOWN,
        "reason": reason,
        "remaining_pct": remaining_pct,
        "mean_run_cost_pct": None,
        "priced_runs": 0,
        "runs_remaining": None,
        "resets_at": None if week is None else week.get("resets_at"),
        "observed_at": None if week is None else week.get("observed_at"),
        "age_seconds": None if week is None else week.get("age_seconds"),
        "mean_observed_at": None,
        "mean_age_seconds": None,
        "drain_deadline_hours": None,
        "by_role": {},
        "quantisation_pct": None,
    }


def _group_runway(
    group: str,
    members: Iterable[str],
    clocks: Mapping[str, Mapping[str, Any]],
    rows: Iterable[Mapping[str, Any]],
    config: Mapping[str, Any] | None,
    *,
    moment: datetime,
) -> dict[str, Any]:
    """Runs remaining in the wallet's week, from the week's mean run cost.

    The figure is the group's remaining seven-day window divided by the mean
    per-run quota cost over the trailing seven days: two quantities of one weekly
    window, so the quotient is a count of runs. The remaining window is the
    seven-day clock the group's pace already reads, so runway and pace place the
    group in the same week rather than in two; the deadline is
    :func:`reckon.crew.pace.drain_deadline`'s, and the membership is the declared
    group's, neither restated here. The mean comes from the committed run records'
    own receipts, read as a flow -- the rise within a reset window over the runs
    that produced it, never a window's level divided by another level -- and the
    figure reports the age of both inputs rather than a single fresh one.

    The mean is an aggregate of whole percentage points and the quantisation rides
    the figure rather than being dropped, so a reader never mistakes it for an
    exact count. A figure no window priced -- fewer than two receipts sharing a
    reset boundary, or no rise between them -- is unmeasured rather than zero,
    because a zero would refuse every wave and read as a measurement rather than
    an absence, and a non-positive cost would read as an infinite count.
    """
    from reckon.budget import (
        CLOCK_SEVEN_DAY,
        OBSERVED,
        _iso,
        _parse_stamp,
    )
    from reckon.crew import pace as pace_module

    week = clocks[CLOCK_SEVEN_DAY]
    if week["state"] != OBSERVED:
        return _unmeasured_runway(
            group,
            "the group's weekly clock was not read, so no remaining window "
            "divides the mean run cost",
        )
    week_utilisation = float(week["utilisation"])
    remaining_pct = max(0.0, 1.0 - week_utilisation) * 100.0
    priced = _priced_runs(rows, members, moment=moment)
    flow = _run_cost_flow(priced)
    if flow is None:
        return _unmeasured_runway(
            group,
            "no reset window in the trailing seven days held two receipts with a "
            "rise between them, so no per-run cost is measured to divide the "
            "remaining window",
            remaining_pct=remaining_pct,
            week=week,
        )
    mean_cost, priced_runs, newest = flow
    drain_hours = pace_module.drain_deadline(
        pace_module.GroupReading(
            group=group, utilisation=week_utilisation, elapsed_hours=0.0
        ),
        pace_module.policy(config),
    )
    by_role: dict[str, dict[str, Any]] = {}
    for role in sorted({role for _backend, role, _observed, _used, _resets in priced}):
        role_flow = _run_cost_flow([row for row in priced if row[1] == role])
        if role_flow is None:
            by_role[role] = {
                "mean_run_cost_pct": None,
                "runs_remaining": None,
                "priced_runs": 0,
            }
            continue
        role_mean, role_runs, _role_newest = role_flow
        by_role[role] = {
            "mean_run_cost_pct": role_mean,
            "runs_remaining": remaining_pct / role_mean,
            "priced_runs": role_runs,
        }
    week_observed = _parse_stamp(week.get("observed_at"))
    return {
        "group": group,
        "state": OBSERVED,
        "reason": None,
        "remaining_pct": remaining_pct,
        "mean_run_cost_pct": mean_cost,
        "priced_runs": priced_runs,
        "runs_remaining": remaining_pct / mean_cost,
        "resets_at": week.get("resets_at"),
        "observed_at": None if week_observed is None else _iso(week_observed),
        "age_seconds": week.get("age_seconds"),
        "mean_observed_at": _iso(newest),
        "mean_age_seconds": (moment - newest).total_seconds(),
        "drain_deadline_hours": drain_hours,
        "by_role": by_role,
        "quantisation_pct": 1.0,
    }


def _wave_refusal(runway: Mapping[str, Any], node_count: int) -> dict[str, Any] | None:
    """Refuse a wave whose projected cost outruns the wallet's remaining week.

    The projection is the wave's own node count against the mean per-run cost,
    both in the unit the runway divides. A wave that fits inside the remaining
    window is admitted; one that does not would run past the reset, so it is
    refused before a worktree exists to reclaim. A runway nothing priced projects
    nothing, and absence of a signal never holds a wave -- the same rule the hold
    reads. The refusal names the runs remaining and the reset time, because those
    two figures are what let a coordinator size the wave and time its retry.
    """
    from reckon.budget import (
        OBSERVED,
    )
    from reckon.crew.refusals import format_refusal

    if node_count <= 0 or runway.get("state") != OBSERVED:
        return None
    mean_cost = runway.get("mean_run_cost_pct")
    remaining = runway.get("remaining_pct")
    runs = runway.get("runs_remaining")
    if mean_cost is None or remaining is None or runs is None:
        return None
    projected = node_count * float(mean_cost)
    if projected <= float(remaining):
        return None
    reset = runway.get("resets_at") or "an unstated reset"
    return {
        "refused": True,
        "node_count": node_count,
        "projected_cost_pct": projected,
        "remaining_pct": float(remaining),
        "mean_run_cost_pct": float(mean_cost),
        "runs_remaining": float(runs),
        "resets_at": runway.get("resets_at"),
        "withheld": [],
        "reason": format_refusal(
            "D02",
            f"the wave's {node_count} ready node(s) project {projected:g}% of the "
            f"weekly window against {float(remaining):g}% remaining -- "
            f"{float(runs):.2f} runs remain before the weekly reset at {reset}",
        ),
    }


def _group_bar(
    clocks: Mapping[str, Mapping[str, Any]],
    ready: Iterable[Mapping[str, Any]],
    runway: Mapping[str, Any] | None = None,
    *,
    operative: window_reading.WindowFigure | None = None,
) -> dict[str, Any]:
    """Judge a stated ready set against the group's filling window.

    The bar rises with the window that fills, so the five-hour utilisation is
    the fill it is drawn against. Each node keeps the bar's own four outcomes —
    ``send-metered``, ``send-local``, ``split``, ``hold`` — because a verdict
    naming the lane is what a coordinator routes on, and one send covering both
    lanes cannot say "route this local".

    A node whose group has no read window is undecided, with no verdict and no
    fill, rather than judged against a fabricated empty window. The one
    exception is prescribed work: the bar decides prescription *before* it
    consults the window, so a maximally prescribed node's outcome is the same at
    every fill and is therefore decidable with no window at all. Its
    ``decided_by`` says so, which keeps "we read a window" and "no window could
    have changed this" distinguishable in the record, while ``window_fill`` is
    still reported as unread because a fill never observed is not a measurement.
    """
    from reckon.budget import (
        CLOCK_FIVE_HOUR,
        CLOCK_SEVEN_DAY,
        OBSERVED,
        UNKNOWN,
    )
    from reckon.crew import bar as bar_module

    five = clocks[CLOCK_FIVE_HOUR]
    fill = five["utilisation"] if five["state"] == OBSERVED else None
    fill_state = five["state"]
    if (
        fill is None
        and operative is not None
        and clocks[CLOCK_SEVEN_DAY]["state"] != OBSERVED
    ):
        # An account reporting one operative window and neither historical clock
        # is not a window nobody read: fill the bar from that window's own
        # utilisation rather than leaving every node undecided. A five-hour or
        # seven-day clock that *was* observed keeps the fill it defines, so a
        # receipt carrying only the week still reports no five-hour fill.
        fill = float(operative.utilisation)
        fill_state = OBSERVED

    recommendations: list[dict[str, Any]] = []
    admitted: list[dict[str, Any]] = []
    split: list[str] = []
    held: list[str] = []
    undecided: list[str] = []
    for node in ready:
        name = str(node["name"])
        score = float(node["score"])
        if fill is None and score > bar_module.PRESCRIBED_MAX:
            recommendations.append(
                {
                    "name": name,
                    "score": score,
                    "state": UNKNOWN,
                    "verdict": None,
                    "window_fill": None,
                    "bar": None,
                    "margin": None,
                    "decided_by": None,
                }
            )
            undecided.append(name)
            continue
        # Any fill returns the same outcome for a prescribed score, which is
        # what makes that verdict decidable with no window read.
        judged = bar_module.recommend(0.0 if fill is None else fill, score)
        prescribed = score <= bar_module.PRESCRIBED_MAX
        recommendations.append(
            {
                "name": name,
                "score": score,
                "state": OBSERVED,
                "verdict": judged.verdict,
                "window_fill": fill,
                "bar": None if fill is None else judged.bar,
                "margin": None if fill is None else judged.margin,
                "decided_by": "prescription" if prescribed else "window",
            }
        )
        if judged.verdict in (bar_module.SEND_METERED, bar_module.SEND_LOCAL):
            admitted.append({"name": name, "verdict": judged.verdict})
        elif judged.verdict == bar_module.SPLIT:
            split.append(name)
        else:
            held.append(name)

    # The five-hour bar judges one node's fill; the runway judges the whole wave
    # against the week. A wave projected past the reset is refused here, before a
    # worktree exists to reclaim, and the node it would have admitted is moved
    # into ``held`` rather than silently dropped -- a ready node missing from the
    # admitted set is the failure a pre-flight exists to prevent.
    refusal = None if runway is None else _wave_refusal(runway, len(recommendations))
    if refusal is not None:
        refusal["withheld"] = [entry["name"] for entry in admitted]
        held.extend(entry["name"] for entry in admitted)
        admitted = []

    return {
        "window_fill": fill,
        "state": fill_state,
        "recommendations": recommendations,
        "admitted": admitted,
        "split": split,
        "held": held,
        "undecided": undecided,
        "runway": runway,
        "refusal": refusal,
    }


def _lift_resolved_config(
    config: Mapping[str, Any],
    *,
    group: str,
    members: Iterable[str],
    reading: window_reading.WindowReading,
    records: Iterable[Mapping[str, Any]],
    moment: datetime,
    session: str | None,
) -> Mapping[str, Any]:
    """Substitute a group's budget block with the one a lift puts in force.

    A lift has no single reader: it raises the pace multiple, releases all three
    reserves, and carries either a drain-by line or an uncapped release. Rather
    than teach each consumer of the budget block about lifts, the block is
    resolved once here through :func:`reckon.crew.budget_lift.effective_budget`
    and the substituted configuration is handed to every consumer, which is the
    one seam placed before ``pace.policy``. A group no lift covers keeps the
    configuration unchanged, so nothing downstream reads a substituted block
    that says the same thing.
    """
    from reckon.crew import budget_lift

    history = _lift_history(
        records, members=set(members), reading=reading, moment=moment
    )
    block = budget_lift.effective_budget(
        config, group=group, readings=history, now=moment, session=session
    )
    if block is config.get("budget"):
        return config
    return {**config, "budget": block}


def _lift_history(
    records: Iterable[Mapping[str, Any]],
    *,
    members: set[str],
    reading: window_reading.WindowReading,
    moment: datetime,
) -> list[Mapping[str, Any]]:
    """A group's window readings in observed order, for a lift's expiry read.

    A reset-anchored lift ends when a clock's utilisation falls between two
    observations, so the resolver needs the sequence and not only the newest
    figure. The sequence is the pace-bearing records the group's members own,
    read exactly as ``scripts/census/pace_replay.py`` reads a row's clocks, with the
    group's freshest reading appended so the group's position now is the last
    one the lift is judged against. The rows are then ordered by observation, so
    a fall between any two consecutive readings is visible to the resolver. An
    empty sequence is an honest absence: a lift then holds on its own record and
    clock alone, exactly as when no history was requested.
    """
    from reckon.budget import (
        _run_backend,
    )

    history: list[Mapping[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if _run_backend(record) not in members:
            continue
        entry = _record_lift_reading(record)
        if entry is not None:
            history.append(entry)
    if reading.known:
        current = _reading_lift_entry(reading)
        if current is not None:
            history.append(current)
    history.sort(key=_lift_order)
    return history


def _record_lift_reading(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """One record's own clocks as a lift reading, or ``None``.

    A pace-bearing run record carries the clocks it paced against, so the row
    pace replay reads is the row read here: an observed clock contributes its
    utilisation and reset, an unobserved one contributes nothing, and a record
    carrying no observed clock yields no reading rather than a zero.
    """
    from reckon.budget import (
        CLOCK_FIVE_HOUR,
        CLOCK_SEVEN_DAY,
        OBSERVED,
    )

    pace_row = record.get("pace")
    if not isinstance(pace_row, Mapping):
        return None
    clocks = pace_row.get("clocks")
    if not isinstance(clocks, Mapping):
        return None
    entry: dict[str, Any] = {"observed_at": pace_row.get("recorded_at")}
    found = False
    for period in (CLOCK_FIVE_HOUR, CLOCK_SEVEN_DAY):
        clock = clocks.get(period)
        if not isinstance(clock, Mapping) or clock.get("state") != OBSERVED:
            continue
        utilisation = clock.get("utilisation")
        if isinstance(utilisation, bool) or not isinstance(utilisation, (int, float)):
            continue
        entry[period] = {
            "utilisation": float(utilisation),
            "resets_at": clock.get("resets_at"),
        }
        found = True
    return entry if found else None


def _reading_lift_entry(
    reading: window_reading.WindowReading,
) -> dict[str, Any] | None:
    """The group's freshest window reading in the lift resolver's own shape."""
    from reckon.budget import (
        CLOCK_FIVE_HOUR,
        CLOCK_SEVEN_DAY,
    )

    entry: dict[str, Any] = {
        "observed_at": (
            reading.observed_at.isoformat() if reading.observed_at else None
        )
    }
    found = False
    for period in (CLOCK_FIVE_HOUR, CLOCK_SEVEN_DAY):
        figure = reading.figure(period)
        if figure is None:
            continue
        entry[period] = {
            "utilisation": float(figure.utilisation),
            "resets_at": figure.resets_at,
        }
        found = True
    return entry if found else None


def _lift_order(entry: Mapping[str, Any]) -> datetime:
    """An observation instant for ordering lift readings, undated first."""
    from reckon.budget import (
        _parse_stamp,
    )

    moment = _parse_stamp(entry.get("observed_at"))
    return moment if moment is not None else datetime.min.replace(tzinfo=UTC)


def _re_anchor(
    allowance: Mapping[str, Any],
    *,
    group: str,
    moment: datetime,
) -> dict[str, Any] | None:
    """The line a group's last ended lift leaves, or ``None``.

    When a lift stops inside the window it was anchored to, the budget it left
    unspent is spread evenly from that point to the window's reset, leaned by
    the configured pace multiple: the line stands at ``u_end`` at ``t_end`` and
    at ``u_end + multiple * (1 - u_end)`` at the reset. A group still under a
    lift, one whose last lift ended on the window's reset rather than inside it,
    and one that never carried a lift all report ``None``, so the pace keeps the
    comparison it always made.
    """
    from reckon.budget import (
        _iso,
        _parse_stamp,
    )
    from reckon.crew import budget_lift

    minutes = allowance.get("window_minutes")
    reset = _parse_stamp(allowance.get("resets_at"))
    multiple = allowance.get("pace_multiple")
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, (int, float))
        or float(minutes) <= 0
        or reset is None
        or isinstance(multiple, bool)
        or not isinstance(multiple, (int, float))
        or float(multiple) <= 0
    ):
        return None
    origin = budget_lift.re_anchor_origin(group=group)
    if origin is None:
        return None
    u_end, t_end, lift_id = origin
    window = timedelta(minutes=float(minutes))
    if not reset - window < t_end < reset:
        return None
    span = (reset - t_end).total_seconds()
    if span <= 0:
        return None
    share = min(1.0, max(0.0, (moment - t_end).total_seconds() / span))
    line = min(1.0, u_end + float(multiple) * (1.0 - u_end) * share)
    return {
        "u_end": u_end,
        "t_end": _iso(t_end),
        "allowance": line,
        "multiple": float(multiple),
        "lift_id": lift_id,
    }


def group_pace(
    config: Mapping[str, Any],
    *,
    windows: Mapping[str, Any] | None = None,
    ready: Iterable[Mapping[str, Any]] = (),
    records: Iterable[Mapping[str, Any]] = (),
    now: datetime | None = None,
    session: str | None = None,
) -> list[dict[str, Any]]:
    """Report the pace of every declared budget group, one entry each.

    One entry per declared group and never one per lane: lanes sharing a wallet
    hold one allowance between them, and a figure computed per lane would report
    each share as though it were the whole. Membership comes from the declared
    ``budget_group`` slot in resolved flight config, which is the same authority
    the position and the hold read.

    ``windows`` maps a backend name to its window reading — either an already
    read :class:`~reckon.crew.window_reading.WindowReading` or something the
    reader can open — and a group no member reading reached reports unknown
    rather than zero. ``ready`` states the nodes a wave would open with, each a
    mapping of ``name``, ``group`` and ``score``, the score being that node's
    open-endedness. A node naming a group that is not declared is refused rather
    than dropped, because a ready node silently missing from the admitted set is
    exactly the failure a pre-flight exists to prevent.

    Each group's budget block is resolved through
    :func:`reckon.crew.budget_lift.effective_budget` before its allowance is
    derived, so a lift in force raises the multiple the allowance is derived
    against. ``session`` is the dispatching session, threaded to that resolver
    so a session-scoped lift governs its own dispatch and no other. A group
    under a lift carries the lift and how it withholds inside the allowance the
    entry already reports, so a reader sees the lift without re-reading the
    document and a group under no lift reports the allowance it always has. A
    group under no lift whose last one ended inside the current window carries
    the point it ended at and the allowance that spreads from there in
    ``re_anchor``, so the hold is judged against the re-anchored line.
    """
    from reckon.budget import (
        CLOCK_FIVE_HOUR,
        CLOCK_SEVEN_DAY,
        OBSERVED,
        UNKNOWN,
        _clock,
        _freshest_reading,
        _now,
    )
    from reckon.crew import budget_group, window_reading
    from reckon.crew import pace as pace_module

    moment = _now(now)
    readings = windows if isinstance(windows, Mapping) else {}
    records = list(records)
    groups = budget_group.declared_groups(config)
    empty = window_reading.WindowReading()

    # A list per group rather than one list shared by every key: the shared form
    # gives each group the same list object, so every node would be judged
    # against every group's bar.
    nodes_by_group: dict[str, list[Mapping[str, Any]]] = {name: [] for name in groups}
    for node in ready:
        if not isinstance(node, Mapping):
            # A malformed ready set is one caller error, not two, so it is
            # refused as one kind: the command's refusal path handles ValueError.
            raise ValueError(  # noqa: TRY004
                f"a ready node must be a mapping, not {node!r}"
            )
        name = node.get("name")
        group = node.get("group")
        score = node.get("score")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"a ready node needs a name, not {name!r}")
        if group not in groups:
            declared = ", ".join(groups) or "none"
            raise ValueError(
                f"ready node {name!r} names group {group!r}, which is not a "
                f"declared budget group (declared groups: {declared})"
            )
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError(  # noqa: TRY004
                f"ready node {name!r} needs an open-endedness score, not {score!r}"
            )
        nodes_by_group[str(group)].append(node)

    report: list[dict[str, Any]] = []
    for group, members in groups.items():
        freshest = _freshest_reading(members, readings, moment=moment)
        member = None if freshest is None else freshest[0]
        reading = empty if freshest is None else freshest[1]
        clocks = {
            period: _clock(reading, period)
            for period in (CLOCK_FIVE_HOUR, CLOCK_SEVEN_DAY)
        }
        # One resolution per group, and every figure below reads the block it
        # returns: the allowance's multiple, the hold's own rule, and the
        # reserve the group's ceiling is measured against all come from here.
        effective = _lift_resolved_config(
            config,
            group=group,
            members=members,
            reading=reading,
            records=records,
            moment=moment,
            session=session,
        )
        effective_block = effective.get("budget") or {}
        runway = _group_runway(
            group, members, clocks, records, effective, moment=moment
        )
        allowance = _group_allowance(group, reading, clocks, effective, moment=moment)
        if pace_module.is_advisory_group(config, group):
            # The declaration is carried on the group's own allowance, so the
            # hold that reads it and any reader of the report see it on the one
            # payload they already consult rather than resolving it again from
            # configuration they may not hold.
            allowance = {**allowance, "advisory": True}
        lift_id = effective_block.get("lift_id")
        session_lift = None
        if lift_id:
            # ``lift_id`` is set only by the resolver, and only when a lift
            # governs. The block's ``lift`` key is not that signal: resolved
            # flight config declares the lift ceilings under ``budget.lift``, so
            # that key is present whenever a configuration states them and a
            # group under no lift would otherwise report the ceilings as a lift.
            # The lift rides inside the allowance the entry already carries, so
            # the group block's own key set does not grow a case per lift form;
            # a group under no lift keeps the allowance it always has.
            allowance = {
                **allowance,
                "lift": effective_block.get("lift"),
                "lift_hold": effective_block.get("pace_hold"),
            }
        else:
            session_lift = _other_session_lift(
                config,
                group=group,
                session=session,
                readings=_lift_history(
                    records, members=set(members), reading=reading, moment=moment
                ),
                moment=moment,
            )
            anchor = _re_anchor(allowance, group=group, moment=moment)
            if anchor is not None:
                allowance = {**allowance, "re_anchor": anchor}
        report.append(
            {
                "group": group,
                "members": list(members),
                "member": member,
                "state": OBSERVED if freshest is not None else UNKNOWN,
                "clocks": clocks,
                "allowance": allowance,
                "bar": _group_bar(
                    clocks,
                    nodes_by_group[group],
                    runway,
                    operative=_operative_window(reading),
                ),
            }
        )
        if session_lift is not None:
            report[-1]["session_lift"] = session_lift
    return report


def _other_session_lift(
    config: Mapping[str, Any],
    *,
    group: str,
    session: str | None,
    readings: Sequence[Mapping[str, Any]],
    moment: datetime,
) -> Mapping[str, Any] | None:
    """The in-force session lift on ``group`` that another session owns.

    A session lift decides who may spend past pace, not whose budget is spent,
    so a session that is not the one named still reports it: the entry names the
    lift and its session without applying it, so a lane emptying faster than
    pace is never a surprise. The lift is reported only while it is in force.
    """
    from reckon.crew import budget_lift

    prefix = budget_lift.SESSION_PREFIX
    for lift in budget_lift.list_lifts(group=group):
        scope = str(lift.get("scope") or "")
        if not scope.startswith(prefix):
            continue
        owner = scope[len(prefix) :]
        if not owner or owner == str(session or ""):
            continue
        block = budget_lift.effective_budget(
            config, group=group, readings=readings, now=moment, session=owner
        )
        if block.get("lift_id") == lift.get("id"):
            return block.get("lift")
    return None


def _pace_releases_at(
    allowance: Mapping[str, Any],
    *,
    utilisation: float,
    multiple: float,
    factor: float,
) -> str | None:
    """The instant a group's own burn comparison first admits, utilisation held.

    The hold is ``burn > multiple`` with ``burn = utilisation / elapsed_fraction``,
    and ``_scale_for_banked_reset`` has already divided the reported burn by the
    banked-reset factor, so the comparison admits once the elapsed fraction
    reaches ``utilisation / (multiple * factor)``. With no further spend the
    utilisation holds, so that instant is the window start plus that share of the
    window's own length, which is what the hold reason must name rather than the
    window's reset days away. A window whose own length or reset cannot be read
    yields no instant, which is an absence rather than a hold released now.
    """
    from reckon.budget import (
        _iso,
        _parse_stamp,
    )

    minutes = allowance.get("window_minutes")
    resets_at = allowance.get("resets_at")
    reset_moment = _parse_stamp(resets_at) if resets_at else None
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, (int, float))
        or float(minutes) <= 0
        or reset_moment is None
        or multiple <= 0
        or factor <= 0
    ):
        return None
    window = timedelta(minutes=float(minutes))
    share = min(1.0, max(0.0, utilisation / (multiple * factor)))
    return _iso(reset_moment - window + window * share)


def _re_anchor_releases_at(
    allowance: Mapping[str, Any],
    *,
    re_anchor: Mapping[str, Any],
    utilisation: float,
) -> str | None:
    """The instant the re-anchored line first admits with utilisation held.

    The line runs from ``u_end`` at ``t_end`` to ``u_end + multiple * (1 - u_end)``
    at the reset, so the utilisation crosses it at the same share of the span to
    the reset that separates the two figures, which is where the hold releases
    with no further spend. A single-point line, where ``u_end`` is already full
    or the utilisation stands at or below it, has no instant, an absence rather
    than a release now.
    """
    from reckon.budget import (
        _iso,
        _parse_stamp,
    )

    u_end = re_anchor.get("u_end")
    t_end = _parse_stamp(re_anchor.get("t_end"))
    reset = _parse_stamp(allowance.get("resets_at"))
    multiple = allowance.get("pace_multiple")
    if (
        isinstance(u_end, bool)
        or not isinstance(u_end, (int, float))
        or isinstance(multiple, bool)
        or not isinstance(multiple, (int, float))
        or t_end is None
        or reset is None
    ):
        return None
    slope = float(multiple) * (1.0 - float(u_end))
    if slope <= 0:
        return None
    share = (float(utilisation) - float(u_end)) / slope
    instant = t_end + (reset - t_end) * min(1.0, max(0.0, share))
    return _iso(min(instant, reset))


def _banked_reset_factor(allowance: Mapping[str, Any]) -> float:
    """The banked-reset factor a scaled allowance was divided by, else ``1.0``.

    Read from the figure :func:`_scale_for_banked_reset` records as it scales the
    burn, so the release instant divides by the same factor the derivation did
    rather than a second copy of the constant kept here. An allowance carrying
    no scaled factor was not scaled, so its factor is one.
    """
    factor = allowance.get("reset_factor")
    if (
        isinstance(factor, (int, float))
        and not isinstance(factor, bool)
        and float(factor) > 0
    ):
        return float(factor)
    return 1.0


def _lift_label(allowance: Mapping[str, Any]) -> str:
    """A short parenthetical naming the lift a group is under, for a reason."""
    lift = allowance.get("lift")
    lift_id = lift.get("id") if isinstance(lift, Mapping) else None
    return f"(lift {lift_id})" if lift_id else "(the lift in force)"


def pace_hold(entry: Mapping[str, Any], role: str | None) -> dict[str, Any]:
    """Whether a group's pace withholds a dispatch from a role's work.

    A group is over its pace when the window it reports stands above the
    allowance its own elapsed fraction grants: at that utilisation the account
    would exhaust the window before its reset if it kept spending at the rate
    it has. The verdict is read whole from the group entry -- the utilisation
    the provider reported, the allowance derived from the window it reports, and
    that window's reset -- so a hold names the figures it rests on rather than
    an assertion a reader has to take on faith.

    The reserve's bookend roles are never held by pace: review and verify are
    the expenditure a spent window is being protected for, so they are admitted
    A group whose pace the configuration declares advisory is also never held:
    the pace is reported, not enforced, so the work that spends that wallet is
    admitted at any utilisation the window still has. The ceiling and a credit
    lift's cap are judged elsewhere and are unchanged by the declaration.

    The reserve's bookend roles are never held by pace: review and verify are
    the expenditure a spent window is being protected for, so they are admitted
    at any utilisation and the hold applies only to the roles that spend the
    window down. A group carrying no derived allowance -- an unread window or a
    reset that cannot be placed -- holds nothing, because absence of a reading
    is not a measured overspend.

    A held verdict also names ``releases_at``: the instant the hold's own burn
    comparison first admits with the utilisation held fixed, which is almost
    always far sooner than the window reset it used to print. The hold reason
    prints that instant rather than the reset, so a reader sees a ten-hour hold
    for the ten hours it is. An admitted or bookend verdict, and one whose
    window cannot be placed, carries ``releases_at`` as ``None``.

    A group under no lift whose last one ended inside the current window is
    compared instead against the line that spreads its remaining budget from
    where the lift stopped to the reset, which :func:`_re_anchor` records in the
    entry's allowance block. The verdict then names ``u_end``, ``t_end`` and the
    re-anchored allowance it rests on, and ``releases_at`` is computed on that
    line rather than on the elapsed-fraction burn.
    """
    from reckon.crew import reserve as reserve_module
    from reckon.crew.refusals import format_refusal

    allowance = entry.get("allowance")
    allowance = allowance if isinstance(allowance, Mapping) else {}
    utilisation = allowance.get("utilisation")
    derived = allowance.get("derived")
    resets_at = allowance.get("resets_at")
    burn = allowance.get("burn_multiple")
    multiple = allowance.get("pace_multiple")
    bookend = reserve_module.is_bookend(role)
    # A re-anchor judges against the line the group's last lift left behind, and
    # does not need the elapsed-fraction burn: a window too young to project can
    # still have spent past what the lift left it. It is read before the
    # measurement gate so such a window is judged rather than admitted unmeasured.
    re_anchor = allowance.get("re_anchor")
    re_anchor = re_anchor if isinstance(re_anchor, Mapping) else None
    raw_anchor = re_anchor.get("allowance") if re_anchor is not None else None
    re_anchor_line = (
        float(raw_anchor)
        if isinstance(raw_anchor, (int, float)) and not isinstance(raw_anchor, bool)
        else None
    )
    numeric = (
        isinstance(utilisation, (int, float))
        and not isinstance(utilisation, bool)
        and isinstance(multiple, (int, float))
        and not isinstance(multiple, bool)
    )
    re_anchored = not bookend and re_anchor_line is not None and numeric
    measured = all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in (utilisation, derived, burn, multiple)
    )
    if not measured and not re_anchored:
        measured_utilisation = (
            float(utilisation)
            if isinstance(utilisation, (int, float))
            and not isinstance(utilisation, bool)
            else None
        )
        return {
            "role": str(role),
            "bookend": bookend,
            "held": False,
            "utilisation": measured_utilisation,
            "allowance": None,
            "resets_at": resets_at,
            "releases_at": None,
            "reason": str(allowance.get("reason") or "")
            or (
                f"the {entry.get('group')!r} group's allowance was not read, so "
                "no overspend can be measured and nothing is held"
            ),
        }
    from reckon.crew import budget_lift

    # A lift changes what the hold compares. The multiple form has already
    # raised ``pace_multiple`` in the allowance this entry carries, so the burn
    # comparison below judges the lifted figure with no branch of its own. The
    # other two forms keep the configured multiple and mark themselves in
    # ``lift_hold``: a drain-by lift holds only while the utilisation stands
    # above the line from grant to its target, and an uncapped lift holds nothing.
    lift_hold = allowance.get("lift_hold")
    lift_hold = lift_hold if isinstance(lift_hold, Mapping) else {}
    lift_kind = str(lift_hold.get("kind") or "")
    raw_line = lift_hold.get("line")
    drain_line = (
        float(raw_line)
        if isinstance(raw_line, (int, float)) and not isinstance(raw_line, bool)
        else None
    )
    # A group whose pace the configuration declares advisory is admitted at any
    # utilisation the window still has: the pace is a reading of how fast the
    # wallet is emptying, not a boundary its work is stopped at, and the two
    # hard stops below still bound it.
    advisory = bool(allowance.get("advisory"))
    if advisory or (
        not bookend and lift_kind in (budget_lift.UNCAPPED, budget_lift.CREDITS)
    ):
        held = False
    elif not bookend and lift_kind == budget_lift.DRAIN_BY:
        held = drain_line is not None and float(utilisation) > drain_line
    elif re_anchored:
        held = float(utilisation) > re_anchor_line
    else:
        held = (not bookend) and float(burn) > float(multiple)
    # The reported burn has already been scaled by the banked-reset factor, so
    # the release instant must divide by the same factor, read from where
    # ``_scale_for_banked_reset`` recorded it, or it would name an instant the
    # hold does not actually release at.
    factor = _banked_reset_factor(allowance)
    releases_at = None
    if advisory:
        reason = (
            f"the {entry.get('group')!r} group declares its pace advisory, so it "
            f"does not withhold a {role} dispatch: the window stands at "
            f"{float(utilisation) * 100:g}% against a {float(derived) * 100:g}% "
            "allowance and the pace is reported rather than held"
        )
    elif bookend:
        reason = (
            f"the {role} role is a bookend, so the {entry.get('group')!r} group's "
            f"pace does not withhold it: the window stands at "
            f"{float(utilisation) * 100:g}% against a {float(derived) * 100:g}% "
            "allowance"
        )
    elif lift_kind == budget_lift.UNCAPPED:
        reason = (
            f"the {entry.get('group')!r} group is under an uncapped lift "
            f"{_lift_label(allowance)}, so no pace hold applies and a {role} "
            "dispatch is admitted at any reported burn"
        )
    elif lift_kind == budget_lift.CREDITS:
        reason = (
            f"the {entry.get('group')!r} group is under a credit lift "
            f"{_lift_label(allowance)}, so no pace hold applies; its cap is "
            "judged against the account's credit balance at the window ceiling"
        )
    elif lift_kind == budget_lift.DRAIN_BY:
        if held:
            reason = format_refusal(
                "D02",
                f"the {entry.get('group')!r} group reports utilisation "
                f"{float(utilisation) * 100:g}%, above the "
                f"{drain_line * 100:g}% drain-by line {_lift_label(allowance)}, so a "
                f"{role} dispatch is held until the utilisation falls to or below "
                f"that line, with the window resetting at "
                f"{resets_at or 'an unstated time'}",
            )
        else:
            reason = (
                f"the {entry.get('group')!r} group reports utilisation "
                f"{float(utilisation) * 100:g}%, at or below the "
                f"{drain_line * 100:g}% drain-by line {_lift_label(allowance)}, so a "
                f"{role} dispatch is admitted"
            )
    elif re_anchored:
        u_end = float(re_anchor["u_end"])
        t_end = re_anchor.get("t_end")
        if held:
            releases_at = _re_anchor_releases_at(
                allowance, re_anchor=re_anchor, utilisation=float(utilisation)
            )
            reason = format_refusal(
                "D02",
                f"the {entry.get('group')!r} group reports utilisation "
                f"{float(utilisation) * 100:g}%, above the "
                f"{re_anchor_line * 100:g}% allowance a lift that ended at {t_end} "
                f"left it (from {u_end * 100:g}% then, spread to the "
                f"{float(multiple):g}x pace at the reset), so a {role} dispatch is "
                f"held until the pace releases it at "
                f"{releases_at or 'an unstated instant'}, with the window itself "
                f"resetting at {resets_at or 'an unstated time'}",
            )
        else:
            reason = (
                f"the {entry.get('group')!r} group reports utilisation "
                f"{float(utilisation) * 100:g}%, at or below the "
                f"{re_anchor_line * 100:g}% allowance a lift that ended at {t_end} "
                f"left it (from {u_end * 100:g}% then, spread to the "
                f"{float(multiple):g}x pace at the reset), so a {role} dispatch is "
                "admitted"
            )
    elif held:
        releases_at = _pace_releases_at(
            allowance,
            utilisation=float(utilisation),
            multiple=float(multiple),
            factor=factor,
        )
        reason = format_refusal(
            "D02",
            f"the {entry.get('group')!r} group reports utilisation "
            f"{float(utilisation) * 100:g}% at {float(burn):g}x burn against a "
            f"{float(multiple):g}x pace multiple, above its "
            f"{float(derived) * 100:g}% allowance for the elapsed window, so a "
            f"{role} dispatch is held until the pace releases it at "
            f"{releases_at or 'an unstated instant'}, with the window itself "
            f"resetting at {resets_at or 'an unstated time'}",
        )
    else:
        reason = (
            f"the {entry.get('group')!r} group reports utilisation "
            f"{float(utilisation) * 100:g}% at {float(burn):g}x burn, at or below "
            f"the {float(multiple):g}x pace multiple and its "
            f"{float(derived) * 100:g}% allowance, so a {role} dispatch is admitted"
        )
    verdict = {
        "role": str(role),
        "bookend": bookend,
        "held": held,
        "utilisation": float(utilisation),
        "allowance": float(derived) if measured else None,
        "burn_multiple": float(burn) if measured else None,
        "resets_at": resets_at,
        "releases_at": releases_at,
        "reason": reason,
    }
    if re_anchor is not None:
        verdict["re_anchor"] = re_anchor
    return verdict


def _merge_readings(
    recorded: Mapping[str, window_reading.WindowReading],
    surface: Mapping[str, window_reading.WindowReading],
) -> dict[str, window_reading.WindowReading]:
    """Combine two readings per backend, keeping the newest dated one.

    The recorded evidence and the account surface are two witnesses of one
    wallet, so the freshest dated reading of the two speaks for the group rather
    than either source winning by kind: a surface read taken now supersedes a
    receipt from before the window reset, and a receipt written after the
    surface was last cached is retained.
    """
    from reckon.budget import (
        _reading_stamp,
    )

    merged = dict(recorded)
    for name, reading in surface.items():
        current = merged.get(name)
        if current is None or _reading_stamp(reading) >= _reading_stamp(current):
            merged[name] = reading
    return merged


def _account_surface_readings(
    project: str,
    config: Mapping[str, Any],
    *,
    root: str | Path | None,
    moment: datetime,
) -> dict[str, window_reading.WindowReading]:
    """One window reading per opted-in group member, from its own account surface.

    The account surface describes the account now, and a recorded receipt
    describes whenever the run that wrote it ended. A group whose newest receipt
    predates a reset would otherwise pace by a window that has already rolled
    over, admitting the whole window when it should admit only the pace its
    elapsed fraction allows. Only members whose config sets ``budget_check`` are
    asked, so a lane that did not opt into a surface read is never probed and
    keeps its recorded evidence.
    """
    from reckon import ledger
    from reckon.budget import (
        _state_window_reading,
        latest_recorded,
        state_for,
    )
    from reckon.crew import budget_group

    members = sorted(
        {
            member
            for group_members in budget_group.declared_groups(config).values()
            for member in group_members
        }
    )
    configured = config.get("backends") or {}
    try:
        recorded = latest_recorded(project, root=root, config=config)
    except (OSError, TypeError, ValueError, ledger.LedgerError):
        recorded = None
    readings: dict[str, window_reading.WindowReading] = {}
    for name in members:
        settings = configured.get(name)
        settings = settings if isinstance(settings, Mapping) else {}
        if not settings.get("budget_check"):
            continue
        try:
            state = state_for(
                name,
                settings,
                recorded=None if recorded is None else recorded.for_backend(name),
                unattributed=() if recorded is None else recorded.unattributed,
                now=moment,
            )
        except (OSError, TypeError, ValueError):
            continue
        reading = _state_window_reading(state.as_dict(), moment=moment)
        if reading is not None:
            readings[name] = reading
    return readings


def pace_row(
    config: Mapping[str, Any],
    *,
    project: str,
    lane: str,
    node: str,
    score: float,
    root: str | Path | None = None,
    hold: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Record one dispatch's pace, from the wallet that paced it.

    A dispatch runs on one lane, and the wallet that paced it is that lane's
    declared budget group — the same authority the pre-flight and the position
    read. A lane declaring no wallet was paced by no group, and the row says so
    rather than reporting an allowance nothing derived: an absent wallet is not
    an empty one.

    Every figure in the row is the output of the module that owns it. The clocks
    and the allowance come from :func:`group_pace`, which delegates the
    derivation to :mod:`reckon.crew.pace` and draws the bar in
    :mod:`reckon.crew.bar`; the readings come from :func:`recorded_windows`,
    which is where every other consumer of a recorded window reads. Nothing is
    derived here and nothing is read twice, so the row cannot disagree with the
    pace the dispatch itself was judged against.

    ``score`` is the node's own open-endedness, taken from the caller for the
    same reason the bar takes it from its caller: it is a property of the node
    rather than of the wallet being read. ``hold`` is the verdict that fired
    against the lane the dispatch asked for, where one fired, and is recorded
    with the evidence behind it beside the lane that ran instead.

    The row is what a week of rows replays: each carries the week's utilisation
    and the instant it was judged at, so the allowance curve and every hold
    decision can be recomputed from the record alone, without a stream being
    opened. A governor whose own account of itself cannot be checked is a
    governor that has to be trusted instead.
    """
    from reckon.budget import (
        CLOCK_FIVE_HOUR,
        CLOCK_SEVEN_DAY,
        OBSERVED,
        UNKNOWN,
        WINDOW_SOURCE_RECORDED,
        _clock,
        _now,
        _parse_stamp,
        recorded_windows,
    )
    from reckon.crew import budget_group, window_reading
    from reckon.crew import pace as pace_module

    moment = _now(now)
    # The pace policy rides the row because the allowance is only recomputable
    # from the figures the policy was applied to: a stored derived figure beside
    # an unstated multiple cannot be checked by a reader, only believed.
    policy_block = pace_module.policy(config)
    row: dict[str, Any] = {
        "lane": lane,
        "node": node,
        "score": float(score),
        "recorded_at": moment.isoformat(),
        # The dispatching session rides the row so a session-scoped lift governs
        # this dispatch and no other: the pace is resolved for the session the
        # dispatch named, and the reserve refusal reads that same session off the
        # row rather than resolving it a second time and risking a disagreement.
        "session": session,
        # A row made under a lift carries the lift id beside the multiple in
        # force: the replay reads the multiple each row recorded, and the id says
        # whether a lift set it. None is a group under no lift, and it rides the
        # row for every group so the absence is stated rather than inferred.
        "lift_id": None,
        "policy": {
            "drain_lead_hours": float(policy_block.drain_lead_hours),
            "pace_multiple": float(policy_block.pace_multiple),
        },
        # The hold is recorded whole, with the reading and the reason that fired
        # it: a row saying a dispatch was held without saying against what is a
        # decision a reader has to take on faith.
        "hold": None if hold is None else dict(hold),
    }
    group = next(
        (
            name
            for name, members in budget_group.declared_groups(config).items()
            if lane in members
        ),
        None,
    )
    if group is None:
        empty = window_reading.WindowReading()
        row.update(
            {
                "group": None,
                "state": UNKNOWN,
                "source": None,
                "member": None,
                "clocks": {
                    period: _clock(empty, period)
                    for period in (CLOCK_FIVE_HOUR, CLOCK_SEVEN_DAY)
                },
                "allowance": None,
                "bar": None,
                "reason": "the lane declares no budget group, so no wallet paced it",
            }
        )
        return row
    readings = recorded_windows(project, config, root=root, now=moment)
    # The account surface is read beside the receipts and the freshest dated
    # reading of the two speaks for the group, so a dispatch paces by the window
    # the provider reports now rather than by a receipt from a window that has
    # already reset.
    readings = _merge_readings(
        readings,
        _account_surface_readings(project, config, root=root, moment=moment),
    )
    entry = next(
        item
        for item in group_pace(
            config,
            windows=readings,
            ready=[{"name": node, "group": group, "score": float(score)}],
            now=moment,
            session=session,
        )
        if item["group"] == group
    )
    clocks = dict(entry["clocks"])
    if (
        clocks[CLOCK_FIVE_HOUR]["state"] != OBSERVED
        and clocks[CLOCK_SEVEN_DAY]["state"] != OBSERVED
    ):
        allowance = entry["allowance"]
        if allowance.get("state") == OBSERVED:
            observed = _parse_stamp(allowance.get("observed_at"))
            clocks[CLOCK_FIVE_HOUR] = {
                "period": "primary",
                "state": OBSERVED,
                "utilisation": allowance["utilisation"],
                "age_seconds": (
                    None
                    if observed is None
                    else max(0.0, (moment - observed).total_seconds())
                ),
                "observed_at": allowance.get("observed_at"),
                "resets_at": allowance.get("resets_at"),
                "window_minutes": allowance.get("window_minutes"),
            }
    row.update(
        {
            "group": group,
            "state": entry["state"],
            "source": WINDOW_SOURCE_RECORDED,
            "member": entry["member"],
            "clocks": clocks,
            # The bar's whole judgement of this one node: the fill it was drawn
            # against, the verdict, the bar itself, the margin, and whether the
            # window or prescription decided it. The group's other ready nodes
            # are not the dispatch's business — one dispatch is one node.
            "bar": entry["bar"]["recommendations"][0],
            "allowance": entry["allowance"],
            "reason": None,
        }
    )
    # The lift a governing block names, recorded beside the multiple in force so
    # a replay reads the multiple the dispatch was actually judged at. A lift
    # raises the multiple, so the row's stored policy is the lifted one when a
    # lift governs and the configured one otherwise; the id tells the two apart.
    allowance = entry["allowance"]
    lift_block = allowance.get("lift") if isinstance(allowance, Mapping) else None
    if isinstance(lift_block, Mapping):
        row["lift_id"] = lift_block.get("id")
        multiple = allowance.get("pace_multiple")
        if isinstance(multiple, (int, float)) and not isinstance(multiple, bool):
            row["policy"]["pace_multiple"] = float(multiple)
    return row
