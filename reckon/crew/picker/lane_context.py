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
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any

from reckon import _store, budget, capabilities, ledger
from reckon._timestamps import parse_utc
from reckon.crew import lane_document
from reckon.crew.dispatch import _dispatch_lane_gate
from reckon.crew.run_time_profile import (
    _group_rows,
    _number,
    _size_class,
    local_lane_load,
    run_time_profile,
)
from reckon.crew.runs import list_live

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


def _size_of(budget_value: object) -> tuple[str | None, str | None]:
    """Classify a node's own size from its declared time budget.

    Delegates to :func:`reckon.crew.run_time_profile._size_class`, the one owner
    of the budget-versus-token thresholds, by handing it a one-field row: the
    node declares a budget and no output tokens, so the token fallback is never
    taken here and the classification matches the ledger profile's.
    """

    return _size_class({"time_budget": budget_value})


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
    size_key, size_bucket = _size_of(getattr(node, "time_budget", "") or "")
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


#: A project's run-time profile, memoized within one process. Each entry holds
#: the freshness key the profile was read at beside the profile itself. This is
#: a first layer only: a real dispatch is a fresh process, so the memo cannot
#: carry a reading between dispatches and the persisted copy below is what makes
#: the reuse survive.
_PROFILE_CACHE: dict[str, tuple[str, Mapping[str, Any]]] = {}


def _profile_cache_root() -> Path:
    """The directory the persisted run-time profiles live under.

    Resolved through the shared cache-kind owner so the directory keeps the
    ``run-time-profile`` kind's environment variable and home precedence, and
    handed to the shared pick-input cache as its root.
    """

    return _store.cache_root("run-time-profile")


def _ledger_stamp(project: str) -> list[Any] | None:
    """The ledger change stamp the profile cache is keyed on, or ``None``.

    This is the stamp handed to
    :func:`reckon.capabilities.cached_pick_input`, not a cache of its own: the
    persisted reader, writer, schema marker and key all belong to that function.
    A ledger whose index cannot be read yields no stamp, and the profile is then
    read without being cached.

    The derived run index's own identity is dropped from the stamp. Building a
    profile reads the ledger, and that read refreshes the index, so its identity
    moves as a consequence of the very computation the stamp keys; keeping it
    would make every profile a guaranteed miss. The aggregate's and the runs
    directory's identities remain, and a source change moves those.
    """

    try:
        return ledger.index_stamp(project)[:-1]
    except (ledger.LedgerError, OSError, ValueError):
        return None


def _cached_run_time_profile(project: str, *, now: datetime) -> Mapping[str, Any]:
    """Return a project's run-time profile, reusing the last read while its
    ledger is unchanged.

    :func:`_expected_wait` asks for the profile of every distinct foreign
    project with a live local worker on every pick, so reading each project's
    whole ledger makes a pick's cost grow with the number of live foreign
    projects -- the figure that pushes a pick past its five-second dispatch
    bound. A real dispatch is a fresh process, so the reading is persisted beside
    its freshness stamp through
    :func:`reckon.capabilities.cached_pick_input`, which owns the schema marker,
    the atomic write and the corrupt-entry handling: the in-process memo is
    checked first, then that shared cache, and only a miss reads the ledger. The
    stamp folds in the window's end date beside the ledger stamp, so a profile
    is recomputed at least once a day even for a project whose ledger stays
    still.
    """

    stamp = _ledger_stamp(project)
    if stamp is None:
        return run_time_profile(project, now=now)
    key = [stamp, now.date().isoformat()]
    cached = _PROFILE_CACHE.get(project)
    if cached is not None and cached[0] == key:
        return cached[1]
    profile = capabilities.cached_pick_input(
        project,
        key,
        lambda: run_time_profile(project, now=now),
        root=_profile_cache_root(),
        filename=f"{project}.json",
    )
    _PROFILE_CACHE[project] = (key, profile)
    return profile


def _expected_wait(
    *,
    project: str | None,
    records: Sequence[Mapping[str, Any]] | None,
    local_backend: str | None,
    now: datetime,
) -> float | None:
    """Median historical wall time for the shapes of local workers now live.

    Each live worker contributes its matching profile median once, from
    whichever project it belongs to, so the figure reflects the whole live
    local load rather than only the picking project's share of it. This is a
    typical total run duration, not a prediction of the next slot's release.
    Unmeasured shapes contribute no invented duration.
    """
    profiles = {}
    walls = []
    for row in list_live():
        agent = _as_mapping(row.get("agent"))
        if row.get("phase") not in {"starting", "working", "running"}:
            continue
        if not (
            agent.get("local") is True
            or (
                row.get("project") == project
                and local_backend
                and row.get("backend") == local_backend
            )
        ):
            continue
        owner = row.get("project")
        if not owner:
            continue
        # Every live local worker contributes its shape's median once, whatever
        # project it belongs to: the figure is the typical wall time for the
        # local runs now live, so a worker from another project must be
        # profiled rather than dropped. Dropping it would let the estimate read
        # null (or low) while the lane is genuinely busy with that project's
        # runs -- a wrong answer about load, not merely a slow one. The pick's
        # own records are reused rather than re-read; each other owner is read
        # from its own ledger only when its ledger has moved since the last
        # pick, so a repeated pick does not re-decode every foreign project's
        # history, and only owners with live local workers are read at all.
        if owner not in profiles:
            if owner == project and records is not None:
                recent = [
                    r
                    for r in records
                    if (
                        stamp := parse_utc(
                            str(r.get("completed_at") or r.get("dispatched_at") or "")
                        )
                    )
                    is not None
                    and now - timedelta(days=14) <= stamp <= now
                ]
                profiles[owner] = {"groups": _group_rows(recent)}
            else:
                profiles[owner] = _cached_run_time_profile(owner, now=now)
        shape = _as_mapping(row.get("node"))
        node = SimpleNamespace(
            role=shape.get("role") or row.get("role"),
            spec_level=shape.get("spec_level") or row.get("spec_level"),
        )
        group = _group(profiles[owner], row.get("backend"), node, agent.get("effort"))
        wall = _number((group or {}).get("wall_seconds_median"))
        if wall is not None:
            walls.append(wall)
    return median(walls) if walls else None


def local_lane(
    *,
    config: Mapping[str, Any] | None = None,
    candidates: Sequence[Any] | None = None,
    project: str | None = None,
    records: Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return admission, typical wait and live load; unknown figures stay null."""
    moment = now if now is not None else datetime.now(UTC)
    load = local_lane_load()
    config = config or {}
    local_backend = config.get("local_backend")
    backend = config.get("backends", {}).get(local_backend, {})
    gate = _dispatch_lane_gate(backend)
    # The lane document local_lane_load() just read is reused here rather than
    # read a second time; it is the same published file.
    document = load.get("document")
    reading = lane_document.read_lane_document(document, now=moment)
    published_gate = _as_mapping(_as_mapping(document).get("router_generation_gate"))
    slots = _number(load.get("worker_slots"))
    if slots is None:
        slots = _number(load.get("headroom"))
    if (
        gate["state"] == "paused"
        or published_gate.get("paused") is True
        or reading["admission_verdict"] == "paused"
    ):
        admission = "paused"
    elif (
        gate["state"] == "unreadable"
        or reading["stale"]
        or (
            candidates is not None
            and local_backend
            and not any(c.local for c in candidates)
        )
    ):
        admission = "unavailable"
    elif reading["admission_verdict"] in {"full", "congested"} or (
        slots is not None and slots <= 0
    ):
        admission = "full"
    elif slots is not None and slots > 0:
        admission = "admitting"
    else:
        admission = "unavailable"
    return {
        "admission": admission,
        "expected_wait_s": _expected_wait(
            project=project,
            records=records,
            local_backend=local_backend,
            now=moment,
        ),
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
        size = _size_of(getattr(node, "time_budget", ""))
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
        profile = _cached_run_time_profile(project, now=moment)
    return {
        "return_times": return_times(
            profile,
            node,
            candidates,
            budget_snapshot=budget_snapshot,
            config=config,
            now=moment,
        ),
        "local_lane": local_lane(
            config=config,
            candidates=candidates,
            project=project,
            records=records,
            now=moment,
        ),
        "read_at": moment.isoformat(),
    }
