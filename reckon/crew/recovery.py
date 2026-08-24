from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from reckon.crew.node import CrewError, LOG_STALE_AFTER_SECONDS, _TERMINAL_RUN_PHASES, parse_duration
from reckon.crew.reports import parse_manifest
from reckon.crew.routing import _signal_process_group
from reckon.crew.runs import _manifest_freshness, _utc_now, list_live, process_alive

# ── Recovery: what an interrupted orchestrator left behind ───────────────────

# What a live pointer can be once nobody is watching it. Worker-reported
# blocked and failed outcomes remain distinct so neither can be mistaken for a
# completed delivery that is eligible for promotion.
RECOVERY_CLASSES = (
    "running",
    "stopped",
    "completed_unpromoted",
    "blocked",
    "failed",
    "abandoned",
)

def _budget_timing(
    record: Mapping[str, Any], *, now_seconds: float | None = None
) -> dict[str, Any]:
    """Measure one live run against its declared allowance without mutating it."""
    node = record.get("node") or {}
    try:
        budget_seconds = parse_duration(str(node.get("time_budget") or ""))
        started = datetime.fromisoformat(
            str(record.get("created_at") or "").replace("Z", "+00:00")
        )
    except (CrewError, TypeError, ValueError):
        return {
            "budget_seconds": None,
            "elapsed_seconds": None,
            "budget_overrun": False,
            "budget_overrun_seconds": 0,
        }
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    elapsed = max(0, int(moment - started.timestamp()))
    overrun = max(0, elapsed - budget_seconds)
    return {
        "budget_seconds": budget_seconds,
        "elapsed_seconds": elapsed,
        "budget_overrun": overrun > 0,
        "budget_overrun_seconds": overrun,
    }


def _apply_budget_watchdog(
    record: dict[str, Any], config: Mapping[str, Any] | None
) -> None:
    """Record deadline posture and optionally stop an over-grace CLI worker."""
    timing = _budget_timing(record)
    record.update(timing)
    fences = (config or {}).get("fences") or {}
    if not fences.get("enforce_budget_watchdog"):
        return
    budget_seconds = timing["budget_seconds"]
    elapsed_seconds = timing["elapsed_seconds"]
    try:
        grace = float(fences.get("budget_grace_multiple", 1.0))
    except (TypeError, ValueError):
        return
    if (
        budget_seconds is None
        or elapsed_seconds is None
        or elapsed_seconds <= budget_seconds * grace
        or record.get("launch") != "cli"
        or record.get("phase") in _TERMINAL_RUN_PHASES
        or record.get("process_alive") is not True
    ):
        return
    pid = record.get("pid")
    try:
        _signal_process_group(int(pid), record.get("pid_start_time"))
    except (
        CrewError,
        ProcessLookupError,
        PermissionError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        record["watchdog_detail"] = f"budget watchdog could not stop pid {pid}: {exc}"
        return
    record["phase"] = "stopped"
    record["stopped_at"] = _utc_now()
    record["watchdog_enforced"] = True
    record["detail"] = (
        f"budget watchdog stopped pid {pid} after {elapsed_seconds}s "
        f"against {budget_seconds}s with {grace:g}x grace"
    )


def classify_pointer(
    record: Mapping[str, Any],
    *,
    stale_after_seconds: int = LOG_STALE_AFTER_SECONDS,
    now_seconds: float | None = None,
) -> dict[str, Any]:
    """Classify one live pointer, without touching it.

    Pure and read-only, so the same judgement serves an MCP read and
    :func:`recover`. Liveness comes from the process table and delivery from the
    manifest's status, because a terminal stream event only says the worker's
    turn ended. It does not say the node completed successfully.
    """
    run_id = str(record.get("run_id") or "")
    phase = str(record.get("phase") or "")
    manifest = Path(str(record.get("manifest_path") or ""))
    manifest_file_present, manifest_present = _manifest_freshness(record)
    manifest_data: dict[str, Any] = {}
    manifest_error = ""
    if manifest_present:
        try:
            manifest_data = parse_manifest(manifest.read_text())
        except OSError as exc:
            manifest_error = str(exc)
    manifest_status = str(manifest_data.get("status") or "").strip().lower()
    manifest_commits = list(manifest_data.get("commits") or [])
    manifest_blockers = list(manifest_data.get("blockers") or [])
    needs_help = manifest_data.get("needs_help")
    alive = (
        process_alive(record.get("pid"))
        if record.get("pid")
        else record.get("process_alive")
    )
    log = Path(str(record.get("log_path") or ""))
    age = None
    if log.is_file():
        age = max(0, int(_utc_seconds() - log.stat().st_mtime))
    terminal = phase in ("complete", "failed")
    terminal_at = None
    terminal_age_seconds = None
    if manifest_status in {"complete", "blocked", "failed"}:
        terminal_seconds = manifest.stat().st_mtime
        moment = _utc_seconds() if now_seconds is None else float(now_seconds)
        terminal_at = (
            datetime.fromtimestamp(terminal_seconds, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        terminal_age_seconds = max(0, int(moment - terminal_seconds))

    if manifest_status == "complete":
        classification = "completed_unpromoted"
        detail = (
            "the worker manifest reports completion and the run is still a "
            "pointer; promoting it moves the delivered record into the repository ledger"
        )
        action = f"reckon crew complete --run {run_id} --gate <verdict>"
        action += "".join(f" --commit {commit}" for commit in manifest_commits)
    elif manifest_status == "blocked":
        classification = "blocked"
        blocker = "; ".join(manifest_blockers) or "the manifest reports a blocker"
        detail = f"the worker manifest reports blocked: {blocker}"
        if isinstance(needs_help, Mapping) and needs_help.get("complete"):
            action = f"reckon crew resume --run {run_id} --advice <answer>"
        else:
            action = f"read {manifest}; resolve the blocker before resuming the run"
    elif manifest_status == "failed":
        classification = "failed"
        failure = "; ".join(manifest_blockers) or "the worker manifest reports failure"
        detail = f"the worker manifest reports failed: {failure}"
        action = (
            f"read {manifest} and launch log {record.get('stderr_path')}; "
            "repair or redispatch the run"
        )
    elif phase == "stopped":
        classification = "stopped"
        detail = "the run was intentionally stopped"
        action = (
            f"inspect the worktree at {record.get('worktree')} and discard when safe"
        )
    elif terminal:
        classification = "abandoned"
        if not manifest_present:
            delivery = "no manifest was delivered"
        elif manifest_error:
            delivery = f"the manifest could not be read: {manifest_error}"
        else:
            delivery = f"the manifest status {manifest_status!r} is not usable"
        detail = (
            f"the stored phase is terminal but {delivery}; nothing is eligible "
            "for promotion"
        )
        action = (
            f"read launch log {record.get('stderr_path')}; inspect the worktree at "
            f"{record.get('worktree')} and redispatch if needed"
        )
    elif alive is True:
        classification = "running"
        detail = "the process is alive"
        action = f"reckon crew observe --run {run_id}"
    elif alive is False:
        classification = "abandoned"
        detail = (
            "the process is gone without a complete manifest; nothing is eligible "
            "for promotion"
        )
        action = (
            f"read launch log {record.get('stderr_path')}; the worktree at "
            f"{record.get('worktree')} is left in place for review and is never "
            "force-removed"
        )
    else:
        classification = "running"
        detail = (
            "an in-harness run: liveness belongs to the calling harness, so it "
            "is reported as running until a manifest appears"
        )
        action = f"reckon crew observe --run {run_id}"

    timing = _budget_timing(record, now_seconds=now_seconds)
    return {
        "run_id": run_id,
        "project": record.get("project"),
        "plan": (record.get("node") or {}).get("plan"),
        "node": (record.get("node") or {}).get("id"),
        "classification": classification,
        "phase": phase,
        "process_alive": alive,
        "manifest_present": manifest_present,
        "manifest_file_present": manifest_file_present,
        "manifest_fresh": manifest_present,
        "manifest_path": str(manifest) if str(manifest) != "." else "",
        "manifest_status": manifest_status or None,
        "manifest_commits": manifest_commits,
        "terminal_at": terminal_at,
        "terminal_age_seconds": terminal_age_seconds,
        "log_age_seconds": age,
        "log_fresh": None if age is None else age <= stale_after_seconds,
        **timing,
        "worktree": record.get("worktree"),
        "detail": detail,
        "next_action": action,
    }


def overdue_unreconciled_runs(
    *,
    project: str,
    grace: str,
    now_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Return actionable terminal pointers older than the configured grace."""
    if not grace:
        return []
    grace_seconds = parse_duration(grace)
    rows = []
    for pointer in list_live(project=project):
        row = classify_pointer(pointer, now_seconds=now_seconds)
        age = row.get("terminal_age_seconds")
        if (
            row["classification"] in {"completed_unpromoted", "blocked"}
            and isinstance(age, int)
            and age > grace_seconds
        ):
            rows.append(row)
    return rows


def _utc_seconds() -> float:
    """Current time as epoch seconds, matching a file mtime's clock."""
    return datetime.now(tz=timezone.utc).timestamp()


def recover(
    *,
    project: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify every live pointer, repairing the record and nothing else.

    Each pointer is re-observed first, so the classification rests on the
    current stream and process table rather than on whatever the last writer
    believed. What gets repaired is the *record*: no worktree is removed, no
    process is signalled, and no run is promoted on this command's initiative —
    a completed-but-unpromoted run is reported with its manifest path so the
    orchestrator can promote it deliberately.
    """
    from reckon.crew.dispatch import observe

    reports = []
    for pointer in list_live():
        if project and str(pointer.get("project") or "") != project:
            continue
        run_id = str(pointer.get("run_id") or "")
        observed: Mapping[str, Any] = pointer
        unreadable = ""
        if run_id:
            try:
                observed = observe(run_id, config=config)
            except CrewError as exc:
                unreadable = str(exc)
        report = classify_pointer(observed)
        if unreadable:
            report["detail"] = f"{report['detail']} (stream unreadable — {unreadable})"
        reports.append(report)
    counts = {
        name: sum(1 for item in reports if item["classification"] == name)
        for name in ("running", "completed_unpromoted", "abandoned")
    }
    for name in ("stopped", "blocked", "failed"):
        count = sum(1 for item in reports if item["classification"] == name)
        if count:
            counts[name] = count
    return {"runs": reports, "counts": counts, "classes": list(RECOVERY_CLASSES)}
