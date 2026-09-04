from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from reckon.crew.node import (
    DEFAULT_WATCH_STALL_WINDOW,
    CrewError,
    LOG_STALE_AFTER_SECONDS,
    _TERMINAL_RUN_PHASES,
    parse_duration,
)
from reckon.crew.reports import ManifestParseError, parse_manifest
from reckon.crew.routing import _signal_process_group
from reckon.crew.ticker import NEEDS_ACTION, Ticker, _agent_label
from reckon.crew.runs import (
    _manifest_freshness,
    _mutate_pointer,
    _process_start_time,
    _project_watch_claim,
    _read_watch_record,
    _stream_quiet_seconds,
    _utc_now,
    _write_watch_record,
    list_live,
    watch_lock_path,
)


# ── Recovery: what an interrupted orchestrator left behind ───────────────────

# What a live pointer can be once nobody is watching it. Worker-reported
# blocked and failed outcomes remain distinct so neither can be mistaken for a
# completed delivery that is eligible for promotion. An unreadable manifest is
# its own outcome: a file exists but no reader can judge it, which is neither a
# delivered record (completed_unpromoted) nor an absence (abandoned).
RECOVERY_CLASSES = (
    "running",
    "waiting",
    "stopped",
    "completed_unpromoted",
    "blocked",
    "failed",
    "unreadable",
    "abandoned",
)

WAITING_STATUS = "waiting"
WAITING_STATES = frozenset({"waiting", "wait-aged"})


def _budget_timing(
    record: Mapping[str, Any], *, now_seconds: float | None = None
) -> dict[str, Any]:
    """Measure one live run against its declared allowance without mutating it."""
    node = record.get("node") or {}
    try:
        if "attempt_budget_seconds" in record:
            budget_seconds = int(record["attempt_budget_seconds"])
        else:
            budget_seconds = parse_duration(str(node.get("time_budget") or ""))
        started = datetime.fromisoformat(
            str(
                record.get("attempt_started_at") or record.get("created_at") or ""
            ).replace("Z", "+00:00")
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


def _refusal_block(
    record: Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any]:
    """Normalise a refusal budget block into the fields a blocked reason needs."""
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": str(budget.get("rate_limit_type") or "quota"),
        "resets_at": str(budget.get("resets_at") or "unknown"),
    }


def _stream_refusal_block(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The provider refusal a cli run's stream records, folded in or read fresh.

    A spend or usage refusal is a block, not an abandonment: the account is not
    broken, only spent until a moment the refusal names. observe() folds the
    stream's refusal into the pointer's budget, while the ticker reads raw
    pointers that have not been through observe. Both paths resolve through the
    same backend translation, so they reach the same verdict and a ticker
    reading a raw pointer cannot disagree with observe's phase.

    Declining is the load-bearing half. A stream that reports an ordinary
    failed turn — a bad model id, a lost stream, a context overflow — carries
    none of the recognised limit phrases and returns None, so a crash is never
    mistaken for a block.
    """
    budget = record.get("budget")
    if isinstance(budget, Mapping) and budget.get("refusal"):
        return _refusal_block(record, budget)
    if record.get("launch") != "cli":
        return None
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return None
    argv = record.get("argv")
    command = argv[0] if isinstance(argv, list) and argv else record.get("dialect")
    if not command:
        return None
    from reckon import _backends

    try:
        observation = _backends.observe_log(
            backend_name=str(record.get("backend") or ""),
            backend={"command": command},
            log_path=log,
        )
    except (_backends.BackendError, CrewError, OSError, ValueError):
        # An unreadable or untranslatable stream is a reading problem, not a
        # block; the manifest and liveness paths still classify the run.
        return None
    observed = observation.as_dict().get("budget") or {}
    if observed.get("refusal"):
        return _refusal_block(record, observed)
    return None


# A print-mode invocation makes exactly one turn and exits when it ends, so a
# worker still waiting on a background task at that moment leaves one of two
# traces rather than a clean result. The ceiling message is the harness's own
# stderr line when it gave up waiting and terminated the task itself. The
# duration is read from the environment and varies, so only the sentence
# around it is fixed.
_BACKGROUND_WAIT_CEILING_RE = re.compile(
    r"Background tasks still running after \d+s; terminating\.\s*"
    r"Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely\.",
)
# The agent's own last words when its turn ended before the background work
# it was waiting on did. Matched loosely around the fixed clause so a run
# naming a different suite or task still recognises the same shape.
_BACKGROUND_WAIT_FINAL_MESSAGE_RE = re.compile(
    r"waiting for (the )?background .+? before finalizing the manifest",
    re.IGNORECASE | re.DOTALL,
)


def _background_wait_signal(record: Mapping[str, Any]) -> str | None:
    """The one sentence proving a vanished process was waiting on background work.

    A dead process with no complete manifest is indistinguishable from one
    that simply crashed, unless the run directory itself says otherwise. Two
    traces say otherwise: the harness's own ceiling message on stderr, or the
    agent's last turn stating in its own words that it was waiting on
    background work before finalizing the manifest — with nothing after that
    turn because a print-mode invocation has no next one to write. Neither is
    a crash; both name a run whose session is intact and whose only
    outstanding step is a resume long enough to collect the manifest it was
    already about to write.
    """
    stderr_path = record.get("stderr_path")
    if stderr_path:
        try:
            stderr_text = Path(str(stderr_path)).read_text()
        except OSError:
            stderr_text = ""
        if _BACKGROUND_WAIT_CEILING_RE.search(stderr_text):
            return (
                "the worker's stderr recorded the background-wait ceiling "
                "before the process terminated"
            )

    final_message = str(record.get("final_message") or "")
    if not final_message and record.get("launch") == "cli":
        # observe() folds the stream's final message onto the pointer, but a
        # caller reading the raw pointer — the watch producer's path — has
        # none of it cached yet. Reading the log directly keeps that path
        # answering the same question the folded record would.
        log = Path(str(record.get("log_path") or ""))
        if log.is_file():
            argv = record.get("argv")
            command = (
                argv[0] if isinstance(argv, list) and argv else record.get("dialect")
            )
            if command:
                from reckon import _backends

                try:
                    observation = _backends.observe_log(
                        backend_name=str(record.get("backend") or ""),
                        backend={"command": command},
                        log_path=log,
                    )
                except (_backends.BackendError, CrewError, OSError, ValueError):
                    observation = None
                if observation is not None:
                    final_message = str(
                        observation.as_dict().get("final_message") or ""
                    )

    if final_message and _BACKGROUND_WAIT_FINAL_MESSAGE_RE.search(final_message):
        return (
            "the worker's last turn reported waiting on background work "
            f"before finalizing the manifest: {final_message.strip()}"
        )
    return None


def _wait_probe(value: Any) -> list[str]:
    """Read a shell-free argument vector from a waiting manifest."""
    if isinstance(value, list):
        probe = value
    else:
        try:
            probe = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(probe, list) or not probe:
        return []
    if any(not isinstance(item, str) or not item.strip() for item in probe):
        return []
    return [item.strip() for item in probe]


def _wait_terminal_values(value: Any) -> list[str]:
    """Read the external states that mean a condition has terminated."""
    values = value if isinstance(value, list) else str(value or "").split(",")
    return [str(item).strip() for item in values if str(item).strip()]


def _manifest_wait(
    manifest_data: Mapping[str, Any],
    manifest: Path,
    *,
    now_seconds: float,
    stale_after_seconds: int,
) -> dict[str, Any] | None:
    """Return the complete external-wait declaration carried by a manifest."""
    if str(manifest_data.get("status") or "").strip().lower() != WAITING_STATUS:
        return None
    condition = str(manifest_data.get("wait_condition") or "").strip()
    probe = _wait_probe(manifest_data.get("wait_probe"))
    terminal = _wait_terminal_values(manifest_data.get("wait_terminal"))
    resume_brief = str(manifest_data.get("resume_brief") or "").strip()
    missing = [
        name
        for name, value in (
            ("wait_condition", condition),
            ("wait_probe", probe),
            ("wait_terminal", terminal),
            ("resume_brief", resume_brief),
        )
        if not value
    ]
    started = None
    started_value = str(manifest_data.get("wait_started_at") or "").strip()
    if started_value:
        try:
            started = datetime.fromisoformat(started_value)
        except ValueError:
            missing.append("readable wait_started_at")
        else:
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
    if started is None:
        try:
            started_seconds = manifest.stat().st_mtime
        except OSError:
            started_seconds = now_seconds
    else:
        started_seconds = started.timestamp()
    age_seconds = max(0, int(now_seconds - started_seconds))
    return {
        "condition": condition,
        "probe": probe,
        "terminal": terminal,
        "resume_brief": resume_brief,
        "started_at": started_value
        or datetime.fromtimestamp(started_seconds, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "age_seconds": age_seconds,
        "overdue": age_seconds > stale_after_seconds,
        "signature": f"condition:{manifest.stat().st_mtime_ns}",
        "valid": not missing,
        "error": "missing or invalid " + ", ".join(missing) if missing else "",
    }


def external_wait(
    record: Mapping[str, Any],
    *,
    now_seconds: float | None = None,
    stale_after_seconds: int = LOG_STALE_AFTER_SECONDS,
) -> dict[str, Any] | None:
    """Read a fresh external-wait declaration from one live pointer."""
    manifest = Path(str(record.get("manifest_path") or ""))
    _present, fresh = _manifest_freshness(record)
    if not fresh:
        return None
    try:
        data = parse_manifest(manifest.read_text(encoding="utf-8"))
    except (OSError, ManifestParseError):
        return None
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    return _manifest_wait(
        data,
        manifest,
        now_seconds=moment,
        stale_after_seconds=stale_after_seconds,
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
        except (OSError, ManifestParseError) as exc:
            # The file exists but no reader can judge it: an unreadable file is
            # a condition of the delivery, not an exception in the classifier.
            # Collecting it here keeps the refusal text (the parse error) in a
            # channel the classification branches read, so a manifest that
            # declares a format and is not readable degrades to its own outcome
            # rather than escaping this function and failing every ticker
            # refresh for every session.
            manifest_error = str(exc)
    manifest_status = str(manifest_data.get("status") or "").strip().lower()
    manifest_derived = str(manifest_data.get("derived") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if manifest_derived:
        # A recovery artifact preserves evidence; it is not delivery by the
        # worker and therefore cannot satisfy the promotion precondition.
        manifest_present = False
    manifest_commits = list(manifest_data.get("commits") or [])
    manifest_blockers = list(manifest_data.get("blockers") or [])
    needs_help = manifest_data.get("needs_help")
    # Fleet reads refresh this fact once while loading the pointer, so every
    # consumer of the same record shares one liveness verdict. Direct callers
    # may also classify an observation they already hold without silently
    # replacing it with a second process-table reading.
    alive = record.get("process_alive")
    log = Path(str(record.get("log_path") or ""))
    age = None
    if log.is_file():
        age = max(0, int(_utc_seconds() - log.stat().st_mtime))
    # Superseded-by-newer-activity applies to any manifest that is not yet a
    # verdict. Complete and failed are preserved while a living worker keeps
    # producing output, but neither is rendered as its outcome until that
    # worker exits. Complete over a dead process is delivery and therefore a
    # verdict; failed over a live process can still be a placeholder. Blocked
    # is a solicitation: a newer log line from a live worker has answered it,
    # so discarding that overtaken report makes the resumed work visible.
    if (
        manifest_status
        and manifest_status not in {"complete", "failed"}
        and alive is True
        and log.is_file()
        and manifest.is_file()
        and log.stat().st_mtime_ns > manifest.stat().st_mtime_ns
    ):
        manifest_present = False
        manifest_data = {}
        manifest_status = ""
        manifest_commits = []
        manifest_blockers = []
        needs_help = None
    # A provider refusal makes an otherwise-abandoned run a block: the process
    # is gone but the stop is triageable (a named backend, limit and reset) and
    # resumable once the limit lifts. Detected from the same stream observe
    # reads, so the two paths agree.
    refusal_block = _stream_refusal_block(record)
    # A background wait is checked alongside the refusal, at the same
    # priority, and only consulted when no refusal already explains the stop:
    # both name a process that is gone but resumable, and a refusal is the
    # more specific of the two when both happen to be present.
    background_wait = None if refusal_block else _background_wait_signal(record)
    terminal = phase in ("complete", "failed")
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    wait = _manifest_wait(
        manifest_data,
        manifest,
        now_seconds=moment,
        stale_after_seconds=stale_after_seconds,
    )
    terminal_at = None
    terminal_age_seconds = None
    deferred_outcome = alive is True and manifest_status in {"complete", "failed"}
    if manifest_status in {"complete", "blocked", "failed"} and not deferred_outcome:
        terminal_seconds = manifest.stat().st_mtime
        terminal_at = (
            datetime.fromtimestamp(terminal_seconds, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        terminal_age_seconds = max(0, int(moment - terminal_seconds))

    marker = None
    needs_help_complete_value = None
    if manifest_status == "complete" and alive is not True:
        classification = "completed_unpromoted"
        detail = (
            "the worker manifest reports completion and the run is still a "
            "pointer; promoting it moves the delivered record into the repository ledger"
        )
        action = f"reckon crew complete --run {run_id} --gate <verdict>"
        action += "".join(f" --commit {commit}" for commit in manifest_commits)
    elif manifest_status == "blocked":
        classification = "blocked"
        # A blocked transition explains itself from the best source available,
        # in order: the worker's own escape-hatch question (already parsed and
        # complete — the sentence a coordinator can answer in one turn), then
        # the manifest's blockers, then a generic fallback. A bare-punctuation
        # result (a block-scalar indicator misread as its value, upstream)
        # explains nothing, so it is treated as absent too.
        needs_help_complete = isinstance(needs_help, Mapping) and bool(
            needs_help.get("complete")
        )
        headline = str(needs_help.get("headline") or "") if needs_help_complete else ""
        blocker = "; ".join(manifest_blockers)
        reason_text = headline or blocker or "the manifest reports a blocker"
        if not re.search(r"[A-Za-z0-9]", reason_text):
            reason_text = "the manifest reports a blocker"
        needs_help_complete_value = needs_help_complete
        detail = f"the worker manifest reports blocked: {reason_text}"
        if needs_help_complete:
            marker = "?"
            action = f"reckon crew resume --run {run_id} --advice <answer>"
        else:
            marker = "!"
            action = f"read {manifest}; resolve the blocker before resuming the run"
    elif manifest_status == "failed" and alive is not True:
        classification = "failed"
        failure = "; ".join(manifest_blockers) or "the worker manifest reports failure"
        detail = f"the worker manifest reports failed: {failure}"
        action = (
            f"read {manifest} and launch log {record.get('stderr_path')}; "
            "repair or redispatch the run"
        )
    elif wait is not None and wait["valid"]:
        classification = WAITING_STATUS
        detail = (
            f"waiting {wait['age_seconds']}s on {wait['condition']}; "
            f"terminal when the probe reports {', '.join(wait['terminal'])}"
        )
        action = (
            f"the recovery sweep will resume run {run_id} when the condition "
            "test reports a terminal state"
        )
    elif wait is not None:
        classification = "unreadable"
        manifest_error = str(wait["error"])
        detail = (
            f"the manifest at {manifest} declares an external wait but is "
            f"incomplete: {manifest_error}"
        )
        action = f"repair the waiting declaration in {manifest} before resuming"
    elif phase == "stopped":
        classification = "stopped"
        detail = "the run was intentionally stopped"
        action = (
            f"inspect the worktree at {record.get('worktree')} and discard when safe"
        )
    elif refusal_block:
        classification = "blocked"
        block = (
            f"backend {refusal_block['backend']!r} refused the turn on a "
            f"{refusal_block['limit_kind']}; reset {refusal_block['resets_at']}"
        )
        # The block states what was delivered so a reader does not conclude
        # nothing happened. A run killed with no manifest has nothing to show;
        # one whose manifest never reached a verdict still names its delivery
        # in the file, and pointing at it is the difference between a blocked
        # run and a vanished one.
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {block}; {delivery}"
        action = f"reckon crew resume --run {run_id} once the limit lifts"
    elif background_wait:
        # A vanished process is not the same fact as a crashed one: the run
        # directory itself says it was waiting on background work when it
        # ended, so it blocks and resumes rather than reading as abandoned
        # and inviting a redispatch that throws away an intact session.
        classification = "blocked"
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {background_wait}; {delivery}"
        action = f"reckon crew resume --run {run_id}"
    elif manifest_error and manifest_present:
        # The third manifest outcome next to absent and readable-and-terminal:
        # a file that is present but that no supported reader can parse is
        # neither a delivered record nor an absence. The name states what the
        # reader is to do, and the refusal text (the parse error, naming the
        # format the file declared and why it was rejected) travels in the same
        # manifest_error channel the abandoned arm used so the operator's next
        # question is answerable one turn before the run can be judged.
        classification = "unreadable"
        detail = (
            f"the manifest at {manifest} is present but could not be read: "
            f"{manifest_error}"
        )
        action = (
            f"read launch log {record.get('stderr_path')}; the manifest at "
            f"{manifest} cannot be read — repair or replace it before judging "
            "the run"
        )
    elif terminal:
        classification = "abandoned"
        if manifest_derived:
            delivery = "only a recovery-derived manifest exists"
        elif not manifest_present:
            delivery = "no manifest was delivered"
        else:
            # A present-but-unreadable manifest never reaches this arm: it is
            # intercepted above as its own outcome before the terminal reading
            # can fold it into abandoned.
            delivery = f"the manifest status {manifest_status!r} is not usable"
        detail = (
            f"the stored phase is terminal but {delivery}; nothing is eligible "
            "for promotion"
        )
        action = (
            f"reckon crew resume --run {run_id} --advice "
            f"{shlex.quote(f'review {manifest} and replace it with a worker-written manifest')}"
            if manifest_derived
            else (
                f"read launch log {record.get('stderr_path')}; inspect the worktree at "
                f"{record.get('worktree')} and redispatch if needed"
            )
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
        # Several coordinator sessions share one project, so every read of a
        # run has to say whose it is. Without it a session reading the live
        # view cannot tell its own fleet from a peer's, and acting on a peer's
        # row is worse than not seeing it.
        "session": record.get("session"),
        "plan": (record.get("node") or {}).get("plan"),
        "node": (record.get("node") or {}).get("id"),
        "classification": classification,
        "phase": phase,
        "process_alive": alive,
        "manifest_present": manifest_present,
        "manifest_file_present": manifest_file_present,
        "manifest_fresh": manifest_present,
        "manifest_path": str(manifest) if str(manifest) != "." else "",
        # A living worker's complete or failed report remains on disk but is
        # not exposed as an outcome. The single-event watcher consumes this
        # field, so returning the raw report here would call the run terminal
        # while the classification and ticker correctly call it live.
        "manifest_status": None if deferred_outcome else manifest_status or None,
        # Keep the worker's raw spelling alongside the effective status. A
        # live process defers terminal-looking placeholders, while the one-shot
        # watcher still needs to recognise a fresh completion written by the
        # resumed attempt it is waiting for.
        "manifest_reported_status": manifest_status or None,
        "manifest_derived": manifest_derived,
        "manifest_commits": manifest_commits,
        # The refusal text when a present manifest could not be read, carried on
        # the row so a surface that discards nothing has it one field away.
        "manifest_error": manifest_error or None,
        "terminal_at": terminal_at,
        "terminal_age_seconds": terminal_age_seconds,
        "log_age_seconds": age,
        "log_fresh": None if age is None else age <= stale_after_seconds,
        **timing,
        "worktree": record.get("worktree"),
        "detail": detail,
        "next_action": action,
        # Set only for a blocked run: "?" when the escape-hatch question is
        # complete enough that `reckon crew resume --advice` can answer it,
        # "!" when the reader has to read the manifest itself. The fact behind
        # it travels too so the renderer derives the glyph instead of persisting it.
        "marker": marker,
        "needs_help_complete": needs_help_complete_value,
        "external_wait": wait,
        "wait_age_seconds": wait.get("age_seconds") if wait else None,
        "wait_overdue": wait.get("overdue") if wait else None,
    }


def _worktree_diff_paths(record: Mapping[str, Any]) -> list[str]:
    """Return the base-to-worktree path census used for recovery evidence."""
    worktree = Path(str(record.get("worktree") or ""))
    base = str(record.get("base_sha") or record.get("base") or "").strip()
    if not base or not worktree.is_dir():
        return []
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, "--"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if tracked.returncode or untracked.returncode:
        return []
    paths = {
        os.fsdecode(raw)
        for raw in (*tracked.stdout.split(b"\0"), *untracked.stdout.split(b"\0"))
        if raw
    }
    return sorted(paths)


def _derived_manifest_text(record: Mapping[str, Any], paths: list[str]) -> str:
    """Render evidence that recovery found without claiming worker delivery."""
    node = str((record.get("node") or {}).get("id") or record.get("run_id") or "")
    final_message = " ".join(str(record.get("final_message") or "").split())
    changed = ", ".join(paths) or "none"
    evidence = (
        f"final message: {final_message}" if final_message else "final message: none"
    )
    return (
        f"node: {node}\n"
        "status: derived\n"
        "derived: true\n"
        "derived_reason: terminal run omitted its worker manifest\n"
        "commits: none\n"
        f"changed_paths: {changed}\n"
        "tests: not verified — worker manifest missing\n"
        "test_logs: none\n"
        "baseline_suite: none\n"
        "after_suite: none\n"
        "artifacts: none\n"
        f"evidence_inputs: {evidence}\n"
        "follow_ons: none\n"
        "blockers: replace this derived artifact with a worker-written manifest\n"
    )


def _derive_missing_manifest(
    record: Mapping[str, Any], *, config: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """Preserve terminal evidence without turning it into delivered work."""
    fences = (config or {}).get("fences") or {}
    if fences.get("manifest_required", True) is False:
        return record
    if str(record.get("phase") or "") not in {"complete", "failed"}:
        return record
    manifest_value = str(record.get("manifest_path") or "")
    if not manifest_value:
        return record
    manifest = Path(manifest_value)
    if manifest.exists():
        return record
    paths = _worktree_diff_paths(record)
    final_message = str(record.get("final_message") or "").strip()
    if not paths and not final_message:
        return record
    manifest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with manifest.open("x", encoding="utf-8") as handle:
            handle.write(_derived_manifest_text(record, paths))
    except FileExistsError:
        # Worker delivery won the race and remains authoritative.
        return record

    run_id = str(record.get("run_id") or "")

    def record_gap(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["delivery_gap"] = {
            "kind": "missing-worker-manifest",
            "derived_manifest_path": str(manifest),
            "derived_at": _utc_now(),
            "final_message_present": bool(final_message),
            "changed_paths": paths,
        }
        return pointer

    return _mutate_pointer(run_id, record_gap) if run_id else record


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


@contextmanager
def _watch_registration(project: str, stall_window: str):
    """Register a watcher together with the process responsible for reaping it."""
    with _project_watch_claim(project, stall_window) as (acquired, watcher):
        if acquired:
            parent_pid = os.getppid()
            watcher.update(
                {
                    "parent_pid": parent_pid,
                    "parent_start_time": _process_start_time(parent_pid),
                }
            )
            with watch_lock_path(project).open("r+b") as handle:
                _write_watch_record(handle, watcher)
        yield acquired, watcher


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


def agent_label(pointer: Mapping[str, Any]) -> str:
    """Compact `model/effort` — or `alias·effort` — for the ticker.

    Read from the configuration persisted at dispatch rather than from current
    flight config, because a later config change must not silently restate what
    ran. The alias and its effort spelling are display decisions frozen at
    dispatch, so an aliased run renders the alias in place of the model it
    shortens; the composition is the renderer's so the two cannot drift. A run
    dispatched before aliases existed carries no alias and keeps the
    precomposed `model/effort` form it rendered then. Absent fields are simply
    omitted: a partial label is still useful and an invented one is not.
    """
    agent = pointer.get("agent")
    if not isinstance(agent, Mapping):
        return ""
    alias = str(agent.get("alias") or "").strip()
    if not alias:
        model = str(agent.get("model") or "").strip()
        effort = str(agent.get("effort") or "").strip()
        if model and effort:
            return f"{model}/{effort}"
        return model or effort
    return _agent_label(agent)


def _pointer_role(pointer: Mapping[str, Any]) -> str:
    """The dispatch role a run carried, from its own record.

    Read from the persisted pointer rather than current config for the same
    reason :func:`agent_label` reads its agent block from the record: a later
    role change must not restate what actually ran. Dispatch writes the role on
    the record root and on the node, so either spelling is accepted. The display
    narrowing (``documentation`` to ``docs``, unknown to the marker) happens in
    the renderer where the column lives; the snapshot threads the raw spelling.
    """
    role = str(pointer.get("role") or "").strip()
    if not role:
        role = str(((pointer.get("node") or {}) or {}).get("role") or "").strip()
    return role


# A state that needs action is exactly a state that may explain itself, so this
# is the grid's set rather than a second copy of it.
# The actionable states may keep the classifier's reason; an unreadable
# manifest is one of them, because the refusal text naming the rejected format
# is the one sentence a reader needs before repairing the file.
EXPLAINED_STATES = frozenset(NEEDS_ACTION | WAITING_STATES | {"unreadable"})


def _watch_snapshot(
    pointer: Mapping[str, Any], *, moment: float, stall_seconds: int
) -> dict[str, Any]:
    """Reduce one pointer to the state and reason a ticker compares."""
    row = classify_pointer(
        pointer,
        now_seconds=moment,
        stale_after_seconds=stall_seconds,
    )
    phase = str(pointer.get("phase") or "")
    classification = str(row.get("classification") or "")
    alive = row.get("process_alive")

    # The working bucket is keyed on a process that is genuinely still alive,
    # never on the record phase alone: a run whose process died at any phase it
    # held — the starting phase included — has stopped working regardless of the
    # label the last writer left behind. classify_pointer has already checked
    # the process table, so a dead process falls through to the abandoned state
    # a coordinator must act on instead of the stale working label. A manifest
    # that has reached a verdict likewise cannot keep a run in working, so the
    # terminal readings are arbitrated before any working state is chosen. The
    # classifier also defers complete and failed reports while the pointer says
    # their process is alive, so this reducer consumes that decision instead of
    # deriving a second verdict from the manifest.
    if classification == "completed_unpromoted":
        state = "complete"
    elif classification == WAITING_STATUS:
        state = "wait-aged" if row.get("wait_overdue") else WAITING_STATUS
    elif classification in {"blocked", "failed"}:
        # A provider refusal blocks even though no manifest reached a verdict:
        # the process is gone, but the stop is triageable and resumable once
        # the limit lifts, so it reads as a block rather than an abandonment.
        state = classification
    elif classification == "unreadable":
        # A manifest that is present but unreadable is neither a delivery nor
        # an absence, so the run reads as unreadable rather than falling into
        # the abandoned bucket the liveness checks below would assign it.
        state = "unreadable"
    elif phase == "stopped":
        state = "stopped"
    elif alive is False:
        state = "abandoned"
    elif classification == "running" or phase in {"working", "running"}:
        state = "dispatched" if phase == "starting" else "working"
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

    if state in ("dispatched", "working"):
        # A run stops progressing whether it dies during dispatch or mid-work,
        # so the stall check has to reach every non-terminal state a pointer
        # can sit in — gating it on "working" alone left a run killed before
        # its phase ever advanced past "starting" permanently exempt.
        quiet = _stream_quiet_seconds(pointer, now_seconds=moment)
        if quiet > stall_seconds:
            state = "stalled"
            detail = f"stream quiet for {quiet}s"
        else:
            detail = ""
    elif state not in EXPLAINED_STATES:
        # Named as the states that MAY explain themselves rather than the ones
        # that may not. An allow-list of states to clear leaves every state
        # added later carrying whatever the classifier attached, which makes
        # routine progress read as a warning.
        detail = ""

    # What ran it, as facts rather than a display string. The alias and effort
    # spelling were decided at dispatch and frozen onto the pointer; a later
    # configuration edit must not restate what ran, so the facts are read from
    # the record. Composition is the renderer's, so the model and effort travel
    # separately and the monitor decides how they read.
    agent_map = (
        pointer.get("agent") if isinstance(pointer.get("agent"), Mapping) else {}
    )
    return {
        "run_id": str(row.get("run_id") or ""),
        "node": str(row.get("node") or row.get("run_id") or "unknown"),
        # The dispatching session, so a reader can tell its own fleet from a
        # peer's on a stream that is necessarily project-wide.
        "session": str(pointer.get("session") or ""),
        "backend": str(agent_map.get("backend") or "").strip(),
        "model": str(agent_map.get("model") or "").strip(),
        "effort": str(agent_map.get("effort") or "").strip(),
        "alias": str(agent_map.get("alias") or "").strip(),
        # What kind of work it is, on the record the same way. Read beside the
        # agent because the two describe the same run and are reduced the same
        # way — the snapshot carries the raw spelling and the renderer narrows
        # it to fit its column.
        "role": _pointer_role(pointer),
        # Whether this run is a shadow of a committed primary. Dispatch decides
        # shadowship at launch and writes the lineage onto the pointer; the
        # renderer dims a shadow row end to end from that fact, so the snapshot
        # carries it under its own name rather than as a flattened display flag.
        "lineage": pointer.get("lineage"),
        "state": state,
        # The full, untruncated reason. The bounded clause a reader can act on
        # is derived from it at render time, so nothing here is shaped for the
        # grid before it is stored.
        "detail": detail,
        # The fact a block's glyph is derived from. Only a "blocked" state
        # carries a marker; a run entering any other state has nothing for the
        # reader to answer or read a manifest for.
        "needs_help_complete": row.get("needs_help_complete"),
        "wait_overdue": row.get("wait_overdue"),
    }


# The ordinary three buckets remain unchanged when no external wait exists. A
# waiting bucket appears while at least one declared condition is outstanding,
# keeping healthy waits out of both work-in-progress and needs-action figures.
# Every snapshot belongs to exactly one bucket, so the figures still add up.
FLEET_WORKING_STATES = ("dispatched", "working", "running")
FLEET_UNPROMOTED_STATES = ("complete", "completed_unpromoted")
FLEET_WAITING_STATES = tuple(sorted(WAITING_STATES))
# The same set again: what the counter calls blocked is what a reader must act
# on, and a state in one and not the other is a number nobody can explain.
FLEET_BLOCKED_STATES = tuple(sorted(NEEDS_ACTION))


def _fleet_counts(snapshots: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """Partition the fleet into working, blocked, delivered, and waiting work.

    ``working`` is what a reader means by a live worker. ``blocked`` is
    everything that has stopped progressing and needs the coordinator, a stall
    or a failure included. ``unpromoted`` is delivered work waiting on a gate.
    ``waiting`` is a run whose declared external condition remains outstanding.
    A run that leaves the fleet is in none of them.
    """
    states = [str(snapshot.get("state") or "") for snapshot in snapshots.values()]
    counts = {
        "working": sum(state in FLEET_WORKING_STATES for state in states),
        "blocked": sum(state in FLEET_BLOCKED_STATES for state in states),
        "unpromoted": sum(state in FLEET_UNPROMOTED_STATES for state in states),
    }
    waiting = sum(state in FLEET_WAITING_STATES for state in states)
    if waiting:
        counts["waiting"] = waiting
    return counts


def fleet_transitions(
    known: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
) -> tuple[
    list[tuple[dict[str, Any], str | None, str, dict[str, int]]],
    dict[str, dict[str, Any]],
]:
    """Fold one fleet observation into ordered transitions and the next state.

    The counts travel per transition, recomputed after each one is applied,
    because a line's numbers are read as the fleet *at that line*. Stamping one
    batch-wide count on every line of a multi-transition poll describes the end
    of the batch instead: a promotion would report the fleet it had already
    left, and three simultaneous landings would all claim the third one's
    totals.

    Departures first, then arrivals, then state changes — a promotion frees its
    slot before the next dispatch is counted into it, which is the order a
    reader infers from the numbers.
    """
    running = {run_id: dict(snapshot) for run_id, snapshot in known.items()}
    changes: list[tuple[Mapping[str, Any], str | None, str]] = []

    for run_id in (item for item in known if item not in current):
        # A departure is its own fact and inherits no clause or marker from the
        # state it left. Carrying one forward reports a block on the line
        # announcing that the block is over.
        departed = {**known[run_id], "detail": "", "needs_help_complete": None}
        changes.append((departed, str(known[run_id]["state"]), "promoted"))
    for run_id in (item for item in current if item not in known):
        changes.append(
            (
                {
                    **current[run_id],
                    "state": "dispatched",
                    "detail": "",
                    "needs_help_complete": None,
                },
                None,
                "dispatched",
            )
        )
    for run_id in (item for item in current if item in known):
        previous = str(known[run_id]["state"])
        state = str(current[run_id]["state"])
        if state != previous:
            changes.append((current[run_id], previous, state))

    events: list[tuple[dict[str, Any], str | None, str, dict[str, int]]] = []
    for snapshot, previous, state in changes:
        run_id = str(snapshot.get("run_id") or "")
        if state == "promoted":
            running.pop(run_id, None)
        else:
            running[run_id] = dict(snapshot)
        events.append((dict(snapshot), previous, state, _fleet_counts(running)))
    return events, running


def _watch_transition(
    project: str,
    *,
    kind: str,
    snapshot: Mapping[str, Any],
    previous: str | None,
    current: str,
    counts: Mapping[str, int],
) -> dict[str, Any]:
    """Build one lossless transition object for text or JSON rendering.

    This is the surface the events log persists, so it carries facts only: the
    model, effort, alias and backend separately, the full untruncated detail,
    and the structured fact a block's glyph is derived from. No composed label,
    no pre-claused reason and no display glyph are written here — the monitor
    derives those from these facts, so the log stays re-renderable.
    """
    event = {
        "project": project,
        "event": kind,
        "observed_at": _utc_now(),
        "run_id": snapshot.get("run_id"),
        "node": snapshot.get("node"),
        "session": snapshot.get("session") or "",
        "role": snapshot.get("role") or "",
        # The shadow lineage the snapshot carried from the pointer, threaded
        # through the field-by-field rebuild so the events log records the same
        # fact the renderer reads to dim the row.
        "lineage": snapshot.get("lineage"),
        "backend": str(snapshot.get("backend") or ""),
        "model": str(snapshot.get("model") or ""),
        "effort": str(snapshot.get("effort") or ""),
        "alias": str(snapshot.get("alias") or ""),
        "from_state": previous,
        "to_state": current,
        "working": counts["working"],
        "blocked": counts["blocked"],
        "unpromoted": counts["unpromoted"],
        "detail": str(snapshot.get("detail") or ""),
        "needs_help_complete": snapshot.get("needs_help_complete"),
    }
    if "waiting" in counts or previous in WAITING_STATES or current in WAITING_STATES:
        event["waiting"] = counts.get("waiting", 0)
    return event


# Rendering a transition is a layout concern with its own contract, so it lives
# beside the grid it fills. The plain default keeps this module's callers, and
# every test that reads a line as a string, free of escape sequences.
_PLAIN = Ticker()


def format_watch_transition(
    event: Mapping[str, Any],
    *,
    with_session: bool = False,
    ticker: Ticker | None = None,
) -> str:
    """Render one transition as the compact human-facing watch line.

    ``ticker`` supplies a caller's own grid — the CLI passes one carrying the
    reader's width, theme and colour choice. Omitted, the shared plain grid
    renders, because there is no terminal to detect: the pane is a pipe, so
    colour is a decision a caller makes rather than one this module can infer.
    """
    if event.get("legacy"):
        return str(event.get("rendered") or "")
    return (ticker or _PLAIN).render(event, with_session=with_session)


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

    with _watch_registration(project, stall_window) as (acquired, watcher):
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

            folded, next_known = fleet_transitions(known, current)
            events = [
                _watch_transition(
                    project,
                    kind="transition",
                    snapshot=snapshot,
                    previous=previous,
                    current=state,
                    counts=event_counts,
                )
                for snapshot, previous, state, event_counts in folded
            ]
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

    with _watch_registration(project, stall_window) as (acquired, watcher):
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
        observed = _derive_missing_manifest(observed, config=config)
        report = classify_pointer(observed)
        if unreadable:
            report["detail"] = f"{report['detail']} (stream unreadable — {unreadable})"
        reports.append(report)
    counts = {
        name: sum(1 for item in reports if item["classification"] == name)
        for name in ("running", "completed_unpromoted", "abandoned")
    }
    for name in ("waiting", "stopped", "blocked", "failed", "unreadable"):
        count = sum(1 for item in reports if item["classification"] == name)
        if count:
            counts[name] = count
    return {"runs": reports, "counts": counts, "classes": list(RECOVERY_CLASSES)}
