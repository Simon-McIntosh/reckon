"""Budget-aware dispatch — hold a wave rather than burn it into a spent quota.

An orchestrator running unattended waves against metered backends will
eventually launch into an exhausted quota, and that failure is expensive in a
specific way: the worktrees already exist, the nodes are already fenced, and the
work is already part done, so the loss is not one rejected call but a whole
wave's setup plus half-finished commits somebody has to judge. Holding *before*
a wave costs nothing by comparison, which is the whole argument for this module.

Three rules shape it, and each one exists because its opposite fails.

**Unknown never blocks; only recorded exhaustion holds.** The backends disagree
about what they publish, so a block showing no headroom is indistinguishable, on
any single field, from one showing plenty. Reading silence as exhaustion would
make the system refuse to work on whichever backend happens to publish least —
an invisible failure that stalls everything, where the reactive failure it was
trying to avoid is cheap and announces itself.

**A newer silence never overwrites an older measurement.** An observation
carrying no headroom carries no information, so the latest *known* reading is the
state, and it decays only through its own reset time. Taking "most recent" at
face value would let one silent run erase a real exhaustion and open the wave
this module exists to hold.

**An undated refusal is probed, not timed.** A refusal naming a reset remains
stronger evidence until that time. One naming no reset cannot say when the lane
will serve again, so an opted-in backend receives one minimal serving request.
Its bounded per-backend cache turns a wave of callers into one request. Where a
host cannot make that request, the declared shelf life remains the fallback.

A hold is never destructive and never silent. It creates no worktree, fails no
node and cancels nothing; the nodes stay ready. It reports which backend, at what
utilisation, and when that resets — because a hold that looks like silence is
indistinguishable from a crashed orchestrator, and because the reset time is what
lets the wave resume without a human.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from reckon import _backends, crew, ledger

# What a pre-flight is deciding about. The two differ only in whether the resume
# reserve applies: withholding headroom from a fresh dispatch is the point of the
# reserve, and withholding it from the resumption the reserve exists to protect
# would defeat it.
PURPOSES = ("dispatch", "resume")

# Used when no config layer supplied a policy value. Deliberately permissive
# rather than a mirror of the shipped defaults: the shipped layer is where real
# defaults live so that resolution can report which layer set each key, and a
# second copy here would be indistinguishable from a value nobody ever set.
UNSET_CEILING_PCT = 100.0
UNSET_RESERVE_PCT = 0.0

# How long a recorded exhaustion that names no reset time keeps describing now.
# This one is a real default rather than a permissive placeholder, because the
# permissive reading is the defect: a judgement carrying no reset decays through
# nothing, so an hour-old refusal and a day-old one hold a lane identically and
# for as long as the record survives. The hold cannot clear itself either — only
# a served run writes a budget record, and the hold refuses every run — so the
# lane is unreachable by the one thing that would update it. Nor can a reader
# override it: a refusal records full utilisation and the ceiling is capped at
# the same figure, so the comparison refuses under every permitted setting.
#
# An hour balances the two costs this module already weighs against each other.
# It is longer than a wave takes to rediscover a genuine refusal — which is
# cheap, announces itself, and writes a fresh record as it does so — and it is a
# fraction of the shortest metered window measured, so a lane that really is
# spent is re-recorded by the very dispatch that tests it. Set the key to zero
# or less to disable ageing and keep the indefinite hold.
DEFAULT_SHELF_LIFE_MINUTES = 60.0
DEFAULT_AVAILABILITY_PROBE_CACHE_SECONDS = 60.0


@dataclass
class BudgetState:
    """What is known about one backend's remaining headroom, and how.

    ``source`` and ``observed_at`` are part of the answer rather than
    bookkeeping: a caller deciding whether to hold a wave needs to know whether
    the figure came from a run that finished minutes ago or from a record that
    has since reset.
    """

    backend: str
    headroom: str = "unknown"
    utilisation_pct: float | None = None
    rate_limit_type: str | None = None
    rate_limit_period_minutes: float | None = None
    resets_at: str | None = None
    seconds_until_reset: int | None = None
    threshold_status: str | None = None
    observed_at: str | None = None
    source: str = "none"
    expired: bool = False
    detail: str = ""
    availability: str | None = None
    availability_observed_at: str | None = None
    availability_cached: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the state as sorted JSON-ready data."""
        return {
            "backend": self.backend,
            "availability": self.availability,
            "availability_cached": self.availability_cached,
            "availability_observed_at": self.availability_observed_at,
            "detail": self.detail,
            "expired": self.expired,
            "headroom": self.headroom,
            "observed_at": self.observed_at,
            "rate_limit_period_minutes": self.rate_limit_period_minutes,
            "rate_limit_type": self.rate_limit_type,
            "resets_at": self.resets_at,
            "seconds_until_reset": self.seconds_until_reset,
            "source": self.source,
            "threshold_status": self.threshold_status,
            "utilisation_pct": self.utilisation_pct,
        }


# ── Policy ──────────────────────────────────────────────────────────────────


def policy(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read the thresholds that decide a hold out of flight config.

    The shelf life is one of them, and it governs only a judgement that states
    no reset time. One that states a reset already names its own decay and is
    left to it, so the two mechanisms never both apply to the same record.
    """
    block = (config or {}).get("budget") or {}
    ceiling = block.get("utilisation_ceiling_pct")
    reserve = block.get("resume_reserve_pct")
    statuses = block.get("exhausted_statuses") or ()
    shelf_life = block.get("evidence_shelf_life_minutes")
    resolved = {
        "utilisation_ceiling_pct": (
            UNSET_CEILING_PCT if ceiling is None else float(ceiling)
        ),
        "resume_reserve_pct": UNSET_RESERVE_PCT if reserve is None else float(reserve),
        "exhausted_statuses": [str(status) for status in statuses],
        "evidence_shelf_life_minutes": (
            DEFAULT_SHELF_LIFE_MINUTES if shelf_life is None else float(shelf_life)
        ),
    }
    resolved["availability_probe_cache_seconds"] = availability_probe_cache_seconds(
        resolved
    )
    return resolved


def availability_probe_cache_seconds(policy_block: Mapping[str, Any]) -> float:
    """Bound probe reuse by the configured shelf life and the short default."""
    shelf_seconds = max(
        0.0,
        float(
            policy_block.get("evidence_shelf_life_minutes", DEFAULT_SHELF_LIFE_MINUTES)
        )
        * 60.0,
    )
    return min(DEFAULT_AVAILABILITY_PROBE_CACHE_SECONDS, shelf_seconds)


def effective_ceiling(policy_block: Mapping[str, Any], purpose: str) -> float:
    """Return the utilisation a dispatch of this purpose will not cross.

    A fresh dispatch stops at the ceiling less the reserve; answering a worker
    that stopped and asked for help may spend the reserve, because that is the
    exact expenditure the reserve was withheld for. Spending it on a new node
    instead strands the wave in its worst state — work in flight and nothing left
    to unblock it with.
    """
    ceiling = float(policy_block.get("utilisation_ceiling_pct", UNSET_CEILING_PCT))
    if purpose == "resume":
        return ceiling
    reserve = float(policy_block.get("resume_reserve_pct", UNSET_RESERVE_PCT))
    return max(0.0, ceiling - reserve)


# ── Reading what was already recorded ───────────────────────────────────────


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(tz=timezone.utc)


def _parse_stamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 stamp, returning None for anything unreadable."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Reading:
    """One recorded budget block with the backend and moment it belongs to."""

    backend: str | None
    budget: dict[str, Any]
    observed_at: str
    when: datetime
    source: str
    record_id: str = ""
    attribution: str = ""


class _RecordedReadings(dict[str, _Reading]):
    """Best reading per backend plus signals whose owner is still unknown."""

    def __init__(
        self,
        best: Mapping[str, _Reading],
        *,
        unattributed: Iterable[_Reading] = (),
    ) -> None:
        super().__init__(best)
        self.unattributed = tuple(unattributed)


def _stream_evidence_backend(
    budget_block: Mapping[str, Any], config: Mapping[str, Any] | None
) -> str | None:
    """Match a normalised stream reading to its configured producer.

    A backend's stream interpreter writes both the headroom posture and its
    explanation into the durable budget block. Asking each configured
    interpreter for that same empty-reading shape recovers the producer without
    consulting orchestration-owned paths. Multiple matches stay unattributed.
    """
    evidence = (
        budget_block.get("headroom"),
        str(budget_block.get("detail") or ""),
    )
    matches: set[str] = set()
    configured = (config or {}).get("backends") or {}
    for name, settings in configured.items():
        if not isinstance(settings, Mapping) or settings.get("launch") != "cli":
            continue
        try:
            interpreter = _backends.dialect_for(settings)
        except _backends.BackendError:
            continue
        normalise = getattr(interpreter, "_budget", None)
        if not callable(normalise):
            continue
        try:
            template = normalise({"utilization": 0.0})
        except (TypeError, ValueError):
            continue
        signature = (template.get("headroom"), str(template.get("detail") or ""))
        if signature == evidence:
            matches.add(str(name))
    return next(iter(matches)) if len(matches) == 1 else None


def _record_backend(
    record: Mapping[str, Any],
    budget_block: Mapping[str, Any],
    *,
    members: Mapping[str, str],
    config: Mapping[str, Any] | None,
) -> tuple[str | None, str]:
    """Resolve a durable record's backend without inventing an attribution."""
    agent = record.get("agent")
    candidates = (
        (record.get("backend"), "record"),
        (
            agent.get("backend") if isinstance(agent, Mapping) else None,
            "agent",
        ),
        (budget_block.get("backend"), "budget"),
        (members.get(str(record.get("member") or "")), "member"),
    )
    for candidate, source in candidates:
        if candidate:
            return str(candidate), source

    # A silent block carries no measurement, so evidence recovery is neither
    # needed nor useful. Restrict the fallback to known signals that would
    # otherwise disappear from the budget view.
    if _is_known(budget_block):
        recovered = _stream_evidence_backend(budget_block, config)
        if recovered:
            return recovered, "budget-evidence"
    return None, "unattributed"


def _readings(
    project: str,
    *,
    root: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> list[_Reading]:
    """Collect every recorded budget block for a project, from both homes.

    Both homes are read because a run's record moves between them: while it is in
    flight its budget block lives in a pointer under the crew home, and on
    promotion it lands in the repository's committed ledger. Reading only one
    would lose the freshest signal or all the history.
    """
    found: list[_Reading] = []
    for pointer in crew.list_live():
        if str(pointer.get("project") or "") != project:
            continue
        fresh_stamp = pointer.get("observed_at") or pointer.get("created_at")
        when = _parse_stamp(fresh_stamp)
        budget = pointer.get("budget")
        if when is None or not isinstance(budget, Mapping):
            continue
        # `when` picks the freshest reading and may legitimately track the
        # mutable observed_at — re-reading a live run is exactly what makes
        # its data the best available. The age an ageing rule measures is a
        # different question: the pointer's observed_at is rewritten by
        # every `observe()`, so anchoring age to it lets a coordinator
        # investigating a hold renew that hold by looking. created_at is
        # written once at dispatch and never touched again, and it precedes
        # every event the run's stream can carry — including whichever one
        # produced this budget block — so it is a lower bound on the
        # refusal's own time that survives any number of re-reads.
        refusal_stamp = pointer.get("created_at") or fresh_stamp
        found.append(
            _Reading(
                backend=str(pointer.get("backend") or ""),
                budget=dict(budget),
                observed_at=str(refusal_stamp),
                when=when,
                source="live-run",
                record_id=str(pointer.get("run_id") or ""),
                attribution="record",
            )
        )
    try:
        data, _version = ledger.load(project, root)
        records = data["runs"]
        members = {
            str(item.get("id") or ""): str(item.get("harness") or "")
            for item in data.get("members", ())
            if item.get("id") and item.get("harness")
        }
    except ledger.LedgerError:
        records = []
        members = {}
    for record in records:
        stamp = record.get("completed_at")
        when = _parse_stamp(stamp)
        budget = record.get("budget")
        if when is None or not isinstance(budget, Mapping):
            continue
        backend, attribution = _record_backend(
            record,
            budget,
            members=members,
            config=config,
        )
        found.append(
            _Reading(
                backend=backend,
                budget=dict(budget),
                observed_at=str(stamp),
                when=when,
                source="ledger",
                record_id=str(record.get("run_id") or ""),
                attribution=attribution,
            )
        )
    return found


def latest_recorded(
    project: str,
    *,
    root: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> _RecordedReadings:
    """Return the best recorded reading per backend, preferring a known one.

    A known measurement outranks any silence, however recent, because silence
    carries no information: letting a later unknown win would erase a recorded
    exhaustion and open exactly the wave this module holds. Between two
    readings of the same kind, the newer wins. Known signals that cannot be
    matched remain available through ``unattributed`` on the result.
    """
    best: dict[str, _Reading] = {}
    unattributed: list[_Reading] = []
    for reading in _readings(project, root=root, config=config):
        if reading.backend is None:
            if _is_known(reading.budget):
                unattributed.append(reading)
            continue
        current = best.get(reading.backend)
        if current is None:
            best[reading.backend] = reading
            continue
        known = _is_known(reading.budget)
        current_known = _is_known(current.budget)
        if known != current_known:
            if known:
                best[reading.backend] = reading
            continue
        if reading.when > current.when:
            best[reading.backend] = reading
    return _RecordedReadings(best, unattributed=unattributed)


# ── State ───────────────────────────────────────────────────────────────────


def state_for(
    backend_name: str,
    backend: Mapping[str, Any] | None = None,
    *,
    recorded: _Reading | None = None,
    unattributed: Iterable[_Reading] = (),
    now: datetime | None = None,
    probe_runner: Callable[[Any], Mapping[str, Any] | None] | None = None,
) -> BudgetState:
    """Resolve one backend's budget state from its records and its own surface.

    The recorded reading is the base. A backend whose config sets
    ``budget_check`` also has its account surface read, and a *known* answer from
    there wins because it describes now rather than whenever the last run ended.
    An unreadable probe changes nothing but the reported detail — an instrument
    that fails must not become a hold.
    """
    moment = _now(now)
    state = BudgetState(backend=backend_name)
    if recorded is not None:
        state = _from_block(
            backend_name,
            recorded.budget,
            observed_at=recorded.observed_at,
            source=recorded.source,
            now=moment,
        )
    elif unmatched := sorted(unattributed, key=lambda item: item.when):
        latest = unmatched[-1]
        count = len(unmatched)
        noun = "signal" if count == 1 else "signals"
        identity = f"; latest record {latest.record_id}" if latest.record_id else ""
        state.source = "unattributed-ledger"
        state.observed_at = latest.observed_at
        state.detail = (
            f"{count} known headroom {noun} were recorded but could not be "
            f"attributed to a backend{identity}"
        )
    if (backend or {}).get("budget_check"):
        block = _backends.probe_budget(
            backend_name=backend_name,
            backend=backend or {},
            runner=probe_runner,
        )
        if _is_known(block):
            return _from_block(
                backend_name,
                block,
                observed_at=_iso(moment),
                source="account-surface",
                now=moment,
            )
        detail = str(block.get("detail") or "")
        state.detail = (
            f"{state.detail}; {detail}".strip("; ") if detail else state.detail
        )
    return state


def _from_block(
    backend_name: str,
    block: Mapping[str, Any],
    *,
    observed_at: str,
    source: str,
    now: datetime,
) -> BudgetState:
    """Build a state from one budget block, expiring a window that has reset.

    Expiry is what stops a single exhausted record holding a project forever: the
    figure described a window, and once that window has rolled over the figure
    describes nothing. It degrades to unknown, which never blocks — the honest
    answer, since the next run will measure it again.
    """
    resets_at = block.get("resets_at")
    reset_moment = _parse_stamp(resets_at) if resets_at else None
    remaining: int | None = None
    if reset_moment is not None:
        remaining = max(0, int((reset_moment - now).total_seconds()))
    expired = reset_moment is not None and reset_moment <= now
    headroom = "known" if _is_known(block) else "unknown"
    detail = str(block.get("detail") or "")
    if block.get("headroom") == "known" and headroom == "unknown":
        detail = "headroom was labelled known but carried no numeric utilisation"
    if expired and headroom == "known":
        headroom = "unknown"
        detail = (
            f"the measured window reset at {resets_at}, so the recorded "
            "utilisation no longer describes it"
        )
    utilisation = block.get("utilisation_pct")
    numeric_utilisation = (
        float(utilisation)
        if isinstance(utilisation, (int, float)) and not isinstance(utilisation, bool)
        else None
    )
    return BudgetState(
        backend=backend_name,
        headroom=headroom,
        utilisation_pct=numeric_utilisation,
        rate_limit_type=(
            str(block["rate_limit_type"])
            if block.get("rate_limit_type") is not None
            else None
        ),
        rate_limit_period_minutes=(
            float(block["rate_limit_period_minutes"])
            if block.get("rate_limit_period_minutes") is not None
            else None
        ),
        resets_at=resets_at,
        seconds_until_reset=remaining,
        threshold_status=block.get("threshold_status"),
        observed_at=observed_at,
        source=source,
        expired=expired,
        detail=detail,
    )


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_known(block: Mapping[str, Any]) -> bool:
    """Return whether a block contains a numeric utilisation measurement."""
    utilisation = block.get("utilisation_pct")
    return (
        block.get("headroom") == "known"
        and isinstance(utilisation, (int, float))
        and not isinstance(utilisation, bool)
    )


# ── The decision ────────────────────────────────────────────────────────────


def _lapsed_minutes(
    state: BudgetState, policy_block: Mapping[str, Any], now: datetime
) -> tuple[int, float] | None:
    """The evidence's age and its bound, when the age has outrun the bound.

    Only a judgement that states no reset time is aged this way. One that states
    a reset carries its own expiry and is already degraded by that, and applying
    both would let the shelf life clear a hold whose window is demonstrably still
    open. An observation with no readable stamp has no age to compare, so it is
    left alone rather than aged on a guess.
    """
    if state.resets_at:
        return None
    bound = float(
        policy_block.get("evidence_shelf_life_minutes", DEFAULT_SHELF_LIFE_MINUTES)
    )
    if bound <= 0:
        return None
    observed = _parse_stamp(state.observed_at) if state.observed_at else None
    if observed is None:
        return None
    minutes = int((now - observed).total_seconds() // 60)
    return (minutes, bound) if minutes > bound else None


def decide(
    state: BudgetState,
    policy_block: Mapping[str, Any],
    *,
    purpose: str = "dispatch",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Judge one backend: is the wave held, and on what evidence?

    Every branch names its reason in the verdict. A hold whose reason is not
    legible cannot be argued with, and the lead reading a held wave needs to see
    which backend, what utilisation, and against which threshold.

    ``now`` is what the age of the evidence is measured against, so a caller
    that already knows the moment it is deciding at states it rather than
    letting two readings of the clock disagree inside one verdict.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"purpose {purpose!r} must be one of {', '.join(PURPOSES)}")
    ceiling = float(policy_block.get("utilisation_ceiling_pct", UNSET_CEILING_PCT))
    limit = effective_ceiling(policy_block, purpose)
    verdict: dict[str, Any] = {
        "backend": state.backend,
        "purpose": purpose,
        "ceiling_pct": ceiling,
        "effective_ceiling_pct": limit,
        "held": False,
        "state": state.as_dict(),
    }
    if state.availability == "served":
        verdict["reason"] = (
            f"backend {state.backend!r} served the minimal availability request at "
            f"{state.availability_observed_at}; the lane is open"
        )
        return verdict
    if state.availability == "refused":
        verdict["held"] = True
        verdict["reason"] = (
            f"backend {state.backend!r} refused the minimal availability request at "
            f"{state.availability_observed_at}; that refusal is the current evidence"
        )
        return verdict
    if state.headroom != "known":
        verdict["reason"] = (
            "headroom is unknown, and absence of a signal is never read as "
            f"exhaustion — {state.detail or 'nothing recorded for this backend'}"
        )
        return verdict

    if (lapse := _lapsed_minutes(state, policy_block, _now(now))) is not None:
        minutes, bound = lapse
        stale = replace(
            state,
            headroom="unknown",
            detail=(
                f"the reading is {minutes} minutes old, past the {bound:g} minute "
                "shelf life, and states no reset time to decay through"
            ),
        )
        verdict["state"] = stale.as_dict()
        verdict["reason"] = (
            f"the only evidence is {minutes} minutes old against a {bound:g} minute "
            "shelf life and names no reset, so it describes the past rather than "
            "now — headroom is unknown until a run records a fresh reading"
        )
        return verdict

    exhausted = [str(status) for status in policy_block.get("exhausted_statuses") or ()]
    if state.threshold_status is not None and str(state.threshold_status) in exhausted:
        verdict["held"] = True
        verdict["reason"] = (
            f"backend reports threshold status {state.threshold_status!r}, which "
            "policy counts as exhausted regardless of utilisation"
        )
        return verdict

    utilisation = state.utilisation_pct
    if utilisation is None:
        verdict["reason"] = (
            "headroom is reported known but carries no utilisation, so there is "
            "nothing to compare against the ceiling"
        )
        return verdict
    if utilisation >= limit:
        verdict["held"] = True
        margin = "" if purpose == "resume" else f" (ceiling {ceiling}% less reserve)"
        verdict["reason"] = (
            f"utilisation {utilisation}% is at or above the {limit}% ceiling for a "
            f"{purpose}{margin}"
        )
        return verdict
    verdict["reason"] = f"utilisation {utilisation}% is below the {limit}% ceiling"
    return verdict


# ── The pre-flight ──────────────────────────────────────────────────────────


def record_checks(
    project: str,
    verdicts: Iterable[Mapping[str, Any]],
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
    resumption_fired: bool = False,
) -> dict[str, Any]:
    """Persist held and newly clear verdicts beside completed run records.

    ``resumption_fired`` is explicit because a stuck-worker resume uses the
    same budget ceiling as a scheduled wave resumption without proving that a
    scheduler fired.
    """
    checks = [
        {**dict(verdict), "resumption_fired": resumption_fired} for verdict in verdicts
    ]
    return ledger.record_hold_checks(
        project,
        checks,
        checked_at=_iso(_now(now)),
        root=root,
    )


def backends_for_roles(config: Mapping[str, Any], roles: Iterable[str]) -> list[str]:
    """Resolve every backend a set of roles can reach."""
    names: list[str] = []
    for role in roles:
        for spec_level in ("", "exact", "guided", "open"):
            name, _settings = crew.resolve_role(config, role, spec_level)
            if name not in names:
                names.append(name)
    return names


def preflight(
    project: str,
    config: Mapping[str, Any],
    *,
    backends: Iterable[str] | None = None,
    roles: Iterable[str] | None = None,
    root: str | Path | None = None,
    purpose: str = "dispatch",
    now: datetime | None = None,
    probe_runner: Callable[[Any], Mapping[str, Any] | None] | None = None,
    lane_probe_runner: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None]
    | None = None,
) -> dict[str, Any]:
    """Decide, per backend, whether a wave may open.

    This is an observational check. Command paths that act on its verdict call
    :func:`record_checks`; read-only surfaces return the report without
    inventing hold history.

    A backend opting into ``budget_check`` is asked only when recorded
    exhaustion names no reset. The probe is serialized and cached per backend,
    so ten callers in one wave still issue one minimal request. A backend that
    has never reported exhaustion is neither held nor probed.

    Per-backend rather than global, because that is the whole reason budget state
    is tracked per backend: one backend being spent must not stop ready nodes
    that would run somewhere else. A held backend and a clear one in the same
    report is the normal case, not an edge one.
    """
    moment = _now(now)
    policy_block = policy(config)
    configured = config.get("backends") or {}
    if backends is not None:
        names = [str(name) for name in backends]
    elif roles is not None:
        names = backends_for_roles(config, roles)
    else:
        names = sorted(str(name) for name in configured)

    recorded = latest_recorded(project, root=root, config=config)
    verdicts = []
    for name in names:
        settings = configured.get(name)
        state = state_for(
            name,
            settings if isinstance(settings, Mapping) else {},
            recorded=recorded.get(name),
            unattributed=recorded.unattributed,
            now=moment,
            probe_runner=probe_runner,
        )
        # First judge without ageing. This identifies an undated exhaustion
        # even after its fallback shelf life has elapsed: when this host can
        # probe, provider availability is observed rather than inferred from a
        # clock. A stated reset and an unknown backend never reach the probe.
        timeless_policy = {
            **policy_block,
            "evidence_shelf_life_minutes": 0,
        }
        recorded_verdict = decide(state, timeless_policy, purpose=purpose, now=moment)
        if (
            recorded_verdict["held"]
            and state.resets_at is None
            and isinstance(settings, Mapping)
            and bool(settings.get("budget_check"))
        ):
            from reckon.crew.resumption import probe_lane_availability

            observation = probe_lane_availability(
                project,
                name,
                settings,
                root=root,
                cache_seconds=availability_probe_cache_seconds(policy_block),
                now=moment,
                runner=lane_probe_runner,
            )
            status = str(observation.get("status") or "unavailable")
            if status in {"served", "refused"}:
                observed_at = str(observation.get("observed_at") or _iso(moment))
                probe_budget = observation.get("budget")
                if status == "refused" and isinstance(probe_budget, Mapping):
                    state = _from_block(
                        name,
                        probe_budget,
                        observed_at=observed_at,
                        source="lane-probe",
                        now=moment,
                    )
                else:
                    state = replace(state, source="lane-probe")
                state = replace(
                    state,
                    observed_at=(
                        observed_at if status == "refused" else state.observed_at
                    ),
                    availability=status,
                    availability_observed_at=observed_at,
                    availability_cached=bool(observation.get("cached")),
                    detail=str(observation.get("detail") or state.detail),
                )
        verdicts.append(decide(state, policy_block, purpose=purpose, now=moment))

    held = [verdict for verdict in verdicts if verdict["held"]]
    waits = [
        verdict["state"]["seconds_until_reset"]
        for verdict in held
        if verdict["state"]["seconds_until_reset"] is not None
    ]
    report = {
        "project": project,
        "purpose": purpose,
        "checked_at": _iso(moment),
        "policy": policy_block,
        "held": bool(held),
        "held_backends": [verdict["backend"] for verdict in held],
        "clear_backends": [
            verdict["backend"] for verdict in verdicts if not verdict["held"]
        ],
        "backends": verdicts,
        "unattributed_records": [
            {
                "observed_at": reading.observed_at,
                "record_id": reading.record_id,
                "source": reading.source,
            }
            for reading in recorded.unattributed
        ],
        "resume_after_seconds": min(waits) if waits else None,
        "resume_at": _earliest_reset(held),
    }
    report["summary"] = summary(report) if held else ""
    return report


def _earliest_reset(held: Iterable[Mapping[str, Any]]) -> str | None:
    """Return the first reset time that would clear one of the holds."""
    stamps = [
        verdict["state"]["resets_at"]
        for verdict in held
        if verdict["state"]["resets_at"]
    ]
    parsed = [(stamp, _parse_stamp(stamp)) for stamp in stamps]
    live = [(when, stamp) for stamp, when in parsed if when is not None]
    return min(live)[1] if live else None


def summary(report: Mapping[str, Any]) -> str:
    """Render a held wave as the four-axis summary a dispatched one gets.

    A hold is a decision the lead needs to see, so it reports on the same axes as
    a dispatch: what is held, why, how it stays recoverable, and when it lifts.
    Reporting it any other way, or not at all, makes a held wave look like a
    crashed orchestrator.
    """
    held = [verdict for verdict in report.get("backends", ()) if verdict.get("held")]
    clear = list(report.get("clear_backends") or ())
    reasons = "; ".join(str(verdict.get("reason") or "") for verdict in held)
    if clear:
        how = (
            "no worktree created and no node failed; ready nodes on "
            f"{', '.join(clear)} dispatch normally"
        )
    else:
        how = "no worktree created and no node failed; every node stays ready"
    wait = report.get("resume_after_seconds")
    resume_at = report.get("resume_at")
    if resume_at and wait is not None:
        when = f"resets at {resume_at}, in {wait}s — resume the wave then"
    else:
        when = (
            "the backend reported no reset time, so the wave waits for a fresh "
            "observation rather than a clock"
        )
    return "\n".join(
        [
            f"WHAT   wave held before opening — {len(held)} backend(s) held, "
            f"{len(clear)} clear",
            f"WHY    {reasons}",
            f"HOW    {how}",
            f"WHEN   {when}",
        ]
    )
