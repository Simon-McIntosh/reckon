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

import json
import random
import re
import sys
import time
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

# Substitutions attributable only to the backend swap a shadow exists to
# measure. Any other key in a lineage's "substituted" map means something
# besides the candidate backend changed, which confounds the comparison.
_SHADOW_BACKEND_ONLY_KEYS = frozenset({"backend", "launch", "model"})

FAILURE_CLASSIFICATIONS = (
    "work-rejected",
    "correct-refusal",
    "malformed-node",
    "infrastructure-failure",
    "pre-existing-failure",
    "negative-result",
)

USABLE_COMPLETION_SOURCES = frozenset({"terminal_event", "stream_mtime", "provided"})

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_MAX_RETRY_DELAY_SECONDS = 0.05

# Fields every completed record carries. Named here so a test can assert the
# calibration inputs exist rather than trusting each writer to remember them.
RECORD_FIELDS = (
    "run_id",
    "plan",
    "section",
    "node",
    "node_definition",
    "role",
    "spec_level",
    "member",
    "backend",
    "agent",
    "dispatched_at",
    "completed_at",
    "completed_at_source",
    "worker_seconds",
    "worker_seconds_source",
    "wall_seconds",
    "stalled",
    "time_budget",
    "base_sha",
    "commits",
    "changed_lines",
    "tests_added",
    "gate",
    "gate_check",
    "failure_classification",
    "outcome",
    "manifest_path",
    "scope_changed",
    "session_id",
    "budget",
    "lineage",
    "shadow_controlled",
    "shadow_patch",
    "unreconciled_override",
)


class LedgerError(Exception):
    """A ledger read or write cannot proceed, and the message says why."""


def normalize_section(value: Any) -> str:
    """Return the canonical spelling for a numbered plan section."""
    section = re.sub(r"\s+", " ", str(value or "").strip())
    match = re.fullmatch(r"(?:§\s*|#?s(?:ection)?\s*)?(\d+(?:\.\d+)*)", section, re.I)
    return f"§{match.group(1)}" if match else section


def shadow_controlled(lineage: Any) -> bool:
    """Return whether a shadow's recorded lineage reproduced its primary's configuration.

    The sole definition of the control predicate: read ``lineage.configuration``
    (written once, at shadow dispatch, by comparing the resolved primary and
    shadow agent settings) and check that every key it marks "substituted" is
    the candidate backend, its launch mode, or its model — the differences a
    backend swap necessarily produces. A substituted effort, sandbox, or time
    budget means the pair also varied on a dimension the shadow ladder is not
    measuring, so the comparison is confounded. Lineage carrying no recorded
    configuration cannot be verified either way and is not controlled.
    """
    if not isinstance(lineage, Mapping) or lineage.get("kind") != "shadow":
        return False
    configuration = lineage.get("configuration")
    if not isinstance(configuration, Mapping):
        return False
    substituted = configuration.get("substituted")
    if not isinstance(substituted, Mapping) or not substituted:
        return False
    return all(str(key) in _SHADOW_BACKEND_ONLY_KEYS for key in substituted)


def gate_check_missing_fields(gate_check: Any) -> list[str]:
    """Name which required fields a gate-check mapping is missing, if any."""
    check = gate_check if isinstance(gate_check, Mapping) else {}
    missing: list[str] = []
    if not str(check.get("command") or "").strip():
        missing.append("command")
    exit_status = check.get("exit_status")
    if (
        exit_status is None
        or isinstance(exit_status, bool)
        or not isinstance(exit_status, int)
    ):
        missing.append("exit_status")
    if (
        not str(check.get("log_path") or "").strip()
        and not str(check.get("log_digest") or "").strip()
    ):
        missing.append("log path or digest")
    return missing


def _normalized_gate_check(gate_check: Any) -> dict[str, Any] | None:
    """Return the stored shape of a gate check, or None when nothing was given."""
    if not isinstance(gate_check, Mapping):
        return None
    command = str(gate_check.get("command") or "").strip()
    exit_status = gate_check.get("exit_status")
    log_path = str(gate_check.get("log_path") or "").strip()
    log_digest = str(gate_check.get("log_digest") or "").strip()
    if not command and exit_status is None and not log_path and not log_digest:
        return None
    return {
        "command": command,
        "exit_status": exit_status,
        "log_path": log_path or None,
        "log_digest": log_digest or None,
    }


def evidence_records_for_plan(
    project: str,
    plan: str,
    root: str | Path | None = None,
) -> list[Path]:
    """Return typed evidence resources whose back-link names ``plan``.

    Both live evidence and frozen evidence are authoritative at closure. A
    project-qualified reference only matches when its qualifier names the
    project being written.
    """
    from reckon import _plan_html
    from reckon._schema import parse_plan_ref

    docs_dir = _store._docs_dir_for_project(project, root)
    if docs_dir is None:
        return []
    evidence_dir = docs_dir / "evidence"
    candidates = [
        *evidence_dir.glob("*.html"),
        *evidence_dir.glob("archive/*.html"),
    ]
    matches: list[Path] = []
    for path in sorted(candidates):
        record = _plan_html.parse_plan(path)
        if record.get("type") != "evidence":
            continue
        for raw_ref in record.get("evidence_for") or []:
            ref = parse_plan_ref(raw_ref)
            if ref is not None and not ref.is_external(project) and ref.slug == plan:
                matches.append(path)
                break
    return matches


def _utc_now() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _retry_backoff(attempt: int) -> None:
    """Yield to competing ledger writers with a small jittered delay."""
    ceiling = min(_MAX_RETRY_DELAY_SECONDS, 0.002 * (2**attempt))
    time.sleep(random.uniform(ceiling / 2, ceiling))


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


def session_for_model(entry: Mapping[str, Any], model: str) -> str | None:
    """Return the member session recorded for exactly one resolved model."""
    model_key = str(model).strip()
    if not model_key:
        return None
    sessions = entry.get("sessions")
    if isinstance(sessions, Mapping) and sessions.get(model_key):
        return str(sessions[model_key])
    if str(entry.get("session_model") or "") == model_key and entry.get("session_id"):
        return str(entry["session_id"])
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
    existing = next(
        (
            dict(item)
            for item in data["members"]
            if str(item.get("id")) == str(member_id)
        ),
        {},
    )
    entry = {
        "id": str(member_id),
        "harness": str(harness),
        "role": str(role),
        "session_id": str(session_id) if session_id else None,
        "session_model": (
            existing.get("session_model")
            if session_id and str(existing.get("session_id") or "") == str(session_id)
            else None
        ),
        "sessions": dict(existing.get("sessions") or {}),
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
    *,
    model: str = "",
) -> dict[str, Any]:
    """Persist a session id without replacing another model's session.

    The first id wins independently for every recorded model. Calls without a
    model retain the scalar compatibility contract used by older ledgers.
    """
    if not str(session_id).strip():
        raise LedgerError("cannot capture an empty session id")
    data, version = load(project, root)
    for entry in data["members"]:
        if str(entry.get("id")) != str(member_id):
            continue
        model_key = str(model).strip()
        if model_key:
            sessions = dict(entry.get("sessions") or {})
            current = sessions.get(model_key)
            if current and str(current) != str(session_id):
                return {
                    "captured": False,
                    "member": dict(entry),
                    "detail": (
                        f"member {member_id!r} already reuses session {current!r} "
                        f"for model {model_key!r}; run reported {session_id!r} and "
                        "it was not written over the top"
                    ),
                }
            if current:
                return {
                    "captured": False,
                    "member": dict(entry),
                    "detail": "unchanged",
                }
            sessions[model_key] = str(session_id)
            entry["sessions"] = sessions
            if not entry.get("session_id"):
                entry["session_id"] = str(session_id)
                entry["session_model"] = model_key
            elif str(entry.get("session_id")) == str(session_id) and not entry.get(
                "session_model"
            ):
                entry["session_model"] = model_key
            write(project, data, version, root)
            return {
                "captured": True,
                "member": dict(entry),
                "detail": f"first run for model {model_key!r}",
            }
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
    failure_classification: str = "",
    node: str = "",
    node_definition: Mapping[str, Any] | None = None,
    section: str = "",
    role: str = "",
    spec_level: str = "",
    member_id: str = "",
    backend: str = "",
    agent: Mapping[str, Any] | None = None,
    dispatched_at: str = "",
    completed_at: str = "",
    completed_at_source: str = "promotion_time",
    worker_seconds: int | None = None,
    worker_seconds_source: str = "",
    wall_seconds: int | None = None,
    stalled: bool = False,
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
    lineage: Mapping[str, Any] | None = None,
    shadow_patch: str = "",
    unreconciled_override: Mapping[str, Any] | None = None,
    gate_check: Mapping[str, Any] | None = None,
    require_gate_check: bool = False,
) -> dict[str, Any]:
    """Assemble one completed-run record, refusing an unknown gate verdict.

    ``gate_check`` is stored whenever given, independent of ``require_gate_check``
    — a caller that already has the evidence should record it even when not
    asking for enforcement. ``require_gate_check=True`` is what makes a passing
    verdict with no check unfalsifiable-by-construction: it is refused rather
    than stored, naming exactly which of command, exit status, and log
    path/digest is missing.
    """
    verdict = str(gate).strip().lower()
    if verdict not in GATE_VERDICTS:
        raise LedgerError(
            f"gate verdict {gate!r} is not one of {', '.join(GATE_VERDICTS)}; "
            "a gate whose evidence could not be produced is 'not-run'"
        )
    classification = str(failure_classification).strip().lower()
    if classification and classification not in FAILURE_CLASSIFICATIONS:
        raise LedgerError(
            f"failure classification {failure_classification!r} is not one of "
            f"{', '.join(FAILURE_CLASSIFICATIONS)}"
        )
    if require_gate_check and verdict == "passed":
        missing = gate_check_missing_fields(gate_check)
        if missing:
            raise LedgerError(
                "a passing gate requires the check that produced it; missing "
                + ", ".join(missing)
            )
    stored_lineage = None if lineage is None else dict(lineage)
    # Completion consults the same accessor capability derivation will read
    # later, but the verdict lands as its own field rather than mutating the
    # lineage a caller (e.g. shadow dispatch) already holds a copy of.
    is_shadow_lineage = (
        isinstance(stored_lineage, Mapping) and stored_lineage.get("kind") == "shadow"
    )
    stored_shadow_controlled = (
        shadow_controlled(stored_lineage) if is_shadow_lineage else None
    )
    return {
        "run_id": str(run_id),
        "plan": str(plan),
        "section": normalize_section(section),
        "node": str(node),
        "node_definition": (None if node_definition is None else dict(node_definition)),
        "role": str(role),
        "spec_level": str(spec_level),
        "member": str(member_id),
        # Routing is a property of the run, not only of the agent description.
        # Keeping it at the record level preserves attribution when a recovered
        # pointer carries no agent block.
        "backend": str(backend),
        "agent": dict(agent or {}),
        "dispatched_at": str(dispatched_at),
        "completed_at": str(completed_at) or _utc_now(),
        "completed_at_source": str(completed_at_source) or "promotion_time",
        "worker_seconds": None if worker_seconds is None else int(worker_seconds),
        "worker_seconds_source": str(worker_seconds_source)
        or ("provided" if worker_seconds is not None else "unavailable"),
        "wall_seconds": None if wall_seconds is None else int(wall_seconds),
        "stalled": bool(stalled),
        "time_budget": str(time_budget),
        "base_sha": str(base_sha),
        "commits": [str(sha) for sha in commits if str(sha).strip()],
        "changed_lines": None if changed_lines is None else dict(changed_lines),
        "tests_added": None if tests_added is None else int(tests_added),
        "gate": verdict,
        "gate_check": _normalized_gate_check(gate_check),
        "failure_classification": classification or None,
        "outcome": str(outcome),
        "manifest_path": str(manifest_path),
        "scope_changed": bool(scope_changed),
        "session_id": session_id,
        # Whatever headroom the backend reported while this run was in flight.
        # Carried here because the pointer that held it is deleted on promotion,
        # and a pre-flight that has to make a call to learn headroom spends the
        # very resource it is measuring — most often when it is scarcest.
        "budget": dict(budget or {}),
        "lineage": stored_lineage,
        "shadow_controlled": stored_shadow_controlled,
        "shadow_patch": str(shadow_patch),
        "unreconciled_override": (
            None if unreconciled_override is None else dict(unreconciled_override)
        ),
    }


def measurement_exclusion_reason(record: Mapping[str, Any]) -> str | None:
    """Name why a run cannot feed duration consumers, if it cannot."""
    if record.get("scope_changed"):
        return "scope_changed"
    if record.get("stalled"):
        return "stalled"
    if str(record.get("completed_at_source") or "") not in USABLE_COMPLETION_SOURCES:
        return "unusable_completion"
    return None


def per_run_budget(
    cumulative: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Label cumulative counters and expose their non-negative run deltas."""
    result = dict(cumulative)
    prior_budget = dict((previous or {}).get("budget") or {})
    current_tokens = dict(result.get("tokens") or {})
    prior_tokens = dict(prior_budget.get("tokens") or {})
    measured_tokens: dict[str, Any] = {}
    for name, value in current_tokens.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            measured_tokens[name] = value
            continue
        cumulative_name = f"{name}_cumulative"
        prior_value = prior_tokens.get(cumulative_name, prior_tokens.get(name, 0))
        if not isinstance(prior_value, (int, float)) or isinstance(prior_value, bool):
            prior_value = 0
        measured_tokens[name] = max(0, value - prior_value)
        measured_tokens[cumulative_name] = value
    if current_tokens:
        result["tokens"] = measured_tokens

    cost = result.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        prior_cost = prior_budget.get(
            "cost_usd_cumulative", prior_budget.get("cost_usd", 0)
        )
        if not isinstance(prior_cost, (int, float)) or isinstance(prior_cost, bool):
            prior_cost = 0
        result["cost_usd"] = max(0.0, cost - prior_cost)
        result["cost_usd_cumulative"] = cost
    return result


def append_run(
    project: str,
    record: Mapping[str, Any],
    *,
    root: str | Path | None = None,
    attempts: int = 12,
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
            _retry_backoff(_attempt)
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


def runs(
    project: str,
    root: str | Path | None = None,
    *,
    plan: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return selected completed runs in promotion order.

    ``since`` is an inclusive completion-time boundary. Records without a
    usable completion timestamp cannot satisfy that filter. ``limit`` selects
    the most recently promoted matching records while preserving their stored
    order.
    """
    records, _version = read_records(
        project,
        root,
        plan=plan,
        since=since,
        limit=limit,
    )
    return records


def read_records(
    project: str,
    root: str | Path | None = None,
    *,
    plan: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Read selected completed runs and their ledger version once."""
    data, version = load(project, root)
    records = list(data["runs"])
    selected_plan = str(plan or "").strip()
    if selected_plan:
        records = [
            record
            for record in records
            if str(record.get("plan") or "") == selected_plan
        ]
    if since is not None:
        boundary = _parse_timestamp(since, "since")
        filtered: list[dict[str, Any]] = []
        for record in records:
            completed_at = record.get("completed_at")
            if not completed_at:
                continue
            try:
                completion = _parse_timestamp(completed_at, "completed_at")
            except LedgerError:
                continue
            if completion >= boundary:
                filtered.append(record)
        records = filtered
    if limit is not None:
        if isinstance(limit, bool) or limit < 1:
            raise LedgerError("record limit must be a positive integer")
        records = records[-limit:]
    return records, version


def _resume_stream_order(path: Path) -> tuple[int, str]:
    """Order numbered resume streams by turn rather than filename text."""
    match = re.fullmatch(r"resume-(\d+)\.jsonl", path.name)
    return (int(match.group(1)), path.name) if match else (sys.maxsize, path.name)


def _run_streams(run_id: str, streams_root: Path) -> list[Path]:
    """Return every surviving stream for one run in stable path order."""

    directory = streams_root / run_id
    resumes = sorted(directory.glob("resume-*.jsonl"), key=_resume_stream_order)
    candidates = [directory / "stream.jsonl", *resumes]
    return [path for path in candidates if path.is_file()]


def _event_completion(paths: Iterable[Path]) -> str | None:
    """Return the newest aware event timestamp across surviving streams."""

    timestamps: list[tuple[datetime, str]] = []
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                timestamp = event.get("timestamp")
                if not isinstance(timestamp, str) or not timestamp.strip():
                    continue
                try:
                    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed.tzinfo is not None:
                    timestamps.append((parsed, timestamp))
    return max(timestamps, key=lambda item: item[0])[1] if timestamps else None


def _stream_completion(paths: list[Path]) -> tuple[str, str]:
    """Resolve completion from event timestamps, then the newest stream mtime."""

    event_time = _event_completion(paths)
    if event_time:
        return event_time, "terminal_event"
    newest = max(path.stat().st_mtime for path in paths)
    completed = (
        datetime.fromtimestamp(newest, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return completed, "stream_mtime"


def _worker_seconds(dispatched_at: Any, completed_at: Any) -> int | None:
    """Return elapsed seconds, treating timestamps without an offset as UTC."""

    try:
        start = datetime.fromisoformat(str(dispatched_at).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if finish.tzinfo is None:
        finish = finish.replace(tzinfo=timezone.utc)
    return max(0, int((finish - start).total_seconds()))


def repair_completion(
    project: str,
    *,
    root: str | Path | None = None,
    write_changes: bool = False,
    streams_root: str | Path | None = None,
) -> dict[str, Any]:
    """Re-derive historical completion measurements from surviving streams.

    Reporting is the default.  Persistence requires ``write_changes=True``;
    records with no surviving stream are reported as unusable and never
    rewritten, because promotion time is not a worker completion measurement.
    """

    data, version = load(project, root)
    stream_base = (
        Path(streams_root)
        if streams_root is not None
        else _store._config_home() / "crew" / "runs"
    )
    repaired_runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    changes = 0
    unusable = 0
    unchanged = 0
    for raw_record in data["runs"]:
        record = dict(raw_record)
        run_id = str(record.get("run_id") or "")
        paths = _run_streams(run_id, stream_base)
        if not paths:
            unusable += 1
            rows.append(
                {
                    "run_id": run_id,
                    "action": "unusable",
                    "calibration_usable": False,
                    "completion_source": None,
                    "detail": "no surviving stream file; record left unchanged",
                }
            )
            repaired_runs.append(record)
            continue

        completed_at, source = _stream_completion(paths)
        seconds = _worker_seconds(record.get("dispatched_at"), completed_at)
        replacement = {
            "completed_at": completed_at,
            "completed_at_source": source,
            "worker_seconds": seconds,
        }
        changed_fields = {
            field: {"before": record.get(field), "after": value}
            for field, value in replacement.items()
            if record.get(field) != value
        }
        action = "updated" if changed_fields else "unchanged"
        if changed_fields:
            changes += 1
            record.update(replacement)
        else:
            unchanged += 1
        rows.append(
            {
                "run_id": run_id,
                "action": action,
                "calibration_usable": True,
                "completion_source": source,
                "completed_at": completed_at,
                "worker_seconds": seconds,
                "changed_fields": changed_fields,
                "stream_files": [str(path) for path in paths],
            }
        )
        repaired_runs.append(record)

    new_version = version
    written = False
    if write_changes and changes:
        repaired = {**data, "runs": repaired_runs}
        new_version = write(project, repaired, version, root)
        written = True
    return {
        "project": project,
        "path": str(ledger_path(project, root)),
        "write_requested": bool(write_changes),
        "written": written,
        "version_before": version,
        "version_after": new_version,
        "records": len(rows),
        "updated": changes,
        "unchanged": unchanged,
        "unusable": unusable,
        "rows": rows,
    }


# ── Budget holds ────────────────────────────────────────────────────────────


def _parse_timestamp(value: Any, label: str) -> datetime:
    """Parse one labelled ledger timestamp as an aware instant."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"{label} {value!r} is not ISO-8601") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_utc(value: Any) -> datetime:
    """Parse one hold-check timestamp and normalise it to an aware instant."""
    return _parse_timestamp(value, "hold check time")


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
    attempts: int = 12,
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
            open_hold["resumption_fired"] = bool(check.get("resumption_fired"))
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
            _retry_backoff(_attempt)
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
    excluded_scope_changed = 0
    excluded_stalled = 0
    excluded_unusable_completion = 0
    for record in runs(project, root):
        plan = str(record.get("plan") or "")
        row = by_plan.setdefault(
            plan,
            {
                "plan": plan,
                "declared_effort": letters.get(plan, ""),
                "runs": 0,
                "excluded_scope_changed": 0,
                "excluded_stalled": 0,
                "excluded_unusable_completion": 0,
                "measured_minutes": 0.0,
                "durations": [],
            },
        )
        exclusion = measurement_exclusion_reason(record)
        if exclusion:
            row[f"excluded_{exclusion}"] += 1
            if exclusion == "scope_changed":
                excluded_scope_changed += 1
            elif exclusion == "stalled":
                excluded_stalled += 1
            else:
                excluded_unusable_completion += 1
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
        "excluded": {
            "scope_changed": excluded_scope_changed,
            "stalled": excluded_stalled,
            "unusable_completion": excluded_unusable_completion,
        },
        "excluded_scope_changed": excluded_scope_changed,
        "excluded_stalled": excluded_stalled,
        "excluded_unusable_completion": excluded_unusable_completion,
        "note": (
            "A run whose scope was widened mid-flight measures neither the "
            "estimate nor the worker, a stalled run measures idle wall time, "
            "and a run completed at promotion time has no surviving stream "
            "boundary. All are excluded from the "
            "measured columns and counted separately."
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
    gates: dict[str, dict[str, int]] = {"live": {}, "shadow": {}}
    worker_gate = {
        "passed": 0,
        "work_rejected": 0,
        "pass_rate": None,
        "excluded": {classification: 0 for classification in FAILURE_CLASSIFICATIONS[1:]},
        "unclassified": 0,
    }
    run_kinds = {"live": 0, "shadow": 0}
    for record in records:
        lineage = record.get("lineage")
        kind = (
            "shadow"
            if isinstance(lineage, Mapping) and lineage.get("kind") == "shadow"
            else "live"
        )
        run_kinds[kind] += 1
        verdict = str(record.get("gate") or "unknown")
        gates[kind][verdict] = gates[kind].get(verdict, 0) + 1
        if kind != "live":
            continue
        if verdict == "passed":
            worker_gate["passed"] += 1
        elif verdict == "failed":
            classification = str(record.get("failure_classification") or "")
            if classification == "work-rejected":
                worker_gate["work_rejected"] += 1
            elif classification in worker_gate["excluded"]:
                worker_gate["excluded"][classification] += 1
            else:
                worker_gate["unclassified"] += 1
    denominator = worker_gate["passed"] + worker_gate["work_rejected"]
    worker_gate["pass_rate"] = (
        round(worker_gate["passed"] / denominator, 4) if denominator else None
    )
    for classification, count in worker_gate["excluded"].items():
        worker_gate[f"excluded_{classification.replace('-', '_')}"] = count
    sessions = sum(
        1
        for entry in data["members"]
        if entry.get("session_id") or entry.get("sessions")
    )
    return {
        "version": version,
        "path": str(ledger_path(project, root)),
        "members": len(data["members"]),
        "members_with_session": sessions,
        "runs": len(records),
        "run_kinds": run_kinds,
        "holds": len(hold_records),
        "open_holds": sum(1 for record in hold_records if not record.get("closed_at")),
        "total_held_seconds": sum(
            int(record.get("held_seconds") or 0) for record in hold_records
        ),
        "gates": {kind: dict(sorted(counts.items())) for kind, counts in gates.items()},
        "worker_gate": worker_gate,
        "plans": sorted({str(record.get("plan") or "") for record in records}),
        "effort": effort_report(project, root=root, declared=declared),
    }
