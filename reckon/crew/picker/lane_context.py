"""Per-candidate return-time and local-lane load for the picker state.

A router weighing where to send a node needs to know how soon a job of this
size would return on each offered backend, and how loaded the local lane is
right now. This module renders both from the figures the fleet already holds:
:func:`reckon.crew.run_time_profile.run_time_profile` for the recorded wall
times and :func:`reckon.crew.run_time_profile.local_lane_load` for the local
lane.

* :func:`return_times` reports, per offered backend, the median and
  90th-percentile wall time for runs of this node's role and specification
  level, the number of runs behind them, the node's size bucket and the key it
  was read from, and the budget reading behind the candidate -- its source, its
  age, and whether a ledger-only reading has passed its shelf life.
* :func:`local_lane` reports the local serving lane's live load.

Both blocks carry ``None`` for every figure no reading supports. A group with
no runs has no median, and a lane document that was never published is not a
lane with zero of anything; reading an undefined figure as a measured zero
would let the router weigh a lane it never heard from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from reckon import budget
from reckon._timestamps import parse_utc
from reckon.crew.run_time_profile import (
    BUDGET_BUCKETS,
    TOKEN_BUCKETS,
    _budget_minutes,
    _group_rows,
    _size_class,
    local_lane_load,
    run_time_profile,
)

#: Sources a budget reading may carry that came from the ledger rather than a
#: live account surface read, and so are subject to the shelf life.
_LEDGER_SOURCES = frozenset({"ledger", "unattributed-ledger"})

_NULL_RETURN_TIME: dict[str, Any] = {
    "p50_s": None,
    "p90_s": None,
    "runs": None,
    "size_key": None,
    "size_bucket": None,
    "budget_source": None,
    "budget_age_s": None,
    "stale": False,
}


def _number(value: object) -> float | None:
    """Return ``value`` as a float when it is a real number, else ``None``.

    ``bool`` is rejected so a JSON ``true`` never reads as a one.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _bucket(
    minutes: float | None, tokens: float | None
) -> tuple[str | None, str | None]:
    """Classify a node's own size, naming the key the bucket was read from.

    The declared time budget is preferred, matching the ledger profile, and
    output tokens are the fallback where no budget was declared. A node
    carrying neither resolves to ``(None, None)`` and names no size.
    """

    if minutes is not None:
        if minutes <= 30:
            return "time_budget", BUDGET_BUCKETS[0]
        if minutes <= 60:
            return "time_budget", BUDGET_BUCKETS[1]
        return "time_budget", BUDGET_BUCKETS[2]
    if tokens is not None:
        if tokens < 20000:
            return "output_tokens", TOKEN_BUCKETS[0]
        if tokens <= 100000:
            return "output_tokens", TOKEN_BUCKETS[1]
        return "output_tokens", TOKEN_BUCKETS[2]
    return None, None


def _group(
    profile: Mapping[str, Any], backend: str, node: Any, effort: Any
) -> dict[str, Any] | None:
    """Return the profile group matching one backend and this node's shape.

    The group table is keyed by backend, effort, role and specification level.
    Runs matching the node's role and specification level are selected first;
    where several efforts recorded such runs the candidate's own effort picks
    between them, and otherwise the largest cohort does.
    """

    role = getattr(node, "role", None)
    spec_level = getattr(node, "spec_level", None)
    matching = [
        group
        for group in profile.get("groups", [])
        if group.get("backend") == backend
        and group.get("role") == role
        and group.get("spec_level") == spec_level
    ]
    if not matching:
        return None
    if effort is not None:
        exact = [group for group in matching if group.get("effort") == effort]
        if exact:
            return exact[0]
    return max(matching, key=lambda group: group.get("runs") or 0)


def _budget_readings(
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    """Index a composed budget snapshot by backend.

    The snapshot is a ``budget.preflight`` report; each entry in its
    ``backends`` list carries the backend name and the state block that names
    the reading's source and observation stamp.
    """

    readings: dict[str, Mapping[str, Any]] = {}
    if not isinstance(snapshot, Mapping):
        return readings
    for entry in snapshot.get("backends", []):
        if not isinstance(entry, Mapping) or not entry.get("backend"):
            continue
        state = entry.get("state")
        readings[str(entry["backend"])] = _as_mapping(state)
    return readings


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _budget_block(
    reading: Mapping[str, Any], *, moment: datetime, shelf_life_minutes: float
) -> dict[str, Any]:
    """Name a reading's source and age and mark a stale ledger reading.

    An account-surface reading describes now and is never stale by age; a
    ledger-only reading carries the age of the run that recorded it, and past
    its shelf life it no longer describes the present and is marked stale.
    """

    raw_source = reading.get("source")
    if raw_source == "account-surface":
        source = "account-surface"
    elif raw_source in _LEDGER_SOURCES:
        source = "ledger"
    else:
        source = None
    observed = parse_utc(str(reading.get("observed_at") or "") or "")
    age = None if observed is None else max(0.0, (moment - observed).total_seconds())
    stale = bool(
        source == "ledger" and age is not None and age > shelf_life_minutes * 60.0
    )
    return {"budget_source": source, "budget_age_s": age, "stale": stale}


def return_times(
    profile: Mapping[str, Any],
    node: Any,
    candidates: Sequence[Any],
    *,
    budget_snapshot: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Build one return-time block per offered candidate backend."""

    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    shelf_life = float(
        budget.policy(config).get(
            "evidence_shelf_life_minutes", budget.DEFAULT_SHELF_LIFE_MINUTES
        )
    )
    readings = _budget_readings(budget_snapshot)
    size_key, size_bucket = _bucket(
        _budget_minutes(getattr(node, "time_budget", "") or ""), None
    )
    blocks: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        backend = getattr(candidate, "backend", None)
        if backend is None:
            continue
        block = dict(_NULL_RETURN_TIME)
        block["size_key"] = size_key
        block["size_bucket"] = size_bucket
        group = _group(profile, backend, node, getattr(candidate, "effort", None))
        if group is not None:
            block["p50_s"] = group.get("wall_seconds_median")
            block["p90_s"] = group.get("wall_seconds_p90")
            block["runs"] = group.get("runs")
        reading = readings.get(str(backend))
        if reading is not None:
            block.update(
                _budget_block(reading, moment=moment, shelf_life_minutes=shelf_life)
            )
        blocks[str(backend)] = block
    return blocks


def local_lane() -> dict[str, Any]:
    """Return the local lane's live load with nulls for unpublished fields."""

    load = local_lane_load()
    return {
        "running": load.get("running"),
        "waiting": load.get("waiting"),
        "headroom": load.get("headroom"),
        "worker_slots": load.get("worker_slots"),
        "tokens_per_second": load.get("mean_tokens_per_second"),
        "read_at": load.get("read_at"),
    }


def build(
    *,
    node: Any,
    candidates: Sequence[Any],
    project: str | None = None,
    records: Sequence[Mapping[str, Any]] | None = None,
    budget_snapshot: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    lane_document: str | None = None,
) -> dict[str, Any]:
    """Compose the picker's return-time and local-lane blocks.

    ``project`` names the ledger the run times come from; without it the
    return-time figures are null rather than invented. ``lane_document`` is
    unrouted -- the local lane is read from whichever document the serving
    lane publishes -- so it takes no argument here.
    """

    moment = now if now is not None else datetime.now(UTC)
    profile: Mapping[str, Any] = {}
    if records is not None:
        size = _bucket(_budget_minutes(getattr(node, "time_budget", "")), None)
        selected = []
        for row in records:
            stamp = parse_utc(
                str(row.get("completed_at") or row.get("dispatched_at") or "")
            )
            if stamp is None or not moment - timedelta(days=14) <= stamp <= moment:
                continue
            if size[0] is not None and _size_class(row) != size:
                continue
            selected.append(row)
        profile = {"groups": _group_rows(selected)}
    elif project:
        profile = run_time_profile(project, now=moment)
    return {
        "return_times": return_times(
            profile,
            node,
            candidates,
            budget_snapshot=budget_snapshot,
            config=config,
            now=moment,
        ),
        "local_lane": local_lane(),
        "read_at": moment.isoformat(),
    }
