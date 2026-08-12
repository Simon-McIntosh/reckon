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

**The pre-flight spends nothing.** It reads what earlier runs already recorded in
the ledger (:mod:`reckon.ledger`) and the pointers of runs still in flight. A
probe would spend the very resource it is measuring, and would do so most often
exactly when headroom is scarcest. A backend whose config asks for it may also
have its own account surface read — that runs no model and costs no worker
budget — but it is never the base case.

A hold is never destructive and never silent. It creates no worktree, fails no
node and cancels nothing; the nodes stay ready. It reports which backend, at what
utilisation, and when that resets — because a hold that looks like silence is
indistinguishable from a crashed orchestrator, and because the reset time is what
lets the wave resume without a human.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    resets_at: str | None = None
    seconds_until_reset: int | None = None
    threshold_status: str | None = None
    observed_at: str | None = None
    source: str = "none"
    expired: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the state as sorted JSON-ready data."""
        return {
            "backend": self.backend,
            "detail": self.detail,
            "expired": self.expired,
            "headroom": self.headroom,
            "observed_at": self.observed_at,
            "resets_at": self.resets_at,
            "seconds_until_reset": self.seconds_until_reset,
            "source": self.source,
            "threshold_status": self.threshold_status,
            "utilisation_pct": self.utilisation_pct,
        }


# ── Policy ──────────────────────────────────────────────────────────────────


def policy(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read the three thresholds that decide a hold out of flight config."""
    block = (config or {}).get("budget") or {}
    ceiling = block.get("utilisation_ceiling_pct")
    reserve = block.get("resume_reserve_pct")
    statuses = block.get("exhausted_statuses") or ()
    return {
        "utilisation_ceiling_pct": (
            UNSET_CEILING_PCT if ceiling is None else float(ceiling)
        ),
        "resume_reserve_pct": UNSET_RESERVE_PCT if reserve is None else float(reserve),
        "exhausted_statuses": [str(status) for status in statuses],
    }


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

    backend: str
    budget: dict[str, Any]
    observed_at: str
    when: datetime
    source: str


def _readings(project: str, *, root: str | Path | None = None) -> list[_Reading]:
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
        stamp = pointer.get("observed_at") or pointer.get("created_at")
        when = _parse_stamp(stamp)
        budget = pointer.get("budget")
        if when is None or not isinstance(budget, Mapping):
            continue
        found.append(
            _Reading(
                backend=str(pointer.get("backend") or ""),
                budget=dict(budget),
                observed_at=str(stamp),
                when=when,
                source="live-run",
            )
        )
    try:
        records = ledger.runs(project, root)
    except ledger.LedgerError:
        records = []
    for record in records:
        stamp = record.get("completed_at")
        when = _parse_stamp(stamp)
        budget = record.get("budget")
        if when is None or not isinstance(budget, Mapping):
            continue
        found.append(
            _Reading(
                backend=str((record.get("agent") or {}).get("backend") or ""),
                budget=dict(budget),
                observed_at=str(stamp),
                when=when,
                source="ledger",
            )
        )
    return [reading for reading in found if reading.backend]


def latest_recorded(
    project: str, *, root: str | Path | None = None
) -> dict[str, _Reading]:
    """Return the best recorded reading per backend, preferring a known one.

    A known measurement outranks any silence, however recent, because silence
    carries no information: letting a later unknown win would erase a recorded
    exhaustion and open exactly the wave this module holds. Between two readings
    of the same kind, the newer wins.
    """
    best: dict[str, _Reading] = {}
    for reading in _readings(project, root=root):
        current = best.get(reading.backend)
        if current is None:
            best[reading.backend] = reading
            continue
        known = reading.budget.get("headroom") == "known"
        current_known = current.budget.get("headroom") == "known"
        if known != current_known:
            if known:
                best[reading.backend] = reading
            continue
        if reading.when > current.when:
            best[reading.backend] = reading
    return best


# ── State ───────────────────────────────────────────────────────────────────


def state_for(
    backend_name: str,
    backend: Mapping[str, Any] | None = None,
    *,
    recorded: _Reading | None = None,
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
    if (backend or {}).get("budget_check"):
        block = _backends.probe_budget(
            backend_name=backend_name,
            backend=backend or {},
            runner=probe_runner,
        )
        if block.get("headroom") == "known":
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
    headroom = str(block.get("headroom") or "unknown")
    detail = str(block.get("detail") or "")
    if expired and headroom == "known":
        headroom = "unknown"
        detail = (
            f"the measured window reset at {resets_at}, so the recorded "
            "utilisation no longer describes it"
        )
    utilisation = block.get("utilisation_pct")
    return BudgetState(
        backend=backend_name,
        headroom=headroom,
        utilisation_pct=None if utilisation is None else float(utilisation),
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


# ── The decision ────────────────────────────────────────────────────────────


def decide(
    state: BudgetState,
    policy_block: Mapping[str, Any],
    *,
    purpose: str = "dispatch",
) -> dict[str, Any]:
    """Judge one backend: is the wave held, and on what evidence?

    Every branch names its reason in the verdict. A hold whose reason is not
    legible cannot be argued with, and the lead reading a held wave needs to see
    which backend, what utilisation, and against which threshold.
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
    if state.headroom != "known":
        verdict["reason"] = (
            "headroom is unknown, and absence of a signal is never read as "
            f"exhaustion — {state.detail or 'nothing recorded for this backend'}"
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


def backends_for_roles(config: Mapping[str, Any], roles: Iterable[str]) -> list[str]:
    """Resolve the backends a set of roles would dispatch to."""
    names: list[str] = []
    for role in roles:
        name, _settings = crew.resolve_role(config, role)
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
) -> dict[str, Any]:
    """Decide, per backend, whether a wave may open — without spending anything.

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

    recorded = latest_recorded(project, root=root)
    verdicts = []
    for name in names:
        settings = configured.get(name)
        state = state_for(
            name,
            settings if isinstance(settings, Mapping) else {},
            recorded=recorded.get(name),
            now=moment,
            probe_runner=probe_runner,
        )
        verdicts.append(decide(state, policy_block, purpose=purpose))

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
