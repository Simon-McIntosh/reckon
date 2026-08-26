from __future__ import annotations

import html
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from reckon import _backends, _store, ledger
from reckon.crew.dispatch import _backend_settings, _capture_member_session
from reckon.crew.node import CrewError, STALL_BUDGET_MULTIPLE, parse_duration
from reckon.crew.runs import (
    _pointer_lock,
    _utc_now,
    pointer_path,
    process_alive,
    read_pointer,
    run_dir,
)

# ── Promotion: the transient record becomes committed evidence ──────────────


def scoped_diff_stat(
    *,
    cwd: str | Path,
    base: str,
    head: str = "HEAD",
    paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Count the lines a run changed inside its own write scope.

    Measured against the node's exclusive paths rather than the whole diff, so
    the number describes the node rather than whatever else the branch carried.
    An unmeasurable diff is an explicit absence. Command diagnostics are not
    measurements and must never enter the durable numeric field.
    """
    if not base:
        return {"available": False, "reason": "missing_base"}
    for revision in (base, head):
        resolved = subprocess.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        if resolved.returncode:
            return {"available": False, "reason": "unresolvable_revision"}
    argv = ["git", "diff", "--numstat", f"{base}..{head}"]
    if paths:
        argv += ["--", *[str(path) for path in paths]]
    result = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode:
        return {"available": False, "reason": "diff_unavailable"}
    added = removed = files = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        # A binary file reports "-" for both counts; it changed, but no lines did.
        added += int(parts[0]) if parts[0].isdigit() else 0
        removed += int(parts[1]) if parts[1].isdigit() else 0
    return {"added": added, "removed": removed, "files": files}


def _is_shadow(record: Mapping[str, Any]) -> bool:
    """Return whether a live or committed record is shadow evidence."""
    lineage = record.get("lineage")
    return isinstance(lineage, Mapping) and lineage.get("kind") == "shadow"


def _write_shadow_patch(record: Mapping[str, Any]) -> Path:
    """Persist the complete diff from a shadow's fixed base, including new files."""
    run_id = str(record.get("run_id") or "")
    worktree = Path(str(record.get("worktree") or ""))
    base = str(record.get("base_sha") or "")
    if not worktree.is_dir():
        raise CrewError(
            f"shadow run {run_id!r} has no readable worktree; its patch cannot be preserved"
        )
    resolved = subprocess.run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{base}^{{commit}}",
        ],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if not base or resolved.returncode:
        raise CrewError(
            f"shadow run {run_id!r} base {base!r} is not reachable in its worktree"
        )

    tracked = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", base, "--"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if tracked.returncode:
        raise CrewError(f"shadow run {run_id!r} could not produce its tracked diff")
    patch = bytearray(tracked.stdout)

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if untracked.returncode:
        raise CrewError(f"shadow run {run_id!r} could not enumerate new files")
    for raw_path in (item for item in untracked.stdout.split(b"\0") if item):
        path = os.fsdecode(raw_path)
        addition = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", path],
            cwd=worktree,
            capture_output=True,
            check=False,
        )
        if addition.returncode not in (0, 1):
            raise CrewError(
                f"shadow run {run_id!r} could not preserve new file {path!r}"
            )
        if patch and not patch.endswith(b"\n"):
            patch.extend(b"\n")
        patch.extend(addition.stdout)

    artifact = run_dir(run_id) / "shadow.patch"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(bytes(patch))
    return artifact


def _shadow_patch_stat(path: Path, *, cwd: Path) -> dict[str, int]:
    """Derive line and file counts from the retained patch artifact."""
    if not path.read_bytes():
        return {"added": 0, "removed": 0, "files": 0}
    result = subprocess.run(
        ["git", "apply", "--numstat", str(path)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise CrewError(f"shadow patch {path} is not a measurable git patch")
    added = removed = files = 0
    for line in result.stdout.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            continue
        files += 1
        added += int(fields[0]) if fields[0].isdigit() else 0
        removed += int(fields[1]) if fields[1].isdigit() else 0
    return {"added": added, "removed": removed, "files": files}


def _elapsed_seconds(start: Any, end: Any) -> int | None:
    """Return whole seconds between two ISO-8601 stamps, or None."""
    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0, int((last - first).total_seconds()))


def _assume_utc_if_naive(value: str) -> str:
    """Attach UTC to a completion stamp that carries no timezone."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        return value
    return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _wall_exceeded_budget(wall_seconds: int | None, time_budget: Any) -> bool:
    """Flag wall time beyond the bounded multiple used to identify stalls."""
    if wall_seconds is None:
        return False
    try:
        budget_seconds = parse_duration(str(time_budget))
    except CrewError:
        return False
    return wall_seconds > STALL_BUDGET_MULTIPLE * budget_seconds


def _run_streams(path: Path) -> list[Path]:
    """Return the original stream followed by resumes in numeric turn order."""
    original = path.parent / "stream.jsonl" if path.name.startswith("resume-") else path
    resumes = sorted(
        path.parent.glob("resume-*.jsonl"), key=ledger._resume_stream_order
    )
    return [candidate for candidate in (original, *resumes) if candidate.is_file()]


@dataclass(frozen=True)
class StreamMeasures:
    """Measurements recoverable from a run's ordered event streams."""

    completed_at: str | None
    completion_source: str | None
    worker_seconds: int | None
    budget: dict[str, Any]
    session_id: str | None


def _section_anchor(section: Any) -> str:
    """Map a numbered section reference to its semantic HTML anchor."""
    normalized = ledger.normalize_section(section)
    numbered = re.fullmatch(r"§(\d+(?:\.\d+)*)", normalized)
    if numbered:
        return f"s{numbered.group(1).replace('.', '-')}"
    return normalized.removeprefix("#") or "_top"


def _record_landing_comment(
    *,
    project: str,
    plan: str,
    section: str,
    run_id: str,
    narrative: str,
    author: str,
    when: str,
    root: str | Path | None,
) -> dict[str, Any]:
    """Append one idempotent section comment for a promoted run."""
    narrative = str(narrative).strip()
    if not narrative or not plan:
        return {"recorded": False, "reason": "empty_narrative"}
    comment_id = f"c-run-{re.sub(r'[^A-Za-z0-9._-]+', '-', run_id)}"
    anchor = _section_anchor(section)
    for _attempt in range(4):
        state, version = _store.read_plan(project, plan, root, artifact_type="plan")
        if not state or state.get("type") != "plan":
            return {"recorded": False, "reason": "plan_unavailable"}
        comments = {
            key: list(items) for key, items in (state.get("comments") or {}).items()
        }
        items = comments.setdefault(anchor, [])
        if any(str(item.get("id") or "") == comment_id for item in items):
            return {
                "recorded": True,
                "comment_id": comment_id,
                "section": anchor,
                "already_recorded": True,
            }
        items.append(
            {
                "id": comment_id,
                "who": author,
                "when": when,
                "body": f"<p>{html.escape(narrative)}</p>",
            }
        )
        try:
            _store.write_plan(
                project,
                plan,
                {**state, "comments": comments},
                version,
                root,
                artifact_type="plan",
            )
        except _store.VersionConflict:
            continue
        return {
            "recorded": True,
            "comment_id": comment_id,
            "section": anchor,
            "already_recorded": False,
        }
    raise CrewError(
        f"could not record landing comment for plan {plan!r}: "
        "the plan changed during four consecutive write attempts"
    )


def _terminal_stream_data(
    record: Mapping[str, Any],
) -> StreamMeasures:
    """Resolve completion from events, then stream mtimes, across all turns."""
    budget = dict(record.get("budget") or {})
    if record.get("launch") != "cli":
        return StreamMeasures(None, None, None, budget, None)

    backend_name = str(record.get("backend") or "")
    backend = _backend_settings(record, None)
    path = Path(str(record.get("log_path") or ""))
    paths = _run_streams(path)
    if not paths:
        return StreamMeasures(None, None, None, budget, None)

    timestamps: list[tuple[datetime, str]] = []
    session_id = None
    for candidate in paths:
        observation = _backends.observe_log(
            backend_name=backend_name,
            backend=backend,
            log_path=candidate,
        )
        if observation.terminal:
            budget = dict(observation.budget)
        session_id = observation.session_id or session_id
        with candidate.open(encoding="utf-8", errors="replace") as handle:
            events, _malformed = _backends.parse_events(handle)
        for event in events:
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str) or not timestamp.strip():
                continue
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                timestamps.append((parsed, timestamp))
    if timestamps:
        first = min(timestamps, key=lambda item: item[0])
        last = max(timestamps, key=lambda item: item[0])
        return StreamMeasures(
            last[1],
            "terminal_event",
            max(0, int((last[0] - first[0]).total_seconds())),
            budget,
            session_id,
        )

    newest = max(candidate.stat().st_mtime for candidate in paths)
    completed = (
        datetime.fromtimestamp(newest, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return StreamMeasures(completed, "stream_mtime", None, budget, session_id)


def complete(
    run_id: str,
    *,
    gate: str,
    failure_classification: str = "",
    commits: Iterable[str] = (),
    outcome: str = "",
    tests_added: int | None = None,
    scope_changed: bool = False,
    changed_lines: Mapping[str, Any] | None = None,
    completed_at: str = "",
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Promote a run, or finish cleanup when its record already landed."""
    verdict = str(gate).strip().lower()
    if verdict not in ledger.GATE_VERDICTS:
        raise ledger.LedgerError(
            f"gate verdict {gate!r} is not one of "
            f"{', '.join(ledger.GATE_VERDICTS)}; a gate whose evidence could "
            "not be produced is 'not-run'"
        )
    if verdict != "passed" and not str(outcome).strip():
        raise CrewError(
            "a non-passing gate requires --outcome; write what failed or why "
            "the evidence could not be produced"
        )
    classification = str(failure_classification).strip().lower()
    if verdict == "failed" and classification not in ledger.FAILURE_CLASSIFICATIONS:
        raise CrewError(
            "a failing gate requires --failure-classification from: "
            + ", ".join(ledger.FAILURE_CLASSIFICATIONS)
        )
    if verdict != "failed" and classification:
        raise CrewError("--failure-classification is valid only when --gate failed")
    commit_list = tuple(str(sha) for sha in commits if str(sha).strip())
    with _pointer_lock(run_id):
        record = read_pointer(run_id)
        if _is_shadow(record) and commit_list:
            raise CrewError(
                f"shadow run {run_id!r} is commitless evidence; --commit is refused"
            )
        return _complete_locked(
            run_id,
            gate=gate,
            failure_classification=classification,
            commits=commit_list,
            outcome=outcome,
            tests_added=tests_added,
            scope_changed=scope_changed,
            changed_lines=changed_lines,
            completed_at=completed_at,
            root=root,
        )


def _complete_locked(
    run_id: str,
    *,
    gate: str,
    failure_classification: str = "",
    commits: Iterable[str] = (),
    outcome: str = "",
    tests_added: int | None = None,
    scope_changed: bool = False,
    changed_lines: Mapping[str, Any] | None = None,
    completed_at: str = "",
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Promote a finished run into the owning repository's committed ledger.

    The plan comment and ledger append both happen before the pointer is
    deleted. The comment uses a stable run-derived id, so a retry after an
    interruption cannot duplicate the narrative.

    Worker-time spans the first and last timestamped stream events. A healthy
    timestamp-less stream falls back to wall duration with an explicit source;
    a stalled run keeps that duration absent. Promotion time remains an
    explicit completion fallback when no stream survives.
    """
    record = read_pointer(run_id)
    project = str(record.get("project") or "")
    node = record.get("node") or {}
    shadow = _is_shadow(record)
    ledger_root = root if root is not None else record.get("repo")
    ledger_data, ledger_version = ledger.load(project, root=ledger_root)
    existing = next(
        (
            item
            for item in ledger_data["runs"]
            if str(item.get("run_id") or "") == run_id
        ),
        None,
    )
    if existing is not None:
        comment = (
            {"recorded": False, "reason": "shadow evidence does not land code"}
            if shadow
            else _record_landing_comment(
                project=project,
                plan=str(node.get("plan") or ""),
                section=str(node.get("section") or ""),
                run_id=run_id,
                narrative=outcome,
                author=str(record.get("member") or record.get("role") or "reckon"),
                when=str(existing.get("completed_at") or _utc_now()),
                root=ledger_root,
            )
        )
        capture = _capture_member_session(record)
        path = pointer_path(run_id)
        path.unlink(missing_ok=True)
        return {
            "run_id": run_id,
            "project": project,
            "ledger_path": str(ledger.ledger_path(project, ledger_root)),
            "ledger_version": ledger_version,
            "pointer_removed": not path.exists(),
            "record": dict(existing),
            "already_promoted": True,
            "session_capture": capture,
            "plan_comment": comment,
        }

    stream = _terminal_stream_data(record)
    if completed_at:
        finished = _assume_utc_if_naive(completed_at)
        completion_source = "provided"
    elif stream.completed_at:
        finished = stream.completed_at
        completion_source = stream.completion_source or "terminal_event"
    else:
        finished = _utc_now()
        completion_source = "promotion_time"
    commit_list = [str(sha) for sha in commits if str(sha).strip()]
    if shadow and commit_list:
        raise CrewError(
            f"shadow run {run_id!r} is commitless evidence; --commit is refused"
        )
    worktree = Path(str(record.get("worktree") or ""))
    tree = worktree if worktree.is_dir() else Path(str(record.get("repo") or "."))
    shadow_patch = ""
    if shadow:
        artifact = _write_shadow_patch(record)
        changed_lines = _shadow_patch_stat(artifact, cwd=tree)
        shadow_patch = str(artifact)
    else:
        changed_lines = (
            scoped_diff_stat(
                cwd=tree,
                base=str(record.get("base_sha") or ""),
                head=commit_list[-1],
                paths=node.get("write_paths") or (),
            )
            if commit_list
            else None
        )

    session_id = record.get("session_id") or stream.session_id
    previous = next(
        (
            item
            for item in reversed(ledger_data["runs"])
            if session_id and item.get("session_id") == session_id
        ),
        None,
    )
    measured_budget = ledger.per_run_budget(stream.budget, previous)
    wall_seconds = _elapsed_seconds(record.get("created_at"), finished)
    stalled = _wall_exceeded_budget(wall_seconds, node.get("time_budget"))
    worker_seconds = stream.worker_seconds
    if worker_seconds is not None:
        worker_seconds_source = "stream_events"
    elif stream.completion_source == "stream_mtime" and stalled:
        worker_seconds_source = "stalled"
    elif stream.completion_source == "stream_mtime" and wall_seconds is not None:
        worker_seconds = wall_seconds
        worker_seconds_source = "wall_fallback"
    else:
        worker_seconds_source = "unavailable"

    comment = (
        {"recorded": False, "reason": "shadow evidence does not land code"}
        if shadow
        else _record_landing_comment(
            project=project,
            plan=str(node.get("plan") or ""),
            section=str(node.get("section") or ""),
            run_id=run_id,
            narrative=outcome,
            author=str(record.get("member") or record.get("role") or "reckon"),
            when=finished,
            root=ledger_root,
        )
    )
    run = ledger.build_record(
        run_id=run_id,
        plan=str(node.get("plan") or ""),
        section=str(node.get("section") or ""),
        node=str(node.get("id") or ""),
        node_definition=node,
        role=str(record.get("role") or ""),
        spec_level=str(node.get("spec_level") or ""),
        member_id=str(record.get("member") or ""),
        backend=str(
            record.get("backend") or (record.get("agent") or {}).get("backend") or ""
        ),
        agent=record.get("agent") or {},
        dispatched_at=str(record.get("created_at") or ""),
        completed_at=finished,
        completed_at_source=completion_source,
        worker_seconds=worker_seconds,
        worker_seconds_source=worker_seconds_source,
        wall_seconds=wall_seconds,
        stalled=stalled,
        time_budget=str(node.get("time_budget") or ""),
        base_sha=str(record.get("base_sha") or ""),
        commits=commit_list,
        changed_lines=changed_lines,
        tests_added=tests_added,
        gate=gate,
        failure_classification=failure_classification,
        outcome="" if comment.get("recorded") else outcome,
        manifest_path=str(record.get("manifest_path") or ""),
        scope_changed=scope_changed,
        session_id=session_id,
        budget=measured_budget,
        lineage=record.get("lineage"),
        shadow_patch=shadow_patch,
        unreconciled_override=record.get("unreconciled_override"),
    )
    run["attempt"] = int(record.get("attempt") or 1)
    run["attempt_kind"] = str(record.get("attempt_kind") or "dispatch")
    watch_override = record.get("watch_override")
    if isinstance(watch_override, Mapping):
        run["watch_override"] = dict(watch_override)
    execution_fit = record.get("execution_fit")
    if isinstance(execution_fit, Mapping):
        run["execution_fit"] = dict(execution_fit)
    already_promoted = False
    try:
        written = ledger.append_run(project, run, root=ledger_root)
    except ledger.LedgerError:
        # Another completion can land after the read above. Treat only an
        # observed matching record as success; every other ledger error is
        # still a refusal.
        refreshed, ledger_version = ledger.load(project, root=ledger_root)
        existing = next(
            (
                item
                for item in refreshed["runs"]
                if str(item.get("run_id") or "") == run_id
            ),
            None,
        )
        if existing is None:
            raise
        already_promoted = True
        written = {
            "path": str(ledger.ledger_path(project, ledger_root)),
            "version": ledger_version,
            "run": dict(existing),
        }

    # The session id lives only in the pointer until it reaches the roster, so
    # it has to be captured before the pointer goes.
    capture = _capture_member_session(record)
    pointer_path(run_id).unlink(missing_ok=True)
    return {
        "run_id": run_id,
        "project": project,
        "ledger_path": written["path"],
        "ledger_version": written["version"],
        "pointer_removed": not pointer_path(run_id).exists(),
        "record": written["run"],
        "already_promoted": already_promoted,
        "session_capture": capture,
        "plan_comment": comment,
    }


def discard(run_id: str) -> dict[str, Any]:
    """Remove a stopped or abandoned pointer without promoting it."""
    with _pointer_lock(run_id):
        record = read_pointer(run_id)
        pid = record.get("pid")
        if process_alive(pid) is True:
            raise CrewError(
                f"cannot discard live run {run_id!r}: recorded pid {pid} is alive"
            )
        path = pointer_path(run_id)
        path.unlink()
        return {
            "run_id": run_id,
            "pointer_path": str(path),
            "pointer_removed": not path.exists(),
            "removed": record,
        }
