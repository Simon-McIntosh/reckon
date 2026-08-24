from __future__ import annotations

import fcntl
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from reckon.crew.node import (
    DEFAULT_WATCH_STALL_WINDOW,
    CrewError,
    LOG_STALE_AFTER_SECONDS,
    _TERMINAL_RUN_PHASES,
    parse_duration,
)
from reckon.crew.reports import parse_manifest
from reckon.crew.routing import _signal_process_group
from reckon.crew.runs import (
    _manifest_freshness,
    _project_watch_claim,
    _read_watch_record,
    _stream_quiet_seconds,
    _utc_now,
    _write_watch_record,
    list_live,
    process_alive,
    watch_lock_path,
)

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


def unwatch(project: str) -> dict[str, Any]:
    """Stop the registered watcher for one project and release its claim."""
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            watcher = _read_watch_record(handle)
            registered_project = str(watcher.get("project") or "")
            if registered_project != project:
                raise CrewError(
                    f"refusing to stop watcher for project {project!r}: "
                    f"the locked registration names {registered_project!r}"
                )
            try:
                pid = int(watcher.get("pid"))
            except (TypeError, ValueError) as exc:
                raise CrewError(
                    f"refusing to stop watcher for project {project!r}: "
                    "the locked registration has no valid pid"
                ) from exc

            try:
                _signal_process_group(pid, watcher.get("pid_start_time"))
            except ProcessLookupError:
                stopped = False
                reason = "watcher-exited"
                detail = (
                    f"watcher pid {pid} exited before it could be signalled; "
                    "its registration was released"
                )
            else:
                stopped = True
                reason = "stopped"
                detail = f"stopped registered watcher pid {pid}"

            # The watcher owns this lock until its process exits. Taking it
            # before clearing the record makes registration release observable
            # to a subsequent arming command, without replacing the lock inode.
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _write_watch_record(handle, {})
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return {
                "project": project,
                "stopped": stopped,
                "registration_released": True,
                "reason": reason,
                "detail": detail,
                "watcher": watcher,
            }

        watcher = _read_watch_record(handle)
        _write_watch_record(handle, {})
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {
            "project": project,
            "stopped": False,
            "registration_released": True,
            "reason": "nothing-to-stop",
            "detail": f"project {project!r} has no registered watcher to stop",
            "watcher": watcher,
        }


def _single_clause(value: Any, *, limit: int = 96) -> str:
    """Collapse free text to one bounded clause suitable for a ticker."""
    compact = " ".join(str(value or "").split())
    clause = re.split(
        r";|(?<=[.!?])\s+|\s+[\N{EM DASH}\N{EN DASH}]\s+", compact, maxsplit=1
    )[0].strip()
    if len(clause) <= limit:
        return clause
    boundary = clause.rfind(" ", 0, limit)
    if boundary < limit // 2:
        boundary = limit - 1
    return clause[:boundary].rstrip(" ,:") + "…"


def _watch_snapshot(
    pointer: Mapping[str, Any], *, moment: float, stall_seconds: int
) -> dict[str, Any]:
    """Reduce one pointer to the state and reason a ticker compares."""
    row = classify_pointer(pointer, now_seconds=moment)
    manifest_status = str(row.get("manifest_status") or "")
    phase = str(pointer.get("phase") or "")
    classification = str(row.get("classification") or "")

    if manifest_status in {"complete", "blocked", "failed"}:
        state = manifest_status
    elif phase == "starting":
        state = "dispatched"
    elif phase in {"working", "running"} or classification == "running":
        state = "working"
    elif phase == "stopped":
        state = "stopped"
    else:
        state = classification or phase or "unknown"

    detail = str(row.get("detail") or "")
    for prefix in (
        "the worker manifest reports blocked: ",
        "the worker manifest reports failed: ",
    ):
        if detail.startswith(prefix):
            detail = detail[len(prefix) :]
            break

    if state == "working":
        quiet = _stream_quiet_seconds(pointer, now_seconds=moment)
        if quiet > stall_seconds:
            state = "stalled"
            detail = f"stream quiet for {quiet}s"
        else:
            detail = ""
    elif state in {"dispatched", "complete"}:
        detail = ""

    return {
        "run_id": str(row.get("run_id") or ""),
        "node": str(row.get("node") or row.get("run_id") or "unknown"),
        "state": state,
        "reason": _single_clause(detail),
    }


def _fleet_counts(snapshots: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """Count the ambient fleet posture included on every transition."""
    states = [str(snapshot.get("state") or "") for snapshot in snapshots.values()]
    return {
        "live": len(states),
        "blocked": states.count("blocked"),
        "unpromoted": states.count("complete"),
    }


def _watch_transition(
    project: str,
    *,
    kind: str,
    snapshot: Mapping[str, Any],
    previous: str | None,
    current: str,
    counts: Mapping[str, int],
) -> dict[str, Any]:
    """Build one lossless transition object for text or JSON rendering."""
    event = {
        "project": project,
        "event": kind,
        "observed_at": _utc_now(),
        "run_id": snapshot.get("run_id"),
        "node": snapshot.get("node"),
        "from_state": previous,
        "to_state": current,
        "live": counts["live"],
        "blocked": counts["blocked"],
        "unpromoted": counts["unpromoted"],
    }
    reason = _single_clause(snapshot.get("reason"))
    if reason:
        event["reason"] = reason
    return event


def format_watch_transition(event: Mapping[str, Any]) -> str:
    """Render one transition as the compact human-facing watch line."""
    observed = str(event.get("observed_at") or "")
    clock = observed[11:19] if len(observed) >= 19 else observed or "--:--:--"
    node = str(event.get("node") or event.get("run_id") or "unknown")
    previous = str(event.get("from_state") or "")
    current = str(event.get("to_state") or "unknown")
    movement = f"{previous} → {current}" if previous else f"→ {current}"
    line = (
        f"{clock}  {node:<28}  {movement:<24}  "
        f"{int(event.get('live') or 0)} live · "
        f"{int(event.get('blocked') or 0)} blocked · "
        f"{int(event.get('unpromoted') or 0)} unpromoted"
    )
    reason = _single_clause(event.get("reason"))
    return f"{line} · {reason}" if reason else line.rstrip()


def watch_ticker(
    project: str,
    *,
    stall_window: str = DEFAULT_WATCH_STALL_WINDOW,
    poll_interval: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> Iterator[dict[str, Any]]:
    """Yield a baseline and then every observed fleet state transition."""
    stall_seconds = parse_duration(stall_window)
    known: dict[str, dict[str, Any]] = {}
    fleet_seen = False

    with _project_watch_claim(project, stall_window) as (acquired, watcher):
        if not acquired:
            yield {
                "project": project,
                "event": "watcher-live",
                "run_id": None,
                "classification": "watcher_live",
                "next_action": "wait for the live project watcher to report",
                "watcher_live": True,
                "watcher": watcher,
            }
            return

        while True:
            pointers = list_live(project=project)
            moment = _utc_seconds()
            current = {
                str(pointer.get("run_id") or ""): _watch_snapshot(
                    pointer, moment=moment, stall_seconds=stall_seconds
                )
                for pointer in pointers
                if pointer.get("run_id")
            }
            if not current and not fleet_seen:
                sleeper(poll_interval)
                continue

            counts = _fleet_counts(current)
            if not fleet_seen:
                fleet_seen = True
                known = {run_id: dict(snapshot) for run_id, snapshot in current.items()}
                for snapshot in current.values():
                    yield _watch_transition(
                        project,
                        kind="baseline",
                        snapshot=snapshot,
                        previous=None,
                        current=str(snapshot["state"]),
                        counts=counts,
                    )
                continue

            events: list[dict[str, Any]] = []
            next_known = {run_id: dict(snapshot) for run_id, snapshot in known.items()}
            for run_id in (item for item in known if item not in current):
                snapshot = known[run_id]
                next_known.pop(run_id, None)
                events.append(
                    _watch_transition(
                        project,
                        kind="transition",
                        snapshot=snapshot,
                        previous=str(snapshot["state"]),
                        current="promoted",
                        counts=counts,
                    )
                )

            for run_id in (item for item in current if item not in known):
                snapshot = current[run_id]
                dispatched = {**snapshot, "state": "dispatched", "reason": ""}
                next_known[run_id] = dispatched
                events.append(
                    _watch_transition(
                        project,
                        kind="transition",
                        snapshot=dispatched,
                        previous=None,
                        current="dispatched",
                        counts=counts,
                    )
                )

            for run_id in (item for item in current if item in known):
                snapshot = current[run_id]
                previous = str(known[run_id]["state"])
                current_state = str(snapshot["state"])
                if current_state != previous:
                    next_known[run_id] = dict(snapshot)
                    events.append(
                        _watch_transition(
                            project,
                            kind="transition",
                            snapshot=snapshot,
                            previous=previous,
                            current=current_state,
                            counts=counts,
                        )
                    )

            known = next_known
            if events:
                yield from events
                if not current:
                    return
                continue
            sleeper(poll_interval)


def watch_follow(
    project: str,
    *,
    stall_window: str = DEFAULT_WATCH_STALL_WINDOW,
    poll_interval: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
    transitions: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield each newly terminal run, or the full transition stream on request.

    An empty project remains armed until its first pointer appears. Once a
    fleet has appeared, removing its last pointer ends the stream. Terminal
    and stalled run ids are remembered so an unreconciled pointer cannot
    repeatedly wake the watcher or hide a later run.
    """
    if transitions:
        yield from watch_ticker(
            project,
            stall_window=stall_window,
            poll_interval=poll_interval,
            sleeper=sleeper,
        )
        return

    stall_seconds = parse_duration(stall_window)
    reported_runs: set[str] = set()
    fleet_seen = False

    with _project_watch_claim(project, stall_window) as (acquired, watcher):
        if not acquired:
            yield {
                "project": project,
                "event": "watcher-live",
                "run_id": None,
                "classification": "watcher_live",
                "next_action": "wait for the live project watcher to report",
                "watcher_live": True,
                "watcher": watcher,
            }
            return

        while True:
            pointers = list_live(project=project)
            if not pointers:
                if fleet_seen:
                    return
                sleeper(poll_interval)
                continue
            fleet_seen = True

            moment = _utc_seconds()
            classified = [
                (pointer, classify_pointer(pointer, now_seconds=moment))
                for pointer in pointers
            ]
            for _pointer, row in classified:
                run_id = str(row.get("run_id") or "")
                if run_id not in reported_runs and row.get("manifest_status") in {
                    "complete",
                    "blocked",
                    "failed",
                }:
                    reported_runs.add(run_id)
                    yield {"project": project, "event": "terminal", **row}
                    break
            else:
                for pointer, row in classified:
                    run_id = str(row.get("run_id") or "")
                    if run_id not in reported_runs and row.get(
                        "manifest_status"
                    ) not in {"complete", "blocked", "failed"}:
                        quiet = _stream_quiet_seconds(pointer, now_seconds=moment)
                        if quiet > stall_seconds:
                            reported_runs.add(run_id)
                            yield {
                                "project": project,
                                "event": "stalled",
                                **row,
                                "stalled_for_seconds": quiet,
                            }
                            break
                else:
                    sleeper(poll_interval)


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
