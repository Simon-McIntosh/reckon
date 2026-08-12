"""The durable half of a run: a committed ledger owned by the repository.

Run state has two natures that must not share a home. While a worker is in
flight its record changes every few seconds and is worthless once the run ends,
so it lives in a pointer under reckon's config home that is never committed.
Once the run completes, the record is durable evidence of how a plan was
implemented — and plans are repo-local and committed, so their implementation
record is too.

The mechanism is already there: ``<config-home>/state/<project>`` is a symlink
into ``<repo>/docs/state/<project>``, which is how ``index.json`` is
server-written and git-committed. A ledger placed beside it inherits that
property for free, and inherits the same version-paired write, so two
orchestrators cannot clobber each other.

Two rules shape everything here.

**Nothing transient is committed, and nothing durable is only cached.** The
ledger holds the roster, completed runs and budget holds; it never holds a pid,
a worktree path or a phase. Holds sit beside runs because they have no worker,
worktree, commit or gate, and therefore must not enter worker-effort
measurements. :func:`append_run` refuses a second record for a run id
because a double promotion double-counts the very measurements
``effort-calibration`` will read.

**A measurement is only falsifiable if it is captured at the moment it is
knowable.** Wall-clock, the agent configuration that ran the node, the scoped
diff and whether the scope was widened mid-flight cannot be reconstructed after
the worktree is gone, so they are fields of the record rather than something a
later reader derives. A scope-changed run measures neither the estimate nor the
worker, so :func:`effort_report` excludes it and says how many it excluded
rather than averaging it in silently.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# Reached through the module rather than by importing its names: a reload
# rebinds the store's classes in place, and a captured exception class would
# then no longer match the one its own function raises.
from reckon import _store

# The slug the ledger occupies in a project's state directory. It sits beside
# index.json rather than inside it because project config and implementation
# history have different write cadences and different readers.
LEDGER_SLUG = "crew"

# A gate verdict is one of three so the field stays machine-readable for the
# calibration loops that read it. "not-run" is a real answer: a gate whose
# evidence could not be produced is a recorded negative, not a silent pass.
GATE_VERDICTS = ("passed", "failed", "not-run")

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Fields every completed record carries. Named here so a test can assert the
# calibration inputs exist rather than trusting each writer to remember them.
RECORD_FIELDS = (
    "run_id",
    "plan",
    "section",
    "node",
    "role",
    "member",
    "agent",
    "dispatched_at",
    "completed_at",
    "worker_seconds",
    "time_budget",
    "base_sha",
    "commits",
    "changed_lines",
    "tests_added",
    "gate",
    "outcome",
    "manifest_path",
    "scope_changed",
    "session_id",
    "budget",
)


class LedgerError(Exception):
    """A ledger read or write cannot proceed, and the message says why."""


def _utc_now() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ── Location and raw access ─────────────────────────────────────────────────


def ledger_path(project: str, root: str | Path | None = None) -> Path:
    """Return the ledger file for a project.

    Without ``root`` this resolves through the config-home state directory,
    which is symlinked into the registered checkout — the same route
    ``index.json`` takes. With ``root`` (a checkout's repo root) it resolves
    under that checkout, which is what lets a worker inside a worktree read and
    write its own ledger instead of the registered main one.
    """
    if not _SAFE_ID.fullmatch(str(project)):
        raise LedgerError(f"project {project!r} must match {_SAFE_ID.pattern}")
    return _store.state_path(project, LEDGER_SLUG, root)


def load(project: str, root: str | Path | None = None) -> tuple[dict[str, Any], int]:
    """Read the ledger, returning its data and current version.

    An absent ledger is the ordinary state of a project that has run no workers
    yet, so it reads as an empty roster at version 0 rather than an error.
    """
    path = ledger_path(project, root)
    data, version = _store._load_json_envelope(path)
    members = data.get("members")
    runs = data.get("runs")
    holds = data.get("holds")
    return (
        {
            "members": list(members) if isinstance(members, list) else [],
            "runs": list(runs) if isinstance(runs, list) else [],
            "holds": list(holds) if isinstance(holds, list) else [],
        },
        version,
    )


def write(
    project: str,
    data: Mapping[str, Any],
    expected_version: int,
    root: str | Path | None = None,
) -> int:
    """Write the ledger, refusing a stale expected version.

    Pairing every write with the version it read is what makes two concurrent
    orchestrators safe: the loser is told to re-read rather than silently
    overwriting a record it never saw.
    """
    path = ledger_path(project, root)
    payload = {
        "members": sorted(
            (dict(member) for member in data.get("members", [])),
            key=lambda member: str(member.get("id", "")),
        ),
        "runs": list(data.get("runs", [])),
        "holds": list(data.get("holds", [])),
    }
    try:
        return _store._write_json_envelope(
            path, project, LEDGER_SLUG, payload, expected_version
        )
    except _store.VersionConflict as exc:
        raise LedgerError(
            f"ledger for {project!r} moved from version {exc.expected} to "
            f"{exc.current} while this write was being prepared; re-read and retry"
        ) from exc


# ── The roster ──────────────────────────────────────────────────────────────


def members(project: str, root: str | Path | None = None) -> list[dict[str, Any]]:
    """Return the project's team roster."""
    return load(project, root)[0]["members"]


def member(
    project: str, member_id: str, root: str | Path | None = None
) -> dict[str, Any] | None:
    """Return one roster member, or None when the project has no such member."""
    for entry in members(project, root):
        if str(entry.get("id")) == str(member_id):
            return entry
    return None


def register_member(
    project: str,
    member_id: str,
    *,
    harness: str,
    role: str = "implement",
    session_id: str | None = None,
    root: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Add or update a roster member, returning the stored entry.

    A member registered with a null session is the normal case: the id it will
    reuse does not exist until its first run reports one, and
    :func:`capture_session` persists it then.
    """
    if not _SAFE_ID.fullmatch(str(member_id)):
        raise LedgerError(f"member id {member_id!r} must match {_SAFE_ID.pattern}")
    if not str(harness).strip():
        raise LedgerError("a member must name the harness it dispatches to")
    data, version = load(project, root)
    entry = {
        "id": str(member_id),
        "harness": str(harness),
        "role": str(role),
        "session_id": str(session_id) if session_id else None,
        "created": next(
            (
                str(existing.get("created"))
                for existing in data["members"]
                if str(existing.get("id")) == str(member_id) and existing.get("created")
            ),
            (now or _utc_now())[:10],
        ),
    }
    data["members"] = [
        existing
        for existing in data["members"]
        if str(existing.get("id")) != str(member_id)
    ] + [entry]
    write(project, data, version, root)
    return entry


def capture_session(
    project: str,
    member_id: str,
    session_id: str,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Persist a session id onto a member that does not have one yet.

    The first id wins. A later run that started a fresh session instead of
    resuming is reported rather than written over the top, because overwriting
    would silently retire the long-lived session every subsequent node and every
    escape-hatch resumption is meant to reach.
    """
    if not str(session_id).strip():
        raise LedgerError("cannot capture an empty session id")
    data, version = load(project, root)
    for entry in data["members"]:
        if str(entry.get("id")) != str(member_id):
            continue
        current = entry.get("session_id")
        if current and str(current) != str(session_id):
            return {
                "captured": False,
                "member": dict(entry),
                "detail": (
                    f"member {member_id!r} already reuses session {current!r}; "
                    f"run reported {session_id!r} and it was not written over the top"
                ),
            }
        if current:
            return {"captured": False, "member": dict(entry), "detail": "unchanged"}
        entry["session_id"] = str(session_id)
        write(project, data, version, root)
        return {"captured": True, "member": dict(entry), "detail": "first run"}
    return {
        "captured": False,
        "member": None,
        "detail": f"project {project!r} has no member {member_id!r}",
    }


# ── Completed runs ──────────────────────────────────────────────────────────


def build_record(
    *,
    run_id: str,
    plan: str,
    gate: str,
    node: str = "",
    section: str = "",
    role: str = "",
    member_id: str = "",
    agent: Mapping[str, Any] | None = None,
    dispatched_at: str = "",
    completed_at: str = "",
    worker_seconds: int | None = None,
    time_budget: str = "",
    base_sha: str = "",
    commits: Iterable[str] = (),
    changed_lines: Mapping[str, Any] | None = None,
    tests_added: int | None = None,
    outcome: str = "",
    manifest_path: str = "",
    scope_changed: bool = False,
    session_id: str | None = None,
    budget: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one completed-run record, refusing an unknown gate verdict."""
    verdict = str(gate).strip().lower()
    if verdict not in GATE_VERDICTS:
        raise LedgerError(
            f"gate verdict {gate!r} is not one of {', '.join(GATE_VERDICTS)}; "
            "a gate whose evidence could not be produced is 'not-run'"
        )
    return {
        "run_id": str(run_id),
        "plan": str(plan),
        "section": str(section),
        "node": str(node),
        "role": str(role),
        "member": str(member_id),
        "agent": dict(agent or {}),
        "dispatched_at": str(dispatched_at),
        "completed_at": str(completed_at) or _utc_now(),
        "worker_seconds": None if worker_seconds is None else int(worker_seconds),
        "time_budget": str(time_budget),
        "base_sha": str(base_sha),
        "commits": [str(sha) for sha in commits if str(sha).strip()],
        "changed_lines": dict(changed_lines or {}),
        "tests_added": None if tests_added is None else int(tests_added),
        "gate": verdict,
        "outcome": str(outcome),
        "manifest_path": str(manifest_path),
        "scope_changed": bool(scope_changed),
        "session_id": session_id,
        # Whatever headroom the backend reported while this run was in flight.
        # Carried here because the pointer that held it is deleted on promotion,
        # and a pre-flight that has to make a call to learn headroom spends the
        # very resource it is measuring — most often when it is scarcest.
        "budget": dict(budget or {}),
    }


def append_run(
    project: str,
    record: Mapping[str, Any],
    *,
    root: str | Path | None = None,
    attempts: int = 5,
) -> dict[str, Any]:
    """Append one completed run, retrying when a concurrent write intervenes.

    The retry is what makes two interleaved promotions both survive: the loser
    re-reads the ledger the winner just wrote and appends to that, so neither
    record is lost. A second record for the same run id is refused instead —
    that is not concurrency, it is a double promotion, and it would double-count
    the measurements the calibration loops read.
    """
    run_id = str(record.get("run_id") or "")
    if not run_id:
        raise LedgerError("a run record must carry a run_id")
    last: LedgerError | None = None
    for _attempt in range(max(1, attempts)):
        data, version = load(project, root)
        existing = next(
            (item for item in data["runs"] if str(item.get("run_id")) == run_id), None
        )
        if existing is not None:
            raise LedgerError(
                f"run {run_id!r} is already in the ledger for {project!r} "
                f"(completed {existing.get('completed_at')!r}); promoting it twice "
                "would double-count its measurements"
            )
        data["runs"] = data["runs"] + [dict(record)]
        try:
            new_version = write(project, data, version, root)
        except LedgerError as exc:
            last = exc
            continue
        return {
            "path": str(ledger_path(project, root)),
            "version": new_version,
            "run": dict(record),
        }
    raise LedgerError(
        f"ledger for {project!r} was rewritten on every attempt — {last}"
        if last
        else f"ledger for {project!r} could not be written"
    )


def runs(project: str, root: str | Path | None = None) -> list[dict[str, Any]]:
    """Return every completed run record, in promotion order."""
    return load(project, root)[0]["runs"]


# ── Budget holds ────────────────────────────────────────────────────────────


def _parse_utc(value: Any) -> datetime:
    """Parse one ledger timestamp and normalise it to an aware instant."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"hold check time {value!r} is not ISO-8601") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _hold_id(
    backend: str, checked_at: str, existing: Iterable[Mapping[str, Any]]
) -> str:
    """Return a readable id that remains unique within one ledger."""
    safe_backend = re.sub(r"[^A-Za-z0-9._-]+", "-", backend).strip("-") or "backend"
    stamp = re.sub(r"[^0-9]", "", checked_at)
    base = f"hold-{safe_backend}-{stamp}"
    used = {str(item.get("hold_id") or "") for item in existing}
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


def record_hold_checks(
    project: str,
    checks: Iterable[Mapping[str, Any]],
    *,
    checked_at: str,
    root: str | Path | None = None,
    attempts: int = 5,
) -> dict[str, Any]:
    """Open, deduplicate or close budget holds from one pre-flight.

    At most one hold is open per backend. Rechecking a backend while it remains
    held therefore preserves one record for the continuous hold window. The
    first later clear check closes that record and measures actual wall-clock
    elapsed time; the reported reset remains separate because a scheduled
    resumption can fire early or late.
    """
    moment = _parse_utc(checked_at)
    prepared = [dict(check) for check in checks]
    last: LedgerError | None = None
    for _attempt in range(max(1, attempts)):
        data, version = load(project, root)
        history = [dict(item) for item in data["holds"]]
        changed = False
        outcomes: list[dict[str, Any]] = []
        for check in prepared:
            backend = str(check.get("backend") or "").strip()
            if not backend:
                raise LedgerError("a hold check must name its backend")
            open_hold = next(
                (
                    item
                    for item in reversed(history)
                    if str(item.get("backend") or "") == backend
                    and not item.get("closed_at")
                ),
                None,
            )
            if bool(check.get("held")):
                if open_hold is not None:
                    outcomes.append({"action": "unchanged", "hold": dict(open_hold)})
                    continue
                state = check.get("state") or {}
                record = {
                    "hold_id": _hold_id(backend, checked_at, history),
                    "backend": backend,
                    "purpose": str(check.get("purpose") or "dispatch"),
                    "opened_at": checked_at,
                    "closed_at": None,
                    "held_seconds": None,
                    "utilisation_pct": state.get("utilisation_pct"),
                    "resets_at": state.get("resets_at"),
                    "effective_ceiling_pct": check.get("effective_ceiling_pct"),
                    "reason": str(check.get("reason") or ""),
                    "closed_by_purpose": None,
                    "resumption_fired": False,
                }
                history.append(record)
                outcomes.append({"action": "opened", "hold": dict(record)})
                changed = True
                continue
            if open_hold is None:
                continue
            opened = _parse_utc(open_hold.get("opened_at"))
            open_hold["closed_at"] = checked_at
            open_hold["held_seconds"] = max(0, int((moment - opened).total_seconds()))
            open_hold["closed_by_purpose"] = str(check.get("purpose") or "dispatch")
            open_hold["resumption_fired"] = (
                str(check.get("purpose") or "dispatch") == "resume"
            )
            outcomes.append({"action": "closed", "hold": dict(open_hold)})
            changed = True

        if not changed:
            return {
                "path": str(ledger_path(project, root)),
                "version": version,
                "outcomes": outcomes,
            }
        data["holds"] = history
        try:
            new_version = write(project, data, version, root)
        except LedgerError as exc:
            last = exc
            continue
        return {
            "path": str(ledger_path(project, root)),
            "version": new_version,
            "outcomes": outcomes,
        }
    raise LedgerError(
        f"ledger for {project!r} was rewritten on every hold-check attempt — {last}"
        if last
        else f"ledger for {project!r} could not record its hold check"
    )


def holds(project: str, root: str | Path | None = None) -> list[dict[str, Any]]:
    """Return every budget hold, in opening order."""
    return load(project, root)[0]["holds"]


# ── Measured effort ─────────────────────────────────────────────────────────


def declared_efforts(project: str, root: str | Path | None = None) -> dict[str, str]:
    """Map each live plan slug to the effort letter it declares.

    Read from the plans themselves rather than stored beside the runs, so the
    claim and the measurement cannot drift apart: re-sizing a plan changes what
    the next report compares against, with no second copy to update.
    """
    from reckon import _plan_html
    from reckon._store import _docs_dir_for_project
    from reckon.resources import iter_resources

    docs_dir = _docs_dir_for_project(project, root)
    if docs_dir is None:
        return {}
    efforts: dict[str, str] = {}
    for resource in iter_resources(
        docs_dir, project, include_archived=False, ignore_invalid=True
    ):
        if resource.type != "plan":
            continue
        effort = str(_plan_html.parse_meta(resource.path).get("effort") or "").strip()
        if effort:
            efforts[resource.slug] = effort
    return efforts


def _minutes(seconds: Any) -> float | None:
    try:
        return round(float(seconds) / 60.0, 1)
    except (TypeError, ValueError):
        return None


def effort_report(
    project: str,
    *,
    root: str | Path | None = None,
    declared: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Report measured worker-time per plan against that plan's declared effort.

    Effort is otherwise an asserted letter with no external referent. This makes
    it falsifiable by putting the measurement beside the claim and showing the
    spread, which is the quantity a single mean hides. Weighting and the unit
    migration belong to ``effort-calibration``; this only reports what was
    measured.
    """
    if declared is None:
        declared = declared_efforts(project, root)
    letters = {str(k): str(v) for k, v in declared.items()}
    by_plan: dict[str, dict[str, Any]] = {}
    excluded = 0
    for record in runs(project, root):
        plan = str(record.get("plan") or "")
        row = by_plan.setdefault(
            plan,
            {
                "plan": plan,
                "declared_effort": letters.get(plan, ""),
                "runs": 0,
                "excluded_scope_changed": 0,
                "measured_minutes": 0.0,
                "durations": [],
            },
        )
        if record.get("scope_changed"):
            row["excluded_scope_changed"] += 1
            excluded += 1
            continue
        minutes = _minutes(record.get("worker_seconds"))
        if minutes is None:
            continue
        row["runs"] += 1
        row["durations"].append(minutes)

    plans = []
    for plan in sorted(by_plan):
        row = by_plan.pop(plan)
        durations = sorted(row.pop("durations"))
        row["measured_minutes"] = round(sum(durations), 1)
        row["mean_minutes"] = (
            round(sum(durations) / len(durations), 1) if durations else None
        )
        row["min_minutes"] = durations[0] if durations else None
        row["max_minutes"] = durations[-1] if durations else None
        row["spread_minutes"] = (
            round(durations[-1] - durations[0], 1) if durations else None
        )
        plans.append(row)

    by_effort: dict[str, dict[str, Any]] = {}
    for row in plans:
        letter = row["declared_effort"] or "unknown"
        bucket = by_effort.setdefault(
            letter,
            {"effort": letter, "plans": 0, "runs": 0, "minutes": [], "means": []},
        )
        bucket["plans"] += 1
        bucket["runs"] += row["runs"]
        if row["mean_minutes"] is not None:
            bucket["means"].append(row["mean_minutes"])
        if row["min_minutes"] is not None:
            bucket["minutes"].extend([row["min_minutes"], row["max_minutes"]])
    buckets = []
    for letter in sorted(by_effort):
        bucket = by_effort[letter]
        minutes = sorted(bucket.pop("minutes"))
        means = bucket.pop("means")
        bucket["mean_minutes"] = round(sum(means) / len(means), 1) if means else None
        bucket["spread_minutes"] = (
            round(minutes[-1] - minutes[0], 1) if minutes else None
        )
        buckets.append(bucket)

    return {
        "plans": plans,
        "by_effort": buckets,
        "excluded_scope_changed": excluded,
        "note": (
            "A run whose scope was widened mid-flight measures neither the "
            "estimate nor the worker, so it is excluded from the measured "
            "columns and counted here instead."
        ),
    }


def summary(
    project: str,
    *,
    root: str | Path | None = None,
    declared: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Summarise runs and holds without mixing their measurements."""
    data, version = load(project, root)
    records = data["runs"]
    hold_records = data["holds"]
    gates: dict[str, int] = {}
    for record in records:
        verdict = str(record.get("gate") or "unknown")
        gates[verdict] = gates.get(verdict, 0) + 1
    sessions = sum(1 for entry in data["members"] if entry.get("session_id"))
    return {
        "version": version,
        "path": str(ledger_path(project, root)),
        "members": len(data["members"]),
        "members_with_session": sessions,
        "runs": len(records),
        "holds": len(hold_records),
        "open_holds": sum(1 for record in hold_records if not record.get("closed_at")),
        "total_held_seconds": sum(
            int(record.get("held_seconds") or 0) for record in hold_records
        ),
        "gates": dict(sorted(gates.items())),
        "plans": sorted({str(record.get("plan") or "") for record in records}),
        "effort": effort_report(project, root=root, declared=declared),
    }
