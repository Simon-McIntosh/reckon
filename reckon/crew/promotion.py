from __future__ import annotations

# This module retains its imported names as the package compatibility surface.
# ruff: noqa: F401,E402
import html
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import (
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import (
    ExitStack,
    contextmanager,
    nullcontext,
    suppress,
)
from dataclasses import (
    dataclass,
    field,
    replace,
)
from datetime import (
    UTC,
    datetime,
    timezone,
)
from functools import wraps
from pathlib import (
    Path,
)
from typing import (
    Any,
)

from reckon import (
    _backends,
    _store,
    capabilities,
    clones,
    file_memo,
    flight,
    ledger,
    review_tiers,
)
from reckon._plan_html import (
    section_anchor,
    section_record_id,
)
from reckon._schema import (
    is_implementable_section,
)
from reckon._timestamps import (
    parse_iso,
    parse_utc,
)
from reckon.crew import (
    plan_review,
    promotion_waivers,
    rollout,
)
from reckon.crew import (
    review as review_module,
)
from reckon.crew.dispatch import (
    WORKER_SCRATCH_BUDGET_BYTES,
    _backend_settings,
    project_mount_repository,
    remove_worker_scratch,
    resolve_project_repository,
    tree_size_bytes,
    worker_scratch_root,
)
from reckon.crew.host_lease import (
    LEASE_RENEW_SECONDS,
    HostLease,
)
from reckon.crew.node import (
    NEGATIVE_CONTROL_FIELD,
    NEGATIVE_CONTROL_NONE,
    STALL_BUDGET_MULTIPLE,
    CrewError,
    is_test_path,
    negative_control_is_none,
    negative_control_reason,
    parse_duration,
    role_may_write_repository_paths,
)
from reckon.crew.open_run_index import operational_read
from reckon.crew.picker.outcomes import (
    OVERRIDDEN_HOLD_ROUTE_MODE,
    overridden_hold,
)
from reckon.crew.plan_review import (
    RUN_COMMENT_PREFIX,
    _is_run_comment,
)
from reckon.crew.recovery import (
    _resolve_commit,
)
from reckon.crew.reports import (
    _NONE_VALUES as _MANIFEST_NOTHING,
)
from reckon.crew.reports import (
    OUTSIDE_SCOPE as OUTSIDE_SCOPE_MANIFEST_FIELD,
)
from reckon.crew.reports import (
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    parse_manifest,
    path_within_declared_scope,
)
from reckon.crew.routing import (
    RECLAIMABLE_CLASSES,
    WITHHELD_REASONS,
    _boundary_tree_roots,
    _disposable_member_id,
    _git,
    _inspect_workspace,
    _live_pointer_worktrees,
    _release_worktree_whole,
    _repository_tree_snapshot,
    _shadow_patch_retained,
    _shadow_worktree_records,
    _signal_process_group,
    deferring_worktree_copy_deletion,
    mounted_repository_projects,
    run_directory_of,
)
from reckon.crew.runs import (
    _drain_row,
    _live_worktree_claims,
    _manifest_freshness,
    _pointer_lock,
    _shared_write_paths,
    _utc_now,
    _write_json,
    capture_run_session,
    crew_home,
    dispatch_field,
    drain,
    list_live,
    pointer_path,
    process_alive,
    read_dispatch_detail,
    read_pointer,
    record_process_alive,
    run_dir,
    stacked_descendants_still_live,
)
from reckon.evidence import (
    EXECUTABLE_SECTION_ROLES,
)


# Promotion serialises its landing independently of pointer mutations. The
# flock excludes local processes and threads; the renewable lease excludes a
# promotion on another host through the fleet home's atomic link.
class _PromotionStopped(SystemExit):
    """A promotion that stopped at a stage boundary after a stop signal."""


# A term signal asks the running promotion to stop at its next stage boundary.
# The handler only records the request: raising from it could interrupt a git
# child mid-write, leaving a process the promotion started writing after the
# promotion itself has exited. Stopping at a boundary lets the stage in
# progress finish, so every child it started has been reaped by then, and the
# claim is released on the way out.
_STOP_REQUESTED = threading.Event()


def _request_stop_on_termination(signum: int, frame: object) -> None:
    _STOP_REQUESTED.set()


def _install_stop_handler() -> object | None:
    """Record SIGTERM on the promotion; return the handler it displaced.

    Returns None where no handler may be installed — off the main thread, or
    where the platform has no SIGTERM — and the caller hands the value back to
    :func:`_restore_stop_handler`.
    """
    if threading.current_thread() is not threading.main_thread():
        return None
    if not hasattr(signal, "SIGTERM"):
        return None
    return signal.signal(signal.SIGTERM, _request_stop_on_termination)


def _restore_stop_handler(previous: object | None) -> None:
    if previous is None:
        return
    with suppress(ValueError, TypeError):
        signal.signal(signal.SIGTERM, previous)


def _stop_at_stage_boundary(stage: str) -> None:
    """Stop before the next stage when a signal asked the promotion to.

    Every stage boundary that starts a write calls this first.
    """
    if not _STOP_REQUESTED.is_set():
        return
    raise _PromotionStopped(
        f"promotion was asked to stop by SIGTERM and stopped before {stage}; "
        "the stage in progress had finished, no further stage ran, and the "
        "promotion's claim is released so a retry can take it"
    )


#: The pointer field a promotion sets when it recorded a run but could not
#: retire the live pointer. A promoted pointer is retired by the acceptance
#: sweep once nothing owns it, and is never promoted again.
PROMOTED_POINTER_MARK = "promoted"


@contextmanager
def _promotion_lock(run_id: str) -> Iterator[None]:
    with _pointer_lock(f"promotion-{run_id}"):
        yield


@contextmanager
def _promotion_coordination(run_id: str, *, dry_run: bool) -> Iterator[None]:
    """Hold the promotion's claims, or none of them when nothing is written.

    A real promotion takes the run's pointer lock and its cross-host claim
    before its first write, so two hosts cannot land one run and a second
    promotion waits rather than racing. A dry run writes nothing, so it takes
    neither: the claim is itself a write, and a dry run that waits on one
    answers as a write that timed out instead of the read it is.
    """
    if dry_run:
        yield
        return
    with _promotion_lock(run_id), _promotion_host_lease(run_id):
        yield


#: The pointer fields a promotion must find a pointer it can remove, in the
#: order the refusal message names them. The phase is among them because a
#: promoted attempt whose phase moved is not the attempt this promotion read.
_PROMOTED_POINTER_FIELDS = (
    "attempt",
    "attempt_kind",
    "phase",
    "pid",
    "pid_start_time",
    "launcher_host",
    "log_path",
    "stderr_path",
    "worktree",
    "resume_launch_in_flight",
    "node",
    "repo",
    "backend",
    "agent",
)


def _same_fields(
    current: Mapping[str, Any],
    record: Mapping[str, Any],
    fields: tuple[str, ...],
) -> bool:
    return all(current.get(key) == record.get(key) for key in fields)


def _remove_promoted_pointer(run_id: str, record: Mapping[str, Any]) -> bool:
    """Remove only the attempt that promotion checked, preserving a newer one."""
    from reckon.crew import pending_landing

    outcome = pending_landing.retire_pointer_if(
        run_id,
        lambda current: _same_fields(current, record, _PROMOTED_POINTER_FIELDS),
    )
    if outcome == pending_landing.POINTER_ABSENT:
        return False
    if outcome != pending_landing.POINTER_RETIRED:
        raise CrewError(
            f"run {run_id!r} changed while promotion ran"
            f"{_newer_attempt_named(run_id, record)}; its pointer and "
            "workspace are preserved; retry promotion"
        )
    return True


def _newer_attempt_named(run_id: str, record: Mapping[str, Any]) -> str:
    """Name the attempt a later launch recorded, for a refusal that kept it.

    A cleanup that found the pointer belonging to another attempt preserves it,
    and the attempt number is what tells a coordinator which launch owns the run.
    The refusal says which field moved, so a pointer that changed for any other
    reason still reads as the generic mismatch it is.
    """
    try:
        current = read_pointer(run_id)
    except CrewError:
        return ""
    attempt = current.get("attempt")
    if attempt is None or attempt == record.get("attempt"):
        return ""
    return f": attempt {attempt} was recorded after this promotion read the pointer"


def _pointer_attempt(pointer: Mapping[str, Any]) -> int | None:
    """The attempt a pointer names, or None when it names none."""
    try:
        return int(pointer.get("attempt"))
    except (TypeError, ValueError):
        return None


def _attempt_reservation(run_id: str, attempt: int) -> dict[str, Any]:
    """The launch reservation a later attempt left in the run directory.

    The reservation names the process that took the attempt and the door that
    asked for it, so it is the only record a refusal can quote for a launch
    whose pointer carries just the supervisor's own pid. The file is parsed by
    its owner, ``dispatch_launch._read_reservation``, so every reader of a
    reservation agrees about what one is; this reader adds only the check that
    the record names the run it was looked up for, because a file naming another
    run is no record of this one. A reservation the launch never wrote reads the
    same as an unreadable one.
    """
    from reckon.crew.dispatch_launch import _read_reservation

    loaded = _read_reservation(run_dir(run_id) / f"attempt-{attempt}.reservation")
    if loaded is None or str(loaded.get("run_id") or "") != run_id:
        return {}
    return dict(loaded)


def _launching_process_named(run_id: str, record: Mapping[str, Any]) -> str:
    """Name the launch that owns the pointer this promotion could not retire.

    A refusal is only as useful as the launch it points at: the attempt number
    says which attempt to look for, and the reservation beside the run says
    which process took it and which process launched that, which is what an
    operator needs to find the work in flight instead of guessing from a pid
    naming a supervisor they never started.
    """
    try:
        current = read_pointer(run_id)
    except CrewError:
        return ""
    attempt = _pointer_attempt(current)
    if attempt is None or attempt == _pointer_attempt(record):
        return ""
    reservation = _attempt_reservation(run_id, attempt)
    from reckon.crew.dispatch_launch import _reservation_launcher

    launcher = _reservation_launcher(reservation).get("pid") if reservation else None
    host = str(reservation.get("launcher_host") or "").strip()
    supervisor = reservation.get("supervisor_pid") or current.get("pid")
    named = ""
    if launcher is not None:
        on_host = f" on host {host}" if host else ""
        named = f", launched by pid {launcher}{on_host}"
        if supervisor is not None and supervisor != launcher:
            named += f" (supervisor pid {supervisor})"
    elif current.get("pid") is not None:
        named = f", run by pid {current['pid']}"
    return named


def _attempt_has_ended(run_id: str, pointer: Mapping[str, Any]) -> bool:
    """Whether the attempt a pointer names recorded its own end.

    An attempt's exit receipt is the account its launch writes when it ends and
    the same fact frees its launch reservation. A pointer naming an attempt
    whose receipt is on disk therefore names work that is over, whatever state
    the pointer's own fields are in.
    """
    attempt = _pointer_attempt(pointer)
    if attempt is None:
        return False
    from reckon.crew.dispatch_launch import _reserved_attempt_ended

    return _reserved_attempt_ended(run_dir(run_id), attempt)


def _retire_a_pointer_whose_attempt_ended(
    run_id: str, record: Mapping[str, Any]
) -> bool:
    """Finish cleanup against a pointer a later, ended launch left behind.

    A promotion whose own attempt is no longer the pointer's cannot retire the
    attempt it read, and refusing leaves the run's cleanup to a coordinator who
    has to stop and discard it by hand although its row is already written.
    When the attempt now on the pointer recorded its end, this promotion
    completes the cleanup its own attempt started: the pointer goes, and the
    run directory keeps the ended attempt's receipts. The pointer is removed
    only while it still names the attempt this reading saw, so a launch that
    started after it is left to its own promotion.
    """
    from reckon.crew import pending_landing

    try:
        current = read_pointer(run_id)
    except CrewError:
        return False
    if _pointer_attempt(current) in (None, _pointer_attempt(record)):
        return False
    if not _attempt_has_ended(run_id, current):
        return False
    identity = pending_landing.attempt_identity(current)
    return (
        pending_landing.retire_pointer_if(
            run_id,
            lambda observed: pending_landing.attempt_identity(observed) == identity,
        )
        == pending_landing.POINTER_RETIRED
    )


def _keep_pointer_for_pending_landing(
    run_id: str, record: Mapping[str, Any], *, reviewed_head: str = ""
) -> bool:
    """Keep handover ownership in the journal without rewriting the live attempt."""
    from reckon.crew import pending_landing

    with _pointer_lock(run_id):
        try:
            current = read_pointer(run_id)
        except CrewError:
            return False
        _record_a_raced_landing(run_id, record, reviewed_head=reviewed_head)
        path = pending_landing._record_path(run_id)
        journal = pending_landing._read(path)
        if journal is None:
            return False
        journal.setdefault("pointer", dict(record))
        journal.setdefault(
            "retained_attempt",
            current.get("pending_landing_repair_attempt")
            or pending_landing.marked_identity(current),
        )
        try:
            _write_json(path, journal)
        except OSError:
            return False
        return True


def _retire_review_pointer(run_id: str, record: Mapping[str, Any]) -> bool:
    """Remove a review run's pointer only while its attempt is the promotion's.

    A review run has nothing to land, so a pointer that moved under its promotion
    is retired rather than kept for a landing repair that can never happen. The
    ledger row and any committed review record are already written before this
    runs, and the run reads as promoted from its row; a kept pointer would be
    listed as promotable-stale for good, because a review is never a run a merge
    repair could finish. A pointer a newer attempt has since written belongs to
    work still in flight, so only the attempt the promotion read is removed.
    """
    from reckon.crew import pending_landing

    return (
        pending_landing.retire_pointer_if(
            run_id,
            lambda current: (
                pending_landing.attempt_identity(current)
                == pending_landing.attempt_identity(record)
            ),
        )
        == pending_landing.POINTER_RETIRED
    )


#: What a raced promotion found when it tried to retire its live pointer.
_RACED_POINTER_RETIRED = "retired"
_RACED_POINTER_ABSENT = "absent"
_RACED_POINTER_ANOTHER_ATTEMPT = "another-attempt"


def _retire_a_raced_pointer(run_id: str, record: Mapping[str, Any]) -> str:
    """Remove the live pointer while it still belongs to ``record``'s attempt.

    A promotion that lost the ledger-row race to another writer owns its pointer
    in the sense that matters here: the other writer's row is written, the head
    still has to be merged, and no newer attempt started the run. A mark another
    writer put on the pointer while this promotion ran — a promotion request, a
    landing mark — is bookkeeping about a run whose row now has an owner, not a
    different attempt. A resume that started the run again does change the
    attempt, and its pointer is left to that attempt. An absent pointer needs no
    retirement: the run has no live pointer left to strand, and reporting it as
    one is what let a caller write a mark onto nothing.
    """
    from reckon.crew import pending_landing

    outcome = pending_landing.retire_pointer_if(
        run_id,
        lambda current: (
            pending_landing.attempt_identity(current)
            == pending_landing.attempt_identity(record)
        ),
    )
    if outcome == pending_landing.POINTER_RETIRED:
        return _RACED_POINTER_RETIRED
    if outcome == pending_landing.POINTER_ABSENT:
        return _RACED_POINTER_ABSENT
    return _RACED_POINTER_ANOTHER_ATTEMPT


def _record_a_raced_landing(
    run_id: str, record: Mapping[str, Any], *, reviewed_head: str = ""
) -> None:
    """Record the merge a promotion that lost the ledger-row race still owes.

    The row is the other writer's, so the promotion cannot merge from its own
    return path when its caller did not land: naming the reviewed head in the
    landing record is what lets the landing pass finish that merge, rather than
    leaving a delivered run whose head is off the primary branch.

    ``reviewed_head`` is the revision the promotion resolved before it wrote
    anything — never re-derived here. A resume can advance the run's manifest
    and its worktree while this record waits, and the pass merges the revision
    the record cites in preference to the row's, so deriving a head at this
    point could merge a revision no review covered. A record that already
    exists is left alone for the same reason: it was written by the attempt it
    belongs to, and replacing it moves the head that attempt will land.
    """
    from reckon.crew import pending_landing

    if (pending_landing.pending_dir() / f"{run_id}.json").exists():
        return
    pending_landing.write_pending(
        run_id,
        str(record.get("project") or ""),
        pointer=record,
        head=str(reviewed_head or "").strip(),
    )


def _mark_promoted_pointer(run_id: str) -> bool:
    """Mark the live pointer a promotion recorded the run for but could not retire.

    The mark says the ledger row is written, so a reader that finds the pointer
    still there knows the promotion happened and the pointer is what is left of
    it. It carries no attempt: the pointer it is written onto is whichever one
    is live, and the sweep that retires it reads that pointer's own recorded
    process to decide whether anything still owns it.
    """
    with _pointer_lock(run_id):
        try:
            current = read_pointer(run_id)
        except CrewError:
            return False
        if current.get(PROMOTED_POINTER_MARK):
            return True
        try:
            _write_json(pointer_path(run_id), {**current, PROMOTED_POINTER_MARK: True})
        except OSError:
            return False
        return True


def _remove_promoted_pointer_or_defer(
    run_id: str,
    record: Mapping[str, Any],
    *,
    finishing_another_writers_row: bool = False,
    reviewed_head: str = "",
) -> bool:
    """Retire the promoted attempt or keep its landing ownership on the journal.

    The ledger row precedes retirement, so a pointer that moved during promotion
    can still owe a merge. Its retained attempt is captured on the pending
    record without writing the live pointer. Later attempts keep their own
    pointers while that journal finishes the reviewed head. Reviews have no
    merge to finish and keep their separate process-aware retirement path.
    """
    from reckon.crew.recovery_review_delivery import _is_review_run

    review = _is_review_run(record)
    if review and record_process_alive(record) is True:
        _mark_promoted_pointer(run_id)
        return False
    try:
        return _remove_promoted_pointer(run_id, record)
    except CrewError as exc:
        if review:
            if _retire_review_pointer(run_id, record):
                return True
            _mark_promoted_pointer(run_id)
            return False
        if not finishing_another_writers_row and _retire_a_pointer_whose_attempt_ended(
            run_id, record
        ):
            return True
        if finishing_another_writers_row:
            _record_a_raced_landing(run_id, record, reviewed_head=reviewed_head)
            retired = _retire_a_raced_pointer(run_id, record)
            if retired in (_RACED_POINTER_RETIRED, _RACED_POINTER_ABSENT):
                # An absent pointer has nothing left to strand: the run's row
                # is written and its head is merged by the caller's landing.
                return True
            if retired == _RACED_POINTER_ANOTHER_ATTEMPT:
                return False
        if not _keep_pointer_for_pending_landing(
            run_id, record, reviewed_head=reviewed_head
        ):
            raise CrewError(
                f"run {run_id!r} changed while promotion ran"
                f"{_newer_attempt_named(run_id, record)}"
                f"{_launching_process_named(run_id, record)} and its pointer "
                "could not be recorded for the landing pass: its promotion is "
                "recorded, and the merge of its reviewed head is left to the "
                "landing record this promotion wrote"
            ) from exc
        if finishing_another_writers_row:
            return False
        raise CrewError(
            f"run {run_id!r} changed while promotion ran"
            f"{_newer_attempt_named(run_id, record)}"
            f"{_launching_process_named(run_id, record)}; its promotion is "
            "recorded and its pointer is kept with its attempt on the landing record, so "
            "the next landing pass finishes the merge of its reviewed head"
        ) from exc


def _promotion_lease_name(run_id: str) -> str:
    return f"promotion-{run_id}"


def _promotion_lease_identity() -> tuple[str, int, str]:
    """The host, pid and job a promotion's claim names as its holder."""
    return (
        socket.gethostname().split(".")[0],
        os.getpid(),
        os.environ.get("SLURM_JOB_ID", ""),
    )


def _promotion_lease_held(run_id: str, holder: Any) -> str:
    if holder is None:
        return (
            f"run {run_id} is already being promoted elsewhere; refusing to "
            "promote it a second time"
        )
    return (
        f"run {run_id} is already being promoted by host {holder.host} "
        f"pid {holder.pid} job {holder.job or 'unstated'}; refusing to promote "
        "it a second time"
    )


def promotion_in_flight(run_id: str) -> str:
    """Who holds this run's promotion claim, or empty when nobody does.

    The claim is the cross-host lease promotion takes before its first write and
    releases after its landing, so a fresh holder is a promotion in flight on
    this host or another. A door that must not act under a promotion reads it
    here rather than inferring it from a pointer, because the pointer is the
    promotion's to rewrite and its removal is the last thing the promotion does:
    a reader watching only the pointer sees a run that looks untouched for the
    whole of a promotion.

    A lease past its stale window reads as unheld, so a holder that stopped
    renewing never refuses work forever.
    """
    if not run_id:
        return ""
    host, pid, job = _promotion_lease_identity()
    lease = HostLease(crew_home(), _promotion_lease_name(run_id), host, pid, job)
    holder = lease.holder()
    if holder is None:
        return ""
    return f"host {holder.host} pid {holder.pid}"


class _PromotionLeaseRenewer:
    """Keep a held lease fresh while the promotion runs, then stop."""

    def __init__(self, lease: HostLease) -> None:
        self._lease = lease
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._renew, name="promotion-lease-renew", daemon=True
        )

    def _renew(self) -> None:
        while not self._stop.wait(LEASE_RENEW_SECONDS):
            if not self._lease.renew():
                return

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=LEASE_RENEW_SECONDS * 2)


@contextmanager
def _promotion_host_lease(run_id: str) -> Iterator[HostLease]:
    """Hold the run's cross-host claim for the whole promotion, or refuse.

    The claim is taken before the promotion's first write and released after
    its landing or rollback, and it is renewed while the promotion runs so a
    slow landing on a congested shared filesystem is not taken over mid-landing.
    A lease a foreign holder still renews refuses the promotion at once without
    writing; a lease past its stale window is taken over, so a holder that
    stopped renewing never blocks the run forever.
    """
    host, pid, job = _promotion_lease_identity()
    lease = HostLease(crew_home(), _promotion_lease_name(run_id), host, pid, job)
    if not lease.claim():
        raise CrewError(_promotion_lease_held(run_id, lease.holder()))
    renewer = _PromotionLeaseRenewer(lease)
    renewer.start()
    # The request is cleared only once this promotion holds the claim, so a
    # signal delivered to the process before it took one cannot stop it.
    _STOP_REQUESTED.clear()
    previous_handler = _install_stop_handler()
    try:
        yield lease
    finally:
        _restore_stop_handler(previous_handler)
        renewer.stop()
        lease.release()
        _STOP_REQUESTED.clear()


def _landing_checkout(
    record: Mapping[str, Any], root: str | Path | None
) -> Path | None:
    """The checkout a promotion's landing commit would be written into.

    Resolved the way the landing gate resolves it, so the early judgement of
    the checkout's git state names the same repository the landing commit later
    does: the project's registered mount decides it when one exists, and a run
    whose record names none falls back to the repository the record carries.
    """
    project = str(record.get("project") or "")
    landing_root: str | Path | None = root
    if project and project_mount_repository(project) is not None:
        landing_root = resolve_project_repository(project, root, flag="--checkout-path")
    if landing_root is None:
        landing_root = record.get("repo")
    return (
        Path(landing_root).expanduser().resolve() if landing_root is not None else None
    )


#: The committed range a run's promotion cites moves only when the tree's own
#: head does, so a steady tick must not run one git process per live run to
#: learn that nothing has changed.


def _shared_branch_revision(record: Mapping[str, Any]) -> str:
    """The revision the shared checkout's primary branch carries, or "".

    A run's citations are what it made beyond its base, and a repair tells a
    worker to merge the shared branch into its worktree. The commits that merge
    brought in were made by peers and by already-landed runs, so the inference
    subtracts this revision's history and cites only what the run itself made.

    Read from the checkout the run was dispatched from, through the reader the
    classification sweep already uses, because that reader answers for a whole
    pass from one git process rather than one per live run. A record naming no
    readable checkout answers empty, and the caller then cites the plain range:
    an unknown shared branch is not evidence that its commits are the run's.
    """
    repository_raw = str(record.get("repo") or "").strip()
    if not repository_raw:
        return ""
    repository = Path(repository_raw).expanduser()
    if not repository.is_dir():
        return ""
    from .recovery_classification import _primary_branch_revision

    return _primary_branch_revision(repository, time.monotonic())


def _promotion_commits(
    record: Mapping[str, Any], manifest_commits: Sequence[Any]
) -> list[str]:
    """Read the run's committed range even when a repair cleared its citations.

    The range is the run's own line in ``base..HEAD`` in the run's own tree,
    less the history the shared branch reaches, so imports through a merge
    belong to primary while the merge itself stays the run's. A run that merged
    the shared branch into its worktree therefore cites its own commit and that
    merge and not the commits the merge brought in, and no commit it imported
    from another branch is credited to it either.

    The exclusion is not made redundant by the traversal: a run whose branch was
    rebased onto the shared one has primary's own commits on its first-parent
    line, and those are still not the run's work.

    The range is answered from the previous read until the tree's own head or
    the shared branch moves, and both are resolved as files or by one cached
    read rather than by a git process per live run. When the head cannot be
    read the answer is not stored.
    """
    from .merge import commits_between
    from .recovery_memo import _worktree_head_identity
    from .recovery_review_subject import _review_tree

    tree = _review_tree(record)
    base = str(record.get("base_sha") or record.get("base") or "").strip()
    citations = tuple(str(sha) for sha in manifest_commits)
    shared = _shared_branch_revision(record)

    def read() -> list[str]:
        if tree is None:
            return [str(sha) for sha in manifest_commits]
        history = commits_between(
            tree, base, "HEAD", excluding=shared, first_parent=True
        )
        return (
            [sha for sha, _subject in history]
            if history
            else [str(sha) for sha in manifest_commits]
        )

    key = (str(tree) if tree is not None else "", base, citations, shared)
    head_identity = _worktree_head_identity(tree)
    if head_identity in {"no-tree", "no-git", "no-head"}:
        return read()
    return file_memo.memoized_key(
        "promotion_commits_beyond_primary", (*key, head_identity), read
    )


def _default_gate_evidence_from_manifest(
    record: Mapping[str, Any],
    *,
    verdict: str,
    gate_check: Mapping[str, Any] | None,
    commits: Sequence[str],
    no_commit_reason: str = "",
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Fill omitted citations from history before defaulting the gate evidence.

    Explicit citations and a commitless declaration retain their own guards.
    The manifest remains the fallback when no committed range can be read.
    """
    if not commits and not no_commit_reason.strip():
        commits = _promotion_commits(record, ())
    return _manifest_gate_evidence(
        record,
        verdict=verdict,
        gate_check=gate_check,
        commits=commits,
        no_commit_reason=no_commit_reason,
    )


#: The waiver a measurement run's row carries where an ordinary implement run
#: would owe a review: nothing reviews a run nobody merges.
MEASUREMENT_REVIEW_WAIVER = (
    "the run is a measurement run: it builds and gates, and nothing reviews or lands it"
)

#: The declaration a measurement run's row carries where an ordinary run states
#: why its plan's impl did not move.
MEASUREMENT_IMPL_WAIVER = (
    "a measurement run replays a section already declared done, so it moves no impl"
)


def _measurement_reason(record: Mapping[str, Any]) -> str:
    """The reason this run was dispatched as a measurement run, or "".

    Read off the pointer the dispatch wrote, so the review reflex's decision and
    the promotion's are made from one record: a run the reflex refused to review
    is exactly the run whose row states a measurement.
    """
    marker = record.get("measure_only")
    if isinstance(marker, Mapping):
        return str(marker.get("reason") or "").strip() or "measurement run"
    return str(marker or "").strip()


def _release_measurement_worktree(record: Mapping[str, Any]) -> dict[str, Any]:
    """Remove a measurement run's worktree, whose head is never merged.

    The ordinary release withholds a worktree whose HEAD is not an ancestor of
    the integration branch, because that head is the only copy of work nobody
    has merged yet. A measurement run's head is never merged — not landing is
    what the run is for — so the same policy would hold every replay's tree
    back, and a measurement population, which by construction is every arm
    building the same landed sections, would fill the disk it is measured on.
    Nothing is lost by removing it: the commit lives in the repository's object
    store and the ledger row names it, and what the run produced is its figures.

    Removal happens before the ordinary release rather than after it, so the
    release reports the tree as already absent and its audit describes the
    trees this removal left behind instead of contradicting them. A tree
    another live pointer names is left in place: a share of the tree is not
    this run's to take.
    """
    if not _measurement_reason(record):
        return {}
    worktree_value = str(record.get("worktree") or "")
    repo_value = str(record.get("repo") or "")
    if not worktree_value:
        return {}
    worktree = Path(worktree_value)
    if not worktree.is_dir():
        return {}
    repo = Path(repo_value) if repo_value else None
    if repo is None or not repo.is_dir():
        return {"released": False, "withheld": "repository root is unavailable"}
    run_id = str(record.get("run_id") or "")
    claims = [
        claim
        for claim in _live_pointer_worktrees().get(worktree.resolve(), [])
        if claim != run_id
    ]
    if claims:
        return {
            "released": False,
            "withheld": "another live run names this worktree: "
            + ", ".join(sorted(claims)),
        }
    released = _release_worktree_whole(repo, worktree, run_id=run_id)
    if not released.get("released", True):
        return {"released": False, "withheld": released["withheld"]}
    outcome: dict[str, Any] = {"released": True}
    if not released.get("trash_removed"):
        outcome["trash_left"] = released["trash"]
    return outcome


def _run_plan_slug(record: Mapping[str, Any], node: Mapping[str, Any]) -> str:
    """The plan a run works on, named by its node or by its own subject.

    An implementation run carries its plan on the node. A plan-review run names
    the plan as its subject instead and leaves the node's plan empty, so the
    slug is read from the pointer's node id through the reader that owns that
    grammar; taking only the node's plan would file the review's row with no
    plan and no reader could tell which plan was reviewed.
    """
    plan = str(node.get("plan") or record.get("plan") or "").strip()
    if plan:
        return plan
    from reckon.crew.recovery_review_dispatch import _plan_review_pointer_slug

    return _plan_review_pointer_slug(record)


@operational_read
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
    gate_check: Mapping[str, Any] | None = None,
    require_gate_check: bool = False,
    no_commit: str = "",
    suite_delta_waiver: str = "",
    boundary_waiver: str = "",
    resume_waiver: str = "",
    review_waiver: str = "",
    negative_control_waiver: str | None = None,
    discard_resume_worktree: bool = False,
    accepted_paths: Mapping[str, str] | None = None,
    no_impl_change: str = "",
    live_run_waiver: str = "",
    plan_link: str = "",
    unplanned_reason: str = "",
    dry_run: bool = False,
    promoted_by: str = "",
    domain_waiver: str = "",
    dry_run_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Promote a run, or finish cleanup when its record already landed.

    A run whose dispatch started an advisory pick asynchronously is waited for,
    up to the pick's own bound, so its answer reaches the committed row.

    ``dry_run_state`` is where a dry run's own findings are written for the
    report its caller assembles: the guards run in one order and a refusal
    abandons the sweep, so a fact resolved before the refusal — the review the
    run owes — would otherwise be lost with the local that held it.
    """
    verdict = str(gate).strip().lower()
    if verdict not in ledger.GATE_VERDICTS:
        raise ledger.LedgerError(
            f"gate verdict {gate!r} is not one of "
            f"{', '.join(ledger.GATE_VERDICTS)}; a gate whose evidence could "
            "not be produced is 'not-run'"
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
    # A run whose dispatch started an advisory pick asynchronously is promoted
    # possibly before that pick has answered. Wait for it here — outside the
    # pointer lock the picker itself must take — so the row carries the pick's
    # answer instead of reading as one the picker never gave.
    # A dry run waits for nothing and attaches nothing: the pick's timeout is
    # written onto the pointer, which is one of the writes a dry run promises
    # not to make. A pick still in flight is reported instead, so the operator
    # sees the wait a promotion would take rather than paying it here.
    awaiting_pick = _shadow_pick_in_flight(run_id) if dry_run else False
    if not dry_run:
        _await_in_flight_shadow_pick(run_id)
    # Checks and landing retain their own claim while pointer mutations remain
    # available to sweeps and resume. Pointer access holds only its own lock.
    # A dry run takes neither: it writes nothing for a lock to protect, and a
    # lock held by a promotion of the same run would make the read wait on it.
    with _promotion_coordination(run_id, dry_run=dry_run):
        with ExitStack() as _pointer_region:
            if not dry_run:
                _pointer_region.enter_context(_pointer_lock(run_id))
            record = _read_pointer_or_rebuild(run_id, root=root)
        record = {**record, **read_dispatch_detail(record)}
        # A measurement run is one its dispatch marked: it replays work that has
        # already landed, so its row records a measurement rather than a landing
        # and nothing about it is ever reviewed or merged.
        measurement_reason = _measurement_reason(record)
        if measurement_reason:
            if verdict == "failed":
                outcome = "measured"
            elif str(outcome).strip():
                outcome = f"measured: {str(outcome).strip()}"
            else:
                outcome = "measured"
            if not str(no_impl_change).strip():
                # A replay must not move the plan it replays: the section is
                # already declared done, and an impl tick from a duplicate of
                # that work would be a plan movement describing nothing.
                no_impl_change = MEASUREMENT_IMPL_WAIVER
        # A dispatch whose launch was refused leaves a run that names no
        # project: the refusal removed the pointer or never let it carry a
        # project, and the run directory it left behind holds no supervisor
        # record either, so reconstruction resolves no project from it. There
        # is nothing to promote and no project ledger to hold a row, so the run
        # is reported as the withdrawal it is rather than failing validation on
        # the empty project name further down.
        if not str(record.get("project") or "").strip():
            if dry_run:
                return _dry_run_report(
                    run_id,
                    record,
                    verdict=verdict,
                    gate_check=gate_check,
                    refusals=(
                        (
                            "the run names no project, so its dispatch was "
                            "refused before a project was resolved: a promotion "
                            "would withdraw it — retiring its pointer and "
                            "releasing its workspace — and write no ledger row"
                        ),
                    ),
                    would_withdraw=True,
                    would_wait_for_pick=awaiting_pick,
                )
            return _complete_withdrawn_run(run_id, record)
        # Whether a landing commit can be written at all depends only on the
        # checkout's git state, and nothing else promotion reads changes that
        # answer. A checkout carrying a peer's open merge, rebase, cherry-pick
        # or revert is refused here, before the ledger is read or any store is
        # opened, rather than after the minutes of work whose only possible end
        # is this same refusal — work that in a serial sweep delays every later
        # promotion by as much.
        _require_committable_checkout(_landing_checkout(record, root), run_id)
        # A run review delivers its record inside its own run directory and the
        # supervisor files it into the store when the worker ends. A supervisor
        # that died before that filing leaves the record where the reviewer
        # wrote it, and a record no store reader can find is a review that
        # never happened — so the filing is repeated here, which is the last
        # moment the run's own record still exists to be read from.
        delivered_reviews = _delivered_review_records(record) if dry_run else ()
        if not dry_run:
            _file_delivered_run_reviews(record)
        # A review run's outcome is the review it stored, so the operator's
        # hand is not the only source for a non-passing gate's summary; the
        # refusal below stands for every run with no stored review to read.
        outcome = _resolve_promotion_outcome(
            run_id, record, verdict=verdict, outcome=outcome
        )
        # A promotion writes the ledger row into the project's own mount, so
        # the repository is resolved from that mount before anything is
        # written: a checkout named from elsewhere is refused, and a run whose
        # record names none is given the mount rather than the caller's
        # enclosing repository. A project with no mount keeps the caller's
        # checkout, which is the only root available to it. Judged ahead of the
        # per-run evidence so a repository defect is reported as itself rather
        # than as a missing commit further down.
        landing_project = str(record.get("project") or "")
        # Gate command, exit status, log path and commits default to what the
        # worker's manifest already states, so a passing run whose evidence is
        # on disk promotes without the coordinator retyping figures the manifest
        # holds. Only absent values are filled: an operator's own flag always
        # wins, and a manifest that states nothing leaves the guard's ordinary
        # refusal in place.
        gate_check, commit_list = _default_gate_evidence_from_manifest(
            record,
            verdict=verdict,
            gate_check=gate_check,
            commits=commit_list,
            no_commit_reason=no_commit,
        )
        # Every precondition that can be judged on its own is judged here, in
        # the order the promotion checks them, and reported in one refusal: a
        # coordinator learns every way the record falls short from one call
        # rather than one per attempt. The resolutions a gate needs are read
        # inside that gate's own check, so a refusal never leaves a later check
        # reading a half-built value.
        resolved: dict[str, Any] = {}

        def _repository_root() -> str | Path | None:
            if (
                landing_project
                and project_mount_repository(landing_project) is not None
            ):
                resolved["root"] = resolve_project_repository(
                    landing_project, root, flag="--checkout-path"
                )
            else:
                resolved["root"] = root
            return resolved["root"]

        def _review_gate() -> dict[str, str] | None:
            from reckon.crew.recovery import classify_pointer

            classified = classify_pointer(record)
            resolved["classified"] = classified
            resolved["classification"] = str(classified.get("classification") or "")
            # The tier and the promoted revision read the commits the run
            # presents, so a commitless declaration is read as none here too:
            # the run changed nothing in the repository, and its tier is
            # resolved from what its manifest declares rather than from a
            # sentence nothing can resolve.
            gate_commits = _presented_commits_without_a_declaration(record, commit_list)
            # The revision this promotion asserts, resolved before the gate
            # reads the store, so a review of an earlier revision is refused
            # rather than accepted as evidence about code the repair has
            # already moved past.
            resolved["promoted_revision"] = _run_promoted_revision(record, gate_commits)
            reviewed, stale_review_head = _review_for_promotion(
                landing_project,
                run_id,
                promoted_revision=resolved["promoted_revision"],
                tree=_record_tree(record),
            )
            resolved["reviewed"] = reviewed
            # The tier is computed before the gate reads it, from what the run
            # actually changed at the promoted head, so the refusal and the row
            # it would have written agree about which review the run owes.
            resolved["review_tier"] = _run_review_tier(
                run_id,
                record,
                commit_list=gate_commits,
                root=resolved.get("root", root),
            )
            if measurement_reason:
                # The reflex composes no review for a measurement run, so the
                # run owes none and the row states why instead of holding the
                # landing for a review that will never arrive. The tier is still
                # resolved, because the row records what the run changed.
                return {"reason": MEASUREMENT_REVIEW_WAIVER}
            if dry_run_state is not None:
                # The refusal below abandons the sweep, so the review this run
                # owes is recorded before it is raised: the dry run's report
                # states what is missing even when it also states why.
                dry_run_state["reviews_owed"] = _reviews_the_dry_run_owes(
                    run_id,
                    record,
                    verdict=verdict,
                    classification=resolved["classification"],
                    review=reviewed,
                    review_tier=resolved["review_tier"],
                    promoted_head=resolved["promoted_revision"],
                    stale_head=stale_review_head,
                )
            return _require_review_waiver(
                run_id,
                record,
                verdict=verdict,
                classification=resolved["classification"],
                review=reviewed,
                review_action=str(classified.get("next_action") or ""),
                waiver_reason=review_waiver,
                review_tier=resolved["review_tier"],
                promoted_head=resolved["promoted_revision"],
                stale_head=stale_review_head,
                manifest_commits=commit_list,
            )

        def _resume_gate() -> dict[str, str] | None:
            classified = resolved.get("classified")
            if not isinstance(classified, Mapping):
                classified = {}
            classification_name = str(resolved.get("classification") or "")
            candidate_remedy = classified.get("resume_remedy")
            resume_remedy = (
                dict(candidate_remedy)
                if isinstance(candidate_remedy, Mapping)
                else None
            )
            candidate_resolution = classified.get("session_resolution")
            if classification_name == "blocked" and isinstance(
                candidate_resolution, Mapping
            ):
                recoverable_session = (
                    {
                        "session_id": str(candidate_resolution["session_id"]),
                        "source": str(candidate_resolution["source"]),
                    }
                    if candidate_resolution.get("resolved")
                    else None
                )
            elif resume_remedy is not None:
                recoverable_session = {
                    "session_id": str(resume_remedy["session_id"]),
                    "source": str(resume_remedy["source"]),
                }
            else:
                recoverable_session = _recoverable_session(record)
            resolved["resume_remedy"] = resume_remedy
            resolved["recoverable_session"] = recoverable_session
            waived = _require_resume_waiver(
                run_id,
                verdict=verdict,
                waiver_reason=resume_waiver,
                classification=classification_name,
                recoverable_session=recoverable_session,
            )
            if discard_resume_worktree and waived is None:
                raise CrewError(
                    "discard_resume_worktree requires a reasoned resume waiver "
                    "for a recoverable non-passing run"
                )
            return waived

        def _landing_gate() -> dict[str, Any]:
            landing_root = resolved.get("root", root)
            if landing_root is None:
                landing_root = record.get("repo")
            checkout = (
                Path(landing_root).expanduser().resolve()
                if landing_root is not None
                else None
            )
            return _landing_preconditions(
                run_id,
                record,
                checkout=checkout,
                ledger_root=landing_root,
                commits=commit_list,
                gate=gate,
                failure_classification=classification,
                no_impl_change=no_impl_change,
                plan_link=plan_link,
                unplanned_reason=unplanned_reason,
                boundary_waiver=boundary_waiver,
                negative_control_waiver=negative_control_waiver,
                accepted_paths=accepted_paths,
                gate_check=gate_check,
                require_gate_check=require_gate_check,
                measure=not dry_run,
            )

        def _domain_gate() -> dict[str, Any] | None:
            """Run the repository's declared invariants against the tree shipping.

            The node's own write paths decide which of the repository's declared
            rules apply, through the one module that answers that, so a node
            whose paths match no rule runs nothing here.
            """
            node_record = record.get("node")
            return _require_domain_invariant_checks(
                run_id,
                landing_project,
                node_record if isinstance(node_record, Mapping) else {},
                repository=resolved.get("root", root) or record.get("repo"),
                waiver_reason=domain_waiver,
            )

        # The landing checks read the ledger several times and the locked
        # promotion reads it again a moment later. One region serves them
        # all one index decode per read key, and _complete_locked closes it
        # before its append so a read after the row is written is fresh.
        with ExitStack() as ledger_reads:
            ledger_reads.enter_context(ledger.index_reuse())
            (
                root,
                overridden_worktree_changes,
                _manifest_status,
                _manifest_fault,
                live_run_waived,
                _partner_hold,
                _shadow_commit,
                commit_list_shortfall,
                _commits_beyond_base,
                _gate_log_agrees,
                _verdict_matches_exit_status,
                _runnable_gate_command,
                _executable_gate_command,
                review_waived,
                _misdelivered_review,
                _standing_suite,
                resume_waived,
                landing,
                _png_twin_absent,
                _domain_checks,
            ) = _require_independently(
                [
                    _repository_root,
                    lambda: _require_commit_for_changed_manifest(
                        run_id, record, no_commit_reason=no_commit
                    ),
                    lambda: _require_recognised_manifest_status(run_id, record),
                    # The rest of the manifest's own faults, refused with the
                    # reason the worker's check emits while the run still holds
                    # a turn. Judged from the file rather than the repository, so
                    # the two readers cannot answer differently.
                    lambda: _require_no_manifest_fault(run_id, record),
                    # A run whose own worker is still alive and not finished is
                    # refused before any store is written: deleting its live
                    # pointer now would orphan the process until it exits on its
                    # own. The waiver names the reason and rides the promoted row.
                    lambda: _require_worker_stopped_before_promotion(
                        run_id, record, waiver_reason=live_run_waiver
                    ),
                    # A paired dispatch starts both arms from one base, and that
                    # only holds while neither lands first: an arm promoted while
                    # its partner is still running would put its result in the main
                    # checkout for the partner to read. The hold lifts once the
                    # partner's pointer is terminal or gone.
                    lambda: _require_partner_terminal_before_promotion(run_id, record),
                    lambda: _refuse_commits_for_a_shadow(run_id, record, commit_list),
                    lambda: _require_gate_evidence(
                        run_id,
                        record,
                        verdict=verdict,
                        commits=commit_list,
                        no_commit_reason=no_commit,
                    ),
                    # The citations themselves must be the run's own work, judged
                    # before any store is written so a run that cannot be asserted
                    # truthfully is refused with nothing landed and nothing to
                    # unwind.
                    lambda: _require_commits_beyond_base(run_id, record, commit_list),
                    lambda: _require_gate_log_agrees(
                        run_id, gate_check, record, verdict=verdict
                    ),
                    # The verdict and the asserted status are two statements about
                    # one run: a passing verdict beside a nonzero exit status is
                    # refused from those two facts alone, whether or not the log
                    # carries a terminal EXIT record of its own for the comparison
                    # above to read.
                    lambda: _require_verdict_matches_exit_status(
                        run_id, record, gate_check, verdict=verdict
                    ),
                    # The recorded command is what the integration re-run executes,
                    # so a text that describes the command or names no program must
                    # be refused here rather than land on a row that later reports a
                    # failure the merge did not cause.
                    lambda: _require_runnable_gate_command(run_id, gate_check),
                    lambda: _require_executable_gate_command(run_id, gate_check),
                    _review_gate,
                    # A plan review that wrote its report outside its assigned
                    # directory leaves the delivery read by neither the store nor
                    # promotion, so the review is lost silently. Refuse it, naming
                    # both directories, rather than promote a run whose report sits
                    # where nothing joins it.
                    lambda: _require_plan_review_delivered_in_its_directory(
                        landing_project, run_id, record
                    ),
                    # The project's declared suite is the gate that sees the whole
                    # tree, and the lighter promotions wait on it. The tier is the
                    # one just resolved, so a full review -- which reads the run for
                    # itself -- is never held.
                    lambda: _require_standing_suite(
                        landing_project,
                        resolved.get("review_tier") or review_tiers.FULL,
                        resolved.get("root", root),
                    ),
                    _resume_gate,
                    _landing_gate,
                    # A figure rule that needs no eyes is checked by the tool, so a
                    # coordinator never enforces it by hand. The one such rule a
                    # run's commits can break is the PNG twin: a worker that
                    # rasterises its SVG to look at it leaves the copy in the tree,
                    # and the page renders the SVG while the copy drifts. The same
                    # finding reaches the worker through `crew check --manifest`.
                    lambda: _require_no_png_twin_of_an_svg(run_id, record),
                    # The repository declares, beside each domain rule, the check
                    # whose zero exit means the rule holds. They are replayed
                    # against the tree that ships through the same integration
                    # replay as the project's suite command, so a result that
                    # breaks a declared rule is refused at promotion rather than
                    # only carried in the brief.
                    _domain_gate,
                ]
            )
            suite_delta = _evaluate_suite_delta(
                run_id,
                record,
                waiver_reason=suite_delta_waiver,
                gate_verdict=verdict,
                dry_run=dry_run,
            )
            if dry_run:
                return _dry_run_report(
                    run_id,
                    record,
                    verdict=verdict,
                    gate_check=gate_check,
                    refusals=(),
                    no_commit=no_commit,
                    no_impl_change=no_impl_change,
                    would_file_reviews=delivered_reviews,
                    would_wait_for_pick=awaiting_pick,
                    suite_delta=suite_delta,
                    reviews_owed=(dry_run_state or {}).get("reviews_owed") or (),
                )
            result = _complete_locked(
                run_id,
                record=record,
                gate=gate,
                failure_classification=classification,
                commits=commit_list,
                no_commit=no_commit,
                no_commit_uncommitted=overridden_worktree_changes,
                outcome=outcome,
                tests_added=tests_added,
                scope_changed=scope_changed,
                changed_lines=changed_lines,
                completed_at=completed_at,
                root=root,
                gate_check=gate_check,
                require_gate_check=require_gate_check,
                suite_delta=suite_delta,
                boundary_waiver=boundary_waiver,
                domain_waiver_record=_domain_checks,
                resume_remedy=resolved.get("resume_remedy"),
                resume_waived=resume_waived,
                reviewed=resolved.get("reviewed"),
                review_waived=review_waived,
                review_tier=resolved.get("review_tier") or "",
                negative_control_waiver=negative_control_waiver,
                recoverable_session=resolved.get("recoverable_session"),
                discard_resume_worktree=discard_resume_worktree,
                accepted_paths=accepted_paths,
                commit_list_shortfall=commit_list_shortfall,
                no_impl_change=no_impl_change,
                live_run_waived=live_run_waived,
                plan_link=plan_link,
                unplanned_reason=unplanned_reason,
                promoted_by=promoted_by,
                landing=landing,
                ledger_reads=ledger_reads,
            )
            if commit_list_shortfall is not None:
                result["commit_list_shortfall"] = dict(commit_list_shortfall)
            # The section this run delivered has now landed, so the requests queued
            # behind it are released here rather than at the next admission pass:
            # release latency is then the promotion's own duration instead of the
            # duration of whatever pass is in progress.
            result["released"] = _release_waiters_of_the_landed_section(record)
            # A promotion that succeeded here answers any request queued for
            # the same run, so the request is settled now, in the process that
            # landed it, rather than being drained again or raising a duty for
            # a run that is already promoted.
            from reckon.crew import promotion_requests

            result["settled_requests"] = promotion_requests.settle_requests_for(
                run_id, settled_as="promoted"
            )
            return result


def _release_waiters_of_the_landed_section(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Release the requests waiting on the section this promotion delivered.

    The landing is already recorded when this runs, so a release that raises is
    reported here and leaves the landing as it stands: a release is a
    consequence of a landing, never a condition of one. A failure that belongs
    to a single request is written on that request, by the release itself.
    """
    from reckon.crew import queue_admission

    node = record.get("node") or {}
    plan = str(node.get("plan") or "").strip()
    section = str(node.get("section") or "").strip()
    if not plan or not section:
        return {
            "candidates": [],
            "promoted": [],
            "blocked": [],
            "failed": {},
        }
    ref = f"{plan}#{section}"
    try:
        return queue_admission._release_waiters(
            [ref], project=str(record.get("project") or "").strip()
        )
    except Exception as exc:  # noqa: BLE001 - the landing has already happened
        return {
            "candidates": [],
            "promoted": [],
            "blocked": [],
            "failed": {},
            "error": f"releasing the waiters of {ref!r} raised: {exc}",
        }


# A deferred shadow pick is started detached by dispatch and attaches its answer
# to the live pointer within its own bound. A promotion that lands inside that
# window would otherwise record no picker_selection, and the row would read as
# one the picker never answered rather than one whose pick had not finished.
_DRY_RUN_REASON_FLAGS = (
    "--no-impl-change",
    "--waive-unreviewed-promotion",
    "--waive-suite-delta",
    "--waive-boundary-refusal",
    "--waive-resume-path",
    "--waive-negative-control",
    "--waive-live-run",
    "--no-commit",
)
_DRY_RUN_REASON_PLACEHOLDER = "recorded by the dry run"


def _dry_run_invocation(
    run_id: str,
    *,
    verdict: str,
    gate_check: Mapping[str, Any] | None,
    refusals: Sequence[str],
) -> str:
    """The one command that would promote this run, given the state it is in.

    The refusals are the run's state: each names the flag that resolves it, so
    the invocation is assembled from the remedies the refusals themselves
    state rather than from a second reading of the record that could drift from
    them. A gate-check refusal names no flag — the check is what is missing —
    so the arm for it offers the check that the passing verdict already fixes:
    the exit status of a passing gate is zero, and the command and log are the
    ones the promotion resolved, offered back only where they are absent. The
    reason values are concrete text rather than placeholders, so the emitted
    line can be run as it stands.
    """
    parts = ["reckon", "crew", "complete", "--run", run_id, "--gate", str(verdict)]
    text = "\n".join(refusals)
    if str(verdict).strip().lower() == "failed":
        parts += [
            "--failure-classification",
            "pre-existing",
            "--outcome",
            shlex.quote("state the failure in one line"),
        ]
    check = gate_check if isinstance(gate_check, Mapping) else {}
    if "the check that produced it" in text:
        if not str(check.get("command") or "").strip():
            parts += ["--gate-command", shlex.quote("<the gate command that ran>")]
        if not str(check.get("log_path") or "").strip():
            parts += ["--gate-log-path", shlex.quote("<the captured log path>")]
        if check.get("exit_status") is None:
            parts += [
                "--gate-exit-status",
                "0" if str(verdict).strip().lower() == "passed" else "<exit status>",
            ]
    for flag in _DRY_RUN_REASON_FLAGS:
        if flag in text:
            parts += [flag, shlex.quote(_DRY_RUN_REASON_PLACEHOLDER)]
    if "--accept-path" in text:
        parts += ["--accept-path", "<path>", shlex.quote(_DRY_RUN_REASON_PLACEHOLDER)]
    return " ".join(parts)


def _dry_run_report(
    run_id: str,
    record: Mapping[str, Any],
    *,
    verdict: str,
    gate_check: Mapping[str, Any] | None,
    refusals: Sequence[BaseException],
    no_commit: str = "",
    no_impl_change: str = "",
    would_withdraw: bool = False,
    would_file_reviews: Sequence[str] = (),
    would_wait_for_pick: bool = False,
    suite_delta: Mapping[str, Any] | None = None,
    reviews_owed: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Report every refusal a promotion would raise, without writing any of it.

    A refusal is gathered rather than raised, so an operator fixing a run by
    trial learns every way it falls short from one call. ``refusals`` is empty
    for a run that would promote, and ``invocation`` names the one command that
    would pass for the state the run is in — which for a run with nothing to
    fix is the command that was just evaluated.

    The three paths whose promotion writes before any guard is judged are
    reported rather than taken: ``would_withdraw`` for a run naming no project,
    ``would_file_reviews`` for the delivered review records a filing would move
    into the store, and ``would_wait_for_pick`` for a deferred pick still to
    answer. A withdrawn run has nothing to invoke — its promotion writes no
    ledger row — so no command is offered for it.

    ``suite_delta`` is the suite evaluation the promotion ran, and
    ``would_measure`` names the one state it can be in where nothing was
    measured: the landing would run the armed command over the base and head
    trees itself. ``reviews_owed`` names the reviews a real promotion refuses
    for want of, so an empty ``would_file_reviews`` — no record was delivered —
    cannot be read as no review being needed.
    """
    messages = [str(refusal) for refusal in refusals]
    invocation = (
        ""
        if would_withdraw
        else _dry_run_invocation(
            run_id,
            verdict=verdict,
            gate_check=gate_check,
            refusals=messages,
        )
    )
    report: dict[str, Any] = {
        "ok": not messages,
        "dry_run": True,
        "run_id": run_id,
        "project": str(record.get("project") or ""),
        "gate": str(verdict),
        "would_promote": not messages,
        "refusal_count": len(messages),
        "refusals": messages,
        "invocation": invocation,
        "would_withdraw": would_withdraw,
        "would_file_reviews": [str(path) for path in would_file_reviews],
        "would_wait_for_pick": would_wait_for_pick,
        "suite_delta": dict(suite_delta) if isinstance(suite_delta, Mapping) else None,
        "would_measure": (
            isinstance(suite_delta, Mapping)
            and str(suite_delta.get("status") or "") == "would-measure"
        ),
        "reviews_owed": [dict(entry) for entry in reviews_owed],
    }
    if would_file_reviews:
        # The filing runs before the guards, so a review check that reads the
        # store here reads it without these records. Said plainly rather than
        # left for the operator to infer from a refusal naming no review.
        report["notes"] = [
            (
                "the checks read the review store before the delivered records "
                "listed in would_file_reviews would be filed into it"
            )
        ]
    if report["would_measure"] and not messages:
        # The promotion is not refused, but it is not yet evidenced either: the
        # promotion is contingent on a measurement the landing takes and this
        # call did not, and a reader who took would_promote as the whole answer
        # would be surprised by the field list a failing measurement names.
        report.setdefault("notes", []).append(
            "the run recorded no complete baseline and after suite pair, so the "
            "landing runs the armed command over the base and head trees "
            "itself; this dry run measured nothing and starts nothing, and the "
            "fields listed in suite_delta.missing_fields are the ones a "
            "measurement that cannot read the pair refuses on"
        )
    return report


def complete_dry_run(run_id: str, **kwargs: Any) -> dict[str, Any]:
    """Evaluate a promotion's every guard and report them, writing nothing.

    The guards are the promotion's own, run in the order it runs them, so a run
    the dry run reports as promotable promotes for real on the same evidence.
    Every refusal is carried back rather than raised, because the value of the
    dry run is the whole list.
    """
    gate = str(kwargs.get("gate") or "")
    root = kwargs.get("root")
    state: dict[str, Any] = {}
    try:
        return complete(run_id, dry_run=True, dry_run_state=state, **kwargs)
    except _PromotionRefusalError as refusal:
        refusals = list(refusal.refusals)
    except (CrewError, ledger.LedgerError) as refusal:
        refusals = [refusal]
    try:
        record = _read_pointer_or_rebuild(run_id, root=root)
    except CrewError:
        record = {}
    # The invocation is built from the check the fill resolved, not from the
    # operator's flags alone: a run whose manifest already carries the command
    # and the log needs neither of them offered back, and offering them would
    # name a check the run had already supplied.
    filled_check, _filled_commits = _default_gate_evidence_from_manifest(
        record,
        verdict=gate,
        gate_check=kwargs.get("gate_check"),
        commits=kwargs.get("commits") or (),
        no_commit_reason=str(kwargs.get("no_commit") or ""),
    )
    return _dry_run_report(
        run_id,
        record,
        verdict=gate,
        gate_check=filled_check,
        refusals=refusals,
        no_commit=str(kwargs.get("no_commit") or ""),
        no_impl_change=str(kwargs.get("no_impl_change") or ""),
        would_file_reviews=_delivered_review_records(record),
        would_wait_for_pick=_shadow_pick_in_flight(run_id),
        reviews_owed=state.get("reviews_owed") or (),
    )


_SHADOW_PICK_SPEC_NAME = "shadow-picker.json"
_SHADOW_PICK_WAIT_SECONDS = 5.0
_SHADOW_PICK_POLL_SECONDS = 0.1


def _shadow_pick_in_flight(run_id: str) -> bool:
    """Whether a deferred shadow pick is still to answer for this run.

    Dispatch starts the advisory pick detached, so a promotion landing inside
    the pick's window would otherwise record no selection. Both the wait and the
    dry run's report of what it would wait for are decided here, so the two
    cannot disagree about whether a pick is outstanding.
    """
    if not (run_dir(run_id) / _SHADOW_PICK_SPEC_NAME).exists():
        return False
    return _pointer_awaits_shadow_pick(run_id)


def _await_in_flight_shadow_pick(run_id: str) -> None:
    """Wait for a deferred shadow pick to attach, up to the pick's own bound.

    Dispatch can only start the advisory pick after the run exists, so a run
    promoted inside the pick's window carries no selection. The wait runs before
    the pointer lock is taken — the detached picker needs that lock to publish
    its answer — and on a timeout it names the timeout on the pointer, so the
    ledger row says the pick did not finish rather than saying nothing. A run
    whose dispatch launched no deferred pick, or whose pointer is already gone,
    returns at once.
    """
    if not (run_dir(run_id) / _SHADOW_PICK_SPEC_NAME).exists():
        return
    deadline = _shadow_pick_deadline(run_id)
    if deadline is None:
        return
    while time.monotonic() < deadline:
        if not _pointer_awaits_shadow_pick(run_id):
            return
        time.sleep(_SHADOW_PICK_POLL_SECONDS)
    if _pointer_awaits_shadow_pick(run_id):
        _attach_shadow_pick_timeout(run_id)


def _pointer_awaits_shadow_pick(run_id: str) -> bool:
    """Whether the live pointer is still waiting for the deferred pick's answer."""
    try:
        pointer = read_pointer(run_id)
    except CrewError:
        return False
    return dispatch_field(pointer, "picker_selection") is None


def _shadow_pick_deadline(run_id: str) -> float | None:
    """The monotonic instant the in-flight pick's bound expires, or None.

    None means no live pointer is present to attach an answer to, so there is
    nothing to wait for. The bound runs from the pick's own recorded start — the
    spec file written immediately before it is launched — because the pick does
    not begin until after the worker launch: a wait measured from the pointer's
    created_at would spend a slow launch's seconds before the pick had started.
    created_at is the fallback only for a pointer whose pick start is not
    recorded, and a pointer whose creation time cannot be read either is given
    the full bound rather than skipped, because the pick's start is then unknown.
    """
    try:
        pointer = read_pointer(run_id)
    except CrewError:
        return None
    started = _shadow_pick_started(run_id) or parse_utc(pointer.get("created_at"))
    if started is None:
        return time.monotonic() + _SHADOW_PICK_WAIT_SECONDS
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return time.monotonic() + max(0.0, _SHADOW_PICK_WAIT_SECONDS - elapsed)


def _shadow_pick_started(run_id: str) -> datetime | None:
    """The instant the deferred pick was dispatched, or None.

    The spec file is written by the dispatch immediately before it launches the
    pick, and is only read thereafter, so its mtime is the pick's recorded start.
    An absent or unreadable spec means no start was recorded.
    """
    try:
        spec = (run_dir(run_id) / _SHADOW_PICK_SPEC_NAME).stat()
    except OSError:
        return None
    return datetime.fromtimestamp(spec.st_mtime, tz=UTC)


def _attach_shadow_pick_timeout(run_id: str) -> None:
    """Name the timeout on the pointer, unless the pick landed meanwhile."""
    from reckon.crew.dispatch_picker import (
        _picker_fallback,
        _write_existing_pointer,
    )

    with _pointer_lock(run_id):
        if not _pointer_awaits_shadow_pick(run_id):
            return
        pointer = read_pointer(run_id)
        pointer["picker_selection"] = _picker_fallback(
            "timeout", "the shadow pick did not finish before promotion"
        )
        _write_existing_pointer(run_id, pointer)


def _shadow_control_marks(
    record: Mapping[str, Any],
    ledger_runs: Iterable[Mapping[str, Any]],
    tree: Path | None,
    lineage: Any,
) -> dict[str, Any]:
    """The marks a shadow's row stores about contamination and control.

    The row is assembled before the stream has been read, so the control verdict
    taken there can only judge the configuration; the scan needs the primary's
    row and the run's own stream, and both are in hand here. The verdict is
    re-taken through the one predicate with the scan's answer, so a contaminated
    row is never stored as a controlled arm, and a reason is recorded only when
    there is one.
    """
    contamination = _shadow_stream_contamination(record, ledger_runs, tree)
    marks: dict[str, Any] = {
        "shadow_controlled": ledger.shadow_controlled(
            lineage, contaminated=contamination
        )
    }
    if contamination:
        marks["shadow_contaminated"] = contamination
    return marks


def _profile_fold_anchor(
    project: str, ledger_root: str | Path | None
) -> dict[str, Any] | None:
    """The ledger state a promotion's profile fold may extend, or ``None``.

    Read before the append, from file metadata alone, so the fold that follows
    can compare it with the state the stored profile was written against
    without opening the ledger's index or any run document.
    """

    try:
        return ledger.index_state_stamp(project, ledger_root)
    except (OSError, ValueError, TypeError):
        return None


def _budget_overrun_facts(
    record: Mapping[str, Any], *, finished: str | None = None
) -> dict[str, Any]:
    """The overrun pair a committed row carries, measured as the watch measures it.

    Promotion deletes the live pointer that holds the only other copy of this
    verdict, so a row that does not read it here leaves an overrun recorded
    nowhere durable. The measurement is taken with the same reader the watch
    uses, over the same record, elapsing to the run's own completion — so the
    figure on the row and the figure in the message the worker read are one
    fact rather than two clocks. A run with no declared allowance carries the
    keys with zero overrun rather than omitting them, because an absent field
    and a measured zero read the same to a census.
    """
    from reckon.crew import recovery_stream

    moment = None
    if finished:
        parsed = parse_utc(finished)
        if parsed is not None:
            moment = parsed.timestamp()
    try:
        timing = recovery_stream._budget_timing(record, now_seconds=moment)
    except (CrewError, TypeError, ValueError):
        timing = {}
    overrun_seconds = int(timing.get("budget_overrun_seconds") or 0)
    overrun_tokens = int(timing.get("budget_overrun_tokens") or 0)
    return {
        "budget_overrun": bool(timing.get("budget_overrun")) or overrun_seconds > 0,
        "budget_overrun_seconds": overrun_seconds,
        "budget_overrun_tokens": overrun_tokens,
        "budget_overrun_notice": str(record.get("budget_notice_path") or "") or None,
    }


def _record_release_on_ledger(**kwargs):
    """Serialize the release row update together with its landing commit."""
    from reckon.crew.promotion_scope import (
        _acquire_branch_write,
        _branch_write_scope,
        _release_branch_write,
    )

    with _branch_write_scope():
        _acquire_branch_write(kwargs.get("checkout"))
        try:
            return _write_release_record(**kwargs)
        finally:
            _release_branch_write()


def _with_branch_write_scope(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        from reckon.crew.promotion_scope import _branch_write_scope

        with _branch_write_scope():
            return function(*args, **kwargs)

    return invoke


@dataclass
class _PlanWriteSnapshot:
    """The plan and its path's index state before a landing writes them."""

    checkout: Path
    path: Path
    content: bytes
    mode: int
    index: str
    head_entry: str
    run_id: str
    head_content: bytes | None
    commit_attempted: bool = False
    rollback_content: bytes | None = None
    rollback_removed: bool = False
    expected_content: bytes | None = None
    comment_id: str | None = None
    comment_anchor: str | None = None

    def index_entry(self) -> str:
        return _git(
            self.checkout, "ls-files", "--stage", "-z", "--", str(self.path)
        ).stdout

    def committed_entry(self) -> str:
        return _git(self.checkout, "ls-tree", "-z", "HEAD", "--", str(self.path)).stdout

    def require_owned_content(self, current: bytes) -> None:
        """Accept only exact preimages or the rendered write this run intended."""
        if current not in (self.content, self.expected_content, self.rollback_content):
            raise CrewError(
                "plan content changed after the snapshot; preserving the other writer's plan"
            )

    def require_owned_index(self, current: bytes | None) -> None:
        entry = self.index_entry()
        if entry == self.index or (self.rollback_removed and not entry):
            return
        if self.commit_attempted:
            relative = self.path.relative_to(self.checkout).as_posix()
            mode = "100755" if self.mode & 0o111 else "100644"
            blob = _git(
                self.checkout, "hash-object", "--", str(self.path)
            ).stdout.strip()
            if entry == f"{mode} {blob} 0\t{relative}\0":
                return
            if self.rollback_content is not None:
                metadata, name = self.head_entry.split("\t", 1)
                head_mode, _kind, head_blob = metadata.split()
                if entry == f"{head_mode} {head_blob} 0\t{name}":
                    return
        raise CrewError(
            "plan index changed after the snapshot; preserving the other writer's staging"
        )

    def restore(self, *, plan_locked: bool = False) -> None:
        from reckon.crew.merge import _run_waiting_out_a_held_index_lock
        from reckon.crew.promotion_scope import (
            _INDEX_LOCK_RETRY_ATTEMPTS,
            _INDEX_LOCK_RETRY_INTERVAL_SECONDS,
        )

        index_path = Path(
            _git(self.checkout, "rev-parse", "--git-path", "index").stdout.strip()
        )
        if not index_path.is_absolute():
            index_path = self.checkout / index_path
        lock_path = index_path.with_name(index_path.name + ".lock")

        def restore_owned(_attempt: int) -> subprocess.CompletedProcess:
            try:
                handle = lock_path.open("xb")
            except FileExistsError:
                return subprocess.CompletedProcess(
                    ["git", "update-index"],
                    128,
                    "",
                    f"Unable to create '{lock_path}': File exists",
                )
            try:
                current = self.path.read_bytes() if self.path.exists() else None
                if current is None:
                    if not self.rollback_removed:
                        raise CrewError(
                            "plan was removed after the snapshot; preserving the other writer's deletion"
                        )
                else:
                    self.require_owned_content(current)
                self.require_owned_index(current)
                if current is not None and self.path.stat().st_mode != self.mode:
                    raise CrewError(
                        "plan mode changed after the snapshot; preserving the other writer's plan"
                    )
                index_changed = self.index_entry() != self.index
                if index_changed:
                    # The real index lock covers the ownership check and the
                    # publication; git edits this run's private index copy.
                    handle.write(index_path.read_bytes())
                    handle.close()
                    relative = self.path.relative_to(self.checkout).as_posix()
                    object_length = len(
                        _git(self.checkout, "rev-parse", "HEAD").stdout.strip()
                    )
                    entries = f"0 {'0' * object_length}\t{relative}\0" + self.index
                    result = subprocess.run(
                        ["git", "update-index", "-z", "--index-info"],
                        cwd=self.checkout,
                        input=entries,
                        text=True,
                        capture_output=True,
                        check=False,
                        env={**os.environ, "GIT_INDEX_FILE": str(lock_path)},
                    )
                    if result.returncode:
                        return result
                if current != self.content:
                    _store.write_atomically(
                        self.path,
                        lambda target: target.write(self.content),
                        binary=True,
                        mode=self.mode,
                    )
                if index_changed:
                    restored_index = lock_path.read_bytes()
                    _store.write_atomically(
                        index_path,
                        lambda target: target.write(restored_index),
                        binary=True,
                        mode=index_path.stat().st_mode,
                    )
                if (
                    self.path.read_bytes() != self.content
                    or self.index_entry() != self.index
                ):
                    raise CrewError(
                        "the restored plan or index entry differs from its snapshot"
                    )
                return subprocess.CompletedProcess(["git", "update-index"], 0, "", "")
            finally:
                handle.close()
                lock_path.unlink()

        # Editors use this same resource lock. The git index lock also excludes
        # an index writer between the ownership check and the path-specific update.
        with (
            nullcontext()
            if plan_locked
            else _store._serialized_path_lock(self.path, "plans")
        ):
            restored, _retries = _run_waiting_out_a_held_index_lock(
                self.checkout,
                restore_owned,
                attempts=_INDEX_LOCK_RETRY_ATTEMPTS,
                retry_seconds=_INDEX_LOCK_RETRY_INTERVAL_SECONDS,
            )
            if restored.returncode:
                raise CrewError(
                    restored.stderr.strip() or "could not restore the plan index entry"
                )


def _declared_sections(state: Mapping[str, Any]) -> set[str]:
    """The section ids a plan document declares, from its parsed state."""
    identity = {
        str(record.get("id") or "")
        for record in (state.get("sections") or [])
        if isinstance(record, Mapping)
    }
    identity.update(str(key) for key in (state.get("section_declarations") or {}))
    identity.discard("")
    return identity


def _refuse_a_comment_the_plan_cannot_hold(
    snapshot: _PlanWriteSnapshot | None, section: str
) -> None:
    """Stop a landing whose comment the current plan has nowhere to put.

    A plan that declared the run's section when the snapshot was taken, and no
    longer declares it now, has nowhere to put this landing's comment. The
    landing stops here — before the comment write and before the ledger row —
    rather than recording a promotion with no landing record. A plan that never
    declared the section answers nothing about it, so the landing proceeds as
    it always has.
    """
    if snapshot is None or not str(section or "").strip():
        return
    from reckon import _plan_html

    try:
        text = snapshot.path.read_bytes().decode("utf-8")
    except OSError:
        return
    anchor = _plan_html.section_anchor(section)
    held_before = _declared_sections(
        _plan_html.read_state(snapshot.content.decode("utf-8"))
    )
    if anchor not in held_before:
        return
    held_now = _declared_sections(_plan_html.read_state(text))
    if anchor not in held_now:
        raise CrewError(
            f"the plan no longer holds section {section!r}: the landing comment "
            "cannot be placed, so the landing stopped before writing the ledger row"
        )


def _adopt_a_plan_edited_after_the_snapshot(
    snapshot: _PlanWriteSnapshot, current: bytes
) -> _PlanWriteSnapshot | None:
    """The snapshot to commit when the plan was edited after ours was taken.

    A landing comment is an append, so the document now on disk is the other
    writer's committed plan plus this run's comment. That is recognised by
    re-rendering the plan at HEAD with the state parsed from disk and requiring
    the result to be byte-identical to the file: a writer that changed anything
    else does not match, and the caller's refusal stands. When it does
    match, the rollback target becomes HEAD's plan — the state a refusal must
    leave behind — rather than the preimage this run snapshotted before the
    other writer's edit existed.
    """
    from reckon import _plan_html

    if not snapshot.comment_id:
        return None
    relative = snapshot.path.relative_to(snapshot.checkout).as_posix()
    shown = _git(snapshot.checkout, "show", f"HEAD:{relative}", check=False)
    if shown.returncode:
        return None
    head_text = shown.stdout
    try:
        actual = _plan_html.read_state(current.decode("utf-8"))
    except (ValueError, TypeError):
        return None
    entries = (actual.get("comments") or {}).get(snapshot.comment_anchor or "", [])
    if not any(str(item.get("id") or "") == snapshot.comment_id for item in entries):
        return None
    rendered = _plan_html.write_state(
        head_text,
        actual,
        drop_authored_impl_meta=True,
    )
    if rendered.encode("utf-8") != current:
        return None
    head_bytes = head_text.encode("utf-8")
    return replace(
        snapshot,
        content=head_bytes,
        mode=snapshot.path.stat().st_mode,
        index=snapshot.index_entry(),
        head_entry=snapshot.committed_entry(),
        head_content=head_bytes,
        expected_content=current,
        rollback_content=None,
        commit_attempted=False,
    )


def _record_comment_with_snapshot(
    snapshot: _PlanWriteSnapshot | None, **kwargs: Any
) -> dict:
    if snapshot is not None:
        from reckon import _plan_html

        _refuse_a_comment_the_plan_cannot_hold(
            snapshot, str(kwargs.get("section") or "")
        )
        narrative = str(kwargs.get("landing") or kwargs.get("narrative") or "").strip()
        if narrative:
            state = _store._state_from_text(
                kwargs["project"], snapshot.content.decode("utf-8"), kwargs.get("root")
            )
            from reckon.crew.promotion_scope import _landing_comment_update

            payload, _anchor, _comment, _existing = _landing_comment_update(
                state,
                section=kwargs["section"],
                run_id=snapshot.run_id,
                narrative=narrative,
                author=kwargs["author"],
                when=kwargs["when"],
            )
            snapshot.comment_id = str(_comment["id"])
            snapshot.comment_anchor = _anchor
            if payload.get("comments") != state.get("comments"):
                payload = {
                    **payload,
                    "version": int(state.get("version") or 0) + 1,
                    "modified": _store.date.today().isoformat(),
                }
                snapshot.expected_content = _plan_html.write_state(
                    snapshot.content.decode("utf-8"),
                    payload,
                    drop_authored_impl_meta=True,
                ).encode("utf-8")
    return _record_landing_comment(**kwargs)


def _commit_with_plan_snapshot(
    snapshot: _PlanWriteSnapshot | None, **kwargs: Any
) -> dict:
    if snapshot is None:
        return _commit_landing_writes(**kwargs)
    # The outer snapshot guard holds the plan lock through commit and rollback.
    current = snapshot.path.read_bytes()
    checkpoint = snapshot
    try:
        snapshot.require_owned_content(current)
    except CrewError:
        adopted = _adopt_a_plan_edited_after_the_snapshot(snapshot, current)
        if adopted is not None:
            checkpoint = adopted
        else:
            from bs4 import BeautifulSoup

            from reckon import _plan_html

            if snapshot.expected_content is None:
                raise
            intended = _plan_html.read_state(snapshot.expected_content.decode("utf-8"))
            actual = _plan_html.read_state(current.decode("utf-8"))
            merged = _store._comment_append_onto_current(actual, intended)
            held_comments = actual.get("comments") or {}
            kept = all(
                entry in held_comments.get(section, [])
                for section, entries in (intended.get("comments") or {}).items()
                for entry in entries
            )
            rendered = _plan_html.write_state(
                snapshot.expected_content.decode("utf-8"),
                actual,
                drop_authored_impl_meta=True,
            ).encode("utf-8")
            same_document = BeautifulSoup(
                rendered.decode("utf-8"), "html.parser"
            ) == BeautifulSoup(current.decode("utf-8"), "html.parser")
            if merged is None or not kept or not same_document:
                raise
            # A commutative peer append may land with this run. Its full bytes
            # and staging become the commit's rollback target, never the older
            # snapshot that predates the peer's comment.
            checkpoint = replace(
                snapshot,
                content=current,
                mode=snapshot.path.stat().st_mode,
                index=snapshot.index_entry(),
                expected_content=current,
                rollback_content=None,
                commit_attempted=False,
            )
    checkpoint.require_owned_index(current)
    checkpoint.commit_attempted = True
    try:
        return _commit_landing_writes(**kwargs)
    except BaseException as exc:
        rollback = getattr(exc, LANDING_ROLLBACK_ATTRIBUTE, {})
        if (
            checkpoint.head_content is None
            and isinstance(rollback, Mapping)
            and rollback.get(str(checkpoint.path)) is True
            and not checkpoint.path.exists()
        ):
            # The commit owner's receipt proves it removed this path while
            # restoring a plan absent from HEAD. Restore its saved preimage.
            checkpoint.rollback_removed = True
        elif (
            checkpoint.head_content is not None
            and checkpoint.path.is_file()
            and checkpoint.path.read_bytes() == checkpoint.head_content
        ):
            checkpoint.rollback_content = checkpoint.head_content
        if checkpoint is not snapshot:
            checkpoint.restore(plan_locked=True)
        raise


def _preserve_plan_on_refusal(promote: Callable[..., dict[str, Any]]) -> Callable:
    """Restore uncommitted plan writes on every exit that refuses a landing."""

    @wraps(promote)
    def guarded(run_id: str, **kwargs: Any) -> dict[str, Any]:
        record = kwargs.get("record")
        if record is None:
            record = read_pointer(run_id)
        record = {**record, **read_dispatch_detail(record)}
        root = kwargs.get("root")
        if root is None:
            root = record.get("repo")
        project = str(record.get("project") or "")
        plan = str((record.get("node") or {}).get("plan") or "")
        path = (
            _store._resolve_html_file(project, plan, root, artifact_type="plan")
            if root is not None and plan and not _is_shadow(record)
            else None
        )
        if path is None:
            return promote(run_id, **kwargs)
        checkout = Path(root).expanduser().resolve()
        path = path.resolve()
        with _store._serialized_path_lock(path, "plans"):
            head_entry = _git(checkout, "ls-tree", "-z", "HEAD", "--", str(path)).stdout
            snapshot = _PlanWriteSnapshot(
                checkout=checkout,
                path=path,
                content=path.read_bytes(),
                mode=path.stat().st_mode,
                index=_git(
                    checkout, "ls-files", "--stage", "-z", "--", str(path)
                ).stdout,
                head_entry=head_entry,
                run_id=run_id,
                head_content=(
                    _git(
                        checkout,
                        "show",
                        f"HEAD:{path.relative_to(checkout).as_posix()}",
                    ).stdout.encode("utf-8")
                    if head_entry
                    else None
                ),
            )
            kwargs["_plan_snapshot"] = snapshot
            try:
                return promote(run_id, **kwargs)
            except BaseException as exc:
                # A committed landing survives a cleanup failure. Rewinding its
                # plan here would add an index change reversing a landed commit.
                if snapshot.committed_entry() != snapshot.head_entry:
                    raise
                try:
                    snapshot.restore(plan_locked=True)
                except (CrewError, OSError) as restore_error:
                    message = f"{exc}; could not restore plan document and index entry {path}: {restore_error}"
                else:
                    message = f"{exc}; restored plan document, version and index entry {path} to their pre-promotion state"
                exc.args = (message,)
                raise

    return guarded


@operational_read
@_with_branch_write_scope
@_preserve_plan_on_refusal
def _complete_locked(
    run_id: str,
    *,
    record: Mapping[str, Any] | None = None,
    gate: str,
    failure_classification: str = "",
    commits: Iterable[str] = (),
    no_commit: str = "",
    no_commit_uncommitted: Iterable[str] = (),
    outcome: str = "",
    tests_added: int | None = None,
    scope_changed: bool = False,
    changed_lines: Mapping[str, Any] | None = None,
    completed_at: str = "",
    root: str | Path | None = None,
    gate_check: Mapping[str, Any] | None = None,
    require_gate_check: bool = False,
    suite_delta: Mapping[str, Any] | None = None,
    boundary_waiver: str = "",
    domain_waiver_record: Mapping[str, Any] | None = None,
    resume_remedy: Mapping[str, str] | None = None,
    resume_waived: Mapping[str, str] | None = None,
    reviewed: Mapping[str, Any] | None = None,
    review_waived: Mapping[str, str] | None = None,
    review_tier: str = "",
    negative_control_waiver: str | None = None,
    recoverable_session: Mapping[str, str] | None = None,
    discard_resume_worktree: bool = False,
    accepted_paths: Mapping[str, str] | None = None,
    commit_list_shortfall: Mapping[str, Any] | None = None,
    no_impl_change: str = "",
    live_run_waived: Mapping[str, str] | None = None,
    plan_link: str = "",
    unplanned_reason: str = "",
    landing: Mapping[str, Any] | None = None,
    promoted_by: str = "",
    ledger_reads: ExitStack | None = None,
    _plan_snapshot: _PlanWriteSnapshot | None = None,
) -> dict[str, Any]:
    """Promote a finished run into the owning repository's committed ledger.

    The plan comment and ledger append both happen before the pointer is
    deleted. The comment uses a stable run-derived id, so a retry after an
    interruption cannot duplicate the narrative.

    Worker-time spans the first and last timestamped stream events. The stream
    is read after a bounded settle, so the terminal record the worker's harness
    writes after the manifest is not missed. A live process is normally never
    waited on — but when promotion is about to end that process in its release
    step anyway, it ends it first and then settles the tail the shutdown writes,
    folding the run's own record instead of a file mtime; a promotion that will
    not end the process keeps the live-process behaviour. A healthy
    timestamp-less stream falls back to wall duration with an explicit source;
    a stalled run keeps that duration absent. Promotion time remains an
    explicit completion fallback when no stream survives.

    ``record`` is the run's own record, passed by :func:`complete` so a run
    rebuilt from its directory and one read from its live pointer reach the
    same body. A caller that omits it has the pointer read here, which keeps
    the direct callers that predate the reconcile path working unchanged.
    """
    if record is None:
        record = read_pointer(run_id)
    record = {**record, **read_dispatch_detail(record)}
    project = str(record.get("project") or "")
    node = record.get("node") or {}
    shadow = _is_shadow(record)
    ledger_root = root if root is not None else record.get("repo")
    checkout = (
        Path(ledger_root).expanduser().resolve() if ledger_root is not None else None
    )
    # Promotion commits the stores it writes, so a checkout that cannot host
    # that commit refuses before either store is written rather than leaving a
    # half-landed, uncommitted state behind.
    _require_committable_checkout(checkout, run_id)
    tree = _record_tree(record)
    # The comment captures pending plan text after acquiring the branch lease;
    # the same mapping then carries that projection through the commit.
    from reckon.crew.promotion_scope import _branch_plan_text_edits

    plan_text_edits = _branch_plan_text_edits()
    # Every ledger read between here and the append is served from one index
    # decode per read key: the landing checks and this record's own read
    # re-read the same ledger milliseconds apart, and each read decoded the
    # whole index afresh. The caller opens the region so its checks share it;
    # a direct caller gets one of its own. Exiting it here, before the append,
    # is what makes a read after the append decode afresh and see the row this
    # promotion has just written.
    reads = ledger_reads if ledger_reads is not None else ExitStack()
    if ledger_reads is None:
        reads.enter_context(ledger.index_reuse())
    with reads:
        ledger_data, ledger_version = ledger.load_open(project, root=ledger_root)
        damaged_record = ledger.run_path(project, run_id, ledger_root)
        if damaged_record in ledger.damaged_run_records(project):
            raise ledger.LedgerError(
                f"cannot promote run {run_id!r}: its record is damaged at {damaged_record}"
            )
        from reckon.crew.open_run_index import row_for_run

        existing = row_for_run(project, run_id, ledger_root)
        if existing is not None:
            existing_path = ledger.run_path(project, run_id, ledger_root)
            if not existing_path.is_file():
                existing_path = ledger.ledger_path(project, ledger_root)
            with _report_written_ledger_row(
                run_id,
                ledger_path=existing_path,
                row_present=lambda: _ledger_holds_row(project, ledger_root, run_id),
            ):
                comment = (
                    {"recorded": False, "reason": "shadow evidence does not land code"}
                    if shadow
                    else _record_comment_with_snapshot(
                        _plan_snapshot,
                        project=project,
                        plan=str(node.get("plan") or ""),
                        section=str(node.get("section") or ""),
                        run_id=run_id,
                        narrative=outcome,
                        author=_COORDINATOR_LANDING_AUTHOR,
                        when=str(existing.get("completed_at") or _utc_now()),
                        root=ledger_root,
                        worker_tree=tree,
                        worker_commits=_declared_manifest_commits(record),
                        landing=_declared_manifest_landing(record),
                    )
                )
                landing_check = _report_landing_check(
                    _commit_with_plan_snapshot(
                        _plan_snapshot,
                        plan_text_edits=plan_text_edits,
                        run_id=run_id,
                        verdict=str(gate).strip().lower(),
                        checkout=checkout,
                        paths=_plan_comment_store_path(
                            project=project,
                            plan=str(node.get("plan") or ""),
                            comment=comment,
                            root=ledger_root,
                            checkout=checkout,
                        ),
                    )
                )
                capture = capture_run_session(record)
                path = pointer_path(run_id)
                from reckon.crew.terminal_cleanup import queue_cleanup

                retention = existing.get("worktree_retention")
                queue_cleanup(
                    {**record, "worktree_retention": retention},
                    kind="promoted",
                    gate=str(gate),
                )
                from reckon.crew.pending_landing import _reviewed_head

                _remove_promoted_pointer_or_defer(
                    run_id,
                    record,
                    finishing_another_writers_row=True,
                    reviewed_head=_reviewed_head(existing),
                )
                # The release runs inside this run's promotion lock, so the
                # moved-aside copy is left to the trash sweep rather than
                # deleted here: a copy deleted inline keeps a serial review
                # sweep waiting on the filesystem.
                with deferring_worktree_copy_deletion():
                    release = _release_after_promotion(
                        run_id,
                        record,
                        retention if isinstance(retention, Mapping) else None,
                        gate=str(gate),
                    )
                release, recorded = _record_release_on_ledger(
                    project=project,
                    root=ledger_root,
                    run_id=run_id,
                    release=release,
                    checkout=checkout,
                )
                release.update(_retire_disposable_identity(record))
                if recorded is not None:
                    existing = recorded
                result = {
                    "run_id": run_id,
                    "project": project,
                    "ledger_path": str(existing_path),
                    "ledger_version": ledger_version,
                    "pointer_removed": not path.exists(),
                    "record": dict(existing),
                    "already_promoted": True,
                    "stacked_descendants": stacked_descendants_still_live(record),
                    "session_capture": capture,
                    "plan_comment": comment,
                    "release": release,
                    "landing_check": landing_check,
                }
                lane_receipt = existing.get("lane_receipt")
                if isinstance(lane_receipt, Mapping):
                    result["lane_receipt"] = dict(lane_receipt)
                # This is a bounded fleet reading, not a readiness recommendation: the
                # result states only what promotion observed, and the orchestrator owns
                # every decision about what to do next.
                result["fleet_state"] = _fleet_state_reading(project)
                return result

        # A writer still alive when a prompt promotion arrives is about to be ended
        # by this promotion's own release step; end it before the observation so the
        # bounded settle can fold the terminal record its shutdown writes. A run the
        # release would not signal — no terminal manifest, or a non-cli launch —
        # keeps the existing live-process behaviour, and nothing waits on a process
        # this promotion is not going to end.
        ended_writer = _end_live_writer_for_settle(record)
        stream = _promotion_terminal_observation(
            record, settle_even_if_alive=ended_writer
        )
        if completed_at:
            finished = _assume_utc_if_naive(completed_at)
            completion_source = "provided"
        elif stream.completed_at:
            finished = stream.completed_at
            completion_source = stream.completion_source or "terminal_event"
        else:
            finished = _utc_now()
            completion_source = "promotion_time"
        worktree_retention = _resume_worktree_retention(
            record,
            recoverable_session,
            retained_at=finished,
            discard=discard_resume_worktree,
            gate=str(gate),
        )
        if landing is None:
            landing = _landing_preconditions(
                run_id,
                record,
                checkout=checkout,
                ledger_root=ledger_root,
                commits=commits,
                gate=gate,
                failure_classification=failure_classification,
                no_impl_change=no_impl_change,
                plan_link=plan_link,
                unplanned_reason=unplanned_reason,
                boundary_waiver=boundary_waiver,
                negative_control_waiver=negative_control_waiver,
                accepted_paths=accepted_paths,
                gate_check=gate_check,
                require_gate_check=require_gate_check,
            )
        commit_list = list(landing["commits"])
        shadow_patch = str(landing["shadow_patch"] or "")
        changed_lines = landing["changed_lines"]
        scope_acceptances = list(landing["scope_acceptances"] or [])
        boundary_waived = landing["boundary_waived"]
        brief_owner = landing["brief_owner"]
        impl_move = landing["impl_move"]
        manifest_text = landing["manifest_text"]
        manifest = landing["manifest"]
        negative_control = landing["negative_control"]

        from reckon.crew.resumption import resolve_session

        session_id = resolve_session(
            run_id,
            record=record,
            project=str(record.get("project") or ""),
            root=record.get("repo"),
        )["session_id"]
        lane_receipt = _harvest_lane_receipt(
            record,
            session_id=session_id,
            observed_at=finished,
        )
        from reckon.crew.open_run_index import latest_session_row

        previous = latest_session_row(project, session_id, ledger_root)
        measured_budget = ledger.per_run_budget(stream.budget, previous)
        # The overrun the worker was messaged about rides the committed row, so the
        # live pointer's verdict is not the only place a run's overrun exists. The
        # pair is measured here with the same reader the watch uses, against the
        # same record, so the row's figure and the message's figure are one fact
        # read twice rather than two clocks.
        measured_budget.update(_budget_overrun_facts(record, finished=finished))
        wall_seconds = _elapsed_seconds(record.get("created_at"), finished)
        stalled = _wall_exceeded_budget(wall_seconds, node.get("time_budget"))
        worker_seconds = stream.worker_seconds
        if stream.worker_seconds_source is not None:
            # A run that made more than one attempt carries the composition its
            # worker span was summed from — the attempts' own event spans and their
            # clients' receipts — so the row names what was measured rather than the
            # single-mechanism word a one-attempt run keeps.
            worker_seconds_source = stream.worker_seconds_source
        elif stream.rollout_seconds is not None:
            # A codex receipt measures the model span the exec stream cannot: its
            # own wall span is authoritative over the stream's absence, on a normal
            # finish and on a stalled one alike, so it is read before the fallbacks.
            worker_seconds = stream.rollout_seconds
            worker_seconds_source = "rollout_receipt"
        elif worker_seconds is not None:
            worker_seconds_source = "stream_events"
        elif stream.completion_source == "stream_mtime" and stalled:
            worker_seconds_source = "stalled"
        elif stream.completion_source == "stream_mtime" and wall_seconds is not None:
            worker_seconds = wall_seconds
            worker_seconds_source = "wall_fallback"
        else:
            worker_seconds_source = "unavailable"

        # Routing evidence is read from the delivered manifest at promotion, so a
        # later measure can separate a followup touch from a defect on the row
        # alone; the run directory survives, pruned only later by crew gc. An
        # unreadable manifest
        # leaves outside-scope paths unmeasured (key absent) and the dispute count
        # "unknown" — a node never measured is not one that measured zero.
        outside_scope_entries = (
            None
            if manifest is None
            else list(manifest.get(OUTSIDE_SCOPE_MANIFEST_FIELD) or [])
        )
        outside_scope_paths = (
            None
            if outside_scope_entries is None
            else ledger.outside_scope_paths_from_entries(outside_scope_entries)
        )
        predecessor = ledger.predecessor_run_id(
            supplied=_coordinator_supplied_predecessor(record),
            base_sha=str(record.get("base_sha") or record.get("base") or ""),
            runs=ledger_data["runs"],
            exclude_run_id=run_id,
        )
        dispute_count = (
            "unknown"
            if manifest_text is None
            else ledger.stated_correction_count(manifest_text)
        )
        # An attached review is copied onto the committed row at promotion, so its
        # dimension scores and their total survive the loss of the crew
        # configuration home. The store keeps the verbatim text and findings; the
        # row keeps the compact block that joins to the run which earned it.
        # The cited gate log is copied into the row's own run directory before the
        # record is built, so the row names a path that outlives the worktree and the
        # reaper rather than one that was true only when it was written. The step is
        # best-effort: a log already in the run directory, a citation that does not
        # resolve on this machine, and a copy that cannot be read or written all
        # leave the check as given, because preservation must never decide the verdict.
        gate_check = _preserve_cited_gate_log(run_id, gate_check)
        # The worker's own exit record is copied onto the row before promotion
        # releases the live pointer and the worktree. The run directory survives
        # until crew gc prunes it, but the row must carry the record regardless: the
        # facts a census reads (the terminating signal and whether the exit landed
        # mid-work either way) then outlive the pointer. The row's ``exit_status`` is
        # left as the gate command's status; the worker's exit is a separate fact
        # under its own key, so a run with no exit record carries no ``worker_exit``
        # key rather than an empty one.
        worker_exit = _promoted_worker_exit(run_id)
        # The revision this promotion asserts landed, resolved while the run's tree
        # is still present. A shadow asserts no code, so it records none.
        promoted_revision = (
            "" if shadow else _run_promoted_revision(record, commit_list)
        )
        # The six-line clone detector reports each function this run added or
        # modified whose normalised window duplicates another function already in
        # reckon/ or tests/ at the promoted revision. It is a review signal: a
        # readable revision yields the list of matches (empty when none), and a
        # case no revision pair can be measured for records the unmeasured marker
        # rather than the empty list, so the two never read alike. Neither fails
        # the promotion.
        if shadow:
            clone_matches: Any = ledger.unmeasured_clone_report(
                "shadow run lands no code"
            )
        elif not (commit_list and promoted_revision):
            clone_matches = ledger.unmeasured_clone_report(
                "the run asserted no revision pair to compare"
            )
        else:
            measured = clones.promotion_clone_matches(
                tree,
                base_sha=str(record.get("base_sha") or record.get("base") or ""),
                tip=promoted_revision,
            )
            clone_matches = (
                measured
                if measured is not None
                else ledger.unmeasured_clone_report(
                    "the revision pair could not be read"
                )
            )
        # A brief run carries the digest and stored path of the brief it read in
        # place of a plan section, so a promoted row still points at the exact text
        # the worker saw. The block is present only for a brief run, which is what
        # makes the row's ``plan`` null rather than empty.
        brief_run = str(node.get("brief") or "").strip()
        brief_block = (
            {
                "sha256": str(node.get("brief_sha256") or ""),
                "path": str(node.get("brief_path") or ""),
            }
            if brief_run
            else None
        )
        # The crew session that dispatched this run rides the committed row beside
        # the run it belongs to, because this promotion deletes the live pointer
        # that carried it while ``session_id`` names the worker's own harness
        # session rather than the coordinator's. It is null rather than absent when
        # no pointer named one, because every key in ``RECORD_FIELDS`` is present on
        # every promoted row.
        pointer_session = str(record.get("session") or "").strip() or None
        # The disposition verb rides the committed row for the same reason: the
        # live pointer that carries it is deleted by this promotion, so a row that
        # does not read it here can never recover it. A run with no recorded
        # disposition records null, never an inferred verb.
        disposition = _recorded_disposition(record)
        # A review run's deliverable is the record it stored for the subject it
        # read, so landing the run lands the record with it — and every earlier
        # round of that subject too, so a review of another head is not left only in
        # the host staging store. The rounds are read here, before the ledger row is
        # assembled, so the round this run produced can name the row and every
        # round's committed write can join the row's commit below. A run that is not
        # a review, or a review run that delivered no readable round, leaves the
        # row's review block as the review gate resolved it.
        committed_review_payloads: list[dict[str, Any]] = []
        # A plan review's record reaches the plan-review store from the same call
        # that reads its rounds, so its outcome is read back after that call and
        # reported on the receipt beside the run store's own outcome. The two say
        # different things: ``store`` is the ledger's index append, and a plan
        # review that never reached the review store still shows it as written.
        plan_review_store: dict[str, Any] | None = None
        if not shadow:
            committed_review_payloads = _delivered_review_payloads_for_commit(
                record, project, run_id, root=ledger_root
            )
            plan_review_store = _plan_review_store_outcome(project, run_id, record)
        committed_review_payload = next(
            (
                payload
                for payload in committed_review_payloads
                if str(payload.get("review_run_id") or "").strip() == run_id
            ),
            None,
        )
        if committed_review_payload is None and committed_review_payloads:
            # A round whose own body carried no review run id is filed under this
            # run's id above, so it is normally the selected one; if none names the
            # row, the first round still stands in so the row's review block is not
            # left absent.
            committed_review_payload = committed_review_payloads[0]
        if committed_review_payload is not None:
            committed_block = review_module.ledger_block(committed_review_payload)
            if committed_block is not None:
                committed_block["id"] = run_id
                reviewed = committed_block
        run = ledger.build_record(
            run_id=run_id,
            project=project,
            plan=_run_plan_slug(record, node),
            section=str(node.get("section") or ""),
            brief=brief_block,
            plan_link=str((brief_owner or {}).get("plan_link") or ""),
            unplanned_reason=str((brief_owner or {}).get("unplanned_reason") or ""),
            node=str(node.get("id") or ""),
            node_definition=node,
            role=str(record.get("role") or ""),
            spec_level=str(node.get("spec_level") or ""),
            member_id=str(record.get("member") or ""),
            backend=str(
                record.get("backend")
                or (record.get("agent") or {}).get("backend")
                or ""
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
            outcome=outcome,
            disposition=disposition,
            manifest_path=str(record.get("manifest_path") or ""),
            scope_changed=scope_changed,
            session=pointer_session,
            session_id=session_id,
            session_harness=(record.get("session_harness") or record.get("dialect"))
            if session_id
            else None,
            session_model=(
                record.get("session_model") or (record.get("agent") or {}).get("model")
            )
            if session_id
            else None,
            budget=measured_budget,
            lane_receipt=lane_receipt,
            throughput=stream.throughput,
            budget_fallback=record.get("budget_fallback"),
            lineage=record.get("lineage"),
            shadow_patch=shadow_patch,
            # Read through the accessor: the pointer carries only a path to the
            # dispatch-time detail file, and a pointer written before the move
            # still carries the value inline.
            unreconciled_override=dispatch_field(record, "unreconciled_override"),
            orchestrator_lane_override=record.get("orchestrator_lane_override"),
            gate_check=gate_check,
            require_gate_check=require_gate_check,
            suite_delta=suite_delta,
            resume_remedy=resume_remedy,
            outside_scope_paths=outside_scope_paths,
            predecessor_run=predecessor,
            dispute_count=dispute_count,
            review=reviewed,
            clone_matches=clone_matches,
            promoted_by=promoted_by,
        )
        # The picker's answer rides the dispatch-time detail file once it has moved
        # off the pointer, so the row reads it through the accessor. An answer the
        # record carried inline is the same value through the same call.
        selection = dispatch_field(record, "picker_selection")
        if selection is not None:
            run["picker_selection"] = (
                dict(selection) if isinstance(selection, Mapping) else selection
            )
        # A hold and the dispatch that went past it are both known only here: the
        # advisory pick lands after dispatch returns, so the run's route mode was
        # recorded before the answer existed. A row carrying a holding answer is
        # therefore one the operator overruled, and it is named with its own value
        # rather than sharing the shadow picks, so a reader can count how often a
        # hold is overruled.
        if overridden_hold(run.get("picker_selection")):
            run["route_mode"] = OVERRIDDEN_HOLD_ROUTE_MODE
        run["attempt"] = int(record.get("attempt") or 1)
        run["attempt_kind"] = str(record.get("attempt_kind") or "dispatch")
        # The worker's exit record rides the row verbatim, and the key is written
        # only when the run directory held one: a present-but-empty key would read
        # as a supervisor that ran and recorded nothing.
        if worker_exit is not None:
            run["worker_exit"] = worker_exit
        # The revision the promotion asserts landed rides the row so a later sweep
        # can ask about it without the worktree, which promotion is about to
        # release. A run that asserted no code leaves the key absent rather than
        # recording a base it never touched, so an absent key never reads as a
        # promotion whose work was verified as landed.
        if promoted_revision:
            run["promoted_revision"] = promoted_revision
        # The job a placed launch was charged to rides the committed row beside the
        # run it belongs to, because the ledger row is the durable record a later
        # attribution reads; the live pointer it was first written on is removed by
        # this promotion. A run that declared no placement records the key absent
        # rather than zero, so an unplaced run is distinguishable from a placed one
        # whose scheduler never answered.
        job_id = record.get("job_id")
        if job_id is not None:
            run["job_id"] = str(job_id)
        # A deliberate commitless promotion survives on the record with its reason,
        # so a later reader can tell it from one that recorded nothing by accident.
        if str(no_commit).strip():
            run["no_commit"] = str(no_commit).strip()
        # A deliberate override of the worktree-evidence guard records the paths it
        # covered, so the ledger says which uncommitted repository work was declined
        # a commit beside the reason given, rather than only that a commit was
        # declined. No manifest field can put a path here.
        uncommitted = sorted(
            {str(path).strip() for path in no_commit_uncommitted if str(path).strip()}
        )
        if uncommitted:
            run["no_commit_uncommitted_paths"] = uncommitted
        # The impl comparison and its outcome ride the row, so a later audit can
        # separate a plan that moved from one promoted against a recorded waiver.
        if impl_move.get("at_dispatch") is not None:
            run["plan_impl_at_dispatch"] = impl_move["at_dispatch"]
        run["impl_move"] = dict(impl_move)
        # The negative-control verdict rides the row, so an audit can separate a
        # red log that was delivered and matched from a declaration of none that was
        # recorded with its reason rather than refused.
        run["negative_control"] = dict(negative_control)
        # A presented-list shortfall survives on the record so a reader of the
        # ledger sees the boundary check may have been under-scoped, not only the
        # coordinator that was looking at the immediate report.
        if commit_list_shortfall:
            run["commit_list_shortfall"] = dict(commit_list_shortfall)
        if boundary_waived is not None:
            run["boundary_waiver"] = boundary_waived
        # A waived declared invariant rides the row the way the other waivers do.
        promotion_waivers.record_domain_waiver(run, domain_waiver_record)
        if scope_acceptances:
            run["scope_acceptances"] = scope_acceptances
        # A discarded resume path survives on the row with the session it discarded
        # and the reason given, so an audit can tell a deliberate discard from the
        # accidental one this refusal exists to prevent.
        if resume_waived is not None:
            run["resume_waiver"] = dict(resume_waived)
            if discard_resume_worktree:
                run["resume_waiver"]["worktree_discarded"] = True
        if review_waived is not None:
            run["review_waiver"] = dict(review_waived)
        # The tier rides the row on every promotion. For a run that changed no
        # runtime source it is the recorded reason no review exists — the merged-head
        # gate re-run and the node's negative control are what checked it instead —
        # so a later reader can tell a deliberate no-review tier from a review that
        # was simply never produced.
        if review_tier:
            run["review_tier"] = review_tier
        # A run promoted while its own worker was still alive survives on the row
        # with the reason given, so an audit can tell a deliberate promotion of a
        # live worker from the accidental orphan this refusal exists to prevent.
        if live_run_waived is not None:
            run["live_run_waiver"] = dict(live_run_waived)
        if worktree_retention is not None:
            run["worktree_retention"] = dict(worktree_retention)
        watch_override = record.get("watch_override")
        if isinstance(watch_override, Mapping):
            run["watch_override"] = dict(watch_override)
        execution_fit = record.get("execution_fit")
        if isinstance(execution_fit, Mapping):
            run["execution_fit"] = dict(execution_fit)
        # A run launched without the fence carries the reason it was waived onto the
        # committed row, so the ledger says which runs ran unprotected and why. The
        # pointer holds it only until promotion deletes it, and a reader asking why
        # a worker could write outside its grant needs it in the durable row. An
        # ordinary fenced dispatch records no such key.
        fence_waiver = record.get("fence_waiver")
        if isinstance(fence_waiver, Mapping):
            run["fence_waiver"] = dict(fence_waiver)
        # The defaults a layer left writable ride the row beside the waiver, for the
        # same reason: the pointer that held them is deleted at promotion, and a
        # reader asking whether a run could write outside its fence's own grant needs
        # them in the durable row. A run whose fence removed nothing records no such
        # key, so the row never carries an empty list that would read as a fence
        # built and found whole.
        fence_unprotected = record.get("fence_unprotected_paths")
        if fence_unprotected:
            run["fence_unprotected_paths"] = [str(path) for path in fence_unprotected]
        # The advisory a dispatch computed rides the committed row the same way it
        # rode the live pointer, together with the lane declaration and reading it
        # was derived from. Promotion deletes that pointer, so an advisory reaching
        # only the pointer is readable exactly until the run becomes evidence, and
        # the row is where a later reader asks whether the advice was taken --
        # ``backend`` already names the lane that was chosen, so the pair on the row
        # is what tells taking the advice apart from declining it. Each key is
        # written only when the pointer carried it: a row holding a null advisory
        # would read as a lane that was assessed and found quiet, which is the
        # opposite of a run whose dispatch never emitted one.
        for lane_key in ("lane_advisory", "lane_declaration", "lane_reading"):
            lane_value = record.get(lane_key)
            if lane_value is None:
                continue
            run[lane_key] = (
                dict(lane_value) if isinstance(lane_value, Mapping) else lane_value
            )
        # A shadow whose stream read its primary's landed answer is void as
        # calibration evidence; the stream is still on disk at this point (the run
        # directory survives until crew gc) so promotion scans it rather than
        # trusting a worker's self-report. Contamination recreates the primary's
        # answer from the object store, never from the shadow's own patch.
        if shadow:
            run.update(
                _shadow_control_marks(
                    record, ledger_data["runs"], tree, run.get("lineage")
                )
            )
        # A terminal run's stream is immutable, so its two figures are computed once
        # here, from the run's own stream, and recorded on the row; a later derive
        # then reads the ledger instead of reopening the stream. A stream that is
        # already gone records each figure as absent rather than as zero, so a
        # missing measurement never reads as a free run.
        run.update(capabilities.derive_run_figures(run))
        # The landing comment is written after all plan-facing operations: the record
        # is assembled, and every refusal it raises is raised, before anything is
        # written to the plan. A promotion that refuses after writing the comment
        # leaves the plan mutated for the caller that retries it, and the first
        # attempt's narrative pins the outcome every later attempt must repeat
        # byte-identical to avoid the narrative-conflict refusal. None of the
        # checks above needs the comment to exist, so none of them follows it.
        comment = (
            {"recorded": False, "reason": "shadow evidence does not land code"}
            if shadow
            else {
                "recorded": False,
                "reason": (
                    "a measurement run builds and gates and lands nothing, so it "
                    "records no landing comment on the plan"
                ),
            }
            if _measurement_reason(record)
            else _record_comment_with_snapshot(
                _plan_snapshot,
                project=project,
                plan=str(node.get("plan") or ""),
                section=str(node.get("section") or ""),
                run_id=run_id,
                narrative=outcome,
                author=_COORDINATOR_LANDING_AUTHOR,
                when=finished,
                root=ledger_root,
                worker_tree=tree,
                worker_commits=commit_list or _declared_manifest_commits(record),
                landing=_declared_manifest_landing(record),
            )
        )
        # The narrative the comment recorded lives in the plan, so the row carries
        # its outcome empty rather than twice.
        if comment.get("recorded"):
            run["outcome"] = ""
        already_promoted = False
    # The ledger state the run-time profile fold may extend, read before the
    # append moves it. A fold inside this promotion needs it to prove the
    # samples a previous refresh stored are this history minus the row about to
    # be written, so it can extend them without reading the ledger.
    profile_anchor = _profile_fold_anchor(project, ledger_root)
    try:
        from reckon.crew.promotion_scope import _acquire_branch_write

        _acquire_branch_write(checkout)
        written = ledger.append_run(project, run, root=ledger_root)
    except ledger.LedgerError as exc:
        # Another completion can land after the read above. Treat only an
        # observed matching record as success; every other ledger error is
        # still a refusal. A failed lookup must also restore the plan write:
        # a store that cannot accept a row may be unable to read it back.
        existing = None
        try:
            existing = row_for_run(project, run_id, ledger_root)
        finally:
            if existing is None:
                # The outer snapshot checks ownership under the plan writer's
                # lock before restoring; returning this path to HEAD here
                # would erase an editor's uncommitted write before that check.
                rollback = {}
                _record_landing_refusal(
                    run_id,
                    step="run-store",
                    message=str(exc),
                    checkout=checkout,
                    rollback=rollback,
                )
                if not all(rollback.values()):
                    raise ledger.LedgerError(
                        f"{exc}{_unrestored_landing_writes(rollback)}"
                    ) from exc
        if existing is None:
            raise
        already_promoted = True
        existing_path = ledger.run_path(project, run_id, ledger_root)
        if not existing_path.is_file():
            existing_path = ledger.ledger_path(project, ledger_root)
        written = {
            "path": str(existing_path),
            "version": ledger_version,
            "run": dict(existing),
        }
    # The promotion that moves a project's ledger is the one moment its profile
    # content changes, so it publishes the run-time profile the picks will later
    # serve instead of letting a pick rebuild it. It hands the refresh the row it
    # just wrote beside the ledger state it saw before writing it, so the fold
    # extends the stored samples without decoding the ledger index or reading
    # run documents; a promotion's cost therefore does not grow with the ledger.
    # This never fails the promotion: the profile is an operational reading, and
    # a cache write that fails leaves the previous reading in place rather than
    # undoing a landed run.
    profile_refresh = None
    try:
        from reckon.crew.picker import lane_context as _lane_context

        profile_refresh = _lane_context.refresh_run_time_profile(
            project, appended=(run,), anchor=profile_anchor
        )
    except Exception:  # noqa: BLE001, S110 - advisory profiling cannot prevent a landing
        pass
    committed_review_paths: list[Path] = []
    # The run store is outside the checkout. Only file-backed appends supply
    # a ledger path that belongs in a landing commit or its rollback.
    ledger_commit_paths = [
        Path(path) for path in written.get("commit_paths", [written["path"]])
    ]
    with _report_written_ledger_row(
        run_id,
        ledger_path=written["path"],
        row_present=lambda: _ledger_holds_row(project, ledger_root, run_id),
    ):
        # The shadow store outcome rides on the ordinary payload, not a flag or a
        # log stream: a promotion that otherwise succeeded is the exact consumer
        # that must see a silently failing shadow. When this call did not perform
        # the append, the outcome is the one already recorded on the committed row.
        store_outcome = written.get("store")
        if store_outcome is None:
            recorded = written["run"].get("store_write")
            store_outcome = dict(recorded) if isinstance(recorded, Mapping) else None
        store_row_written = (
            not already_promoted
            and isinstance(store_outcome, Mapping)
            and store_outcome.get("status") == "written"
        )
        # A promoting review run's stored rounds are written into the committed
        # reviews tree now, before the landing commit: the run's own per-run file
        # was just written, so it supplies the records' dispatch and completion
        # stamps, and the records then join the ledger row's commit rather than
        # adding one. Every round of the subject is written, not only the round
        # this run selected. The store refuses a record it cannot key or time,
        # and that refusal takes back every record already written this attempt
        # and the row it appended, so neither a record nor the ledger row is
        # committed and the retry re-promotes cleanly.
        if committed_review_payloads and not already_promoted:
            try:
                committed_review_paths.extend(
                    review_module.store_committed_review(
                        payload, project=project, root=ledger_root
                    )
                    for payload in committed_review_payloads
                )
            except ValueError as exc:
                rollback = _restore_landing_writes(
                    checkout, [*ledger_commit_paths, *committed_review_paths]
                )
                if store_row_written:
                    _discard_run_store_row(run_id)
                _record_landing_refusal(
                    run_id,
                    step="review-record",
                    message=str(exc),
                    checkout=checkout,
                    rollback=rollback,
                )
                raise _landing_refusal(
                    f"cannot store the review record for run {run_id!r} in the "
                    f"committed reviews tree, so the landing was rolled back: {exc}",
                    rollback,
                ) from exc

        # The two tracked stores this promotion wrote (the ledger row and, when a
        # narrative landed, the plan comment) are committed as one landing, so the
        # checkout carries no uncommitted state the next reader would trip on. A
        # refused landing then takes back every write it made, including the store
        # row this attempt's own append inserted, so a retry promotes cleanly.
        # A stop signal takes effect before this stage starts, so the retry the
        # next promotion runs sees the run exactly as it was before the commit.
        _stop_at_stage_boundary("the promotion commit")
        landing_check = _report_landing_check(
            _commit_with_plan_snapshot(
                _plan_snapshot,
                plan_text_edits=plan_text_edits,
                run_id=run_id,
                verdict=str(gate).strip().lower(),
                checkout=checkout,
                paths=[
                    *ledger_commit_paths,
                    *_plan_comment_store_path(
                        project=project,
                        plan=str(node.get("plan") or ""),
                        comment=comment,
                        root=ledger_root,
                        checkout=checkout,
                    ),
                    *committed_review_paths,
                ],
                store_row_written=store_row_written,
            )
        )

        # Return capture metadata while the pointer still exists. Session
        # ownership has already been copied onto the committed ledger row.
        _stop_at_stage_boundary("the release")
        capture = capture_run_session(record)
        from reckon.crew.terminal_cleanup import queue_cleanup

        queue_cleanup(
            {**record, "worktree_retention": worktree_retention},
            kind="promoted",
            gate=str(gate),
        )
        _remove_promoted_pointer_or_defer(
            run_id,
            record,
            finishing_another_writers_row=already_promoted,
            reviewed_head=promoted_revision,
        )
        # The release runs inside this run's promotion lock, so the moved-aside
        # copy is left to the trash sweep rather than deleted here: a copy
        # deleted inline keeps a serial review sweep waiting on the filesystem.
        with deferring_worktree_copy_deletion():
            measurement_worktree = _release_measurement_worktree(record)
            release = _release_after_promotion(
                run_id,
                record,
                worktree_retention,
                process_already_ended=ended_writer,
                gate=str(gate),
            )
        if measurement_worktree:
            release["measurement_worktree"] = measurement_worktree
        _stop_at_stage_boundary("the release record")
        release, recorded = _record_release_on_ledger(
            project=project,
            root=ledger_root,
            run_id=run_id,
            release=release,
            checkout=checkout,
        )
        release.update(_retire_disposable_identity(record))
        if recorded is not None:
            written["run"] = recorded
        # This is a bounded fleet reading, not a readiness recommendation: the
        # result states only what promotion observed, and the orchestrator owns
        # every decision about what to do next.
        receipt = {
            "run_id": run_id,
            "project": project,
            "ledger_path": written["path"],
            "ledger_version": written["version"],
            "pointer_removed": not pointer_path(run_id).exists(),
            "record": written["run"],
            "lane_receipt": dict(written["run"]["lane_receipt"]),
            "fleet_state": _fleet_state_reading(project),
            "already_promoted": already_promoted,
            "stacked_descendants": stacked_descendants_still_live(record),
            "session_capture": capture,
            "plan_comment": comment,
            "release": release,
            "store": store_outcome,
            "impl_move": dict(impl_move),
            "negative_control": dict(negative_control),
            "landing_check": landing_check,
            # Work the worker found beyond its fence rides the report so the
            # dispatching coordinator answers each entry — as a plan section or
            # as an instruction in the next dispatch's prompt — rather than
            # leaving it in a bucket no roadmap reads.
            "outside_scope": outside_scope_entries,
        }
        # A review dimension below its declared floor is work the reflex would
        # otherwise repair, and no floor repair round starts for a run once its
        # promotion has begun. Naming the dimension the promotion lands over is
        # what keeps that work visible instead of leaving it to reappear as a
        # worktree of commits no landing path reads.
        owed_repair = owed_floor_repair(run_id, record, reviewed)
        if owed_repair:
            receipt["owed_floor_repair"] = owed_repair
        # A plan review's receipt names what the plan-review store did with the
        # review it delivered. A run that is not a plan review carries no such
        # field, so the field's absence itself states the run had no review to
        # store.
        if (
            isinstance(profile_refresh, Mapping)
            and profile_refresh.get("status") == "unavailable"
        ):
            receipt["run_time_profile"] = dict(profile_refresh)
        if plan_review_store is not None:
            receipt["plan_review_store"] = plan_review_store
        return receipt


DISCARD_RECORD_NAME = "discard.json"

# The admission record a queued pointer carries: what dispatch wrote when it
# held the request (its queue time, session, lane and the allowance it was
# queued under, with the nested gate reading) and what the admission pass's
# ``_stamp_failure`` writes when a launch was attempted and refused. A request
# is usually discarded because it waited too long, so these are the fields a
# reader needs to see why — and they die with the pointer unless the discard
# keeps them.
DISCARD_ADMISSION_KEYS = (
    "queued_at",
    "session",
    "backend",
    "reason",
    "hold",
    "failure",
    "failure_at",
    "failure_attempts",
    "retry_at",
    "failure_guard",
    "failure_reported_at",
)


def discard_record_path(run_id: str) -> Path:
    """Path of the marker a discard leaves in one run's directory."""
    return run_dir(run_id) / DISCARD_RECORD_NAME


def discard_spool_dir() -> Path:
    """Directory holding a discard's record for a run with no run directory.

    A queued request that never launched has no run directory, and a discard
    must not create one — the run directory is the run's own home, and a write
    that recreated it would bring a discarded run back into existence. So a
    discard of such a request records its departure here instead, keyed by run
    id, where a restore and a later reader find it without a home the run never
    had.
    """
    return crew_home() / "discards"


def discard_spool_path(run_id: str) -> Path:
    """Path of the discard marker for a run whose directory may not exist."""
    return discard_spool_dir() / f"{run_id}.json"


def preserve_discard_record(run_id: str) -> str | None:
    """Copy a run directory's discard marker into the shared spool.

    A discard records its departure in the run's own directory when there is
    one, but the run directory is not always a run's: an admission pass hands a
    queued request's directory to the shared launch path, whose refusal rollback
    removes it. The marker written there goes with it, the restore then reads no
    discard, and the request an operator discarded is written back queued. So
    the pass copies the marker into the shared spool before the directory is
    handed over, where a reader finds it after the directory is gone.

    Nothing is copied when no marker is present — a request nobody discarded has
    no departure to preserve — and an existing spool marker is left as it is,
    because the spool record is the one written when the run had no directory
    and is the older account of the same departure.
    """
    marker = discard_record_path(run_id)
    spool = discard_spool_path(run_id)
    try:
        if spool.is_file() or not marker.is_file():
            return None
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    _write_json(spool, payload)
    return str(spool)


def discard_recorded(run_id: str) -> bool:
    """Whether a deliberate discard has been recorded for this run.

    A discard records its departure in the run's own directory when there is
    one, and in :func:`discard_spool_path` when there is not, so both places are
    read here. An unreadable marker answers ``False``: a departure the fleet
    cannot corroborate is read as one nobody recorded.
    """
    for path in (discard_record_path(run_id), discard_spool_path(run_id)):
        try:
            if path.is_file():
                return True
        except OSError:
            continue
    return False


def discard(run_id: str, *, allowed_phases: Sequence[str] = ()) -> dict[str, Any]:
    """Remove a stopped or abandoned pointer without promoting it.

    The departure is recorded before the pointer goes, so a reader sees a
    discard rather than the same absence a reaped or hand-removed pointer
    produces. The run directory is the run's own home and is never created here
    — a write that recreated it would bring a discarded run back into existence
    — so a request that never launched, and so has no run directory, records its
    departure in the shared discards spool instead, keyed by run id. A reader
    asks :func:`discard_recorded`, which reads both places.

    The run's own worktree is released through the same audit promotion applies
    on release. A discarded run whose tree is clean and whose head adds no
    commit the integration branch lacks holds nothing to preserve, and leaving
    it on disk refuses a redispatch of the same node because the worktree path
    already exists. A dirty or unintegrated tree is withheld with the audit's
    own reason. Runs that were admitted onto this one's chain tip are named in
    the result rather than refused: their worktrees carry this run's commits, so
    the departure leaves them holding the work, and the coordinator reading the
    result is told which runs those are.

    The supplied live-run states narrow the removal, tested against the record
    re-read under the lock, so a caller whose own pre-check raced a test sees a
    launch as the supplied states name it rather than removing a run that has
    started.

    A run whose live pointer names a live process is refused, as is one whose
    attempt names a live supervisor: a worker that is running is what must not
    be abandoned. A launch still composing has recorded only its own door, and
    refusing the discard there left the operator unable to cancel a request
    they can see moving while the pass restored the request the discard had
    removed. Every launch path reads this record at its last step before it
    starts a worker and refuses itself, so the discard lands and the launch
    eventually stops.
    """
    with _promotion_lock(run_id), _pointer_lock(run_id):
        record = read_pointer(run_id)
        if allowed_phases:
            phase = str(record.get("phase") or "")
            if phase not in allowed_phases:
                raise CrewError(
                    f"cannot discard {run_id!r}: it is in phase "
                    f"{phase or 'unknown'} which is not one of "
                    f"{', '.join(allowed_phases)}"
                )
        pid = record.get("pid")
        if record_process_alive(record, process_alive) is True:
            raise CrewError(
                f"cannot discard live run {run_id!r}: recorded pid {pid} is alive"
            )
        # A worker already exists beside a reservation that names its supervisor,
        # so the run holds work still being done. The pointer's pid is the
        # pointer's account of that; a launch composing in another process has
        # recorded only its own pid, which is the door, not a worker. Such a
        # launch started nothing and every launch path re-reads the discard
        # record at its last step before a worker exists and refuses itself, so
        # only a reservation naming a supervisor refuses here.
        from reckon.crew.dispatch_launch import (
            _reservation_refusal,
            held_launch_reservation,
        )

        held = held_launch_reservation(run_dir(run_id))
        if held is not None and held.get("supervisor_pid") is not None:
            raise _reservation_refusal(run_id, held)
        path = pointer_path(run_id)
        # Before the pointer goes: a reader that sees the absence must find the
        # record behind it, and a record written afterwards leaves a window in
        # which the departure is indistinguishable from a vanished pointer.
        written = _write_discard_record(run_id, record)
        from reckon.crew.terminal_cleanup import queue_cleanup

        queue_cleanup(record, kind="discarded")
        path.unlink()
        release = _remove_discarded_worktree(record)
        if written:
            from reckon.crew.runs import resolve_run_path

            written = str(resolve_run_path(written))
        return {
            "run_id": run_id,
            "pointer_path": str(path),
            "pointer_removed": not path.exists(),
            "removed": record,
            "discard_record": written,
            "stacked_descendants": stacked_descendants_still_live(record),
            # The requests waiting on this run wait on something that will now
            # never deliver, so they are released here: the release reads the
            # discard record just written and blocks them, instead of leaving
            # them waiting until the next admission pass reports them stuck.
            "released": _release_waiters_of_a_discard(run_id),
            # A dispatch that took a claim and was cut off before it spawned
            # leaves the claim behind, and it holds the node against every
            # later dispatch for as long as its holder lives. The run this
            # discard removed is the claim's own, so the claim goes with it.
            "dispatch_claim_released": _release_discarded_dispatch_claim(run_id),
            **release,
        }


def _release_discarded_dispatch_claim(run_id: str) -> list[str]:
    """Unlink the node dispatch claim that names the run a discard removed.

    A deliberate discard is not a stale claim, so the claim is given up through
    its own release path rather than displaced to a ``.reclaimed-`` sibling.
    Only a claim recording this run as its holder is released; a claim naming
    another run guards a dispatch this discard never touched and is left
    standing. The claims are identified by the run they record, not by the node
    the discarded pointer names, so a claim another run has since taken over
    that node is not mistaken for this run's.
    """
    from reckon.crew import dispatch_claims

    released: list[str] = []
    claims_dir = crew_home() / dispatch_claims.NODE_DISPATCH_CLAIM_DIRECTORY
    if not claims_dir.is_dir():
        return released
    for path in sorted(claims_dir.glob("*.json")):
        holder = dispatch_claims._read_node_dispatch_claim(path)
        if str(holder.get("run_id") or "") != run_id:
            continue
        dispatch_claims._NodeDispatchClaim(path, run_id).release()
        released.append(str(path))
    return released


def _release_waiters_of_a_discard(run_id: str) -> dict[str, Any]:
    """Release the requests waiting on a run this discard has just removed.

    The departure is recorded before this runs, so the refs a request holds
    against the run resolve to the failure the discard is. A release that
    raises is reported here and does not undo the discard, which has already
    happened.
    """
    from reckon.crew import queue_admission

    try:
        return queue_admission._release_waiters(
            [f"{queue_admission.REQUEST_REF_PREFIX}{run_id}"]
        )
    except Exception as exc:  # noqa: BLE001 - the discard has already happened
        return {
            "candidates": [],
            "promoted": [],
            "blocked": [],
            "failed": {},
            "error": f"releasing the waiters of {run_id!r} raised: {exc}",
        }


def _remove_discarded_worktree(record: Mapping[str, Any]) -> dict[str, Any]:
    """Release a discarded run's worktree through the release audit.

    Reuses ``_release_run_workspace`` — the same classification and withheld
    reasons ``crew complete`` applies on release — so a discard and a promotion
    cannot disagree about which trees are safe to remove. Worktree and scratch
    fields are surfaced; process fields cannot matter here, because discard has
    already refused a run whose recorded process is alive.
    """
    try:
        release = _release_run_workspace(record)
    except Exception as exc:  # noqa: BLE001 - the pointer is already gone
        fallback = {
            "worktree_released": False,
            "worktree_withheld": (
                f"run {record.get('run_id')!r} release step raised: {exc}"
            ),
        }
        fallback.update(_release_scratch_when_release_raised(record))
        return fallback
    return {
        key: release[key]
        for key in (
            "worktree_released",
            "worktree_withheld",
            "worktree_audit",
            "scratch_removed",
            "scratch_path",
            "scratch_withheld",
            "scratch_bytes",
            "scratch_removed_paths",
            "scratch_warning",
            "arms_removed_paths",
            "arms_kept",
            "arms_logs_copied",
            "arms_withheld",
            "run_archived",
            "run_archive_withheld",
        )
        if key in release
    }


def _write_discard_record(run_id: str, record: Mapping[str, Any]) -> str | None:
    """Record a deliberate discard where a reader will find it.

    The record lands in the run's own directory when one exists — the run's
    home, which a discard never creates. A request that never launched has no
    such directory, so its departure is recorded in the shared discards
    directory keyed by run id instead: the marker must outlive the pointer for a
    restore to read, whether or not a run directory was ever made.

    The pointer's admission record — the queue time, session, lane and hold
    dispatch wrote, and the failure fields an attempted launch stamped — is
    copied onto the record wherever it lands. A queued request is normally
    discarded because it waited too long, and these fields are the only account
    of why.
    """
    path = discard_record_path(run_id)
    if not path.parent.is_dir():
        path = discard_spool_path(run_id)
    written = {
        "run_id": run_id,
        "discarded_at": datetime.now(UTC).isoformat(),
        "phase": record.get("phase"),
        "node": record.get("node"),
        "pointer_path": str(pointer_path(run_id)),
    }
    # The admission record travels with the departure, so a request discarded
    # for waiting too long can still be read for why. A key the pointer never
    # carried is left out rather than written as null: an absent field and a
    # field that was never set are different facts, and a null would erase the
    # difference.
    for key in DISCARD_ADMISSION_KEYS:
        if key in record:
            written[key] = record[key]
    _write_json(path, written)
    return str(path)


def sweep_promoted_revisions(
    project: str,
    *,
    target_head: str = "HEAD",
    markers: Iterable[Mapping[str, Any]] = (),
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Report every promoted run whose asserted work is not in the target head.

    A promotion records the revision it asserts landed; nothing at promotion
    time can know whether the merge carrying it into the integration branch
    happens later, or happens and then drops the content. This sweep answers
    both questions per promoted run:

    * is the recorded revision an ancestor of ``target_head``; and
    * does a marker the change introduced survive in ``target_head`` — or, for a
      change whose purpose was a removal, is the removed marker still gone?

    ``markers`` carries what only a caller can know about a change: the literal
    that proves the content landed and whether its passing answer is presence or
    absence. Each entry is a mapping with ``run_id``, ``marker``, an optional
    ``path`` to search, and ``expect`` of ``"present"`` (the default) or
    ``"absent"``.

    Neither half subsumes the other. A run that was never merged fails ancestry
    and, in the ordinary case, the marker too. A run whose work was merged and
    then reverted — a merge conclusion committing an index that dropped it —
    passes ancestry and fails only the marker. A run that removed something
    passes both unless the marker's absence is required, which is why a removal
    assertion exists at all: presence and absence are not one measurement.
    Nothing is reported for a run whose work landed intact, because a check that
    fires on the healthy case is one nobody keeps.

    The sweep reads the ledger and the branch and writes neither: a promotion
    does not merge, and this is a later read, not a read side of landing.
    """
    checkout = (
        resolve_project_repository(project, root, flag="root")
        if root is not None
        else project_mount_repository(project)
    )
    if checkout is None:
        raise CrewError(
            f"the promoted-revision sweep for {project!r} has no repository to "
            "read: pass root=<checkout> or register the project's mount"
        )
    target = _commit_canonical_id(checkout, target_head)
    if target is None:
        raise CrewError(
            f"target head {target_head!r} does not resolve to a commit in "
            f"{checkout}, so no promotion can be compared against it"
        )
    wanted: dict[str, list[dict[str, str]]] = {}
    for entry in markers:
        run_id = str(entry.get("run_id") or "").strip()
        marker = str(entry.get("marker") or "")
        if not run_id or not marker:
            raise CrewError(
                "a sweep marker needs both a run_id and a marker; one without "
                "either names nothing to search for or nothing to search"
            )
        expect = str(entry.get("expect") or "present").strip().lower()
        if expect not in ("present", "absent"):
            raise CrewError(
                f"sweep marker for {run_id!r} has expect={expect!r}; it must be "
                "'present' or 'absent'"
            )
        wanted.setdefault(run_id, []).append(
            {"marker": marker, "path": str(entry.get("path") or ""), "expect": expect}
        )
    records = ledger.runs(project, root=root)
    findings: list[dict[str, Any]] = []
    matched: set[str] = set()
    checked = 0
    for record in records:
        revision = str(record.get("promoted_revision") or "").strip()
        if not revision:
            # Rows promoted before the revision was recorded, and runs that
            # asserted no code, carry no claim to test. An absent key is not a
            # passing revision; it is a row this sweep cannot speak for.
            continue
        checked += 1
        run_id = str(record.get("run_id") or "")
        finding: dict[str, Any] = {
            "run_id": run_id,
            "node": str(record.get("node") or ""),
            "promoted_revision": revision,
            "reasons": [],
            "markers": [],
        }
        if not _commit_resolves_in(checkout, revision):
            finding["reasons"].append("unresolvable-revision")
        elif not _revision_is_ancestor(checkout, revision, target):
            finding["reasons"].append("not-an-ancestor")
        for spec in wanted.get(run_id, ()):
            matched.add(run_id)
            present = _marker_present_in(
                checkout,
                target,
                marker=spec["marker"],
                path=spec["path"],
            )
            finding["markers"].append(
                {
                    "marker": spec["marker"],
                    "path": spec["path"],
                    "expect": spec["expect"],
                    "present": present,
                }
            )
            if spec["expect"] == "present" and not present:
                finding["reasons"].append("marker-absent")
            elif spec["expect"] == "absent" and present:
                finding["reasons"].append("removed-marker-still-present")
        if finding["reasons"]:
            findings.append(finding)
    unresolved = sorted(run_id for run_id in wanted if run_id not in matched)
    return {
        "project": project,
        "checkout": str(checkout),
        "target_head": target,
        "checked": checked,
        "findings": findings,
        "unresolved_markers": unresolved,
    }


def _marker_present_in(
    checkout: Path, target: str, *, marker: str, path: str = ""
) -> bool:
    """Report whether ``marker`` occurs in ``target``'s tree, optionally at ``path``.

    The search is fixed-string and anchored to the target revision, so a marker
    that survives only in a worktree file or in another branch is not counted. A
    git failure raises rather than answering absent: an unreadable tree and a
    genuinely absent marker otherwise return the same empty result.
    """
    argv = ["git", "grep", "-q", "-F", "-e", marker, target]
    if path:
        argv += ["--", path]
    result = subprocess.run(
        argv, cwd=checkout, capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise CrewError(
        f"git could not search {target} in {checkout} for the marker: "
        f"{result.stderr.strip() or result.stdout.strip() or result.returncode}"
    )


def _delivered_review_records(record: Mapping[str, Any]) -> list[str]:
    """The delivered review records a filing would read, resolved but unread.

    The filing's own resolution decides which records a promotion would move
    into the store, so a dry run asks the same question without writing any of
    them: the names it reports are the ones the filing would have opened.
    """
    from reckon.crew import recovery_review_delivery as delivery
    from reckon.crew import review as review_module

    directory = delivery._review_record_delivery_directory(
        record, str(record.get("project") or "")
    )
    if directory is None:
        return []
    try:
        return [
            str(path)
            for path in review_module.delivered_run_review_paths(record, directory)
        ]
    except (OSError, ValueError):
        return []


def _file_delivered_run_reviews(record: Mapping[str, Any]) -> list[str]:
    """File a run's delivered review records into the store, return their paths.

    A review delivers its record through its own run directory rather than
    straight into the store, so the file exists even when the store was never
    written to: the supervisor files it when its worker ends, and a supervisor
    that died first leaves it where the reviewer wrote it. Promotion is the
    last moment that copy still exists, and a record no store reader can find
    is a review that never happened, so it is filed here too. The store's own
    writer places it, so a record already filed is refiled identically and a
    record the store refuses is left where it is.
    """
    from reckon.crew import recovery_review_delivery as delivery

    try:
        written = delivery.file_review_records_into_store(record)
    except (OSError, ValueError, TypeError):
        return []
    return [str(path) for path in written]


from reckon.crew.promotion_checks import (
    _IMPL_MOVE_CORRECTIVE_ATTEMPT_KINDS,
    _IMPL_MOVE_EXEMPT_CLASSIFICATIONS,
    _STREAM_SETTLE_MAX_SECONDS,
    _STREAM_SETTLE_POLL_SECONDS,
    _STREAM_SETTLE_QUIESCENCE_SECONDS,
    StreamMeasures,
    _baseline_suite_failure_ids,
    _capability_risk_of,
    _combined_refusal_text,
    _control_failure_ids,
    _delivered_review_payloads_for_commit,
    _end_live_writer_for_settle,
    _fresh_manifest,
    _head_suite_failure_ids,
    _landed_sections,
    _landing_preconditions,
    _landing_scope_products,
    _light_changed_line_ceiling,
    _manifest_repository_paths,
    _misdelivered_plan_review_directories,
    _negative_control_log_text,
    _newest_stream_mtime,
    _plan_impl_from_state,
    _plan_remaining_sections,
    _plan_review_record_for_promoting_run,
    _plan_review_store_outcome,
    _plan_state_for_run,
    _promotion_terminal_observation,
    _PromotionRefusalError,
    _recoverable_session,
    _refuse_commits_for_a_shadow,
    _require_brief_owner,
    _require_declared_negative_control,
    _require_domain_invariant_checks,
    _require_gate_check_precondition,
    _require_impl_moved,
    _require_independently,
    _require_no_manifest_fault,
    _require_no_png_twin_of_an_svg,
    _require_partner_terminal_before_promotion,
    _require_plan_review_delivered_in_its_directory,
    _require_recognised_manifest_status,
    _require_resume_waiver,
    _require_review_waiver,
    _require_standing_suite,
    _require_worker_stopped_before_promotion,
    _resolve_promotion_outcome,
    _review_changed_scope,
    _review_outcome_summary,
    _reviews_the_dry_run_owes,
    _run_capability_risk,
    _run_review_tier,
    _staging_review_record_by_run,
    _terminal_stream_data,
    _unreviewed_refusal,
    _wait_out_stream_tail,
    _zone_aware_stream_timestamp,
    owed_floor_repair,
    plan_impl_at,
)
from reckon.crew.promotion_evidence import (
    _ABSENCE_BOUNDARY,
    _ATTEMPT_RECORD_NAME,
    _COMMITLESS_ROLES,
    _COORDINATOR_LANDING_AUTHOR,
    _FIELD_LINE,
    _PROVISIONED_WORKTREE_ENTRIES,
    _RUNNER_SUMMARY,
    _UNEXECUTED_EXIT_CODES,
    _WORKER_RECORD_NAME,
    _arm_without_completion,
    _commit_canonical_id,
    _commit_resolves_in,
    _commitless_changed_paths_declares_absence,
    _commitless_raw_field,
    _commits_field_declares_absence,
    _committed_scope,
    _CumulativeDiff,
    _declared_repository_roots,
    _declares_absent_commits,
    _first_command_not_found_line,
    _foreign_repository,
    _head_arm_log_failure_ids,
    _log_shows_the_command_ran,
    _manifest_declares_no_change,
    _manifest_text,
    _opens_with_an_absence_word,
    _presented_commits_without_a_declaration,
    _promoted_worker_exit,
    _raw_manifest_field,
    _recorded_disposition,
    _recorded_exit_status,
    _registered_repository_roots,
    _require_commits_beyond_base,
    _require_gate_evidence,
    _require_gate_log_agrees,
    _require_verdict_matches_exit_status,
    _run_changed_nothing,
    _run_commit_directory,
    _uncommitted_paths,
    _unresolved_citations,
    _worktree_git_paths,
    _worktree_repository_changes,
    _worktree_unchanged_since_base,
    _worktree_untracked_paths,
    _zero_added_against_a_red_base,
    scoped_diff_stat,
)
from reckon.crew.promotion_gate import (
    _GATE_COMMAND_ELLIPSIS,
    _GATE_COMMAND_PARENTHETICAL,
    _GATE_COMMAND_PLACEHOLDER,
    _GATE_COMMAND_SHELL_OPERATORS,
    _GATE_LOG_DURATION,
    _PRESERVED_GATE_LOG_NAME,
    _REPLAY_BOUND_CEILING_SECONDS,
    _REPLAY_BOUND_DEFAULT_SECONDS,
    _REPLAY_BOUND_HEADROOM,
    _REPLAY_GATE_LOG_NAME,
    _SHELL_ASSIGNMENT,
    _cited_changed_paths,
    _merged_gate_finding,
    _paths_differing_between,
    _preserve_cited_gate_log,
    _recorded_gate_seconds,
    _recorded_worktree_roots,
    _replay_bound,
    _replay_cut_short_reason,
    _replay_log_header,
    _require_executable_gate_command,
    _require_runnable_gate_command,
    _rewrite_worktree_roots,
    _write_replay_log,
    gate_command_prose,
    record_gate_rerun_at_integrated_revision,
    rerun_gate_at_integrated_revision,
)
from reckon.crew.promotion_records import (
    _complete_withdrawn_run,
    _evaluate_suite_delta,
    _manifest_reads_blocked,
    _manifest_relative_path,
    _normalized_command,
    _project_for_repository,
    _read_json_object,
    _read_pointer_or_rebuild,
    _rebuild_record_from_run_directory,
    _release_scratch_when_release_raised,
    _release_terminal_manifest,
    _resume_worktree_retention,
    _retire_disposable_identity,
    _worktree_audit,
)
from reckon.crew.promotion_records import (
    _default_gate_evidence_from_manifest as _manifest_gate_evidence,
)
from reckon.crew.promotion_release import (
    _ARM_ARTIFACT_FIELD,
    _ARM_LOG_FIELDS,
    _ARM_SUITE_FIELDS,
    _BASETEMP_IN_COMMAND,
    _CITED_ABSOLUTE_PATH,
    _CITED_PATH_EDGE_PUNCTUATION,
    _FLEET_UNMEASURED_CAUSE_LIMIT,
    _absolute_paths_in,
    _agent_model_identifier,
    _arm_citations_of,
    _artifact_candidates,
    _coordinator_supplied_predecessor,
    _declared_arm_paths,
    _declared_manifest,
    _fleet_state_reading,
    _harvest_lane_receipt,
    _other_live_run_citations,
    _receipt_unmeasured_reason,
    _record_tree,
    _record_worktree,
    _release_after_promotion,
    _release_run_workspace,
    _review_for_promotion,
    _run_promoted_revision,
    _serialize_notional_figure,
    _serialize_rate_basis,
    _string_leaves,
    _suite_record,
    _tree_for_measurement,
    _unavailable_fleet_reading,
    _unreconciled_live_runs,
    _update_run_record,
    remove_cited_arms,
)
from reckon.crew.promotion_release import (
    _record_release_on_ledger as _write_release_record,
)
from reckon.crew.promotion_scope import (
    _OPEN_OPERATION_MARKERS,
    _RECORD_ATTRIBUTES,
    _ZERO_COUNT,
    BOUNDARY_REFERENT_MAX_AGE_SECONDS,
    LANDING_ROLLBACK_ATTRIBUTE,
    _accepted_scope_exceptions,
    _assume_utc_if_naive,
    _boundary_has_no_referent,
    _changed_paths_declare_no_paths,
    _changed_paths_inside_repository,
    _collapse_inter_tag,
    _commit_landing_writes,
    _declared_manifest_commits,
    _declared_manifest_landing,
    _descendant_commit,
    _discard_run_store_row,
    _elapsed_seconds,
    _is_shadow,
    _landing_refusal,
    _ledger_holds_row,
    _ledger_row_was_rolled_back,
    _manifest_cites_a_commit,
    _merge_revisions,
    _open_operation_state,
    _outside_declared_scope,
    _path_change_reaches_integration_head,
    _path_differs_from_head,
    _path_has_a_staged_change,
    _plan_comment_store_path,
    _plan_differs_only_by_the_stores_own_writes,
    _primary_read_targets,
    _promoted_revision,
    _prose_changed_paths_name_no_paths,
    _record_landing_comment,
    _record_landing_refusal,
    _record_stream_paths,
    _refuse_unrelated_plan_edit,
    _report_landing_check,
    _report_written_ledger_row,
    _repository_scope_paths,
    _repository_tree_boundary_violations,
    _require_commit_for_changed_manifest,
    _require_committable_checkout,
    _require_repository_tree_boundary,
    _resolve_commits,
    _resolved_path_key,
    _restore_landing_writes,
    _restore_one_landing_write,
    _revision_is_ancestor,
    _run_directory_tree_snapshot,
    _run_streams,
    _scope_worktree,
    _shadow_patch_stat,
    _shadow_stream_contamination,
    _snapshot_entries,
    _strip_store_owned_content,
    _unrestored_landing_writes,
    _wall_exceeded_budget,
    _worker_authored_landing_record,
    _write_shadow_patch,
)
