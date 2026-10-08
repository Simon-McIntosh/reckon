from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from reckon import ledger, review_tiers
from reckon._timestamps import parse_utc
from reckon.capabilities import _charged_input_from_usage
from reckon.crew import lane_document as _lane_document
from reckon.crew import metering, plan_review, quota_weight, runs
from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import review_need
from reckon.crew.host_lease import LEASE_RENEW_SECONDS
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    DEFAULT_WATCH_STALL_WINDOW,
    INTERRUPTED_RUN_PHASE,
    LOG_STALE_AFTER_SECONDS,
    CrewError,
    parse_duration,
)
from reckon.crew.reports import (
    NON_TERMINAL_MANIFEST_STATUSES,
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    manifest_status_is_template,
    parse_manifest,
)
from reckon.crew.routing import _signal_process_group
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
    producer_lease_seconds,
    read_pointer,
    update_watch_registration,
    watch_lease_renewed_at,
    watch_lock_path,
)
from reckon.crew.ticker import NEEDS_ACTION, Ticker, _agent_label

# The waiting family is the stop that lifts itself, an overdue wait included:
# a run whose declared external wait has aged past its expectation has not
# failed and nothing about it lifts by inspection, so it is still waiting — the
# fleet counts it here, never in the blocked tally. Its age is the news, and
# the news is carried by the action marker on its row, so the wait-aged state
# also sits in the action set while remaining a member of this family.
WAITING_STATES = frozenset({"waiting", "wait-aged", "paused", "queued"})


# Commits-beyond-base per (worktree, base), keyed against the worktree head it
# was taken at. A tree that has not moved answers from here, so an unchanged
# poll spawns no git; a moved head is recounted. Placeholder head identities
# (no tree, no git dir, no head) are never cached, because they name a state
# that will change once the tree or its git directory appears.
_COMMITS_BEYOND_BASE_CACHE: dict[tuple[str, str], tuple[str, int]] = {}

# A live producer classifies the whole fleet on every poll, so this cache gains
# an entry per (worktree, base) it has ever classified. A worktree that has been
# reclaimed leaves its entries behind, and without a bound a long-lived producer
# accumulates one per run's worktree for as long as it runs. Past this many
# entries the cache drops the counts whose worktree is no longer on disk, then
# the oldest of what remains, so it stays bounded by the live fleet rather than
# by every run the producer has ever seen.
_COMMITS_BEYOND_BASE_CACHE_LIMIT = 256


def _evict_gone_worktrees() -> None:
    """Drop cached commit counts whose worktree is no longer on disk.

    A reclaimed worktree cannot move its head again, so its count can never be
    answered from the cache; the entry is dead weight the moment the directory
    goes. Removing it is the cheapest half of keeping the cache bounded, done
    only when the cache is at its ceiling so an ordinary poll pays nothing for
    it.
    """
    cache = _COMMITS_BEYOND_BASE_CACHE
    for key in [key for key in cache if not Path(key[0]).is_dir()]:
        cache.pop(key, None)


def _remember_commits(key: tuple[str, str], head: str, count: int) -> None:
    """Store one commit count, keeping the cache bounded first."""
    cache = _COMMITS_BEYOND_BASE_CACHE
    if len(cache) >= _COMMITS_BEYOND_BASE_CACHE_LIMIT and key not in cache:
        _evict_gone_worktrees()
        # Still full of live worktrees: drop the oldest so a fleet larger than
        # the ceiling evicts rather than growing without bound.
        while len(cache) >= _COMMITS_BEYOND_BASE_CACHE_LIMIT:
            cache.pop(next(iter(cache)), None)
    cache[key] = (head, count)


def _commits_beyond_base(record: Mapping[str, Any]) -> int:
    """Count commits in the worktree past the pointer's recorded base.

    The count lives in git, so it survives any manifest format: a worktree
    whose history carries commits after the base the run launched from
    delivered work, whatever the manifest says, and that fact is never erased
    by a missing or unreported manifest. Zero when the worktree or base is
    absent or the count cannot be read — an unreadable tree proves nothing, so
    it must not fabricate a rescue.

    The count is a function of the worktree's revision, which is read from the
    git directory as files rather than by a subprocess, so it is cached against
    that revision. A run whose tree has not moved is answered without spawning
    git again; when the head moves the count is taken afresh.
    """
    worktree_value = str(record.get("worktree") or "").strip()
    base = str(record.get("base_sha") or record.get("base") or "").strip()
    if not worktree_value or not base:
        return 0
    worktree = Path(worktree_value)
    if not worktree.is_dir():
        return 0
    head = _worktree_head_identity(worktree)
    cacheable = head not in {"no-tree", "no-git", "no-head"}
    key = (str(worktree), base)
    if cacheable:
        cached = _COMMITS_BEYOND_BASE_CACHE.get(key)
        if cached is not None and cached[0] == head:
            return cached[1]
    count = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..HEAD"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if count.returncode != 0:
        return 0
    try:
        resolved = max(0, int(count.stdout.decode().strip()))
    except (ValueError, UnicodeDecodeError):
        return 0
    if cacheable:
        _remember_commits(key, head, resolved)
    return resolved


def _worktree_diff_paths(record: Mapping[str, Any]) -> list[str]:
    """Return the base-to-worktree path census used for recovery evidence."""
    worktree_value = str(record.get("worktree") or "").strip()
    base = str(record.get("base_sha") or record.get("base") or "").strip()
    if not worktree_value or not base:
        return []
    worktree = Path(worktree_value)
    if not worktree.is_dir():
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
    if _commits_beyond_base(record):
        # The work is already committed past the recorded base: git is the
        # evidence, and no recovery artifact should be fabricated over it with
        # a "commits: none" that the history contradicts.
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


def closure_disposition_valid(disposition: str, classification: str) -> bool:
    """Whether a recorded closure disposition excuses a pointer from the fences.

    This is the single definition of ``reconciled`` shared by the closure drain
    and the dispatch fence, so a pointer the drain counts as reconciled is never
    refused by dispatch, and a pointer either surface still calls unreconciled
    is refused on both. A ``handed-off`` disposition remains valid until the
    receiving session reconciles the pointer; ``still-working`` excuses only a
    pointer whose current classification is still ``running``. Any missing,
    malformed or unknown disposition, or a disposition outlived by its run, is
    not valid.
    """
    return disposition == "handed-off" or (
        disposition == "still-working" and classification == "running"
    )


def _partition_session_rows(
    rows: Iterable[Mapping[str, Any]], session: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate one coordinator's rows from visible peer-session rows.

    Omitting a session preserves the project-wide interpretation: every row is
    counted and there is no peer partition. Supplying one uses the dispatching
    session already persisted on each pointer, so aiming the fence adds no
    ownership state of its own. A legacy row with no recorded owner remains in
    the counted set: absence cannot prove that a live pointer belongs to a peer.
    """
    copied = [dict(row) for row in rows]
    if session is None:
        return copied, []
    own: list[dict[str, Any]] = []
    peers: list[dict[str, Any]] = []
    for row in copied:
        owner = str(row.get("session") or "")
        (own if not owner or owner == session else peers).append(row)
    return own, peers


def overdue_unreconciled_runs(
    *,
    project: str,
    grace: str,
    now_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Return actionable terminal pointers older than the configured grace.

    A pointer is listed only when it is terminal past the grace AND not excused
    by a recorded closure disposition: the same predicate the closure drain
    uses, so a past-grace pointer that carries no valid disposition is still
    refused here (the forgotten-work case) while one the drain counts as
    reconciled is never refused.
    """
    if not grace:
        return []
    grace_seconds = parse_duration(grace)
    rows = []
    for pointer in list_live(project=project):
        row = classify_pointer(pointer, now_seconds=now_seconds)
        age = row.get("terminal_age_seconds")
        if (
            row["classification"]
            in {"scoring", "promotable", "completed_unpromoted", "blocked", "paused"}
            and isinstance(age, int)
            and age > grace_seconds
        ):
            recorded = pointer.get("closure_disposition")
            disposition = (
                str(recorded.get("kind") or "") if isinstance(recorded, Mapping) else ""
            )
            if not closure_disposition_valid(disposition, row["classification"]):
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
            # The record is written through the seat handle the claim holds,
            # never by reopening the path: an unlink in the moment between
            # taking the seat and this write would make the reopen raise
            # FileNotFoundError and end the producer before it polls once.
            _write_watch_record(runs._WATCH_SEAT_HANDLES[project], watcher)
        yield acquired, watcher


UNWATCH_SEAT_WAIT_SECONDS = 2.0


def unwatch(project: str) -> dict[str, Any]:
    """Stop the local watcher, refusing remote and unresponsive seat holders."""
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    lease = runs.watch_host_lease(project)
    holder = lease.holder()
    if holder is not None and holder.host != socket.gethostname():
        raise CrewError(
            f"refusing to unwatch {project!r}: producer seat held by "
            f"{holder.host} pid {holder.pid} job {holder.job or 'unknown'}"
        )
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            watcher = _read_watch_record(handle)
            if runs._seat_names_a_foreign_host(watcher):
                raise CrewError(
                    f"refusing to unwatch {project!r}: producer seat names "
                    f"{watcher['host']} pid {watcher.get('pid')}"
                ) from None
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

            # A watcher has no run directory, so the watch directory the seat
            # registration sits in is the sender file's home. The shared writer
            # owns the attribution and outcome records, so unwatch names that
            # directory and the project the watcher serves rather than writing
            # an attribution of its own: the project rides its own field, which
            # a reader of the shared directory can act on without parsing a
            # message.
            try:
                _signal_process_group(
                    pid,
                    watcher.get("pid_start_time"),
                    run_dir=path.parent,
                    reason="unwatch",
                    project=project,
                )
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
            deadline = time.monotonic() + UNWATCH_SEAT_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CrewError(
                            f"refusing to unwatch {project!r}: producer seat held "
                            f"by {watcher.get('host') or socket.gethostname()} "
                            f"pid {pid} beyond {UNWATCH_SEAT_WAIT_SECONDS:g}s"
                        ) from None
                    time.sleep(0.05)
            _write_watch_record(handle, {})
            if holder is not None:
                lease.release_holder(holder)
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
        if holder is not None:
            lease.release_holder(holder)
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


# A state that needs action always may explain itself, and so may a member of
# the waiting family: the clause on a waiting row names what lifts it and on a
# blocked row names what a reader can do, so the explained set is the action
# set plus the self-lifting family — an overdue wait is explained by both
# routes at once. An unreadable manifest is one of the actionable states,
# because the refusal text naming the rejected format is the one sentence a
# reader needs before repairing the file.
EXPLAINED_STATES = frozenset(
    NEEDS_ACTION
    | WAITING_STATES
    | {"unreadable", "unwritten", "ended-without-manifest"}
)


def _promote_record_holds(record: Mapping[str, Any]) -> bool:
    """Whether this live pointer's completed run has a committed ledger row.

    Promotion appends the run's ledger row and then removes the live pointer,
    so for the length of that window the pointer still exists while the work
    has already landed. A classifier that reads only the pointer sees a
    completed manifest whose review no longer matches the moved head and calls
    the run unpromoted — a settled completion reported as unfinished work. The
    ledger row settles the run even when it declares no repository change, so
    row presence, rather than its commit list, answers this question.

    The row is read from the run's own repository, the root promotion wrote it
    under. Promotion writes the per-run file before it touches the aggregate,
    so a single stat answers whether the row was committed.
    An unreadable or absent row answers False — the run then takes the word its
    pointer earns, which is the safe direction because the alternative promises
    a landing nothing recorded.
    """
    from reckon import ledger as ledger_module

    run_id = str(record.get("run_id") or "")
    project = str(record.get("project") or "")
    # The row is read from the run's own repository, so a pointer that records
    # none cannot say where the row would be. Resolving a default root instead
    # would answer a promotion from a directory the run does not own — a row
    # another run or a fixture left there would read as this run's landing.
    repo = str(record.get("repo") or "")
    if not run_id or not project or not repo:
        return False
    try:
        return ledger_module.run_path(project, run_id, repo).is_file()
    except (OSError, ValueError, ledger_module.LedgerError):
        return False


def _recorded_pointer_word(record: Mapping[str, Any]) -> str:
    """Name a settled pointer's committed work or recorded completion.

    The row existence check already established settlement. A row that cannot
    be decoded cannot establish a code landing, so its safe word is recorded.
    """
    from reckon import ledger as ledger_module

    try:
        path = ledger_module.run_path(
            str(record.get("project") or ""),
            str(record.get("run_id") or ""),
            str(record.get("repo") or ""),
        )
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ledger_module.LedgerError):
        return "recorded"
    return (
        "promoted"
        if isinstance(row, Mapping)
        and row.get("run_id") == record.get("run_id")
        and row.get("commits")
        else "recorded"
    )


def _stall_window_seconds(row: Mapping[str, Any], stall_seconds: int) -> int:
    """The window a quiet run is judged against before it reads stalled.

    A run whose worker process is alive with a live process under it is not
    idle: the child is the job the worker is waiting on — a build, a scheduler
    reservation, a probe — and the worker's own stream stays silent for as long
    as the child takes, so the silence says nothing about whether the worker is
    hung. For that shape the window extends to the run's own time budget, the
    allowance the run declared for exactly this work; past it the run's own
    fence speaks. The extension is never a narrowing, so a budget shorter than
    the window leaves the window where it was.

    A run whose worker has no live process under it keeps the window, and that
    is the deliberate narrowing: a live worker with nothing running beneath it
    is the case a stall most often means is hung.
    """
    if (
        row.get("process_alive") is not True
        or row.get("process_descendant_alive") is not True
    ):
        return stall_seconds
    budget_seconds = _int_or_none(row.get("budget_seconds"))
    if budget_seconds is None:
        return stall_seconds
    return max(stall_seconds, budget_seconds)


def _stall_reading(
    state: str,
    detail: str,
    *,
    quiet: int | None,
    window: int,
    pause_reason: str | None,
    process_state: str | None,
) -> tuple[str, str]:
    """Apply the stream-silence reading to a non-terminal state.

    The only part of a verdict that moves with the clock rather than with a
    file: a run that was working and has gone quiet past its window is paused
    when something explains the silence and stalled when nothing does. It is a
    pure function of the state, the silence and the window so a producer that
    reuses a snapshot can re-derive exactly this decision from stored inputs,
    without reading a stream or a manifest again: the silence is recomputed from
    the stream's own stat and the rest is carried on the verdict.
    """
    if state not in ("dispatched", "working"):
        return state, detail
    if quiet is None or quiet <= window:
        return state, detail
    if pause_reason is not None:
        return (
            WAITING_STATUS,
            f"paused: sitting in {pause_reason} for {quiet}s; it lifts itself",
        )
    return "stalled", f"{process_state}, quiet {quiet // 60}m"


def _watch_verdict(
    pointer: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    moment: float,
    stall_seconds: int,
) -> dict[str, Any]:
    """Complete individual and grouped verdicts with the fleet vocabulary.

    Producers carry this result without probing processes, streams or manifests
    for a second judgement.
    """
    if row.get("lane_event"):
        state = "lane-event"
        previous = row.get("fleet_verdict")
        return {
            **(previous if isinstance(previous, Mapping) else {}),
            "state": state,
            "detail": str(row.get("detail") or ""),
            "recovery_classification": state,
            "recovery": str(row.get("recovery") or "inspect"),
            "lifting_condition": None,
        }
    stored_phase = str(pointer.get("phase") or "")
    # The stored phase is the launcher's label; the row carries the phase the
    # run's own evidence supports, so a pointer that never advanced past
    # starting does not pin the run in a pre-spawn bucket while it works.
    phase = str(row.get("effective_phase") or stored_phase)
    classification = str(row.get("classification") or "")
    alive = row.get("process_alive")

    if _promote_record_holds(pointer):
        # A committed row outranks every reading of the pointer, including a
        # report-only completion with no code commit. The pointer remains for
        # a short window after that row lands; both outcomes are settled there.
        state = _recorded_pointer_word(pointer)
        return {
            "state": state,
            "detail": "",
            "recovery_classification": state,
            "recovery": "",
            "lifting_condition": None,
        }

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
    if classification == "scoring":
        state = "completed_unpromoted"
    elif classification in {"promotable", "completed_unpromoted"}:
        state = "complete"
    elif classification == WAITING_STATUS:
        # A declared external wait stays in the waiting family even when it has
        # aged past its expectation — the run has not failed, so the fleet keeps
        # counting it as waiting rather than blocked. The age is the news, and
        # the news is carried by the action marker on the row a reader sees.
        state = "wait-aged" if row.get("wait_overdue") else WAITING_STATUS
    elif classification == "paused":
        # A paused run is a member of the waiting family: nothing needs a
        # person, so it renders under the same calm verb as a declared external
        # wait and its classifier detail names what lifts it. Rendering it as
        # its own grid word would need a second routing route with no reader
        # benefit — the distinction that matters is that it is not blocked.
        state = WAITING_STATUS
    elif classification == INTERRUPTED_RUN_PHASE:
        # The compatibility watch vocabulary does not yet expose interruptions
        # as their own column. Keep the run in the needs-action bucket while the
        # row's recovery classification and action retain the precise cause.
        state = "blocked"
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
    elif classification == "exited-unfinished":
        # The worker's recorded exit ended the run with its work committed
        # while the manifest still reads a working status. Nothing about the
        # stop is unresolved, so the row is neither a block nor an abortion:
        # it reads as its own state and the clause names the verdict word the
        # record still needs.
        state = "exited-unfinished"
    elif str(row.get("recovery_classification") or "") == "unwritten":
        # The compatibility state stays non-terminal while the typed surface
        # names that the worker never replaced its template. This keeps a
        # placeholder from satisfying a terminal fence without inventing a
        # second attention vocabulary in the run registry.
        state = "running"
    elif phase == "stopped":
        state = "stopped"
    elif alive is False:
        # Abandoned means the worker died with nothing of the run surviving it.
        # A dead process whose manifest reported it was still working, or whose
        # worktree carries commits past its base, left work that outlived the
        # process: the classifier reads it running, and this reducer must not
        # paint the same run dead or the pane would disagree with the reader.
        if classification == "running" and (
            (row.get("manifest_status") or "") in NON_TERMINAL_MANIFEST_STATUSES
            or row.get("commits_beyond_base")
        ):
            state = "working"
        else:
            state = "abandoned"
    elif classification == "running" or phase in {"working", "running"}:
        state = "dispatched" if phase == "starting" else "working"
    else:
        state = classification or phase or "unknown"

    # A dead worker is a row on the snapshot that observes the death, never one
    # held behind the stall window. The deferrals above read a non-terminal
    # manifest or retained commits as the worker's own last word, and that word
    # was written before the process ended; the process table has now falsified
    # it, so the deferral stops here. Which end this was is the stream's to say:
    # a last record of result is a turn that ran to its own conclusion, whose
    # remedy is to continue it, while any other last record is a death
    # mid-turn, whose cause a reader has to see before choosing a recovery.
    # The row then reads as the classifier's interrupted run — the reading it
    # already owns for a worker that died — rather than as a second vocabulary
    # this reducer would have to keep in step.
    death_reason = None
    ended_without_manifest = False
    if alive is False and state in ("dispatched", "working"):
        last_record_type = _newest_stream_last_record_type(pointer)
        if last_record_type == STREAM_RESULT_RECORD_TYPE:
            # The worker's process ended after a successful result record and
            # before a terminal manifest: its turn concluded and the record that
            # says so is on disk. Nothing was lost, the session is resumable, and
            # the reading names that rather than the mid-turn death a stalled row
            # would report — a resumable turn has no session to conclude rather
            # than one whose turn was cut off.
            ended_without_manifest = True
            state = "blocked"
        elif last_record_type is not None:
            death_reason = _process_exit_reason(pointer, last_record_type)
            state = "blocked"

    detail = str(row.get("detail") or "")
    for prefix in (
        "the worker manifest reports blocked: ",
        "the worker manifest reports failed: ",
    ):
        if detail.startswith(prefix):
            detail = detail[len(prefix) :]
            break

    # The stream-silence reading is the only part of a verdict that moves with
    # the clock rather than with a file, so it is isolated behind a pure
    # helper and its inputs are exposed on the verdict below. A producer that
    # reuses a snapshot recomputes the silence from the stream's own stat and
    # re-derives exactly this decision without reading anything again.
    stall_base_state = state
    stall_base_detail = detail
    quiet_seconds = None
    stall_window = _stall_window_seconds(row, stall_seconds)
    stall_pause_reason = None
    stall_process_state = None
    if death_reason is not None:
        # The classifier's clause for this record says the run was still
        # working when the process ended, which is the reading this branch
        # exists to replace; the death row states the end it observed instead.
        detail = death_reason
    elif state in ("dispatched", "working"):
        # A run stops progressing whether it dies during dispatch or mid-work,
        # so the stall check has to reach every non-terminal state a pointer
        # can sit in — gating it on "working" alone left a run killed before
        # its phase ever advanced past "starting" permanently exempt.
        quiet_seconds = _run_stream_quiet_seconds(pointer, now_seconds=moment)
        # A quiet stream is a hang only when nothing is waiting. An alive
        # worker sitting in a bounded wait — a sleep, a peer read, a task
        # wait, a rejected window, or a rate-limit retry loop — wakes itself,
        # so it pauses rather than stalling; a genuinely hung process with none
        # of those still stalls and is not weakened here. The stall word covers
        # three situations whose remedies differ: a live worker in a long quiet
        # step needs nothing, a dead one needs a resume, and one whose liveness
        # nothing established needs the check a reader would otherwise run by
        # hand. Death is claimed only where something observed it, and two
        # things can: a pid checked on this host and found dead, or the
        # supervisor's exit record.
        stall_pause_reason = _stall_wait_reason(pointer)
        stall_process_state = _process_reading(
            alive,
            liveness_proven=row.get("liveness_proven") is True,
            exit_record=row.get("exit_record"),
        )
        state, detail = _stall_reading(
            state,
            "",
            quiet=quiet_seconds,
            window=stall_window,
            pause_reason=stall_pause_reason,
            process_state=stall_process_state,
        )
        stall_base_detail = ""
    elif state not in EXPLAINED_STATES:
        # Named as the states that MAY explain themselves rather than the ones
        # that may not. An allow-list of states to clear leaves every state
        # added later carrying whatever the classifier attached, which makes
        # routine progress read as a warning.
        detail = ""

    recovery_classification = str(row.get("recovery_classification") or state)
    recovery_verb = str(row.get("recovery") or "")
    lifting_condition = row.get("lifting_condition")
    if death_reason is not None:
        # A death row carries the classifier's own word for a worker that died,
        # so the cause and remedy a reader sees match what every other surface
        # already calls it rather than a second vocabulary composed here.
        recovery_classification = INTERRUPTED_RUN_PHASE
        recovery_verb = RECOVERY_VERBS[INTERRUPTED_RUN_PHASE]
    elif ended_without_manifest:
        # The classifier's clause for this pointer is about the commits that
        # survived the process; this state is about the end itself, so the
        # reading names the result record that says the turn ended. The end is
        # the clause's own first words because the row is cut to its head: a
        # reader who sees only that much still learns which end this was, and
        # the remedy that follows from it.
        recovery_classification = "ended-without-manifest"
        recovery_verb = RECOVERY_VERBS["ended-without-manifest"]
        lifting_condition = None
        detail = (
            "turn ended: the worker's process exited after a successful result "
            "record and no terminal manifest followed; the run is resumable "
            "rather than stalled"
        )
    elif state == "stalled":
        recovery_classification = "stalled"
        recovery_verb = RECOVERY_VERBS["stalled"]
        lifting_condition = None
    elif state == "wait-aged":
        recovery_classification = "wait-aged"
        recovery_verb = RECOVERY_VERBS["wait-aged"]
    elif state == WAITING_STATUS and classification == "running":
        recovery_classification = "paused"
        recovery_verb = RECOVERY_VERBS["paused"]
        lifting_condition = detail

    return {
        "state": state,
        "detail": detail,
        "recovery_classification": recovery_classification,
        "recovery": recovery_verb,
        "lifting_condition": lifting_condition,
        # The stream-silence inputs, carried so a producer that reuses a
        # snapshot can re-derive the same reading from the stream's stat alone.
        # ``stall_base_state`` is None for a verdict the silence reading never
        # touched, which is what marks a snapshot whose state needs no refresh.
        "stall_base_state": stall_base_state if quiet_seconds is not None else None,
        "stall_base_detail": "" if quiet_seconds is not None else None,
        "stall_window_seconds": stall_window,
        "stall_pause_reason": stall_pause_reason,
        "stall_process_state": stall_process_state,
        "stall_quiet_seconds": quiet_seconds,
    }


def _quiet_clock_latest(record: Mapping[str, Any], *, moment: float) -> float:
    """The latest write instant a run's stream evidence offers, by stat alone.

    The same clock ``runs._stream_quiet_seconds`` falls back to: the pointer's
    own log, then the pointer's mtime, then the run's creation. Read here as an
    absolute instant rather than a delta so a producer reusing a snapshot can
    recompute the silence at a later moment without reading the file again.
    """
    stream = Path(str(record.get("log_path") or ""))
    try:
        if stream.is_file():
            return stream.stat().st_mtime
    except OSError:
        pass
    run_id = str(record.get("run_id") or "")
    if run_id:
        pointer = runs.pointer_path(run_id)
        try:
            if pointer.is_file():
                return pointer.stat().st_mtime
        except OSError:
            pass
    created = parse_utc(str(record.get("created_at") or ""))
    return moment if created is None else created.timestamp()


def _snapshot_reuse_key(record: Mapping[str, Any]) -> str | None:
    """The identity of every input a run's classification is a function of.

    The classification's own composition, as :func:`_classification_inputs`
    resolves it: the pointer, manifest, stream, the run's exit, worker and
    attempt records, the review store's candidates for this run, and the
    worktree's git head. A review landing on a reviewer's target, or its head
    moving, drops the snapshot exactly as it moves the classification. The
    promotion ledger row joins the composition because a promotion writes it
    while the pointer still exists and no other input moves with it. Liveness
    is deliberately not here: it is not a file, so the reuse path reads it
    fresh through the shared host-gated reader.
    """
    run_id = str(record.get("run_id") or "")
    if not run_id:
        return None
    log = Path(str(record.get("log_path") or ""))
    parts = [_classification_key(_classification_inputs(record, log))]
    # The newest stream a run has may differ from the pointer's own log once a
    # resume or a lane change writes beside it; its identity joins the key so a
    # record appended to a resumed stream drops the snapshot.
    found = _record_newest_stream(record)
    parts.append(
        f"stream={_file_identity(found[0]) if found is not None else 'absent'}"
    )
    project = str(record.get("project") or "")
    repo = str(record.get("repo") or "")
    if project and repo:
        from reckon import ledger as ledger_module

        try:
            promote = ledger_module.run_path(project, run_id, repo)
        except (OSError, ValueError):
            promote = None
        parts.append(
            f"promote={_file_identity(promote) if promote is not None else 'absent'}"
        )
    return "|".join(parts)


_SNAPSHOT_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}
_SNAPSHOT_CACHE_LIMIT = 256


def _remember_snapshot(
    run_id: str,
    key: str,
    snapshot: dict[str, Any],
    *,
    cache: dict[str, tuple[str, dict[str, Any]]] | None = None,
) -> None:
    """Store one run's snapshot, keeping the cache bounded.

    Re-storing a run moves it to the most-recent position: a plain assignment
    keeps a key at its original insertion place, so a run stored early and
    updated every poll would sit at the front and be the first evicted by a
    busy process once the cache filled — the entry the poll just wrote.
    """
    store = _SNAPSHOT_CACHE if cache is None else cache
    store.pop(run_id, None)
    store[run_id] = (key, snapshot)
    if len(store) <= _SNAPSHOT_CACHE_LIMIT:
        return
    for rid in list(store):
        if rid != run_id and not runs.run_dir(rid).is_dir():
            del store[rid]
    while len(store) > _SNAPSHOT_CACHE_LIMIT:
        store.pop(next(iter(store)))


def _fresh_liveness(pointer: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    """The process readings a snapshot reports, taken fresh from the process table.

    The same host-gated reading classify_pointer composes: liveness through
    ``local_liveness``, and — only where this host issued the pid — whether
    anything still runs beneath the worker. A signal-0 probe, not a storage
    read, so a poll over a run whose files have not moved takes it rather than
    trusting the reading the previous poll happened to observe.
    """
    alive, proven = local_liveness(pointer)
    descendant: Any = None
    if proven and alive is True:
        worker_pid = (
            _worker_record_pid(pointer)
            if _worker_record_liveness(pointer) is True
            else None
        )
        if worker_pid is None:
            worker_pid = _int_or_none(pointer.get("pid"))
        descendant = _live_descendant(worker_pid) if worker_pid is not None else None
    return (alive, proven, descendant)


def _refresh_snapshot(
    snapshot: Mapping[str, Any], *, moment: float, stall_seconds: int
) -> dict[str, Any]:
    """Re-derive a reused snapshot's clock-derived fields from its stats.

    Everything a snapshot carries was read from files that have not moved, so
    only the silence — the one reading that grows with the clock — is
    recomputed, from the stream instant and the attempt clock already recorded
    on the snapshot and the moment this poll reports. The state and detail are
    then re-derived through the same helper the full recompute uses, so a
    reused snapshot can never disagree with one classified afresh at the same
    moment.

    The window the silence is judged against is recomputed from the calling
    producer's own ``stall_seconds`` rather than read from the frozen snapshot.
    A snapshot is served under a reuse key that does not carry the window, so
    two producers watching one unchanged run with different windows reach the
    same entry; each must judge the run against its own window or one caller's
    verdict leaks into the other's. Everything :func:`_stall_window_seconds`
    reads travels on the snapshot — the liveness pair and the declared budget —
    so the recomputation sees exactly the inputs the full classification saw,
    and the reuse key is left unchanged.
    """
    refreshed = dict(snapshot)
    window = _stall_window_seconds(snapshot, stall_seconds)
    refreshed["stall_window_seconds"] = window
    stream_seconds = snapshot.get("stall_stream_seconds")
    launch_seconds = snapshot.get("stall_launch_seconds")
    quiet: int | None = None
    if stream_seconds is not None or launch_seconds is not None:
        candidates = [
            moment - seconds
            for seconds in (stream_seconds, launch_seconds)
            if seconds is not None
        ]
        quiet = max(0, int(min(candidates)))
    refreshed["quiet_seconds"] = quiet
    base_state = snapshot.get("stall_base_state")
    if base_state is not None:
        state, detail = _stall_reading(
            str(base_state),
            str(snapshot.get("stall_base_detail") or ""),
            quiet=quiet,
            window=window,
            pause_reason=snapshot.get("stall_pause_reason"),
            process_state=snapshot.get("stall_process_state"),
        )
        refreshed["state"] = state
        refreshed["detail"] = detail
    return refreshed


def _compute_watch_snapshot(
    pointer: Mapping[str, Any], *, moment: float, stall_seconds: int
) -> dict[str, Any]:
    """Reduce one pointer to the state and reason a ticker compares."""
    row = classify_pointer(
        pointer,
        now_seconds=moment,
        stale_after_seconds=stall_seconds,
    )
    verdict = row["fleet_verdict"]

    # The absolute instants the silence is measured between, resolved once
    # here so a reused snapshot can recompute it from the stream's stat at a
    # later moment without re-listing the run directory.
    found = _record_newest_stream(pointer)
    if found is not None:
        stream_seconds: float | None = found[1]
        stall_stream_path: str | None = str(found[0])
    else:
        stream_seconds = _quiet_clock_latest(pointer, moment=moment)
        stall_stream_path = None
    attempt_started = _attempt_started_seconds(pointer)
    launch_seconds = (
        attempt_started
        if attempt_started is not None
        else _quiet_clock_latest(pointer, moment=moment)
    )

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
        # The project the run belongs to, carried on the snapshot so the
        # departure fold can resolve the ledger that decides its word when its
        # caller supplies no reader. A snapshot written before this field existed
        # carries none, which leaves a departure's record unknown and therefore
        # departed rather than promised as promoted.
        "project": str(pointer.get("project") or ""),
        # The dispatching session, so a reader can tell its own fleet from a
        # peer's on a stream that is necessarily project-wide.
        "session": str(pointer.get("session") or ""),
        "lane": row.get("lane"),
        "model_key": row.get("model_key"),
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
        "state": verdict["state"],
        "classification": row["classification"],
        "process_alive": row["process_alive"],
        "liveness_proven": row["liveness_proven"],
        # The descendant reading the stall window is widened by, carried so a
        # producer that reuses a snapshot can compare the reading it was built
        # from with a fresh one and drop the entry when the child ends.
        "process_descendant_alive": row.get("process_descendant_alive"),
        # The declared allowance the stall window widens by when a child is
        # live under the worker, carried so a producer that reuses this
        # snapshot recomputes the same window the full classification did
        # rather than reading the one frozen onto the entry.
        "budget_seconds": row.get("budget_seconds"),
        "recovery_classification": verdict["recovery_classification"],
        "recovery": verdict["recovery"],
        "lifting_condition": verdict.get("lifting_condition"),
        "resets_at": row.get("resets_at"),
        "next_action": row.get("next_action"),
        # The full, untruncated reason. The bounded clause a reader can act on
        # is derived from it at render time, so nothing here is shaped for the
        # grid before it is stored.
        "detail": verdict["detail"],
        # The fact a block's glyph is derived from. Only a "blocked" state
        # carries a marker; a run entering any other state has nothing for the
        # reader to answer or read a manifest for.
        "needs_help_complete": row.get("needs_help_complete"),
        "wait_overdue": row.get("wait_overdue"),
        # The rest of a declared wait travels beside its probe's verdict:
        # whether the condition probe has run at all and what it last observed,
        # the horizon the wait was declared against, and the brief a resumed
        # worker reads. A run that declares no wait has taken no measurement, so
        # None is the explicit unmeasured state and a zero never stands in for
        # one.
        "wait_condition_state": row.get("wait_condition_state"),
        "wait_observed": row.get("wait_observed"),
        "expected_horizon_seconds": (row.get("external_wait") or {}).get(
            "expected_horizon_seconds"
        ),
        "resume_brief": (row.get("external_wait") or {}).get("resume_brief"),
        # The manifest facts the fold needs to detect a rewrite: the effective
        # status (empty while a live process defers a terminal report, so a
        # deferred report never reads like a verdict), the commit list, and the
        # content digest that distinguishes a changed rewrite from a touch.
        "manifest_status": str(row.get("manifest_status") or ""),
        "manifest_commits": list(row.get("manifest_commits") or []),
        "manifest_digest": row.get("manifest_digest"),
        # The clock-derived inputs, carried so a producer that reuses this
        # snapshot recomputes the silence from the stream's stat alone rather
        # than re-reading or re-listing anything.
        "quiet_seconds": verdict.get("stall_quiet_seconds"),
        "stall_base_state": verdict.get("stall_base_state"),
        "stall_base_detail": verdict.get("stall_base_detail"),
        "stall_window_seconds": verdict.get("stall_window_seconds"),
        "stall_pause_reason": verdict.get("stall_pause_reason"),
        "stall_process_state": verdict.get("stall_process_state"),
        "stall_stream_seconds": stream_seconds,
        "stall_launch_seconds": launch_seconds,
        "stall_stream_path": stall_stream_path,
    }


def _watch_snapshot(
    pointer: Mapping[str, Any],
    *,
    moment: float,
    stall_seconds: int,
    cache: dict[str, tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Reduce one pointer to the state and reason a ticker compares.

    A run whose classification inputs are all unchanged since the last poll
    serves its previous snapshot: only the silence is recomputed, from the
    stream's stat and the poll's own moment. Any change to an input, or a
    liveness change the snapshot reports, drops the entry and a full
    classification is taken again. ``cache`` is the producer's own store, so a
    cache never carries an answer across two independently armed watchers; the
    module-level cache serves callers that pass none.
    """
    store = _SNAPSHOT_CACHE if cache is None else cache
    run_id = str(pointer.get("run_id") or "")
    served = store.get(run_id)
    key = _snapshot_reuse_key(pointer)
    if run_id and served is not None and key is not None and served[0] == key:
        stored = served[1]
        # A snapshot is reused only while the process reading it was built from
        # still holds. Liveness is not a file: a worker can die, or a child it
        # was waiting on can end, with every file untouched, and the row must
        # change on the poll that observes it. The probe is taken every poll,
        # through the same host-gated reader classify_pointer uses.
        if _fresh_liveness(pointer) == (
            stored.get("process_alive"),
            stored.get("liveness_proven"),
            stored.get("process_descendant_alive"),
        ):
            return _refresh_snapshot(
                stored, moment=moment, stall_seconds=stall_seconds
            )
    snapshot = _compute_watch_snapshot(
        pointer, moment=moment, stall_seconds=stall_seconds
    )
    refreshed = _refresh_snapshot(
        snapshot, moment=moment, stall_seconds=stall_seconds
    )
    if run_id and key is not None:
        _remember_snapshot(run_id, key, refreshed, cache=store)
    return refreshed


# The ordinary three buckets remain unchanged when no external wait exists. A
# waiting bucket appears while at least one declared condition is outstanding,
# keeping healthy waits out of both work-in-progress and needs-action figures.
# Every snapshot belongs to exactly one bucket, so the figures still add up.
FLEET_WORKING_STATES = ("dispatched", "working", "running")
# Both words say that the ledger settled the run. One records landed commits;
# the other records a completed run with no repository commit to claim.
FLEET_SETTLED_STATES = frozenset({"promoted", "recorded"})
# ``departed`` is the word a departure with no resolvable ledger carries: the
# run has gone and no record says whether it landed. It sits with the delivered
# family here so the state vocabulary names every word the fold can emit, while
# a departing run is still dropped from the counted fleet before the counts are
# taken — the word is known, not counted.
FLEET_UNPROMOTED_STATES = ("complete", "completed_unpromoted", "departed")
FLEET_WAITING_STATES = tuple(sorted(WAITING_STATES))
# The blocked bucket is the action set minus the waiting family. The action set
# is the marker set — every state whose row a reader should look at, an overdue
# wait included — but the counter says what kind of run this is, and an overdue
# wait is still waiting. Count and marker therefore separate on that one
# member: what the number reports as blocked is what a reader must act on
# excluding a run that is legitimately still in the waiting column.
FLEET_BLOCKED_STATES = tuple(sorted(NEEDS_ACTION - WAITING_STATES))


def _fleet_counts(
    snapshots: Mapping[str, Mapping[str, Any]], *, session: str | None = None
) -> dict[str, int]:
    """Partition the fleet into working, blocked, delivered, and waiting work.

    ``working`` is what a reader means by a live worker. ``blocked`` is
    everything that has stopped progressing and needs the coordinator, a stall
    or a failure included. ``unpromoted`` is delivered work waiting on a gate.
    ``waiting`` is a run whose declared external condition remains outstanding.
    A run that leaves the fleet is in none of them.

    ``session`` narrows the counted population to the runs that session owns,
    using the ownership already persisted on each snapshot; omitted, the
    partition covers the whole fleet, preserving the project-wide reading. A
    legacy snapshot with no recorded owner stays in the counted set: absence
    cannot prove that a live pointer belongs to a peer.
    """
    rows, _peers = _partition_session_rows(snapshots.values(), session)
    states = [str(snapshot.get("state") or "") for snapshot in rows]
    held = sum(
        str(snapshot.get("recovery_classification") or "") == "held"
        for snapshot in rows
    )
    counts = {
        "working": sum(state in FLEET_WORKING_STATES for state in states),
        "blocked": sum(
            str(snapshot.get("state") or "") in FLEET_BLOCKED_STATES
            and str(snapshot.get("recovery_classification") or "") != "held"
            for snapshot in rows
        ),
        "unpromoted": sum(state in FLEET_UNPROMOTED_STATES for state in states),
    }
    waiting = (
        sum(
            str(snapshot.get("state") or "") in FLEET_WAITING_STATES
            and str(snapshot.get("recovery_classification") or "") != "held"
            for snapshot in rows
        )
        + held
    )
    if waiting:
        counts["waiting"] = waiting
    return counts


def _manifest_rewritten(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    """Whether the report beneath an unchanged verdict was replaced.

    A terminal manifest is the worker's own report and the fold emits one
    state change per verdict, so a worker that replaces that report — the
    measured case: an empty-commit failed placeholder overwritten eighteen
    minutes later by the real failed manifest — must still surface, or the
    coordinator holds the first reading forever. The signal is the content
    digest, not the mtime: a digest fires only when the rewrite changed
    something, while mtime alone fires on a touch of identical content, and a
    transition without news is the noise this display exists to avoid. The
    accepted cost is the opposite hole, a legitimate rewrite to byte-identical
    content goes unseen — it carries no news to deliver, so there is nothing
    a reader should be woken for.

    Both readings must be an effective terminal verdict: a live process's
    terminal report is deferred, so its rewrites stay silent until the process
    dies, and an in-progress manifest is progress, not a verdict.
    """
    if str(previous.get("manifest_status") or "") not in TERMINAL_MANIFEST_STATUSES:
        return False
    if str(current.get("manifest_status") or "") not in TERMINAL_MANIFEST_STATUSES:
        return False
    previous_digest = previous.get("manifest_digest")
    current_digest = current.get("manifest_digest")
    return bool(
        previous_digest and current_digest and previous_digest != current_digest
    )


class _LedgerRunWordReader:
    """Resolve settled words for departures without decoding the whole ledger."""

    def __init__(self, project: str) -> None:
        self.project = project

    def for_runs(self, departures: Iterable[str] | None = None) -> Mapping[str, str]:
        from reckon import ledger as ledger_module

        try:
            recorded = ledger_module.run_ids(self.project)
        except (OSError, ValueError, ledger_module.LedgerError):
            return {}
        targets = recorded if departures is None else recorded.intersection(departures)
        words: dict[str, str] = {}
        aggregate_only: set[str] = set()
        for run_id in targets:
            try:
                path = ledger_module.run_path(self.project, run_id)
                row = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                aggregate_only.add(run_id)
                continue
            except (OSError, ValueError, ledger_module.LedgerError):
                words[run_id] = "recorded"
                continue
            words[run_id] = (
                "promoted"
                if isinstance(row, Mapping)
                and row.get("run_id") == run_id
                and row.get("commits")
                else "recorded"
            )
        if aggregate_only:
            try:
                rows, _version = ledger_module.read_records(
                    self.project, with_figures=False
                )
                words.update(
                    {
                        str(row["run_id"]): (
                            "promoted" if row.get("commits") else "recorded"
                        )
                        for row in rows
                        if row.get("run_id") in aggregate_only
                    }
                )
            except (OSError, ValueError, ledger_module.LedgerError):
                pass
            words.update(dict.fromkeys(aggregate_only - words.keys(), "recorded"))
        return words

    def __call__(self) -> Mapping[str, str]:
        return self.for_runs()


def _ledger_run_id_reader(project: str) -> _LedgerRunWordReader:
    """Read the ledger only when a departure needs its committed word."""
    return _LedgerRunWordReader(project)


def _departure_recorded_run_ids(
    known: Mapping[str, Mapping[str, Any]],
    departures: Sequence[str],
    ledger_run_ids: Callable[[], Iterable[str] | Mapping[str, str]] | None,
) -> dict[str, str] | None:
    """The recorded departure words a fold resolves against.

    An id-only reader means its ids are promotions, preserving the existing
    direct-call contract. When no reader is supplied, one is resolved from the
    departing run's own project, because a caller holding no
    reader — the published fleet stream builds its transitions without one — has
    no way to tell a promotion from a pointer that vanished, and a word chosen
    without that fact promises a landing nobody recorded. Resolving it here
    rather than at the call site keeps the word's authority: whatever supplied
    the reader, promotion still requires a recorded row.

    A departure whose snapshot names no project leaves the record unknown rather
    than empty, and unknown is answered by the ``departed`` word, never by a
    promotion: the alternative asserts a fact no reader established.
    """
    reader = ledger_run_ids
    if reader is None:
        for run_id in departures:
            project = str(known[run_id].get("project") or "")
            if project:
                reader = _ledger_run_id_reader(project)
                break
    if reader is None:
        return None
    recorded = (
        reader.for_runs(departures)
        if isinstance(reader, _LedgerRunWordReader)
        else reader()
    )
    if isinstance(recorded, Mapping):
        return {str(run_id): str(word) for run_id, word in recorded.items()}
    return {str(run_id): "promoted" for run_id in recorded}


def _departure_word(run_id: str, recorded: Mapping[str, str] | None) -> str:
    """The word a departing run's absence carries.

    A recorded ledger row has first claim, because it settles the run whether
    it carries landed commits or a declared commitless completion. Failing
    that, a marker the run's directory holds from a deliberate discard names the
    departure discarded whatever else is known: the discard is a fact the run's
    own home records, so it outranks a ledger that cannot be resolved. Only when
    no such marker exists does an unresolvable ledger decide the word — the
    caller supplies no reader and the run names no project to resolve one from —
    and then the departure reads ``departed``, which promises neither a landing
    nor a withdrawal. A ledger that resolves and records no row leaves the run
    the bare withdrawal a reaped or hand-removed pointer earns.
    """
    if recorded is not None and run_id in recorded:
        return recorded[run_id]
    if _discard_recorded(run_id):
        return "discarded"
    if recorded is None:
        return "departed"
    return "withdrawn"


def _discard_recorded(run_id: str) -> bool:
    """Whether the run directory holds a marker a deliberate discard left.

    The run's own home is read only for a departure and only for a run the
    ledger does not record, so an ordinary observation touches no run
    directory. An unreadable or absent marker answers False: a departure the
    fleet cannot corroborate takes the word that promises nothing.
    """
    from reckon.crew.promotion import discard_record_path

    try:
        return discard_record_path(run_id).is_file()
    except OSError:
        return False


def fleet_transitions(
    known: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
    *,
    ledger_run_ids: Callable[[], Iterable[str] | Mapping[str, str]] | None = None,
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

    Departures first, then arrivals, then state changes — a run removed only by
    its own departure leaves the fleet before the next dispatch is counted into
    its slot, which is the order a reader infers from the numbers. A manifest
    rewrite that leaves the state unchanged is folded after the state changes of
    the same observation: its classification word did not move, so nothing else
    about the fold could have either. A run worded from its terminal ledger row
    settles there: the completion is announced once, a later pointer
    reading cannot move it back to ``dispatched``, and the pointer's own
    disappearance — a gc reap included — emits nothing further for the run.
    """
    if ledger_run_ids is None:
        # The published-stream fold supplies no ledger reader: it is the tick
        # the producer runs to append its transitions to the stream, and the
        # guard in ``_publish_watch_stream`` defers the whole tick when the
        # resolved configuration will not load, so a following reader gets the
        # previous image rather than a transition priced against a layer nobody
        # could read. This read is what lets that guard fire. The reader is
        # strict here only; every other caller of the fold either supplies a
        # ledger reader (the seat's own ticker, which pre-reads the rates and
        # keeps its degradation) or is a direct test of the fold.
        quota_weight.backend_rate_statuses(strict=True)

    running = {run_id: dict(snapshot) for run_id, snapshot in known.items()}
    changes: list[tuple[Mapping[str, Any], str | None, str]] = []

    departures = [item for item in known if item not in current]
    # A run leaves the fleet for reasons a pointer cannot tell apart on its own:
    # a completion that wrote its ledger row, a deliberate discard that left its
    # marker in the run directory, and a pointer that vanished with nothing
    # recorded behind it — a reaped pointer, a file removed by hand. A reader
    # acts on the word, and each of the three asks for a different response, so
    # the fold resolves all three. A settled word is read from the ledger alone:
    # promoted for recorded commits, recorded for a commitless completion. A discard marker
    # in the run directory names the departure discarded. With neither, a ledger
    # that resolves and records no row leaves the word withdrawn, while a ledger
    # that cannot be resolved leaves it departed — the honest unknown, which
    # promises neither a landing nor a withdrawal. A promotion is never inferred
    # from a missing row's absence, so an unrecorded departure cannot read as
    # work that landed. The ledger is read at most once per observation and only
    # when something departed; a reader the caller cannot supply is resolved
    # from the departing run's own project, and a departure whose snapshot names
    # no project at all still reads departed, because the alternative asserts a
    # fact no reader established.
    if departures:
        recorded = _departure_recorded_run_ids(known, departures, ledger_run_ids)
    else:
        recorded = {}
    for run_id in departures:
        if str(known[run_id].get("state") or "") in FLEET_SETTLED_STATES:
            # The run already settled on its terminal ledger row: the landing
            # was announced once, so the pointer's later disappearance — a gc
            # reap included — is not news and emits nothing for the run. The
            # slot is still given up, so a genuine re-dispatch of the same id
            # is read as an arrival rather than suppressed by a stale memory.
            running.pop(run_id, None)
            continue
        # A departure is its own fact and inherits no clause or marker from the
        # state it left. Carrying one forward reports a block on the line
        # announcing that the block is over.
        departed = {**known[run_id], "detail": "", "needs_help_complete": None}
        changes.append((departed, str(known[run_id]["state"]), _departure_word(
            run_id, recorded
        )))
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
        if previous in FLEET_SETTLED_STATES:
            # A terminal ledger row settles the run: once worded promoted, the
            # run stays promoted whatever the live pointer later reads. The
            # landing was announced once, when the row was written, so a stale
            # or superseded pointer that reads ``dispatched`` is not news and
            # cannot drive the row back — holding the promoted word is what
            # stops the row flapping between the two.
            continue
        previous_recovery = str(
            known[run_id].get("recovery_classification") or previous
        )
        current_recovery = str(current[run_id].get("recovery_classification") or state)
        if state != previous or current_recovery != previous_recovery:
            changes.append((current[run_id], previous, state))
        elif _manifest_rewritten(known[run_id], current[run_id]):
            # The classification word did not move but the report it sits on
            # did. The emitted snapshot is marked so the event builder records
            # the rewrite as its own kind; the run's memory keeps the clean
            # copy so the marker never leaks into a later departure.
            rewritten = dict(current[run_id])
            rewritten["manifest_rewritten"] = True
            changes.append((rewritten, previous, state))
            running[run_id] = dict(current[run_id])

    events: list[tuple[dict[str, Any], str | None, str, dict[str, int]]] = []
    for snapshot, previous, state in changes:
        run_id = str(snapshot.get("run_id") or "")
        # A run present in the fleet is remembered, so the next observation
        # compares it against itself rather than reading it as an arrival. A
        # run absent from the fleet has departed and gives up its slot. A run
        # whose terminal ledger row has worded it promoted keeps that word in
        # the memory too: the landing was announced once, and a later pointer
        # reading must not replace it, so the row cannot flap back to
        # ``dispatched`` while the pointer lingers.
        if run_id not in current:
            running.pop(run_id, None)
        elif (
            run_id in running
            and str(running[run_id].get("state") or "") in FLEET_SETTLED_STATES
        ):
            # Settled on its terminal ledger row: hold the promoted memory
            # rather than adopting a later pointer reading.
            pass
        elif not snapshot.get("manifest_rewritten"):
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
    spend_runs: Sequence[Mapping[str, Any]] | None = None,
    rate_statuses: Mapping[str, Any] | None = None,
    streams_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build one lossless transition object for text or JSON rendering.

    This is the surface the events log persists, so it carries facts only: the
    model, effort, alias and backend separately, the full untruncated detail,
    the structured fact a block's glyph is derived from, and the run's
    cumulative spend — wall seconds, model seconds, charged tokens, generation
    rate and notional cost as separate numeric facts. No composed label, no
    pre-claused reason and no display glyph are written here — the monitor
    derives those from these facts, so the log stays re-renderable.

    ``spend_runs`` is the record set the accumulator folds (the live fleet, or
    rows already settled); omitted, the project's own live pointers are read.
    ``rate_statuses`` maps a backend to its dated rate standing for the notional
    cost figure; omitted, the resolved configuration is read.
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
        "classification": snapshot.get("classification"),
        "process_alive": snapshot.get("process_alive"),
        "liveness_proven": snapshot.get("liveness_proven"),
        "working": counts["working"],
        "blocked": counts["blocked"],
        "unpromoted": counts["unpromoted"],
        "detail": str(snapshot.get("detail") or ""),
        "recovery_classification": str(
            snapshot.get("recovery_classification") or current
        ),
        "recovery": str(snapshot.get("recovery") or ""),
        "lifting_condition": snapshot.get("lifting_condition"),
        "resets_at": snapshot.get("resets_at"),
        "next_action": snapshot.get("next_action"),
        # The declared wait's own facts, beside the condition that lifts it: the
        # probe's verdict and last observation, the horizon the wait was
        # declared against, and the brief a resumed worker reads. Carried whole
        # so a renderer can tell a probe that never ran from one still pending,
        # and can hold the wait's age against its horizon without re-deriving
        # either. None is the explicit unmeasured state — a run that declares no
        # wait has taken no measurement, so no zero stands in for one.
        "wait_condition_state": snapshot.get("wait_condition_state"),
        "wait_observed": snapshot.get("wait_observed"),
        "wait_overdue": snapshot.get("wait_overdue"),
        "expected_horizon_seconds": snapshot.get("expected_horizon_seconds"),
        "resume_brief": snapshot.get("resume_brief"),
        "needs_help_complete": snapshot.get("needs_help_complete"),
    }
    if snapshot.get("manifest_rewritten"):
        # A rewrite of the report beneath an unchanged verdict: from_state and
        # to_state are the same word, so the distinct kind is what separates
        # this event from one where the classification moved. The new report's
        # facts ride along, so a reader sees what changed rather than only that
        # something changed.
        event["event"] = "manifest-rewritten"
        event["manifest_status"] = str(snapshot.get("manifest_status") or "")
        event["manifest_commits"] = list(snapshot.get("manifest_commits") or [])
        event["commit_count"] = len(event["manifest_commits"])
    if "waiting" in counts or previous in WAITING_STATES or current in WAITING_STATES:
        event["waiting"] = counts.get("waiting", 0)
    recorded = (
        _recorded_transition_spend(project, str(snapshot.get("run_id") or ""))
        if kind == "transition" and current in FLEET_SETTLED_STATES
        else None
    )
    event.update(
        recorded
        if recorded is not None
        else _spend_facts(
            project,
            snapshot,
            spend_runs=spend_runs,
            rate_statuses=rate_statuses,
            streams_root=streams_root,
        )
    )
    return event


def _recorded_transition_spend(project: str, run_id: str) -> dict[str, Any] | None:
    """Read a settled run's spend from the row committed before pointer unlink.

    The live-pointer fold is empty by the time this transition is published.
    The ledger's throughput block is the measurement promotion already
    resolved, so the transition copies its numeric facts without another fold.
    A missing block stays unknown; a measured zero remains numeric zero.
    """
    if not run_id:
        return None
    from reckon import ledger as ledger_module

    try:
        path = ledger_module.run_path(project, run_id)
        if path.is_file():
            row = json.loads(path.read_text(encoding="utf-8"))
        else:
            rows, _version = ledger_module.read_records(project, with_figures=False)
            row = next((item for item in rows if item.get("run_id") == run_id), None)
    except (OSError, ValueError, ledger_module.LedgerError):
        return None
    if not isinstance(row, Mapping) or row.get("run_id") != run_id:
        return None
    throughput = row.get("throughput")
    throughput = throughput if isinstance(throughput, Mapping) else {}
    budget = row.get("budget")
    budget = budget if isinstance(budget, Mapping) else {}

    def measured(value: Any) -> int | float | None:
        return (
            value
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            else None
        )

    input_tokens = measured(throughput.get("cumulative_input_tokens"))
    output_tokens = measured(throughput.get("generated_tokens"))
    return {
        "spend_folded_run_count": 1,
        "spend_measured_stream_count": None,
        "spend_unmeasured_stream_count": None,
        "spend_wall_seconds": measured(throughput.get("elapsed_seconds")),
        "spend_model_seconds": measured(throughput.get("generation_seconds")),
        "spend_machine_seconds": measured(throughput.get("machine_seconds")),
        "spend_charged_tokens": (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        ),
        "spend_generation_rate": measured(throughput.get("tokens_per_second")),
        "spend_notional_cost_usd": measured(budget.get("cost_usd_cumulative")),
    }


def _spend_facts(
    project: str,
    snapshot: Mapping[str, Any],
    *,
    spend_runs: Sequence[Mapping[str, Any]] | None = None,
    rate_statuses: Mapping[str, Any] | None = None,
    streams_root: str | Path | None = None,
) -> dict[str, Any]:
    """The transition's cumulative spend as separate, re-renderable facts.

    Every figure is written numerically — never as a pre-formatted string — and
    ``None`` is the explicit unmeasured state for each derived figure, because a
    zero would assert a measurement that was never taken. Tokens are the total a
    meter charges (input including cache reads, plus output); the rate is
    generated output over model seconds.
    """
    run_id = str(snapshot.get("run_id") or "")
    facts: dict[str, Any] = {
        "spend_wall_seconds": None,
        "spend_model_seconds": None,
        "spend_machine_seconds": None,
        "spend_charged_tokens": None,
        "spend_generation_rate": None,
        "spend_notional_cost_usd": None,
        "spend_folded_run_count": 0,
        "spend_measured_stream_count": 0,
        "spend_unmeasured_stream_count": 0,
    }
    if not run_id:
        return facts
    rows = spend_runs
    if rows is None:
        rows = runs._list_live_records(project=project)
    spend = metering.accumulate_run_spend(rows, run_id, streams_root=streams_root)
    if not isinstance(spend, metering.AccumulatedRunSpend):
        return facts
    measured = spend.measured_stream_count > 0
    facts.update(
        {
            "spend_folded_run_count": spend.folded_run_count,
            "spend_measured_stream_count": spend.measured_stream_count,
            "spend_unmeasured_stream_count": spend.unmeasured_stream_count,
            "spend_wall_seconds": spend.elapsed_seconds,
            "spend_model_seconds": spend.generation_seconds,
            "spend_machine_seconds": spend.machine_seconds,
            "spend_charged_tokens": spend.total_charged_tokens if measured else None,
            "spend_generation_rate": _generation_rate(spend, measured),
            "spend_notional_cost_usd": _notional_cost(snapshot, spend, rate_statuses),
        }
    )
    return facts


def _generation_rate(
    spend: metering.AccumulatedRunSpend, measured: bool
) -> float | None:
    """Generated tokens over model seconds, or None when either is unknown.

    A measured zero model span is still a span a rate could be divided from, so
    only a strictly positive span rates a denominator; an unmeasured chain never
    fabricates a rate from a zero it did not observe.
    """
    if (
        not measured
        or spend.generation_seconds is None
        or spend.generation_seconds <= 0
    ):
        return None
    return spend.cumulative_output_tokens / spend.generation_seconds


def _notional_cost(
    snapshot: Mapping[str, Any],
    spend: metering.AccumulatedRunSpend,
    rate_statuses: Mapping[str, Any] | None,
) -> float | None:
    """The run's notional dollar figure from declared rates, or None unpriced.

    The figure is computed from public rates and measured tokens, never read
    from the harness — which prices whatever model name it was told to speak and
    therefore ranks the free local lane as the most expensive backend. A lane
    with no dated rate pair stays explicitly unpriced, and a chain with no
    measured stream stays unmeasured.
    """
    if spend.measured_stream_count == 0:
        return None
    if rate_statuses is None:
        rate_statuses = quota_weight.backend_rate_statuses()
    status = rate_statuses.get(str(snapshot.get("backend") or ""))
    priced = bool(getattr(status, "priced", False))
    rate = getattr(status, "rate", None) if priced else None
    if rate is None:
        return None
    return round(
        spend.cumulative_input_tokens / 1_000_000 * rate.input_per_million
        + spend.cumulative_output_tokens / 1_000_000 * rate.output_per_million,
        2,
    )


def _scoped_watch_event(event: Mapping[str, Any], session: str) -> dict[str, Any]:
    """Re-derive a transition's figures over one session's live pointers.

    The watcher seat is project-global and the stream it writes carries the
    whole fleet's totals, so a follower cannot ask for per-session figures on
    the wire; the population is re-selected here at render time from the same
    live pointers, using the ownership already persisted on each pointer. A
    legacy pointer with no recorded owner stays counted, matching the dispatch
    fence.
    """
    project = str(event.get("project") or "")
    stall_seconds = parse_duration(DEFAULT_WATCH_STALL_WINDOW)
    moment = _utc_seconds()
    pointers = runs._list_live_records(project=project) if project else ()
    current = {
        str(pointer.get("run_id") or ""): _watch_snapshot(
            pointer, moment=moment, stall_seconds=stall_seconds
        )
        for pointer in pointers
        if pointer.get("run_id")
    }
    counts = _fleet_counts(current, session=session)
    scoped = dict(event)
    scoped["working"] = counts["working"]
    scoped["blocked"] = counts["blocked"]
    scoped["unpromoted"] = counts["unpromoted"]
    previous = scoped.get("from_state")
    state = scoped.get("to_state")
    if "waiting" in counts or previous in WAITING_STATES or state in WAITING_STATES:
        scoped["waiting"] = counts.get("waiting", 0)
    elif "waiting" in scoped:
        del scoped["waiting"]
    return scoped


def format_watch_transition(
    event: Mapping[str, Any],
    *,
    with_session: bool = False,
    ticker: Ticker | None = None,
    session: str | None = None,
) -> str:
    """Render one transition as the compact human-facing watch line.

    ``ticker`` supplies a caller's own grid — the CLI passes one carrying the
    reader's width, theme and colour choice. Omitted, a fresh plain grid renders
    for this call alone, because there is no terminal to detect: the pane is a
    pipe, so colour is a decision a caller makes rather than one this module can
    infer. A grid holds per-run state, so a fresh one keeps a row's text a
    function of the row, not of which rows a shared instance rendered before it;
    a caller that wants the age of a bucket's oldest member across a stream
    passes its own grid for the whole stream.

    ``session`` re-scopes the line's figures to the runs that session owns. A
    session-scoped follower relays a project-wide stream whose every event
    carries the fleet's totals; re-selecting the population before rendering is
    what makes its trailing figures describe the runs it is following. Omitted,
    the line shows the figures the event arrived with, unchanged.
    """
    if event.get("legacy"):
        return str(event.get("rendered") or "")
    if session is not None:
        event = _scoped_watch_event(event, session)
    return (ticker or Ticker()).render(event, with_session=with_session)


def _refuse_unresolvable_watch(project: str) -> None:
    """Refuse to arm a watcher whose project routes to a missing backend.

    A watcher that cannot resolve a backend it may be asked to lift reads as
    armed and loses every park it lifts, leaving a 0-byte stream per tick while
    the pointer stays working. The check runs before the registration is taken,
    so the seat is never held by a watcher that cannot do its job.
    """
    from reckon.crew.dispatch import assert_routable_backends_resolvable

    assert_routable_backends_resolvable(project, _resolved_review_config(project, None))


def _recreate_unlinked_registration(project: str, watcher: Mapping[str, Any]) -> bool:
    """Restore the seat record when an unlink took its path out from under us.

    The seat record is the file ``crew unwatch`` opens to find the producer it
    must stop. Its record holds the advisory lock the process table reads
    liveness from and carries the pid unwatch signals. A record unlinked under a
    live producer leaves nothing at the path,
    so a later ``unwatch`` opens a fresh inode, takes the lock the producer
    believes it still holds, and reports there is nothing to stop while the
    producer runs on unwatched — it can only be reached by pid. Writing the
    record back to its own path on the producer's next wake-up restores what the
    path is for, so unwatch finds the producer again.

    A path that a replacement producer has meanwhile taken is left alone and
    this returns False, so a superseded producer ends rather than overwrite a
    seat that is no longer its own.
    """
    path = watch_lock_path(project)
    handle = runs._WATCH_SEAT_HANDLES.get(project)
    if handle is None:
        return True
    try:
        held = os.fstat(handle.fileno())
    except OSError:
        return False
    try:
        current = os.stat(path)
    except FileNotFoundError:
        current = None
    if current is not None:
        return (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)

    try:
        replacement = path.open("a+b")
    except OSError:
        return False
    try:
        fcntl.flock(replacement.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another producer holds this path: the seat is no longer ours.
        replacement.close()
        return False
    _write_watch_record(replacement, dict(watcher))
    runs._WATCH_SEAT_HANDLES[project] = replacement
    return True


# An idle producer — one whose project has no live run pointer — doubles its
# poll interval on each wake-up, from the base it was armed with to this
# ceiling, so a project nobody is watching costs one wake a minute rather than
# one a second. A wake that sees a live run returns the interval to the base in
# the same pass. The value is written into the registration as
# ``poll_interval_seconds``, which is what a reader sees.
IDLE_POLL_INTERVAL_CAP_SECONDS = 30.0


def watch_ticker(
    project: str,
    *,
    stall_window: str = DEFAULT_WATCH_STALL_WINDOW,
    poll_interval: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
    signal_run: Callable[..., None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield a baseline and then every observed fleet state transition.

    Each tick also ends any review that has already delivered and kept
    streaming past its grace, so a reviewer whose record is stored and whose
    manifest is a verdict does not go on holding a lane the fleet needs. The
    stop is the tick's own concern rather than a yielded event: it changes run
    state, and a caller reading transitions is watching for a different thing.
    """
    _refuse_unresolvable_watch(project)
    stall_seconds = parse_duration(stall_window)
    known: dict[str, dict[str, Any]] = {}
    fleet_seen = False
    # Rate standings move on their own cadence, so one resolution serves the
    # whole watch session rather than a config read per transition.
    rate_statuses = quota_weight.backend_rate_statuses()

    # The producer holds a lease, not a lifetime: every live follower renews it
    # on its wait pass. A lease whose instant is absent is not a lapsed one — the
    # registration may simply have gone missing, which a later wake-up recreates
    # — so only a recorded instant that has fallen a full interval behind ends
    # the seat. The sleep is bounded by the lease's remaining time so an exit
    # lands within a second of the lapse however far the interval has backed off.
    lease_seconds = producer_lease_seconds()

    def _lease_remaining() -> float | None:
        renewed = watch_lease_renewed_at(project)
        if renewed is None:
            return None
        return lease_seconds - (_utc_seconds() - renewed)

    def _wait(interval: float) -> bool:
        """Sleep, bounded by the lease; report whether the seat has lapsed."""
        remaining = _lease_remaining()
        if remaining is not None and remaining <= 0:
            return True
        if sleeper is not time.sleep:
            # A supplied sleeper may advance a simulated clock or return
            # immediately. Give it one bounded call per poll interval so a
            # simulated long sleep cannot skip the lease renewal entirely.
            sleeper(
                max(
                    0.0,
                    min(interval, remaining, LEASE_RENEW_SECONDS)
                    if remaining is not None
                    else min(interval, LEASE_RENEW_SECONDS),
                )
            )
            if not runs.renew_watch_host_lease(project):
                return True
            remaining = _lease_remaining()
            return remaining is not None and remaining <= 0
        left = interval
        while True:
            remaining = _lease_remaining()
            if remaining is not None and remaining <= 0:
                return True
            step = min(left, LEASE_RENEW_SECONDS)
            if remaining is not None:
                step = min(step, remaining)
            sleeper(max(0.0, step))
            if not runs.renew_watch_host_lease(project):
                return True
            left -= step
            if left <= 0:
                break
        remaining = _lease_remaining()
        return remaining is not None and remaining <= 0

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

        # The interval actually slept grows while the project has no live run
        # pointer and returns to the base on the first wake that sees one. The
        # producer records it each pass so a reader sees how far it has backed
        # off without asking the process.
        poll_interval_current = poll_interval
        # The snapshots one armed watcher carries between its polls. It lives
        # only as long as this watcher, so an independently armed producer
        # starting later can never be served an earlier watcher's reading.
        snapshot_cache: dict[str, tuple[str, dict[str, Any]]] = {}
        while True:
            if not runs.renew_watch_host_lease(project):
                return
            # An unlinked seat record is rewritten before anything else, so a
            # producer whose file was removed is findable by unwatch again, and
            # one superseded by a replacement producer ends here.
            if not _recreate_unlinked_registration(project, watcher):
                return
            remaining = _lease_remaining()
            if remaining is not None and remaining <= 0:
                return
            # Every stream the tick reads is counted, so the registration
            # carries what this poll actually parsed. An unchanged fleet
            # resumes from every cursor and parses nothing, which is the value
            # a reader uses to see the poll is stat-only rather than re-reading
            # the whole of every transcript. The admission check's own reads are
            # counted and reset on the same beat, so the two counters describe
            # one poll each.
            from reckon import _backends

            _backends.take_parsed_stream_bytes()
            take_admission_stream_bytes()
            pointers = list_live(project=project)
            _stop_delivered_reviews(pointers, signal_run=signal_run)
            moment = _utc_seconds()
            current = {
                str(pointer.get("run_id") or ""): _watch_snapshot(
                    pointer, moment=moment, stall_seconds=stall_seconds,
                    cache=snapshot_cache,
                )
                for pointer in pointers
                if pointer.get("run_id")
            }
            if current:
                # A wake that sees a live run ends any back-off: the producer's
                # interval is its base again from this pass.
                poll_interval_current = poll_interval
            update_watch_registration(
                project,
                poll_interval_seconds=poll_interval_current,
                bytes_parsed_last_poll=_backends.take_parsed_stream_bytes(),
            )
            if not current and not fleet_seen:
                if _wait(poll_interval_current):
                    return
                poll_interval_current = min(
                    poll_interval_current * 2.0, IDLE_POLL_INTERVAL_CAP_SECONDS
                )
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
                        spend_runs=pointers,
                        rate_statuses=rate_statuses,
                    )
                continue

            folded, next_known = fleet_transitions(
                known, current, ledger_run_ids=_ledger_run_id_reader(project)
            )
            events = [
                _watch_transition(
                    project,
                    kind="transition",
                    snapshot=snapshot,
                    previous=previous,
                    current=state,
                    counts=event_counts,
                    spend_runs=pointers,
                    rate_statuses=rate_statuses,
                )
                for snapshot, previous, state, event_counts in folded
            ]
            known = next_known
            if events:
                yield from events
                if not current:
                    return
                continue
            if _wait(poll_interval_current):
                return
            if not current:
                poll_interval_current = min(
                    poll_interval_current * 2.0, IDLE_POLL_INTERVAL_CAP_SECONDS
                )


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
                        quiet = _run_stream_quiet_seconds(pointer, now_seconds=moment)
                        # A quiet stream sleeping in a bounded wait is paused,
                        # not stalled, so it must not wake the follower the way
                        # a hang does — the same correction the ticker applies.
                        # Only a live process can be sitting in the wait: a dead
                        # one followed a bounded call no further and is a lost
                        # run the follower must still report.
                        live = row.get("process_alive") is True
                        if quiet > _stall_window_seconds(row, stall_seconds) and (
                            not live or _stall_wait_reason(pointer) is None
                        ):
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
    launcher: Callable[..., Any] | None = None,
    dispatch_reviews: bool = False,
) -> dict[str, Any]:
    """Classify live pointers; launch reviews only with --dispatch-reviews and --project.

    Each pointer is re-observed first, so the classification rests on the
    current stream and process table rather than on whatever the last writer
    believed. What gets repaired is the *record*: no worktree is removed, no
    process is reaped, and no run is promoted on this command's initiative — a
    completed-but-unpromoted run is reported with its manifest path so the
    orchestrator can promote it deliberately.

    Review dispatch requires ``dispatch_reviews`` and a named project. It
    reaches only runs whose dispatching session still has a live follower;
    otherwise the review remains with its coordinator. The sweep prefers the
    local lane unless the reviewed node explicitly declared another backend.
    """
    from reckon.crew.dispatch import observe

    if dispatch_reviews and not project:
        raise CrewError("review dispatch requires --project with --dispatch-reviews")

    reports = []
    scoring: list[dict[str, Any]] = []
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
        if report["classification"] == "scoring":
            scoring.append(observed)
    reports = group_terminal_lane_events(reports)
    counts = {
        name: sum(1 for item in reports if item["classification"] == name)
        for name in (
            "running",
            "scoring",
            "promotable",
            "completed_unpromoted",
            INTERRUPTED_RUN_PHASE,
            "abandoned",
            "lane-event",
        )
    }
    for name in ("waiting", "paused", "stopped", "blocked", "failed", "unreadable"):
        count = sum(1 for item in reports if item["classification"] == name)
        if count:
            counts[name] = count
    reflex = []
    awaiting_coordinator = []
    if dispatch_reviews:
        for record in scoring:
            session = str(record.get("session") or "")
            if not session or not runs.follower_state(project, session).get("live"):
                awaiting_coordinator.append(
                    {
                        "run_id": str(record.get("run_id") or ""),
                        "status": "awaiting-coordinator",
                        "reason": f"dispatching session {session or '<missing>'!r} is not live",
                    }
                )
                continue
            reflex.append(
                dispatch_review_for_run(
                    record, config=config, launcher=launcher, prefer_local=True
                )
            )
    return {
        "runs": reports,
        "counts": counts,
        "classes": list(RECOVERY_CLASSES),
        "reviews_dispatched": [
            r["review_run_id"] for r in reflex if r.get("dispatched")
        ],
        "reviews_awaiting_lane": [
            r["run_id"] for r in reflex if r.get("awaiting_lane")
        ],
        "reviews_refused": [r for r in reflex if r.get("refused")],
        "reviews_awaiting_coordinator": awaiting_coordinator,
    }


from .recovery_classification import (  # noqa: E402
    classify_pointer,
)
from .recovery_liveness import (  # noqa: E402
    _attempt_started_seconds,
    _int_or_none,
    _live_descendant,
    _process_reading,
    _record_newest_stream,
    _run_stream_quiet_seconds,
    _worker_record_liveness,
    _worker_record_pid,
    local_liveness,
)
from .recovery_memo import (  # noqa: E402
    _classification_inputs,
    _classification_key,
    _file_identity,
    _worktree_head_identity,
    group_terminal_lane_events,
    take_admission_stream_bytes,
)
from .recovery_review_delivery import (  # noqa: E402
    _stop_delivered_reviews,
)
from .recovery_review_dispatch import (  # noqa: E402
    _resolved_review_config,
    dispatch_review_for_run,
)
from .recovery_vocabulary import (  # noqa: E402
    RECOVERY_CLASSES,
    RECOVERY_VERBS,
    WAITING_STATUS,
)
from .recovery_wait import (  # noqa: E402
    STREAM_RESULT_RECORD_TYPE,
    _newest_stream_last_record_type,
    _process_exit_reason,
    _stall_wait_reason,
)
