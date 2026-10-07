"""Derive the work one coordinator session still owes.

The recovery classifier owns each live run's state and remedy.  This module
only projects those rows into coordinator duties, adds the duties whose
evidence lives outside live pointers, and reports the closure figure from the
drain view.  No obligation state is persisted here.

One of those duties is derived rather than projected: a stored review whose
dimension sits below the floor flight configuration declares for it is a
finding that must be answered before the session can close cleanly over it.
The finding is never folded into the review's total — it is reported beside
it, for the run the review is about, until a disposition in the closed set is
recorded against the dimension on the stored record itself. The disposition
vocabulary, and the read-back that decides whether a finding still stands,
live with the review store (:mod:`reckon.crew.review`); this module only
reports what is still owed.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _store, flight, ledger
from reckon._timestamps import parse_utc
from reckon.crew import recovery, review_lifecycle, runs
from reckon.crew import review as review_module
from reckon.crew.node import INTERRUPTED_RUN_PHASE, parse_duration
from reckon.crew.routing import _registered_worktrees

CLASSIFICATION_DUTY_KINDS = {
    "scoring": "review-missing",
    "promotable": "review-ready",
    "blocked": "blocked",
    INTERRUPTED_RUN_PHASE: "turn-ended-early",
}
# The two duty kinds whose evidence is a stored review of the run's own head.
# Each is read against the review selection — the one promotion uses — so a run
# whose head moved past the revision its stored review recorded reads
# review-missing naming both heads, whatever the classifier's own reading said.
REVIEW_CLASSIFICATION_KINDS = frozenset({"scoring", "promotable"})
RECOVERY_CLASSIFICATION_DUTY_KINDS = {"needs-help": "needs-help"}

# A duty kind whose evidence is a stored review rather than a live run's state:
# the row is owed while the dimension carries no disposition, and it names the
# run the review is about rather than the reviewing run.
SUB_FLOOR_DUTY_KIND = "review-dimension-sub-floor"

# A review whose attempt the lane's admission gate refused with ``lane-paused``
# is not work the coordinator skipped: the reflex still owns it and retries when
# a slot frees, so the duty reads as queued rather than missing, is listed like
# any other duty, and does not hold a turn open. The reading is bounded by the
# refusal's own instant, because a refusal nobody acted on within the bound is
# evidence about the reflex rather than an explanation for the duty, and the
# duty returns to review-missing so the coordinator sees it again.
REVIEW_QUEUED_DUTY_KIND = "review-queued"
REVIEW_QUEUED_BOUND_SECONDS = 30 * 60

# The status the review reflex records on a run's pointer when the lane refused
# the attempt as paused; the refusal instant it carries is the bound's origin.
REVIEW_LANE_PAUSED_STATUS = "lane-paused"

# The duty kinds that are listed but never hold a stop open: the work behind
# them is already owned by a reflex that retries, so blocking would ask the
# coordinator to act on a wait it cannot shorten.
NON_BLOCKING_DUTY_KINDS = frozenset({REVIEW_QUEUED_DUTY_KIND})


def _utc_now() -> datetime:
    """Return the observation instant through a patchable clock boundary."""
    return datetime.now(tz=UTC)


def _seconds_since(value: Any, *, now: datetime) -> int:
    """Return the non-negative age of one timestamp, or zero if unreadable."""
    text = str(value or "").strip()
    if not text:
        return 0
    stamp = parse_utc(text)
    if stamp is None:
        return 0
    return max(0, int((now - stamp).total_seconds()))


def _row_age(row: Mapping[str, Any], *, now: datetime) -> int:
    """Read a classifier row's best available age measurement."""
    stated_age = row.get("age_seconds")
    if isinstance(stated_age, (int, float)) and not isinstance(stated_age, bool):
        return max(0, int(stated_age))
    terminal_age = row.get("terminal_age_seconds")
    if isinstance(terminal_age, (int, float)) and not isinstance(terminal_age, bool):
        return max(0, int(terminal_age))
    exit_record = row.get("exit_record")
    if isinstance(exit_record, Mapping) and exit_record.get("exited_at"):
        return _seconds_since(exit_record["exited_at"], now=now)
    log_age = row.get("log_age_seconds")
    if isinstance(log_age, (int, float)) and not isinstance(log_age, bool):
        return max(0, int(log_age))
    return 0


def _reflex_review_in_flight(pointer: Mapping[str, Any]) -> bool:
    """Whether the reflex's own recorded review dispatch still holds a live run.

    The reflex records the review it launched on the run it acted for, and that
    record is its claim: while the review run it names holds a live pointer the
    reflex will not compose a second review, so a printed dispatch is one the
    coordinator can only have refused as a scope conflict. The claim is dated
    from the launch window the record carries rather than being re-judged
    against the run's manifest: a run that re-completed after that launch has
    not taken the claim away, because the live review the reflex owns is still
    the thing under way and the reflex is what re-fires once it ends.

    Whether the recorded pointer is live is decided by the reflex's own rule,
    so this reader and the reflex cannot split on a pointer that exists but
    cannot be read: both treat it as no review, and the run stays owed one.
    """
    return recovery._recorded_review_is_live(
        pointer.get(recovery.REVIEW_DISPATCH_FIELD)
    )


def _current_review_in_flight(pointer: Mapping[str, Any]) -> bool:
    """Whether a live review is working on this run's current revision.

    The reflex's own claim is honoured whatever revision the run now carries.
    A hand-launched review carries no such record, so it is recognised by the
    head-keyed record path its reviewer was granted, which keeps a review of an
    older revision from reading as a review of the current one.
    """
    if _reflex_review_in_flight(pointer):
        return True
    review_run_id = recovery._review_in_flight(pointer)
    if not review_run_id:
        return False
    review_pointer = runs.read_pointer(review_run_id)
    if not review_pointer:
        return False
    expected = recovery._review_dispatch_fields(pointer)["write_paths"]
    head_keyed = expected[1:]
    if not head_keyed:
        return True
    node = review_pointer.get("node") or {}
    granted = {str(path) for path in node.get("write_paths") or ()}
    return any(str(path) in granted for path in head_keyed)


def _live_review_runs(project: str, session: str) -> set[str]:
    """Snapshot session runs whose current revision has a review in any session."""
    in_flight: set[str] = set()
    for pointer in runs.list_live(project=project):
        if str(pointer.get("session") or "") != session:
            continue
        run_id = str(pointer.get("run_id") or "")
        if run_id and _current_review_in_flight(pointer):
            in_flight.add(run_id)
    return in_flight


def _classified_rows(project: str) -> list[dict[str, Any]]:
    """Classify current project pointers without observing or launching work."""
    return [
        recovery.classify_pointer(pointer)
        for pointer in runs.list_live(project=project)
    ]


def _acknowledgements_in_force(
    project: str, *, now: datetime
) -> dict[str, dict[str, Any]]:
    """Return each run's recorded deferral that has not yet expired.

    A deferral is read from whichever store the run can own one in: the live
    pointer of a run still in flight, and the run's own file under the crew
    home once it has been promoted and no pointer is left. The promoted file is
    transient state, so reading it costs the acknowledgement and nothing of any
    project's committed history — a deliberate remainder of a promoted run is
    withheld exactly as a live run's is, and expires the same way.

    A deferral whose ``until`` has passed is not returned, so the run it named
    re-enters the list on the next read rather than lingering in the
    acknowledged block. A run whose deferral cannot be parsed as an instant is
    treated as undeferred: an unreadable deadline is no excuse to withhold an
    obligation.
    """
    in_force: dict[str, dict[str, Any]] = {}
    for pointer in runs.list_live(project=project):
        run_id = str(pointer.get("run_id") or "")
        record = runs.run_acknowledgement(pointer)
        if not run_id or not record:
            continue
        until = parse_utc(record.get("until"))
        if until is not None and until > now:
            in_force[run_id] = record
    for record in runs.recorded_promoted_acknowledgements(project):
        run_id = str(record.get("run_id") or "")
        if not run_id or run_id in in_force:
            continue
        until = parse_utc(record.get("until"))
        if until is not None and until > now:
            in_force[run_id] = {
                "reason": record.get("reason"),
                "until": record.get("until"),
                "recorded_at": record.get("recorded_at"),
            }
    return in_force


def _partition_acknowledged(
    items: list[dict[str, Any]],
    acknowledgements: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split duties into those still owed and those deliberately deferred."""
    owed: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for item in items:
        record = acknowledgements.get(str(item.get("run_id") or ""))
        if record is None:
            owed.append(item)
            continue
        deferred.append(
            {
                **item,
                "reason": str(record.get("reason") or ""),
                "until": str(record.get("until") or ""),
            }
        )
    return owed, deferred


def _live_item(row: Mapping[str, Any], *, kind: str, now: datetime) -> dict[str, Any]:
    """Project one recovery row into the stable obligation shape."""
    return {
        "kind": kind,
        "run_id": str(row.get("run_id") or ""),
        "node": str(row.get("node") or ""),
        "plan": str(row.get("plan") or ""),
        "age_seconds": _row_age(row, now=now),
        "next_command": str(row.get("next_action") or ""),
    }


def _published_worker_slots(session: str) -> Any:
    """The session's share of the local lane's published admission block.

    The lane's router publishes each session's fair share as ``worker_slots``;
    the session's own entry is the figure the refusal was about, and the
    new-session and lane-wide shares stand in when the map lists no entry for
    it. An unreadable document, or a block publishing no figure, reads as
    ``"unknown"``: absence of a published figure is not a published zero.
    """
    from reckon.crew import lane_document, paid_lanes

    try:
        document = json.loads(paid_lanes.local_lane_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    admission = lane_document.read_lane_admission(document)
    listed = (
        admission.get("sessions", {}).get(session)
        if admission.get("sessions_present")
        else None
    )
    candidates = [
        listed.get("worker_slots") if isinstance(listed, Mapping) else None,
        (
            admission.get(lane_document.ADMISSION_NEW_SESSION_WORKER_SLOTS_KEY)
            if admission.get("present")
            else None
        ),
        admission.get(lane_document.ADMISSION_WORKER_SLOTS_KEY),
    ]
    for value in candidates:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return "unknown"


def _queued_review_fields(
    pointer: Mapping[str, Any], *, head: str, now: datetime
) -> dict[str, Any] | None:
    """The queued reading of a review the lane refused, or None.

    The reflex records every attempt on the run it acted for, dated and naming
    the head it composed for, so a refusal is read from that record rather than
    inferred from an absent review. Only a paused refusal is queued: any other
    refusal is a defect the coordinator must see. A refusal about an earlier
    revision no longer speaks for the head the run carries, and one older than
    the bound has had its chance to be retried, so both fall back to the
    missing reading.
    """
    record = pointer.get(recovery.REVIEW_DISPATCH_FIELD)
    if not isinstance(record, Mapping):
        return None
    if str(record.get("status") or "") != REVIEW_LANE_PAUSED_STATUS:
        return None
    refused_at = parse_utc(record.get("at"))
    if refused_at is None:
        return None
    age = (now - refused_at).total_seconds()
    if age < 0 or age > REVIEW_QUEUED_BOUND_SECONDS:
        return None
    if not recovery._review_head_covers(str(record.get("head") or ""), head):
        return None
    session = str(pointer.get("session") or "")
    slots = _published_worker_slots(session)
    stamp = str(record.get("at") or "")
    return {
        "refused_at": stamp,
        "worker_slots": slots,
        "next_command": (
            f"the lane refused this review dispatch at {stamp} "
            f"(session worker slots: {slots}); the reflex retries for up to "
            f"{REVIEW_QUEUED_BOUND_SECONDS // 60}m"
        ),
    }


def _review_duty_item(
    project: str,
    row: Mapping[str, Any],
    pointer: Mapping[str, Any] | None,
    *,
    now: datetime,
    grace: int,
) -> dict[str, Any]:
    """One review duty, keyed to the review of the run's current worktree head.

    The kind is decided by the selection promotion uses
    (:func:`reckon.crew.recovery.select_review_for_head`), read for the
    revision the run's tree carries now: a complete review stored at that head
    reads review-ready, and a stored record describing any other revision reads
    review-missing, naming both heads — the revision the review read and the
    head the run now carries — so the reader sees which two disagree rather
    than an absence. With no stored record at all the classifier's own reading
    stands, there being no other head to name.

    A reviewer run owes no review of itself: the record it wrote is its own
    deliverable, so its kind stays the classifier's. A row whose live pointer
    is gone keeps the classifier's reading too, there being no tree left to
    ask, and a review the store cannot answer for reads missing rather than
    ready, because the safe direction is to ask for evidence.

    A missing review whose most recent attempt the lane refused as paused reads
    review-queued while the refusal is inside its bound, naming when the lane
    refused it and the session's published worker slots, so the coordinator
    sees a wait the reflex owns rather than a dispatch nobody ran.
    """
    age = _row_age(row, now=now)
    classification = str(row.get("classification") or "")

    def _classification_kind() -> str:
        return (
            "promotable-stale"
            if classification == "promotable" and age > grace
            else CLASSIFICATION_DUTY_KINDS[classification]
        )

    if pointer is None or recovery._is_review_run(pointer):
        # No live tree to ask, or a reviewer whose own record is its
        # deliverable: the classifier's own reading stands.
        return _live_item(row, kind=_classification_kind(), now=now)
    head, tree = recovery._review_head_and_tree(pointer)
    try:
        review, reviewed_head = recovery.select_review_for_head(
            project, str(row.get("run_id") or ""), head, tree=tree
        )
    except (OSError, ValueError):
        review, reviewed_head = None, ""
    item = _live_item(row, kind=_classification_kind(), now=now)
    if head:
        item["head"] = head
    if review is not None and recovery._review_is_complete(review):
        item["kind"] = "promotable-stale" if age > grace else "review-ready"
    elif reviewed_head and not recovery.same_revision(reviewed_head, head):
        # A stored record describes a revision the run has moved past, so no
        # review of the head the run carries exists; both revisions travel on
        # the duty so the reader sees which two disagree.
        item["kind"] = "review-missing"
        item["reviewed_head"] = reviewed_head
    else:
        # No stored review covers the head, so the duty would read
        # review-missing. A refused attempt the reflex still owns is the one
        # exception: within the bound it reads as queued, and the duty does not
        # hold a turn open over a wait the coordinator cannot shorten.
        queued = _queued_review_fields(pointer, head=head, now=now)
        if queued is not None:
            item["kind"] = REVIEW_QUEUED_DUTY_KIND
            item.update(queued)
    return item


def _live_worktrees(project: str) -> set[Path]:
    """Registered-tree paths a live run currently occupies.

    A retained path reused by a live run is not held by the promoted run whose
    ledger record still names it. The collector sees the path live-referenced
    and leaves it alone, so hinting a collection for it would name work that
    cannot be done and a run that no longer owns the tree.
    """
    trees: set[Path] = set()
    for pointer in runs.list_live(project=project):
        value = str(pointer.get("worktree") or "").strip()
        if value:
            trees.add(Path(value).expanduser().resolve())
    return trees


def _run_file_record(source: Path) -> Mapping[str, Any] | None:
    """Read one per-run ledger file, or None when it cannot stand for a run.

    A file that is absent, unreadable, or not the run it is named for reads as
    no per-run file at all: its tree is then resolved against the whole ledger,
    which refuses a damaged record exactly where the whole-ledger reader always
    has. Treating it as a record here would answer from a file the ledger
    itself would not.
    """
    try:
        record = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(record, Mapping)
        or str(record.get("run_id") or "") != source.stem
    ):
        return None
    return record


def _names_worktree(record: Mapping[str, Any], worktree: Path) -> bool:
    """Whether a run's record names this retained tree.

    Two fields name a tree, and both are read exactly as the whole-ledger scan
    reads them: the path a promotion retained, and the node id the tree is
    named for.
    """
    retention = record.get("worktree_retention")
    if isinstance(retention, Mapping):
        value = str(retention.get("worktree") or "").strip()
        if value and Path(value).expanduser().resolve() == worktree:
            return True
    node = record.get("node")
    node_id = (
        str(node.get("id") or "") if isinstance(node, Mapping) else str(node or "")
    )
    return bool(node_id) and worktree.name == node_id


# The rows crew.json holds, keyed by the stat identity of the file they were
# parsed from. A ledger that has not moved answers from memory, so a call stats
# it once and parses it only when a promotion rewrote it.
_AGGREGATE_ROWS: dict[
    Path, tuple[tuple[int, int, int, int], tuple[Mapping[str, Any], ...]]
] = {}


def _aggregate_rows(project: str, repository: Path) -> tuple[Mapping[str, Any], ...]:
    """The rows crew.json holds, parsed once per stat identity of that file.

    A row lives here while the split has written it no per-run file, so a tree
    is named from both sources; the parse covers the whole file, which is what
    made the whole-ledger read expensive, so an unchanged ledger is served from
    memory instead of parsed again.
    """
    path = ledger.ledger_path(project, repository)
    try:
        status = path.stat()
    except OSError:
        return ()
    identity = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
    cached = _AGGREGATE_ROWS.get(path)
    if cached is not None and cached[0] == identity:
        return cached[1]
    data, _version = ledger._load_aggregate(project, repository)
    rows = tuple(data["runs"])
    _AGGREGATE_ROWS[path] = (identity, rows)
    return rows


def _ledger_order_key(record: Mapping[str, Any]) -> tuple[str, str]:
    """The position one row takes in the reader that merges both sources.

    That reader orders the runs it merges by completion and breaks a tie on the
    run id, so the last row naming a tree carries the greatest key — which is
    not the per-run file with the greatest name, that name being the dispatch
    stamp rather than the completion.
    """
    return (
        str(record.get("completed_at") or record.get("run_id") or ""),
        str(record.get("run_id") or ""),
    )


def _holding_record(
    worktree: Path,
    rows: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    *,
    merged_order: bool,
) -> Mapping[str, Any] | None:
    """The run the whole-ledger reader attributes a tree to, or None.

    Records are taken from both sources: the aggregate rows, which carry a run
    the split has written no file for, and the per-run files the aggregate does
    not carry. A run both sources carry is read once, at the position the
    merged reader gives it. Which of them names a tree is decided by the
    record's own two namings, exactly as the whole-ledger reader decides it: a
    run's node id need not match the tree's directory name, so a file's name
    cannot tell whether its record names the tree.
    """
    candidates = [
        record
        for record in (*rows, *records)
        if isinstance(record, Mapping) and _names_worktree(record, worktree)
    ]
    if not candidates:
        return None
    if merged_order:
        # Stable: rows of one completion keep the merged reader's own order,
        # which reaches an aggregate row before a file-only one.
        candidates.sort(key=_ledger_order_key)
    return candidates[-1]


def _held_worktree_records(project: str) -> dict[Path, Mapping[str, Any]]:
    """Return the ledger record holding each registered tree that is still held.

    A tree is held only when Git's registry lists it, its directory is still on
    disk, a promotion record names it, and no promotion record shows it
    released. Git keeps a path in its registry until the registry is pruned, so
    a directory deleted out from under a registration is still listed; naming
    that registration would raise a duty whose remedy, a project-wide sweep,
    would take peers' trees to clear a tree entry that holds nothing.

    A registered path a live run now occupies is skipped: the promoted run's
    ledger record still names it under the same node id, but the tree belongs
    to the live run and the collector will not take it, so naming the promoted
    run would send a coordinator at the wrong run for a tree it cannot free.

    Each per-run file the aggregate does not carry is read once for the call,
    whatever tree it names: a run's node id need not match the tree's directory
    name, so which tree a file's record names is knowable only by reading it.
    The aggregate's rows, which carry the runs the split has written no file
    for, are matched the same way, and the run the merged ledger's own order
    puts last holds the tree. A tree neither source names falls back to the
    whole ledger, which reads the merged history once for the session however
    many trees need it.
    """
    docs_dir = _store._docs_dir_for_project(project)
    if docs_dir is None:
        return {}
    repository = docs_dir.parent.resolve()
    registered = {
        path
        for path in _registered_worktrees(repository)
        if path != repository and path.is_dir()
    }
    occupied = _live_worktrees(project)
    inspected = sorted(registered - occupied)
    sources = ledger._run_files(project, repository)
    rows = _aggregate_rows(project, repository)
    known = {
        str(row.get("run_id"))
        for row in rows
        if isinstance(row, Mapping) and row.get("run_id")
    }
    # A file whose row the aggregate already holds is never opened: the merged
    # reader reads that run from its row, at the row's own position. Every other
    # file carries a record no row does, and any of them may name an inspected
    # tree, so each is read once for this call.
    records = [
        record
        for source in sources
        if source.stem not in known and (record := _run_file_record(source)) is not None
    ]
    # The reader that merges both sources re-sorts the merged list only when it
    # appends a run the aggregate does not carry; without one the aggregate's own
    # order is the answer, so the candidates keep the order they were gathered in.
    merged_order = any(source.stem not in known for source in sources)
    matched: dict[Path, Mapping[str, Any]] = {}
    unresolved: list[Path] = []
    for worktree in inspected:
        record = _holding_record(worktree, rows, records, merged_order=merged_order)
        if record is None:
            unresolved.append(worktree)
        else:
            matched[worktree] = record
    if unresolved:
        # A tree neither source names is left to the whole-ledger scan, which
        # matches on every recorded row and refuses a damaged one exactly where
        # the whole-ledger reader always has.
        for record in ledger.runs(project, root=repository):
            for worktree in unresolved:
                if _names_worktree(record, worktree):
                    matched[worktree] = record

    held: dict[Path, Mapping[str, Any]] = {}
    for worktree, record in matched.items():
        release = record.get("release")
        if isinstance(release, Mapping) and release.get("worktree_released") is True:
            # The promotion's own record answers for this tree: it reported the
            # tree released, so the path is not held whatever the registry or
            # the filesystem now shows.
            continue
        held[worktree] = record
    return held


def _held_worktrees_by_session(
    project: str, *, now: datetime
) -> dict[str, list[dict[str, Any]]]:
    """One session's held-worktree duties, from the trees that are still held.

    The session is the directory the tree was created under, which is the
    session that dispatched the run whose record still names it.
    """
    docs_dir = _store._docs_dir_for_project(project)
    if docs_dir is None:
        return {}
    repository = docs_dir.parent.resolve()
    by_session: dict[str, list[dict[str, Any]]] = {}
    for worktree, record in _held_worktree_records(project).items():
        run_id = str(record.get("run_id") or "")
        if not run_id:
            # No run record names this tree, so there is no run a sweep could
            # be confined to; the ledger itself is the remedy, and the duty
            # must not widen to a repository-wide sweep.
            continue
        command = " ".join(
            shlex.quote(part)
            for part in (
                "reckon",
                "crew",
                "gc",
                "--repo",
                str(repository),
                "--project",
                project,
                "--run",
                run_id,
                "--apply",
            )
        )
        retention = record.get("worktree_retention")
        retained_at = (
            retention.get("retained_at") if isinstance(retention, Mapping) else None
        )
        by_session.setdefault(worktree.parent.name, []).append(
            {
                "kind": "worktree-held",
                "run_id": run_id,
                "node": str(record.get("node") or ""),
                "plan": str(record.get("plan") or ""),
                "age_seconds": _seconds_since(
                    retained_at or record.get("completed_at"),
                    now=now,
                ),
                "next_command": command,
            }
        )
    return by_session


def _held_worktrees(
    project: str, session: str, *, now: datetime
) -> list[dict[str, Any]]:
    """One session's held worktrees, sliced from the fleet-wide derivation."""
    return _held_worktrees_by_session(project, now=now).get(session, [])


def stored_review_for_run(
    project: str,
    run_id: str,
    pointer: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """The one reading of a run's stored review, shared by its two readers.

    A dimension duty follows from a stored review that exists, whether or not
    the run's worktree survives, and the command that disposes a duty has to
    reach the same record the duty was built from: a row the coordinator is
    shown and no command can retire is worse than no row. Both readers take
    this function with these arguments, so the selection cannot drift between
    them. The head comes from the resolver every other reader uses
    (:func:`reckon.crew.recovery._review_head_and_tree`), so a run whose
    worktree has been reclaimed is keyed to the head its own record carries
    rather than to the shared checkout's HEAD, and the tree alongside it is
    the one the head belongs to. A record naming no resolvable head is read
    through the store's newest record for the run, the evidence there is: a
    low dimension is a finding about a review that exists, and a reclaimed
    worktree neither supplies nor retires one. The second element is the head
    a non-matching record named, empty when none, so a writer can name the
    disagreement rather than report an absence.
    """
    head, tree = recovery._review_head_and_tree(pointer)
    if not head:
        return (
            recovery.newest_review_for_headless_run(project, run_id, reclaimed=False),
            "",
        )
    return recovery.select_review_for_head(project, run_id, head, tree=tree)


def _review_is_hot(
    project: str,
    run_id: str,
    record: Mapping[str, Any],
    *,
    closed: set[str],
) -> bool:
    """Whether a run's stored review still owes a live duty.

    The lifecycle is derived once, in :mod:`reckon.crew.review_lifecycle`, and
    consulted here through its hot-set predicate so the states a live reader
    acts on are not restated. A review lands once its run has a ledger row —
    promoted, or closed without promotion — and is superseded once a later
    review of the same run exists; either way it describes work in the past and
    raises no duty. The run's other stored reviews are the candidates for
    superseding this one, read from the store's own rows so a by-head selection
    cannot hide a later round.
    """
    later = [
        other for _path, other in review_module.stored_records_for_run(project, run_id)
    ]
    return review_lifecycle.is_hot(
        record,
        run_closed=run_id in closed,
        later_records=later,
    )


def _sub_floor_items_by_session(
    project: str,
    floors: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Return one duty per undisposed review dimension below its declared floor.

    The review is selected through :func:`stored_review_for_run`, the reading
    the disposition command takes too, so a duty row is always one the writer
    can retire and a record describing a superseded revision cannot stand in
    for the current one. A run whose worktree has been reclaimed is keyed to
    the head its own record carries rather than to the shared checkout's HEAD.
    Each row names the run the review is about — the run whose work carries
    the low dimension — and carries the dimension, the score it was given and
    the floor it fell below, so a reader can see the finding without opening
    the record.

    A review with no floor declared for the dimension, and a dimension already
    answered by a disposition in the closed set, produce nothing. The total is
    not consulted: a high total over four strong dimensions does not retire a
    fifth one that sits below its floor, which is the case this duty exists
    for.
    """
    if not floors:
        return {}
    closed = recovery._promoted_run_ids(project)
    by_session: dict[str, list[dict[str, Any]]] = {}
    for pointer in runs.list_live(project=project):
        session = str(pointer.get("session") or "")
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            continue
        if review_module.read_review(project, run_id) is None:
            # A run with no stored review owes no dimension duty, and the head
            # is consulted only to choose between reviews that exist, so
            # resolving it here would launch one git process per live run per
            # sweep for an answer that cannot name a duty.
            continue
        record, _described = stored_review_for_run(project, run_id, pointer)
        if not record:
            continue
        if not _review_is_hot(project, run_id, record, closed=closed):
            # A review is a live duty only while it is hot. A run review lands
            # once its run has a ledger row and is superseded once a later
            # review of the same run exists, and a landed or superseded review
            # is the archive: it describes work in the past, so a dimension
            # below its floor is kept but no longer owed.
            continue
        node = pointer.get("node") or {}
        by_session.setdefault(session, []).extend(
            {
                "kind": SUB_FLOOR_DUTY_KIND,
                "run_id": run_id,
                "node": str(node.get("id") or run_id),
                "plan": str(node.get("plan") or ""),
                "age_seconds": _seconds_since(record.get("timestamp"), now=now),
                "dimension": finding["dimension"],
                "score": finding["score"],
                "floor": finding["floor"],
                "next_command": (
                    f"dispose {finding['dimension']} scored "
                    f"{finding['score']} against a floor of {finding['floor']} "
                    f"on run {run_id}: folded with the dispatched node's id, or "
                    "exempted with the recorded reason"
                ),
            }
            for finding in review_module.sub_floor_dimensions(record, floors)
        )
    return by_session


def _sub_floor_items(
    project: str,
    session: str,
    floors: Mapping[str, Any],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """One session's sub-floor duties, sliced from the fleet-wide derivation."""
    return _sub_floor_items_by_session(project, floors, now=now).get(session, [])


def obligations(project: str, session: str) -> dict[str, Any]:
    """Return every current duty owed by one coordinator session.

    Live duty kinds are a projection of :func:`recovery.recover`; the
    promotable grace comes from the same resolved flight configuration used by
    dispatch.  The result is ordered oldest first so the first row is also the
    most urgent age signal.
    """
    now = _utc_now()
    resolved = flight.resolve(project)
    config = resolved.config
    grace = parse_duration(
        str((config.get("fences") or {}).get("unreconciled_run_grace") or "15m")
    )
    reviews_in_flight = _live_review_runs(project, session)
    items: list[dict[str, Any]] = []
    pointers = {
        str(pointer.get("run_id") or ""): pointer
        for pointer in runs.list_live(project=project)
    }
    for row in _classified_rows(project):
        if str(row.get("session") or "") != session:
            continue
        classification = str(row.get("classification") or "")
        recovery_classification = str(row.get("recovery_classification") or "")
        kind = ""
        if classification in REVIEW_CLASSIFICATION_KINDS:
            if (
                classification == "scoring"
                and str(row.get("run_id") or "") in reviews_in_flight
            ):
                continue
            items.append(
                _review_duty_item(
                    project,
                    row,
                    pointers.get(str(row.get("run_id") or "")),
                    now=now,
                    grace=grace,
                )
            )
            continue
        if recovery_classification in RECOVERY_CLASSIFICATION_DUTY_KINDS:
            kind = RECOVERY_CLASSIFICATION_DUTY_KINDS[recovery_classification]
        else:
            kind = CLASSIFICATION_DUTY_KINDS.get(classification, "")
        if kind:
            items.append(_live_item(row, kind=kind, now=now))

    items.extend(
        _sub_floor_items(
            project,
            session,
            review_module.declared_dimension_floors(config),
            now=now,
        )
    )
    items.extend(_held_worktrees(project, session, now=now))
    acknowledgements = _acknowledgements_in_force(project, now=now)
    items, acknowledged = _partition_acknowledged(items, acknowledgements)
    items.sort(
        key=lambda item: (
            -int(item["age_seconds"]),
            str(item["run_id"]),
            str(item["kind"]),
        )
    )
    acknowledged.sort(
        key=lambda item: (
            str(item["until"]),
            str(item["run_id"]),
            str(item["kind"]),
        )
    )
    closure = runs.drain(project, session=session)
    return {
        "project": project,
        "session": session,
        "obligations": items,
        "acknowledged": acknowledged,
        "summary": {
            "count": len(items),
            "oldest_age_seconds": max(
                (int(item["age_seconds"]) for item in items), default=0
            ),
            "unreconciled_runs": int(closure["unreconciled_runs"]),
        },
    }
