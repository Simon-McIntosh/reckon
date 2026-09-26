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
state, and it decays only through its own reset time. A successfully completed
run is different: its trustworthy completion stamp proves that the backend
served, so it can displace an earlier exhaustion without inventing a utilisation
figure. Taking every other "most recent" silence at face value would let one
quiet record erase a real exhaustion and open the wave this module exists to
hold.

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

A pre-flight also names the pace it is judging a wave inside, so a coordinator
reads it before committing rather than discovering it in a refusal. Per declared
budget group it reports both metered clocks with the age of each reading, the
allowance derived from the week that group has to last, and the bar a stated
ready set is judged against — each figure carried with the observation it came
from, because a utilisation that cannot be aged cannot be told from a current
one. A group no reading reached reports unknown for its clocks and its allowance
rather than a zero, which is the first rule above applied to a second quantity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

from reckon import _backends, crew, ledger
from reckon.crew import bar as bar_module
from reckon.crew import budget_group, window_reading
from reckon.crew import pace as pace_module
from reckon.crew import rollout as rollout_module
from reckon.crew.refusals import format_refusal

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

# Provider utilisation is reported as an integer percentage. At 1.69% of a
# window, one percentage point changes the burn multiple by 1 / 1.69 = 0.59,
# so an opening-hours projection would mostly measure quantisation. Waiting for
# 5% elapsed caps that step at 0.2x; requiring 5% used gives the reading at
# least five integer samples, so one point is at most 20% of measured use.
BURN_ELAPSED_FRACTION_FLOOR = 0.05
BURN_UTILISATION_PCT_FLOOR = 5.0

# The account's own severity labels that mean a window is raised. Severity is
# the account's server-computed posture rather than a threshold reckon invents,
# so a raised label holds even when a numeric utilisation alone would sit under
# the configured ceiling: the fence holds no looser than the figure the account
# reports, whichever of the two inputs is currently telling the truth.
RAISED_SEVERITIES = ("warning", "critical")


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
    burn_multiple: float | None = None
    projected_exhaustion_at: str | None = None
    rate_limit_type: str | None = None
    rate_limit_period_minutes: float | None = None
    resets_at: str | None = None
    seconds_until_reset: int | None = None
    threshold_status: str | None = None
    severity: str | None = None
    observed_at: str | None = None
    age_source: str | None = None
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
            "burn_multiple": self.burn_multiple,
            "availability": self.availability,
            "availability_cached": self.availability_cached,
            "availability_observed_at": self.availability_observed_at,
            "age_source": self.age_source,
            "detail": self.detail,
            "expired": self.expired,
            "headroom": self.headroom,
            "observed_at": self.observed_at,
            "projected_exhaustion_at": self.projected_exhaustion_at,
            "rate_limit_period_minutes": self.rate_limit_period_minutes,
            "rate_limit_type": self.rate_limit_type,
            "resets_at": self.resets_at,
            "seconds_until_reset": self.seconds_until_reset,
            "source": self.source,
            "threshold_status": self.threshold_status,
            "severity": self.severity,
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
    coordinator_reserve = block.get("coordinator_reserve_pct")
    statuses = block.get("exhausted_statuses") or ()
    shelf_life = block.get("evidence_shelf_life_minutes")
    resolved = {
        "utilisation_ceiling_pct": (
            UNSET_CEILING_PCT if ceiling is None else float(ceiling)
        ),
        "resume_reserve_pct": UNSET_RESERVE_PCT if reserve is None else float(reserve),
        "coordinator_reserve_pct": (
            UNSET_RESERVE_PCT
            if coordinator_reserve is None
            else float(coordinator_reserve)
        ),
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

    A fresh dispatch stops at the ceiling less the reserves; answering a worker
    that stopped and asked for help may spend them, because that is the exact
    expenditure the reserves were withheld for. The coordinator reserve keeps
    headroom for the coordinator that must survive the wave to audit manifests,
    merge commits and record outcomes; a wave that spends it on a new node
    strands the wave in its worst state — work in flight and nothing left to
    unblock or close it with.
    """
    ceiling = float(policy_block.get("utilisation_ceiling_pct", UNSET_CEILING_PCT))
    if purpose == "resume":
        return ceiling
    resume = float(policy_block.get("resume_reserve_pct", UNSET_RESERVE_PCT))
    coordinator = float(policy_block.get("coordinator_reserve_pct", UNSET_RESERVE_PCT))
    return max(0.0, ceiling - resume - coordinator)


def withheld_reserves(policy_block: Mapping[str, Any]) -> list[str]:
    """Name each nonzero reserve a fresh dispatch is withheld by.

    The reason a hold carries must say which headroom was withheld, or a reader
    who sees a lane stop below the ceiling cannot tell what was protecting whom.
    """
    labels = (
        ("resume_reserve_pct", "resume reserve"),
        ("coordinator_reserve_pct", "coordinator reserve"),
    )
    withheld = []
    for key, label in labels:
        value = float(policy_block.get(key, 0.0) or 0.0)
        if value > 0:
            withheld.append(f"{value:g}% {label}")
    return withheld


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
    age_source: str
    record_id: str = ""
    attribution: str = ""
    surface_opt_in: bool = False
    covered_backends: tuple[str, ...] = ()
    served_completion: bool = False

    def applies_to(self, backend_name: str) -> bool:
        """Return whether this reading explicitly describes ``backend_name``."""
        if self.backend == backend_name:
            return True
        return backend_name in self.covered_backends


@dataclass(frozen=True)
class _RefusalEvent:
    """The newest stream refusal and any later provider-side refutation."""

    stamp: str | None
    served_status: str | None = None


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

    def for_backend(self, backend_name: str) -> _Reading | None:
        """Resolve one backend to its own reading or one declared coverage."""
        direct = self.get(backend_name)
        if direct is not None and direct.backend == backend_name:
            return direct
        covering: list[_Reading] = []
        seen: set[int] = set()
        for reading in self.values():
            identity = id(reading)
            if identity in seen or not reading.applies_to(backend_name):
                continue
            seen.add(identity)
            covering.append(reading)
        return covering[0] if len(covering) == 1 else None


def _declared_coverage(budget_block: Mapping[str, Any]) -> tuple[str, ...]:
    """Read an explicit multi-backend coverage declaration from a budget block."""
    declared = budget_block.get("covered_backends")
    if not isinstance(declared, Iterable) or isinstance(declared, (str, bytes)):
        return ()
    return tuple(dict.fromkeys(str(name) for name in declared if str(name)))


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


def _event_stamp(event: Mapping[str, Any]) -> str | None:
    """Return an event timestamp only when it is usable as an age anchor."""
    stamp = event.get("timestamp")
    return str(stamp) if _parse_stamp(stamp) is not None else None


def _refusal_event_stamp(pointer: Mapping[str, Any]) -> _RefusalEvent | None:
    """Locate the newest rejected rate-limit event in a live run's stream.

    Rate-limit records do not consistently carry their own timestamp. Their
    immutable stream position does, however, place them between timestamped
    records. The closest surrounding stamp is therefore used; an equally near
    following record wins because expiring a real hold early is the unsafe
    direction. A later provider ``rate_limit_event`` with status ``allowed`` or
    ``allowed_warning`` is the served-turn marker: unlike assistant/user prose,
    it records that a request was admitted, while another ``rejected`` event is
    only another refusal.
    """
    log_path = pointer.get("log_path")
    if not log_path:
        return None
    try:
        with Path(str(log_path)).open() as stream:
            events, _malformed = _backends.parse_events(stream)
    except OSError:
        return None

    rejected = []
    for index, event in enumerate(events):
        info = event.get("rate_limit_info")
        if (
            event.get("type") == "rate_limit_event"
            and isinstance(info, Mapping)
            and str(info.get("status") or "").casefold() == "rejected"
        ):
            rejected.append(index)

    if not rejected:
        return None

    index = rejected[-1]
    served_status = next(
        (
            status
            for event in events[index + 1 :]
            if event.get("type") == "rate_limit_event"
            and isinstance((info := event.get("rate_limit_info")), Mapping)
            and (status := str(info.get("status") or "").casefold())
            in {"allowed", "allowed_warning"}
        ),
        None,
    )
    if stamp := _event_stamp(events[index]):
        return _RefusalEvent(stamp, served_status)
    distance = 1
    while index - distance >= 0 or index + distance < len(events):
        if index + distance < len(events) and (
            stamp := _event_stamp(events[index + distance])
        ):
            return _RefusalEvent(stamp, served_status)
        if index - distance >= 0 and (stamp := _event_stamp(events[index - distance])):
            return _RefusalEvent(stamp, served_status)
        distance += 1
    return _RefusalEvent(None, served_status)


def _surface_opt_in(backend_name: str | None, config: Mapping[str, Any] | None) -> bool:
    """Whether the configured backend opts its account surface into a read.

    The opt-in is a config fact, not a property of any one record, but it must
    travel on the reading for the resume path: that path recovers a run's
    backend from the run's own argv, which carries the command but not the
    configuration's ``budget_check`` flag, so the surface read would otherwise
    never fire when the run is the only witness of its own backend.
    """
    if not backend_name:
        return False
    settings = ((config or {}).get("backends") or {}).get(backend_name)
    return bool(isinstance(settings, Mapping) and settings.get("budget_check"))


def _stamp_refusal(*, lane: str, refused_at: str, returns_at: str) -> dict[str, str]:
    """Persist an observed refusal somewhere the refusal's own run cannot reach.

    A refusal keeps the run that observed it from promoting, so the ledger never
    records it and the run's own directory gets reclaimed with the run. The
    shadow store lives outside both, keyed by the refusal's own time. Writing it
    is best-effort the same way promotion-time shadow writes are: a store that
    cannot be written must not break the budget read that just observed the
    refusal — the hold the reading carries still works, only its durable mirror
    is lost. The failure is returned, never raised, so the caller can record it
    where the reading is visible.
    """
    from reckon import run_store

    try:
        with run_store.RunStore() as store:
            store.stamp_refusal(lane=lane, refused_at=refused_at, returns_at=returns_at)
    except Exception as exc:  # noqa: BLE001 — a shadow write never interrupts the read it mirrors
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return {"status": "written"}


def binding_window(
    lane: str, *, store_path: str | Path | None = None
) -> dict[str, Any]:
    """Report which quota window binds a lane, read from its stamped refusals.

    A lane that publishes no quota reading is constrained by whichever window
    keeps refusing it, and only the refusals it could not avoid emitting can
    say which. Those stamps are the durable rows :func:`_stamp_refusal` writes,
    so this reads the store rather than any live probe: the reading survives the
    run directory and the account surface that neither outlives the refusal.

    Two refusals about one short window's period apart are a reset crossing —
    the short window returned and the lane refused anyway, so the weekly window
    is what binds. Two far apart show a lane that was served after the short
    window returned and drained it again, so the short window binds. A single
    refusal distinguishes neither and reports ``undetermined`` rather than
    naming a window. The store is a lower bound rather than a census, since a
    refusal that never surfaced as a log-derived rate-limit event is never
    stamped, which is what makes that third state load-bearing.

    The store carries every lane's refusals in one table; only the records whose
    own lane matches ``lane`` are read; another lane's refusals never place this
    one's window. ``store_path`` overrides the store location and defaults to
    the same one :func:`_stamp_refusal` writes through, so a caller that stamps
    and a caller that reads reach the same rows.
    """
    from reckon import run_store
    from reckon.crew.lane_evidence import infer_binding_window

    with run_store.RunStore(store_path) as store:
        stamps = store.refusal_stamps()
    return infer_binding_window(stamps, lane)


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
        budget_block = dict(budget)
        # `when` chooses the freshest reading and may track mutable observed_at.
        # Age is a different fact: it belongs to the refusal event that created
        # the hold. The event's fixed stream position survives every re-read.
        # created_at is only the explicit lower-bound fallback when the stream
        # carries no rejected event with a usable surrounding timestamp.
        is_refusal = bool(budget.get("refusal")) or str(
            budget.get("threshold_status") or ""
        ).casefold() in {"exhausted", "rejected"}
        refusal_event = _refusal_event_stamp(pointer) if is_refusal else None
        refusal_stamp = refusal_event.stamp if refusal_event is not None else None
        if refusal_event is not None and refusal_event.served_status is not None:
            budget_block["_refuted_by_served_turn"] = refusal_event.served_status
            refusal_stamp = refusal_stamp or pointer.get("created_at") or ""
            age_source = "served-turn-refutation"
        elif refusal_stamp is not None:
            age_source = "rate-limit-event"
        elif is_refusal:
            refusal_stamp = pointer.get("created_at") or ""
            age_source = "created_at-lower-bound"
        else:
            refusal_stamp = fresh_stamp
            age_source = "observed-at"
        backend_name = str(pointer.get("backend") or "")
        # A refused dispatch kills the run, so the refusal is exactly the event a
        # promoted ledger row (and the run it died in) would never record. A
        # rate-limit-event stamp is the only reading with the refusal's own
        # observation time AND the return time it stated; earlier-bound refusals
        # and served-turn refutations must not be minted here, because a fabricated
        # or already-refuted record would mislead anyone reading the store. The
        # outcome is recorded on the reading so a failed shadow write stays
        # visible rather than failing the read that observed the refusal.
        if age_source == "rate-limit-event":
            stamp_outcome = _stamp_refusal(
                lane=backend_name,
                refused_at=str(refusal_stamp),
                returns_at=str(budget_block.get("resets_at") or ""),
            )
            if stamp_outcome["status"] != "written":
                budget_block["_refusal_stamp_error"] = stamp_outcome["error"]
        found.append(
            _Reading(
                backend=backend_name,
                budget=budget_block,
                observed_at=str(refusal_stamp),
                when=when,
                source="live-run",
                age_source=age_source,
                record_id=str(pointer.get("run_id") or ""),
                attribution="record",
                surface_opt_in=_surface_opt_in(backend_name, config),
                covered_backends=_declared_coverage(budget),
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
                age_source="completed-at",
                record_id=str(record.get("run_id") or ""),
                attribution=attribution,
                surface_opt_in=_surface_opt_in(backend, config),
                covered_backends=_declared_coverage(budget),
                # A passed gate plus a stream-derived or explicitly supplied
                # completion stamp shows that this backend served the run.
                # Promotion time alone only shows that a ledger writer ran.
                served_completion=(
                    str(record.get("gate") or "").casefold() == "passed"
                    and str(record.get("completed_at_source") or "")
                    in ledger.USABLE_COMPLETION_SOURCES
                ),
            )
        )
    return found


def _records_exhaustion(
    budget_block: Mapping[str, Any], config: Mapping[str, Any] | None
) -> bool:
    """Whether this known reading would hold at the configured ceiling."""
    if not _is_known(budget_block):
        return False
    policy_block = policy(config)
    exhausted = {str(status) for status in policy_block.get("exhausted_statuses") or ()}
    status = budget_block.get("threshold_status")
    if status is not None and str(status) in exhausted:
        return True
    utilisation = budget_block.get("utilisation_pct")
    return float(utilisation) >= effective_ceiling(policy_block, "resume")


def _completion_after_exhaustion(reading: _Reading) -> _Reading:
    """Carry why a served completion superseded an older exhausted reading."""
    budget_block = dict(reading.budget)
    budget_block["_displaced_exhaustion_by_completion"] = {
        "run_id": reading.record_id,
        "completed_at": reading.observed_at,
    }
    return replace(reading, budget=budget_block, age_source="served-completion")


def latest_recorded(
    project: str,
    *,
    root: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> _RecordedReadings:
    """Return the best recorded reading per backend, preferring a known one.

    A known measurement outranks any silence, however recent, because silence
    carries no information: letting a later unknown win would erase a recorded
    exhaustion and open exactly the wave this module holds. A later served
    completion is positive evidence rather than silence and may displace an
    exhausted reading, while remaining unknown rather than inventing headroom.
    Between two readings of the same kind, the newer wins. Known signals that
    cannot be matched remain available through ``unattributed`` on the result.
    """
    best: dict[str, _Reading] = {}
    unattributed: list[_Reading] = []
    readings = sorted(
        _readings(project, root=root, config=config), key=lambda item: item.when
    )
    for reading in readings:
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
            elif reading.served_completion and _records_exhaustion(
                current.budget, config
            ):
                best[reading.backend] = _completion_after_exhaustion(reading)
            continue
        if not known and current.served_completion and not reading.served_completion:
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

    The opt-in is also honoured when it arrives on the recorded reading, because
    a resumption recovers its backend from the run's own argv and so cannot pass
    the configured flag itself: a backend that opts its surface into a read on
    dispatch must resolve it the same way on the resume that dispatch
    deliberately spared headroom for.
    """
    moment = _now(now)
    state = BudgetState(backend=backend_name)
    applicable_recorded = (
        recorded if recorded is not None and recorded.applies_to(backend_name) else None
    )
    if applicable_recorded is not None:
        state = _from_block(
            backend_name,
            applicable_recorded.budget,
            observed_at=applicable_recorded.observed_at,
            source=applicable_recorded.source,
            age_source=applicable_recorded.age_source,
            now=moment,
        )
    elif unmatched := sorted(unattributed, key=lambda item: item.when):
        latest = unmatched[-1]
        count = len(unmatched)
        noun = "signal" if count == 1 else "signals"
        identity = f"; latest record {latest.record_id}" if latest.record_id else ""
        state.source = "unattributed-ledger"
        state.observed_at = latest.observed_at
        state.age_source = latest.age_source
        state.detail = (
            f"{count} known headroom {noun} were recorded but could not be "
            f"attributed to a backend{identity}"
        )
    if (backend or {}).get("budget_check") or (
        applicable_recorded is not None and applicable_recorded.surface_opt_in
    ):
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
                age_source="account-observation",
                now=moment,
            )
        detail = str(block.get("detail") or "")
        state.detail = (
            f"{state.detail}; {detail}".strip("; ") if detail else state.detail
        )
    return state


# The shared account-weekly window every metered lane can report. A lane whose
# keyed quota windows name another length has its own quota horizon, and the
# budget view must report that one rather than the shared figure: one lane read
# 97% of its own 300-minute window while the report beside it showed 21% on the
# general weekly, so the nearly-exhausted lane looked clear for dispatch.
_SHARED_WEEKLY_MINUTES = 7 * 24 * 60


def _quota_windows(block: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    """Normalise the keyed-by-window quota map a block may carry.

    The account-surface probe records a mapping from window length to a row;
    the fleet-state reader records a list of rows each naming its own window.
    Both shapes are accepted, because either may be the freshest witness of a
    lane's own horizon.
    """
    raw = block.get("quota_windows")
    if isinstance(raw, Mapping):
        rows: dict[int, Mapping[str, Any]] = {}
        for raw_window, row in raw.items():
            if not isinstance(row, Mapping):
                continue
            window = row.get("window_minutes")
            if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
                try:
                    window = int(raw_window)
                except (TypeError, ValueError):
                    continue
            rows[int(window)] = row
        return rows
    if isinstance(raw, list):
        rows = {}
        for row in raw:
            if not isinstance(row, Mapping):
                continue
            window = row.get("window_minutes")
            if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
                continue
            rows[int(window)] = row
        return rows
    return {}


def _own_window_selection(
    block: Mapping[str, Any],
) -> tuple[str, int | None, Mapping[str, Any] | None]:
    """Classify the windows a block declares for the budget view.

    Returns a ``(status, window_minutes, row)`` triple:

    - ``"shared"`` — no window other than the shared account weekly is named,
      so the shared figure is the lane's own; the row is None.
    - ``"own"`` — an own (non-weekly) horizon is named and at least one of the
      lane's windows is measurable. The returned reading is the binding one
      across every measured keyed window — the own horizon and the shared
      weekly alike — keyed by its own length, because that is the window a
      wave would actually run into first. Reporting the more-exhausted window
      never hides an alarm; the measured defect was the reverse, a low shared
      weekly figure standing in for a lane 97% through its own 300-minute
      window, which read the lane as clear for dispatch.
    - ``"own-unmeasured"`` — an own horizon is named but none of its windows
      carries a numeric utilisation, so no figure can be read without
      substituting the shared weekly.
    """
    windows = _quota_windows(block)
    own = {
        window: row
        for window, row in windows.items()
        if window != _SHARED_WEEKLY_MINUTES
    }
    if not own:
        return ("shared", None, None)

    def numeric(row: Mapping[str, Any]) -> float | None:
        used = row.get("used_percent")
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            return float(used)
        return None

    if not any(numeric(row) is not None for row in own.values()):
        return ("own-unmeasured", None, None)
    measured = [
        (window, row) for window, row in windows.items() if numeric(row) is not None
    ]
    window, row = max(measured, key=lambda item: numeric(item[1]))
    return ("own", window, row)


def _effective_quota_block(block: Mapping[str, Any]) -> dict[str, Any]:
    """Re-base a budget block onto a lane's own quota window when it has one.

    A block may carry the shared account-weekly figure in its compatibility
    fields while its keyed quota windows show the lane's own horizon, and the
    two can disagree badly: a lane 97% of the way through its own 300-minute
    window read clear beside a general weekly at 21%. When the block names a
    window other than the shared weekly, the report re-bases on that own
    window. When its own figure cannot be read, the report is unmeasured
    rather than silently substituting the shared number.
    """
    status, window_minutes, row = _own_window_selection(block)
    if status == "own":
        effective = dict(block)
        effective["utilisation_pct"] = row.get("used_percent")
        effective["rate_limit_period_minutes"] = window_minutes
        if row.get("resets_at"):
            effective["resets_at"] = row["resets_at"]
        if row.get("rate_limit_type"):
            effective["rate_limit_type"] = row["rate_limit_type"]
        # The severity rides the same window the utilisation does: only the
        # binding row's label reaches the decision, so a severity the account
        # reports for some other horizon cannot hold a request it does not gate.
        effective["severity"] = row.get("severity")
        effective["headroom"] = "known"
        return effective
    if status == "own-unmeasured":
        effective = dict(block)
        effective["headroom"] = "unknown"
        effective["utilisation_pct"] = None
        effective["rate_limit_period_minutes"] = None
        effective["resets_at"] = None
        detail = str(effective.get("detail") or "")
        clause = (
            "the lane's own quota window could not be read, so its figure was "
            "not substituted from the shared weekly"
        )
        effective["detail"] = f"{detail}; {clause}".strip("; ") if detail else clause
        return effective
    return dict(block)


def _from_block(
    backend_name: str,
    block: Mapping[str, Any],
    *,
    observed_at: str,
    source: str,
    now: datetime,
    age_source: str | None = None,
) -> BudgetState:
    """Build a state from one budget block, expiring a window that has reset.

    Expiry is what stops a single exhausted record holding a project forever: the
    figure described a window, and once that window has rolled over the figure
    describes nothing. It degrades to unknown, which never blocks — the honest
    answer, since the next run will measure it again.
    """
    block = _effective_quota_block(block)
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
    refusal_stamp_error = block.get("_refusal_stamp_error")
    if isinstance(refusal_stamp_error, str) and refusal_stamp_error:
        detail = (
            "the refusal survives only in this reading, not in the durable "
            f"store: {refusal_stamp_error}"
        )
    served_status = block.get("_refuted_by_served_turn")
    if served_status is not None:
        headroom = "unknown"
        detail = (
            "a later served turn in the same stream refuted the newest refusal "
            f"(provider rate-limit status {str(served_status)!r})"
        )
    completion = block.get("_displaced_exhaustion_by_completion")
    if isinstance(completion, Mapping):
        headroom = "unknown"
        run_id = str(completion.get("run_id") or "")
        completed_at = str(completion.get("completed_at") or observed_at)
        identity = f" {run_id!r}" if run_id else ""
        detail = (
            f"completed run{identity} at {completed_at} displaced the earlier "
            "recorded exhaustion without publishing a utilisation figure"
        )
    utilisation = block.get("utilisation_pct")
    numeric_utilisation = (
        float(utilisation)
        if isinstance(utilisation, (int, float)) and not isinstance(utilisation, bool)
        else None
    )
    period = block.get("rate_limit_period_minutes")
    numeric_period = float(period) if period is not None else None
    state = BudgetState(
        backend=backend_name,
        headroom=headroom,
        utilisation_pct=numeric_utilisation,
        burn_multiple=(
            _burn_multiple(numeric_utilisation, numeric_period, remaining)
            if headroom == "known"
            else None
        ),
        rate_limit_type=(
            str(block["rate_limit_type"])
            if block.get("rate_limit_type") is not None
            else None
        ),
        rate_limit_period_minutes=numeric_period,
        resets_at=resets_at,
        seconds_until_reset=remaining,
        threshold_status=block.get("threshold_status"),
        severity=block.get("severity"),
        observed_at=observed_at,
        age_source=age_source,
        source=source,
        expired=expired,
        detail=detail,
    )
    return _with_projected_exhaustion(state, now=now)


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


def _burn_multiple(
    utilisation_pct: float | None,
    period_minutes: float | None,
    seconds_until_reset: int | None,
) -> float | None:
    """Return quota consumption relative to elapsed window time.

    The reset time locates the current point inside the declared window. A
    missing or invalid period cannot be inferred from utilisation alone, and a
    reading at or before the window start has no elapsed denominator yet.
    """
    if utilisation_pct is None or period_minutes is None or seconds_until_reset is None:
        return None
    period_seconds = period_minutes * 60.0
    elapsed_seconds = period_seconds - seconds_until_reset
    if period_seconds <= 0 or elapsed_seconds <= 0 or elapsed_seconds > period_seconds:
        return None
    elapsed_fraction = elapsed_seconds / period_seconds
    return (utilisation_pct / 100.0) / elapsed_fraction


def _window_elapsed_fraction(state: BudgetState) -> float | None:
    """Return how much of the reported window has elapsed."""
    period = state.rate_limit_period_minutes
    remaining = state.seconds_until_reset
    if period is None or remaining is None:
        return None
    period_seconds = period * 60.0
    elapsed_seconds = period_seconds - remaining
    if period_seconds <= 0 or elapsed_seconds <= 0 or elapsed_seconds > period_seconds:
        return None
    return elapsed_seconds / period_seconds


def _with_projected_exhaustion(state: BudgetState, *, now: datetime) -> BudgetState:
    """Attach a projected exhaustion instant when burn evidence is admissible.

    The floors govern the projection alone. A numeric utilisation remains known
    and available to the ceiling comparison even when integer quantisation makes
    its burn projection too coarse. Observation time is required because an
    unstamped projection cannot say which window position it describes, and a
    rolled-over window degrades through the same unknown path as every other
    expired reading.
    """
    reset = _parse_stamp(state.resets_at) if state.resets_at else None
    if state.expired or (reset is not None and reset <= now):
        return replace(
            state,
            headroom="unknown",
            burn_multiple=None,
            projected_exhaustion_at=None,
            expired=True,
        )
    if state.headroom != "known":
        return replace(state, projected_exhaustion_at=None)

    elapsed_fraction = _window_elapsed_fraction(state)
    utilisation = state.utilisation_pct
    burn = state.burn_multiple
    if elapsed_fraction is None or utilisation is None or burn is None:
        return replace(state, projected_exhaustion_at=None)

    below_elapsed_floor = elapsed_fraction < BURN_ELAPSED_FRACTION_FLOOR
    below_utilisation_floor = utilisation < BURN_UTILISATION_PCT_FLOOR
    if below_elapsed_floor or below_utilisation_floor:
        shortfalls = []
        if below_elapsed_floor:
            shortfalls.append(
                f"{elapsed_fraction * 100:.2f}% elapsed is below the "
                f"{BURN_ELAPSED_FRACTION_FLOOR * 100:g}% floor"
            )
        if below_utilisation_floor:
            shortfalls.append(
                f"{utilisation:g}% utilisation is below the "
                f"{BURN_UTILISATION_PCT_FLOOR:g}% floor"
            )
        floor_detail = "burn evidence is quantised too coarsely: " + " and ".join(
            shortfalls
        )
        detail = state.detail
        if floor_detail not in detail:
            detail = f"{detail}; {floor_detail}".strip("; ")
        return replace(
            state,
            projected_exhaustion_at=None,
            detail=detail,
        )

    observed = _parse_stamp(state.observed_at) if state.observed_at else None
    if observed is None or reset is None or state.seconds_until_reset is None:
        return replace(state, projected_exhaustion_at=None)

    projected = (
        reset
        if burn <= 1.0
        else observed + timedelta(seconds=state.seconds_until_reset / burn)
    )
    return replace(state, projected_exhaustion_at=_iso(projected))


def _position(state: BudgetState) -> str:
    """Name utilisation, burn rate and any admitted projection together."""
    utilisation = (
        f"{state.utilisation_pct:g}%"
        if state.utilisation_pct is not None
        else "unknown"
    )
    burn = (
        f"{state.burn_multiple:.1f}x" if state.burn_multiple is not None else "unknown"
    )
    position = f"utilisation {utilisation} with burn multiple {burn}"
    if state.projected_exhaustion_at:
        position += f" and projected exhaustion {state.projected_exhaustion_at}"
    else:
        floor_marker = "burn evidence is quantised too coarsely:"
        if floor_marker in state.detail:
            floor_detail = state.detail[state.detail.index(floor_marker) :]
            position += f"; projected exhaustion withheld because {floor_detail}"
    return position


def _age_basis(state: BudgetState) -> str:
    """Explain an age fallback whose bound is weaker than an event stamp."""
    if state.age_source != "created_at-lower-bound":
        return ""
    return (
        "; the stream had no parseable rejected rate-limit event, so created_at "
        "is only a lower bound on the refusal time, never a fresh refusal"
    )


def _evidence_note(state: BudgetState) -> str:
    """Name which reading a verdict acted on and when it was observed.

    A verdict's reason has to be arguable, and an operator reading a held lane
    needs to see whether the standing figure is a fresh account-surface read or
    a record of some earlier refusal. The surface reading names its observation
    time because that is the fact that makes it supersede an older record.
    """
    if state.source == "account-surface" and state.observed_at:
        return (
            f"; the account surface was read at {state.observed_at} and is the "
            "operative reading"
        )
    return _age_basis(state)


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
    state = _with_projected_exhaustion(state, now=_now(now))
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
        verdict["reason"] = format_refusal(
            "D02",
            f"backend {state.backend!r} refused the minimal availability request at "
            f"{state.availability_observed_at}; that refusal is the current evidence",
        )
        return verdict
    if state.headroom != "known":
        if state.age_source == "served-turn-refutation":
            verdict["reason"] = (
                f"{state.detail}; the refusal no longer describes now and the lane "
                "remains open"
            )
            return verdict
        if state.burn_multiple is not None:
            verdict["reason"] = (
                f"{_position(state)} is not admissible budget evidence — "
                f"{state.detail or 'the reported window is incomplete'}; "
                "headroom is unknown and the lane remains open"
            )
        else:
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
                f"{_evidence_note(state)}"
            ),
        )
        verdict["state"] = stale.as_dict()
        verdict["reason"] = (
            f"the only evidence is {minutes} minutes old against a {bound:g} minute "
            "shelf life and names no reset, so it describes the past rather than "
            "now — headroom is unknown until a run records a fresh reading"
            f"{_evidence_note(state)}"
        )
        return verdict

    exhausted = [str(status) for status in policy_block.get("exhausted_statuses") or ()]
    if state.threshold_status is not None and str(state.threshold_status) in exhausted:
        verdict["held"] = True
        verdict["reason"] = format_refusal(
            "D02",
            f"backend reports threshold status {state.threshold_status!r}, which "
            f"policy counts as exhausted regardless of utilisation; {_position(state)}"
            f"{_evidence_note(state)}",
        )
        return verdict

    utilisation = state.utilisation_pct
    if utilisation is not None and utilisation >= limit:
        verdict["held"] = True
        if purpose == "resume":
            margin = ""
        else:
            withheld = withheld_reserves(policy_block)
            suffix = " and ".join(withheld) if withheld else ""
            margin = f" (ceiling {ceiling:g}% less {suffix})" if suffix else ""
        verdict["reason"] = format_refusal(
            "D02",
            f"{_position(state)} is at or above the {limit}% ceiling for a "
            f"{purpose}{margin}{_evidence_note(state)}",
        )
        return verdict
    if state.severity in RAISED_SEVERITIES:
        # The floor runs before the None-utilisation guard so that a raised
        # label holds even a reading carrying no figure at all to compare.
        verdict["held"] = True
        if utilisation is None:
            position = (
                "the reading carries no utilisation to compare against the ceiling"
            )
        else:
            position = f"{_position(state)} is below the {limit}% ceiling"
        verdict["reason"] = format_refusal(
            "D02",
            f"the account reports severity {state.severity!r} on the window that "
            f"gates this {purpose}; {position}, so this hold comes from the "
            "account's raised severity rather than the configured ceiling"
            f"{_evidence_note(state)}",
        )
        return verdict
    if utilisation is None:
        verdict["reason"] = (
            "headroom is reported known but carries no utilisation, so there is "
            "nothing to compare against the ceiling"
        )
        return verdict
    verdict["reason"] = (
        f"{_position(state)} is below the {limit}% ceiling{_evidence_note(state)}"
    )
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


def published_document_path() -> Path:
    """Where the production callers look for the published headroom document.

    Delegates to the publisher's own resolver so the path a pre-flight reads is
    the path the publishing command writes: the environment override the
    observer and its readers share, then the document under the crew home a
    caller isolates through ``RECKON_HOME``, then the default location. Keeping
    a second rule here is what let the two drift -- the reader honours the crew
    home while the publisher did not, so an isolated run published to one file
    and read another. One resolver, named once in
    :func:`reckon.crew.paid_lanes.document_path`, is what holds them together.
    """
    from reckon.crew import paid_lanes

    return paid_lanes.document_path()


def _published_windows(
    document: Mapping[str, Any] | None,
    document_path: str | Path | None,
    moment: datetime,
) -> dict[str, Any]:
    """Read the published headroom document into one window reading per account.

    The document is the observer's one reconciled record of every metered
    account. Reading is delegated to :mod:`reckon.crew.paid_lanes`, which owns
    the document's shape. A caller that names the document has its reading
    merged with the caller's own recorded evidence per backend by
    :func:`_merge_windows`: the document speaks for a backend it carries a fresh
    reading for, and the recorded reading speaks everywhere else, so naming the
    document adds its reading rather than replacing what the caller measured.
    """
    from reckon.crew import paid_lanes

    resolved = document
    if resolved is None:
        resolved = paid_lanes.read_document(document_path)
    return paid_lanes.document_windows(resolved, moment=moment)


def _reaged(
    reading: window_reading.WindowReading, moment: datetime
) -> window_reading.WindowReading:
    """One published reading with every age recomputed against ``moment``.

    The document bakes each figure's ``age_seconds`` at composition time, so a
    figure read an hour after it was published would otherwise report the age it
    had when written rather than the age it has now -- a figure the document
    calls fresh while nothing has observed it for hours. The observation time
    travels with the figure and does not drift, so the age is re-derived from it
    against the read moment, at both the figure and the reading level, and the
    figure's own age is what a clock reports downstream.
    """
    figures = tuple(
        replace(
            figure,
            age_seconds=(moment - figure.observed_at).total_seconds(),
        )
        for figure in reading.figures
    )
    newest = max((figure.observed_at for figure in figures), default=None)
    return replace(
        reading,
        figures=figures,
        observed_at=newest if newest is not None else reading.observed_at,
        age_seconds=(None if newest is None else (moment - newest).total_seconds()),
    )


def _published_fresh_windows(
    document: Mapping[str, Any] | None,
    document_path: str | Path | None,
    moment: datetime,
) -> dict[str, window_reading.WindowReading]:
    """Published readings whose newest figure is still fresh, per backend.

    The document is one reconciled record, but its age is per backend: a reader
    that treated the whole document as stale would drop a backend it had just
    measured, and one that treated it fresh would trust a backend untouched for
    hours. So each backend is judged on its own newest observation against the
    document's staleness horizon, and only the fresh ones compete. The judgement
    is made on the recomputed age rather than the age the document baked at
    composition, so a figure published long ago and read now falls back rather
    than reading as fresh as the moment it was written. An absent or unreadable
    document resolves to none, which is what the per-backend fallback then
    supplies for.
    """
    from reckon.crew import paid_lanes

    published = _published_windows(document, document_path, moment)
    horizon = paid_lanes.DEFAULT_STALE_SECONDS
    fresh: dict[str, window_reading.WindowReading] = {}
    for name, reading in published.items():
        current = _reaged(reading, moment)
        age = current.age_seconds
        if age is None or age <= horizon:
            fresh[name] = current
    return fresh


def _merge_windows(
    recorded: Mapping[str, Any] | None,
    document: Mapping[str, Any] | None,
    document_path: str | Path | None,
    moment: datetime,
) -> tuple[dict[str, Any], dict[str, str]]:
    """One reading per backend: the document's fresh figure, else recorded.

    Running both sources through one merge is what lets the production callers,
    which always inject their recorded windows, still hear the published
    document without discarding what they measured. A backend the document
    covers freshly is taken from it; every other backend -- the document absent,
    unreadable, silent about it, or holding only a stale figure -- keeps the
    caller's own recorded reading. The returned source map names which spoke for
    each, so the report can say so rather than leave a reader to guess.
    """
    held: dict[str, Any] = dict(recorded) if isinstance(recorded, Mapping) else {}
    fresh = _published_fresh_windows(document, document_path, moment)
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name in sorted({*held, *fresh}):
        if name in fresh:
            merged[name] = fresh[name]
            sources[name] = WINDOW_SOURCE_DOCUMENT
        else:
            merged[name] = held[name]
            sources[name] = WINDOW_SOURCE_RECORDED
    return merged, sources


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
    windows: Mapping[str, Any] | None = None,
    ready: Iterable[Mapping[str, Any]] = (),
    document: Mapping[str, Any] | None = None,
    document_path: str | Path | None = None,
) -> dict[str, Any]:
    """Decide, per backend, whether a wave may open, and at what pace.

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

    ``windows`` and ``ready`` add the pace beside the hold: per declared budget
    group, both metered clocks with each reading's age, the derived allowance and
    the bar the stated ready set is judged against — see :func:`group_pace`. Both
    are optional, and a wave that names neither still gets the hold decision and
    a group block reading unknown, which is what absence of a signal means.

    ``windows`` is the caller's own reading. When the caller also names the
    published headroom document -- through ``document`` directly or through
    ``document_path`` -- the two are merged per backend: a backend the document
    carries a fresh reading for is paced from it, and every other backend keeps
    the caller's recorded reading. Nothing is read when the caller names no
    document, so the pace never depends on a machine-wide file, and a test
    calling here consults its own fixture or nothing. The report's
    ``window_sources`` names, per backend, which source supplied its figures. A
    caller naming neither a reading nor a document gets the hold decision and a
    group block reading unknown, which is what absence of a signal means.
    """
    moment = _now(now)
    window_sources: dict[str, str] = {}
    if document is not None or document_path is not None:
        windows, window_sources = _merge_windows(
            windows, document, document_path, moment
        )
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
            recorded=recorded.for_backend(name),
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
                        age_source="lane-probe",
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
    report["groups"] = group_pace(config, windows=windows, ready=ready, now=moment)
    if window_sources:
        # Name the source only where a figure exists to name one for. A report
        # with no reading at all -- a backend the document and the records both
        # missed -- gains no empty block, so a reader sees an addition only when
        # something was actually read, and no group grows a null source.
        for entry in report["groups"]:
            member = entry.get("member")
            if member is not None and member in window_sources:
                entry["source"] = window_sources[member]
        report["window_sources"] = window_sources
    report["summary"] = summary(report)
    return report


# The metered clocks a served stream carries, paired with the role each plays in
# a group's pace. The five-hour figure is the window that fills, so it is what
# the bar is drawn against; the seven-day figure is the week the allowance
# divides, so it is what the derivation reads.
CLOCK_FIVE_HOUR = "five_hour"
CLOCK_SEVEN_DAY = "seven_day"

# Whether a group's pace was read. ``OBSERVED`` carries a figure and the age of
# the observation behind it; ``UNKNOWN`` is the explicit absence of one, and is
# never a zero. Named rather than spelled inline because the difference is the
# whole point: an unread utilisation and a measured 0.0 must not look alike in
# the payload, since the first admits nothing and the second admits everything.
OBSERVED = "observed"
UNKNOWN = "unknown"

# Where one backend's pace figures came from. The published document speaks when
# it carries a fresh reading for that backend; otherwise the caller's own
# recorded evidence speaks. Named because the report has to say which, and a
# reader that could not tell the two apart would trust a stale document as
# though it were fresh, or ignore a fresh document as though it were absent.
WINDOW_SOURCE_DOCUMENT = "document"
WINDOW_SOURCE_RECORDED = "recorded"


def _window_value(source: object, *, moment: datetime) -> window_reading.WindowReading:
    """Resolve one injected window reading, reading a stream source if given.

    The pre-flight takes no view on where a reading comes from. A caller hands
    it one per backend, either already read — a
    :class:`~reckon.crew.window_reading.WindowReading` — or as a stream source
    the reader can open. A source that cannot be read yields the reader's own
    explicit unknown rather than raising.
    """
    if isinstance(source, window_reading.WindowReading):
        return source
    return window_reading.read_windows(source, now=moment)


def _clock(reading: window_reading.WindowReading, period: str) -> dict[str, Any]:
    """One metered clock as plain data, carrying its own observation age.

    The age is reported even when the reading is seconds old, so a reader never
    has to treat a missing age as evidence of a fresh one. An unknown clock
    reports ``None`` for both its figure and its age rather than a zero: absence
    of a signal is not a position, and a zero utilisation would read as an empty
    window and admit everything.
    """
    figure = reading.figure(period)
    if figure is None:
        return {
            "period": period,
            "state": UNKNOWN,
            "utilisation": None,
            "age_seconds": None,
            "observed_at": None,
            "resets_at": None,
        }
    return {
        "period": period,
        "state": OBSERVED,
        "utilisation": float(figure.utilisation),
        "age_seconds": (
            None if figure.age_seconds is None else float(figure.age_seconds)
        ),
        "observed_at": figure.observed_at.isoformat(),
        "resets_at": figure.resets_at,
    }


def _freshest_reading(
    members: Iterable[str],
    windows: Mapping[str, Any],
    *,
    moment: datetime,
) -> tuple[str, window_reading.WindowReading] | None:
    """Return a group's newest dated reading, and the member that supplied it.

    A group is one wallet, so it is read once: the member carrying the newest
    observation speaks for the group, and a member that supplied nothing or an
    undated reading does not compete. A figure that cannot be aged cannot be
    told from a current one.
    """
    freshest: tuple[datetime, str, window_reading.WindowReading] | None = None
    for member in members:
        source = windows.get(member)
        if source is None:
            continue
        reading = _window_value(source, moment=moment)
        if not reading.known or reading.observed_at is None:
            continue
        if freshest is None or reading.observed_at > freshest[0]:
            freshest = (reading.observed_at, member, reading)
    if freshest is None:
        return None
    return freshest[1], freshest[2]


def _elapsed_hours(clock: Mapping[str, Any], *, moment: datetime) -> float | None:
    """How far into the week a clock stands, or ``None`` if it cannot say.

    The remaining time comes from the window's own reset stamp, so the elapsed
    figure is measured from the same origin as the weekly clock the allowance
    divides. A clock with no readable reset cannot place itself in the week, and
    an invented elapsed time would move the allowance as much as a real one.
    """
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
        }
    )
    return allowance


def _group_allowance(
    group: str,
    clocks: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    moment: datetime,
) -> dict[str, Any]:
    """Derive a group's five-hour allowance from the week it has to last.

    The weekly clock supplies the fraction already spent and, through its own
    reset stamp, how far into the week the group stands; those two figures are
    enough for the derivation. The provider ceiling is left unread because a
    five-hour window's own capacity is not published as a share of the weekly
    budget, and a guessed ceiling would silently cap the allowance.
    """
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
    return pace_module.allowance_for_group(reading, config=config).as_dict()


def _group_bar(
    clocks: Mapping[str, Mapping[str, Any]],
    ready: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Judge a stated ready set against the group's five-hour fill.

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
    five = clocks[CLOCK_FIVE_HOUR]
    fill = five["utilisation"] if five["state"] == OBSERVED else None

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

    return {
        "window_fill": fill,
        "state": five["state"],
        "recommendations": recommendations,
        "admitted": admitted,
        "split": split,
        "held": held,
        "undecided": undecided,
    }


def group_pace(
    config: Mapping[str, Any],
    *,
    windows: Mapping[str, Any] | None = None,
    ready: Iterable[Mapping[str, Any]] = (),
    now: datetime | None = None,
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
    """
    moment = _now(now)
    readings = windows if isinstance(windows, Mapping) else {}
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
        report.append(
            {
                "group": group,
                "members": list(members),
                "member": member,
                "state": OBSERVED if freshest is not None else UNKNOWN,
                "clocks": clocks,
                "allowance": _group_allowance(group, clocks, config, moment=moment),
                "bar": _group_bar(clocks, nodes_by_group[group]),
            }
        )
    return report


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
    entry = next(
        item
        for item in group_pace(
            config,
            windows=readings,
            ready=[{"name": node, "group": group, "score": float(score)}],
            now=moment,
        )
        if item["group"] == group
    )
    row.update(
        {
            "group": group,
            "state": entry["state"],
            "source": WINDOW_SOURCE_RECORDED,
            "member": entry["member"],
            "clocks": entry["clocks"],
            # The bar's whole judgement of this one node: the fill it was drawn
            # against, the verdict, the bar itself, the margin, and whether the
            # window or prescription decided it. The group's other ready nodes
            # are not the dispatch's business — one dispatch is one node.
            "bar": entry["bar"]["recommendations"][0],
            "allowance": entry["allowance"],
            "reason": None,
        }
    )
    return row


# The window lengths a lane receipt names, mapped to the clock a group's pace
# reads. A receipt identifies its windows by length in minutes rather than by
# name, so this map is what lets a recorded receipt and a stream-carried event
# speak one vocabulary. A length the map does not name is skipped rather than
# guessed onto a clock: a window nobody can name is not a reading this reader
# may place.
WINDOW_MINUTES_CLOCK = {300: CLOCK_FIVE_HOUR, 10080: CLOCK_SEVEN_DAY}

# How many of a backend's newest runs are opened looking for a window-carrying
# stream. Most streams carry no window at all, so the newest run is not always
# the one that reported; the scan is bounded because each candidate is a whole
# file read and a run older than these has been superseded by anything they
# reported.
STREAM_SCAN_LIMIT = 3


def recorded_windows(
    project: str,
    config: Mapping[str, Any],
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
    records: Iterable[Mapping[str, Any]] | None = None,
    pointers: Iterable[Mapping[str, Any]] | None = None,
    rollouts: Callable[[str], object] | None = None,
) -> dict[str, window_reading.WindowReading]:
    """One window reading per member of a declared group, from recorded evidence.

    A metered backend's windows are recorded in one of two places and the
    dialect decides which. A codex lane writes them into its *receipt* —
    ``lane_receipt.quota_windows``, one row per length in minutes — while a
    claude lane reports them on the stream of the run that observed them, as a
    ``unifiedWindows`` event. Both carry an observation time, so both carry an
    age.

    A receipt is read from either home a run's record occupies. While a run is in
    flight its record is a pointer under the crew home, and on promotion it lands
    in the repository's committed ledger; reading only one would lose the freshest
    signal or the history behind it. The two homes are read as one set of
    candidates, so the newest dated receipt a member has, in either home, is the
    one that competes.

    The freshest dated reading wins, whichever source carried it: a stream that
    reported after the member's last receipt is the reading the group paces to,
    and a receipt newer than the stream is the reading instead. Only readings that
    resolved a clock compete, so a source that carried nothing never displaces one
    that did; the fork is over which figure is newer, never over which dialect
    spelled it.

    The run in flight has a third source, and it is the freshest of the three.
    A live pointer carries no ``lane_receipt`` because the harness harvests that
    receipt only when the run is promoted; meanwhile the session's own rollout
    holds the client's quota readings, which is what the lanes view reports from.
    So each member's newest live run is read by its ``session_id``, and its
    clocks compete on the same footing as the other two. The age is the run
    record's, since a rollout receipt carries its windows without a stamp of its
    own.

    Only members of a declared group are read. A lane declaring no wallet has no
    group whose pace it could inform, and reading its stream would spend a file
    read on a figure nothing consults.

    A backend nothing reached is absent from the returned mapping rather than
    mapped to an empty reading, so the group reports unknown exactly as it does
    when no window source is supplied at all.
    """
    moment = _now(now)
    members = {
        member
        for group_members in budget_group.declared_groups(config).values()
        for member in group_members
    }
    rows = ledger.runs(project, root) if records is None else list(records)
    if pointers is None:
        # The crew home holds every project's pointers, so the read is scoped
        # here rather than at the call: one project's pace is not informed by
        # another's runs, and a pointer is asked by project only when it is
        # this one's. Every source below reads this same list, so a foreign
        # run's receipt, stream and session are all excluded by the one filter.
        live = [
            record
            for record in crew.list_live()
            if str(record.get("project") or "") == project
        ]
    else:
        live = list(pointers)
    receipts: dict[str, tuple[datetime, Mapping[str, Any]]] = {}
    runs_by_backend: dict[str, list[tuple[str, str]]] = {}
    sessions: dict[str, tuple[str, str, datetime]] = {}
    for row in [*rows, *(record for record in live if isinstance(record, Mapping))]:
        if not isinstance(row, Mapping):
            continue
        name = _run_backend(row)
        if name not in members:
            continue
        run_id = str(row.get("run_id") or "").strip()
        if run_id:
            runs_by_backend.setdefault(name, []).append((_run_order(row), run_id))
        receipt = row.get("lane_receipt")
        if isinstance(receipt, Mapping):
            observed = _parse_stamp(receipt.get("observed_at"))
            if observed is not None:
                known = receipts.get(name)
                if known is None or observed > known[0]:
                    receipts[name] = (observed, receipt)
    for record in live:
        # Only a run still in flight is read for its rollout: a promoted run's
        # receipt is already committed, and its rollout is the same reading
        # recorded a second time.
        if not isinstance(record, Mapping):
            continue
        name = _run_backend(record)
        if name not in members:
            continue
        session_id = str(record.get("session_id") or "").strip()
        observed = _run_observed_at(record)
        if not session_id or observed is None:
            continue
        order = _run_order(record)
        known = sessions.get(name)
        if known is None or order > known[0]:
            sessions[name] = (order, session_id, observed)

    windows: dict[str, window_reading.WindowReading] = {}
    for name in sorted(members):
        candidates: list[window_reading.WindowReading] = []
        receipt = receipts.get(name)
        if receipt is not None:
            candidate = _receipt_reading(receipt[1], moment=moment)
            if candidate.known:
                candidates.append(candidate)
        session = sessions.get(name)
        if session is not None:
            candidate = _rollout_reading(
                _read_rollout(session[1], rollouts),
                observed_at=session[2],
                moment=moment,
            )
            if candidate.known:
                candidates.append(candidate)
        stream = _newest_stream_reading(runs_by_backend.get(name, ()), moment=moment)
        if stream is not None:
            candidates.append(stream)
        if candidates:
            windows[name] = max(candidates, key=_reading_stamp)
    return windows


def _reading_stamp(reading: window_reading.WindowReading) -> datetime:
    """A reading's observation time, for choosing the freshest of two.

    A reading that resolved a clock carries the time it was observed, and a
    reading that resolved none is never a candidate, so the fallback only keeps
    the comparison total: an undated reading sorts behind every dated one rather
    than ahead of it.
    """
    return reading.observed_at or datetime.min.replace(tzinfo=UTC)


def _run_backend(row: Mapping[str, Any]) -> str:
    """The configured backend a run record names, from either durable shape."""
    backend = str(row.get("backend") or "").strip()
    if backend:
        return backend
    agent = row.get("agent")
    if isinstance(agent, Mapping):
        return str(agent.get("backend") or "").strip()
    return ""


def _run_order(row: Mapping[str, Any]) -> str:
    """A run's recency as a sortable stamp, newest-last by string order.

    The stamps a record carries are UTC ISO text of one width, whether the record
    is a committed ledger row or a live pointer, so the string comparison and the
    instant comparison agree; a record carrying none of them sorts before every
    dated one rather than ahead of it.
    """
    return str(
        row.get("completed_at")
        or row.get("dispatched_at")
        or row.get("observed_at")
        or row.get("created_at")
        or ""
    )


# The durable stamps a run record carries, newest first.  A rollout receipt
# keys its quota windows without stamping them, so a reading taken from one is
# dated by the record the session was named by -- and every surface that prices
# such a receipt must read that date the same way, or two views of one run
# disagree about how old its figures are.
_RUN_STAMP_KEYS = (
    "observed_at",
    "completed_at",
    "terminal_at",
    "dispatched_at",
    "started_at",
    "created_at",
)


def run_observed_stamp(row: Mapping[str, Any]) -> str | None:
    """The closest durable stamp a run record carries to its own reading.

    A run record has two durable shapes -- a live pointer while the run is in
    flight and a committed ledger row once it is promoted -- and both carry the
    same stamp fields, so one reader serves either. The record's own budget
    block is the freshest observation when it holds one; otherwise the run's
    lifecycle stamps stand in, newest first.

    The text is returned rather than a parsed instant because a caller may hold
    it as text, and a value that does not parse is not a stamp: it is passed
    over for the next field rather than returned, so a malformed budget stamp
    cannot shadow a lifecycle stamp behind it. A record carrying no stamp
    supplies none, and a reading dated by such a record is undated.
    """
    block = row.get("budget")
    if isinstance(block, Mapping):
        observed = _parse_stamp(block.get("observed_at"))
        if observed is not None:
            return str(block["observed_at"])
    for key in _RUN_STAMP_KEYS:
        observed = _parse_stamp(row.get(key))
        if observed is not None:
            return str(row[key])
    return None


def _run_observed_at(row: Mapping[str, Any]) -> datetime | None:
    """The closest durable stamp a run record carries, as an instant.

    The text form is the shared reader; this is the instant form of the same
    answer, so a run's observation time is decided in one place and the two
    surfaces cannot drift on which field they read.
    """
    return _parse_stamp(run_observed_stamp(row))


def _read_rollout(session_id: str, reader: Callable[[str], object] | None) -> object:
    """One session's rollout receipt, through the reader the lanes view uses.

    The injected seam takes the session id alone, which is the same shape the
    lanes view injects, so a test supplies a reader interchangeable with the
    production one. Absent an injection the production reader is called, which
    answers an explicit unmeasured receipt for a session it cannot locate
    rather than raising.
    """
    if reader is not None:
        return reader(session_id)
    return rollout_module.read_rollout_receipt(session_id)


def _rollout_reading(
    receipt: object, *, observed_at: datetime, moment: datetime
) -> window_reading.WindowReading:
    """One session's rollout receipt as a reading, aged by its run's stamp.

    The receipt keys its quota windows by length in minutes and measures them in
    percent, so both are translated here exactly as a ``lane_receipt`` row's are:
    the length selects the clock and the percent becomes the fraction a group's
    pace reads. A receipt that is unmeasured, that keys nothing, or whose every
    window is unmeasured resolves no clock, and the periods that did resolve are
    still returned rather than the whole reading being dropped.
    """
    readings = getattr(receipt, "quota_readings", None)
    if not isinstance(readings, Mapping):
        return window_reading.WindowReading(
            reason="the session's rollout receipt carried no keyed quotas"
        )
    figures: list[window_reading.WindowFigure] = []
    for minutes, clock in sorted(WINDOW_MINUTES_CLOCK.items()):
        row = readings.get(minutes)
        used = getattr(row, "used_percent", None)
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        figures.append(
            window_reading.WindowFigure(
                period=clock,
                utilisation=float(used) / 100.0,
                observed_at=observed_at,
                age_seconds=(moment - observed_at).total_seconds(),
                resets_at=_reset_text(getattr(row, "resets_at", None)),
            )
        )
    if not figures:
        return window_reading.WindowReading(
            reason="the session's rollout receipt carried no usable quota window"
        )
    return window_reading.WindowReading(
        figures=tuple(figures),
        observed_at=observed_at,
        age_seconds=(moment - observed_at).total_seconds(),
    )


def _rate_limits_reading(
    rate_limits: Mapping[str, Any], *, observed_at: datetime, moment: datetime
) -> window_reading.WindowReading:
    """Translate one raw Codex ``rate_limits`` object into window figures.

    Rollout files retain the provider object directly, while the normal lanes
    reader receives a :class:`RolloutReceipt`.  Keeping this conversion beside
    that reader makes both surfaces use the same window mapping and percentage
    handling; a missing or malformed field remains an unknown window.
    """
    readings: dict[int, rollout_module.QuotaReading] = {}
    for name in ("primary", "secondary"):
        row = rate_limits.get(name)
        if not isinstance(row, Mapping):
            continue
        minutes = row.get("window_minutes")
        used = row.get("used_percent")
        if (
            isinstance(minutes, bool)
            or not isinstance(minutes, int)
            or minutes <= 0
            or isinstance(used, bool)
            or not isinstance(used, (int, float))
        ):
            continue
        readings[minutes] = rollout_module.QuotaReading(
            window_minutes=minutes,
            used_percent=used,
            resets_at=row.get(
                "resets_at", rollout_module.Unmeasured.NO_RATE_LIMIT_VALUE
            ),
        )
    return _rollout_reading(
        SimpleNamespace(quota_readings=readings),
        observed_at=observed_at,
        moment=moment,
    )


def _receipt_reading(
    receipt: Mapping[str, Any], *, moment: datetime
) -> window_reading.WindowReading:
    """One receipt's quota windows as a reading, aged against ``moment``.

    A receipt names its windows by length in minutes and measures them in
    percent, so both are translated here: the length selects the clock and the
    percent becomes the fraction a group's pace reads. A row whose length is
    unnamed, whose figure is not a number, or which carries no observation time
    contributes no clock, and the periods that did resolve are still returned
    rather than the whole reading being dropped — a receipt carrying only its
    weekly window is an honest reading with its five-hour clock unknown.
    """
    windows = receipt.get("quota_windows")
    fallback = _parse_stamp(receipt.get("observed_at"))
    figures: list[window_reading.WindowFigure] = []
    for row in windows if isinstance(windows, list) else ():
        if not isinstance(row, Mapping):
            continue
        period = WINDOW_MINUTES_CLOCK.get(row.get("window_minutes"))
        if period is None:
            continue
        used = row.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        observed = _parse_stamp(row.get("observed_at")) or fallback
        if observed is None:
            continue
        figures.append(
            window_reading.WindowFigure(
                period=period,
                utilisation=float(used) / 100.0,
                observed_at=observed,
                age_seconds=(moment - observed).total_seconds(),
                resets_at=_instant_text(row.get("resets_at")),
            )
        )
    if not figures:
        return window_reading.WindowReading(
            reason="the recorded receipt carried no usable quota window"
        )
    newest = max(figure.observed_at for figure in figures)
    return window_reading.WindowReading(
        figures=tuple(figures),
        observed_at=newest,
        age_seconds=(moment - newest).total_seconds(),
    )


def _newest_stream_reading(
    runs: Iterable[tuple[str, str]], *, moment: datetime
) -> window_reading.WindowReading | None:
    """The newest window-carrying stream among a backend's recent runs.

    A served run reports its windows on its own stream, and most streams carry
    none, so the newest run is not always the one that reported. A stream that
    cannot be read, or that carries no window, is skipped in favour of the next
    candidate; ``None`` means no recent stream reported a window at all.
    """
    for _order, run_id in sorted(runs, reverse=True)[:STREAM_SCAN_LIMIT]:
        path = crew.run_dir(run_id) / "stream.jsonl"
        if not path.is_file():
            continue
        reading = window_reading.read_windows(path, now=moment)
        if reading.known:
            return reading
    return None


def _reset_text(value: Any) -> str | None:
    """A reset time as ISO text, reading an unmeasured marker as no reset.

    The unmeasured marker is a string, so it would otherwise pass the text
    branch below and be reported as a reset time — a reason a figure is absent,
    printed in the place a deadline belongs.
    """
    if isinstance(value, rollout_module.Unmeasured):
        return None
    return _instant_text(value)


def _instant_text(value: Any) -> str | None:
    """A reset time as ISO text, from an epoch second or an ISO string.

    A receipt's ``resets_at`` is written as epoch seconds and a stream event's
    as epoch or text, so both spellings resolve here; anything else is no
    readable reset rather than an invented one.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    return value if isinstance(value, str) else None


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
    """Render budget position as the four-axis summary a wave gets.

    A hold is a decision the lead needs to see, and a clear lane with an
    unsustainable burn is an early warning the position-only fence cannot give.
    Both report on the same axes as a dispatch: what the position is, why, how
    the nodes remain recoverable, and when work may proceed.
    """
    verdicts = list(report.get("backends", ()))
    held = [verdict for verdict in verdicts if verdict.get("held")]
    clear = list(report.get("clear_backends") or ())
    reasons = "; ".join(
        f"{verdict.get('backend')}: {verdict.get('reason')}" for verdict in verdicts
    )
    if clear:
        how = (
            "no worktree created and no node failed; ready nodes on "
            f"{', '.join(clear)} dispatch normally"
        )
    else:
        how = "no worktree created and no node failed; every node stays ready"
    wait = report.get("resume_after_seconds")
    resume_at = report.get("resume_at")
    if not held:
        when = (
            "the wave may open now; compare projected exhaustion with the work "
            "horizon before dispatch"
        )
    elif resume_at and wait is not None:
        when = f"resets at {resume_at}, in {wait}s — resume the wave then"
    else:
        when = (
            "the backend reported no reset time, so the wave waits for a fresh "
            "observation rather than a clock"
        )
    return "\n".join(
        [
            (
                f"WHAT   budget preflight — {len(held)} backend(s) held, "
                f"{len(clear)} clear"
            ),
            f"WHY    {reasons}",
            f"HOW    {how}",
            f"WHEN   {when}",
        ]
    )
