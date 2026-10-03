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

import contextlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any

from reckon import budget, ledger
from reckon._timestamps import parse_utc
from reckon.crew import lane_document
from reckon.crew.dispatch import _dispatch_lane_gate
from reckon.crew.paid_lanes import local_lane_path
from reckon.crew.run_time_profile import (
    BUDGET_BUCKETS,
    TOKEN_BUCKETS,
    _budget_minutes,
    _group_rows,
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


#: A project's run-time profile, memoized within one process. Each entry holds
#: the freshness key the profile was read at beside the profile itself. This is
#: a first layer only: a real dispatch is a fresh process, so the memo cannot
#: carry a reading between dispatches and the persisted copy below is what makes
#: the reuse survive.
_PROFILE_CACHE: dict[str, tuple[str, Mapping[str, Any]]] = {}

#: Persisted profile files hold this schema marker, so a file written by another
#: version is treated as a cache miss rather than read as a profile.
_PROFILE_FILE_SCHEMA = 1


def _stat_stamp(path: Path) -> list[int] | None:
    """Return a path's modification time and size, or ``None`` when absent.

    A list rather than a tuple, so a stamp folded into a persisted key compares
    equal to the same stamp read back from JSON, where a tuple would arrive as a
    list and compare unequal.
    """

    try:
        info = path.stat()
    except OSError:
        return None
    return [info.st_mtime_ns, info.st_size]


def _ledger_stamp(project: str) -> list[Any] | None:
    """A cheap freshness key for one project's ledger.

    Two ``stat`` calls cover every way the ledger changes from outside: a
    promoted run lands as a new file in the runs directory beside the aggregate,
    moving that directory's modification time, and an edit to an existing row
    rewrites the aggregate, moving the aggregate file's. The file's size is
    folded in beside its modification time so a change within the same clock
    tick as the previous read is still visible where the filesystem's resolution
    is coarse. Decoding the whole ledger takes 123-2948 ms across the five
    projects the local lane shares, measured on this GPFS, while these two
    stats take about 0.04 ms -- four orders of magnitude cheaper than the read
    the stamp guards.

    A project with no ledger has a stable stamp, which is correct: it has no
    runs to profile until the first is written, and writing one moves the stamp.
    A path that cannot be resolved returns ``None``, which forfeits the cache
    rather than risk a stale hit.
    """

    try:
        ledger_file = ledger.ledger_path(project)
    except (ledger.LedgerError, OSError, ValueError):
        return None
    return [_stat_stamp(ledger_file), _stat_stamp(ledger_file.parent / "runs")]


def _profile_cache_root() -> Path:
    """The directory the persisted run-time profiles live under.

    Outside every repository, so a cache write never dirties a checkout. The
    resolution order is fixed so every caller on a host lands on one directory:

    1. ``RECKON_RUN_TIME_PROFILE_CACHE``, when a caller names one;
    2. ``RECKON_HOME``, as ``<RECKON_HOME>/cache/run-time-profile`` -- a home
       that isolated the configuration has isolated the cache with it, which is
       how the test suite keeps these writes inside its temporary tree;
    3. ``XDG_CACHE_HOME``, as ``<XDG_CACHE_HOME>/reckon/run-time-profile``;
    4. ``~/.cache/reckon/run-time-profile``.

    ``RECKON_HOME`` outranks ``XDG_CACHE_HOME`` deliberately: the home is the
    isolation hook a test or a sandbox sets, while a host commonly has
    ``XDG_CACHE_HOME`` pointed at the real user cache, so the reverse order would
    let an isolated run write into the live cache.
    """

    configured = os.environ.get("RECKON_RUN_TIME_PROFILE_CACHE")
    if configured:
        return Path(configured).expanduser()
    reckon_home = os.environ.get("RECKON_HOME")
    if reckon_home:
        return Path(reckon_home) / "cache" / "run-time-profile"
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        return Path(cache_home) / "reckon" / "run-time-profile"
    return Path.home() / ".cache" / "reckon" / "run-time-profile"


def _profile_cache_path(project: str) -> Path:
    """One persisted profile per project, named by the project's own id."""

    if not ledger._SAFE_ID.fullmatch(str(project)):
        raise ValueError(f"project {project!r} is not a usable cache filename")
    return _profile_cache_root() / f"{project}.json"


def _profile_key(project: str, now: datetime) -> str | None:
    """The freshness key a persisted profile is checked against, or ``None``.

    The key folds the ledger stamp and the window's end date together and
    serialises them, so the same stamp read back from JSON compares equal to the
    one computed here. A ledger whose path cannot be resolved yields no key, and
    the profile is then never cached.
    """

    stamp = _ledger_stamp(project)
    if stamp is None:
        return None
    return json.dumps([stamp, now.date().isoformat()], sort_keys=True)


def _read_persisted_profile(project: str, key: str) -> Mapping[str, Any] | None:
    """Return a project's persisted profile when it matches ``key``.

    A missing, unreadable, corrupt or foreign-schema file is a cache miss, never
    an error: the profile can always be read again from the ledger, so a damaged
    cache must not take a pick down with it.
    """

    try:
        payload = json.loads(_profile_cache_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping) or (
        payload.get("schema") != _PROFILE_FILE_SCHEMA
    ):
        return None
    if payload.get("key") != key:
        return None
    profile = payload.get("profile")
    return profile if isinstance(profile, Mapping) else None


def _write_persisted_profile(
    project: str, key: str, profile: Mapping[str, Any]
) -> None:
    """Persist one project's profile summary for later processes to reuse.

    The write is atomic -- a sibling temporary replaced over the target -- so a
    reader never sees a half-written file, and a failure to write is swallowed:
    the cache is an optimisation and the next process reads the ledger again.
    Only the profile summary is written, never the rows it was derived from.
    """

    temporary: Path | None = None
    try:
        path = _profile_cache_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(
                {"schema": _PROFILE_FILE_SCHEMA, "key": key, "profile": profile}
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except (OSError, ValueError):
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _cached_run_time_profile(project: str, *, now: datetime) -> Mapping[str, Any]:
    """Return a project's run-time profile, reusing the last read while its
    ledger is unchanged.

    :func:`_expected_wait` asks for the profile of every distinct foreign
    project with a live local worker on every pick, so reading each project's
    whole ledger makes a pick's cost grow with the number of live foreign
    projects -- the figure that pushes a pick past its five-second dispatch
    bound. A real dispatch is a fresh process, so the reading is persisted
    beside its freshness key: the in-process memo is checked first, then the
    file on disk, and only a miss reads the ledger. The profile is a function of
    the ledger's contents and the trailing window alone, and the key folds in
    the window's end date beside the ledger stamp, so a profile is recomputed at
    least once a day even for a project whose ledger stays still.
    """

    key = _profile_key(project, now)
    if key is None:
        return run_time_profile(project, now=now)
    cached = _PROFILE_CACHE.get(project)
    if cached is not None and cached[0] == key:
        return cached[1]
    persisted = _read_persisted_profile(project, key)
    if persisted is not None:
        _PROFILE_CACHE[project] = (key, persisted)
        return persisted
    profile = run_time_profile(project, now=now)
    _PROFILE_CACHE[project] = (key, profile)
    _write_persisted_profile(project, key, profile)
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
    try:
        document = json.loads(local_lane_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = None
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
