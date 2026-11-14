"""A landing that was promoted but never merged is finished by the next pass.

``crew complete --land`` promotes first, pushing its promote and release
commits, and merges the reviewed head afterwards. A process killed between the
two leaves a run that reads as promoted, and that a monitor draws as landed,
while its change is on no branch. Nothing else would ever notice.

So ``--land`` writes one small record per run under the crew home before it
promotes, and removes it once the merge is on the branch. The landing pass reads
that directory every pass. A record whose run has a promotion row in the ledger
and whose reviewed head is not on the primary branch is finished with the same
``merge.land`` call ``--land`` makes. A record whose run never promoted is
dropped once it is old and its run is gone. A record whose run is still live,
whose pointer carries an unanswered land attempt, which has no promoted row and
whose promotion holds no lease names a promotion that ended between the record
and its ledger row: the pass retires the interrupted attempt and then clears the
record, keeping the record whenever that retirement does not reach disk, so the
reflex promotes the run again. A
merge that fails is recorded on the record and retried on later passes, up to a
bound, so a conflicting head stops costing a pass and stays visible.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reckon.crew import runs

#: The directory under the crew home holding one record per landing in flight.
PENDING_DIRNAME = "pending-landings"

#: How many failed finishing attempts a record carries before the pass stops
#: retrying it and leaves it for a coordinator.
MAX_FINISH_ATTEMPTS = 3

#: How long a record whose run never promoted, and whose run is gone, is kept
#: before it is dropped as a landing that was refused or abandoned.
UNPROMOTED_RECORD_SECONDS = 2 * 3600

#: How long a record whose run never promoted, while its run is still live, is
#: left alone before the promotion that wrote it is treated as ended.
#:
#: A promotion claims its host lease within seconds of the record being written:
#: the only step it takes first is waiting out a deferred pick, bounded at five
#: seconds, and the lease itself goes stale after thirty. So a record this old,
#: with no live lease holder and no promoted row, names a promotion that
#: stopped between its record and its ledger row rather than one still running.
UNFINISHED_PROMOTION_SECONDS = 60.0


def pending_dir() -> Path:
    return runs.crew_home() / PENDING_DIRNAME


def _record_path(run_id: str) -> Path:
    return pending_dir() / f"{run_id}.json"


def write_pending(
    run_id: str,
    project: str,
    *,
    now: float | None = None,
    pointer: Mapping[str, Any] | None = None,
    head: str = "",
) -> None:
    """Record that ``run_id`` is about to be promoted and landed. Best-effort.

    ``head`` is the revision the landing cites, read before the promotion runs.
    The record keeps it because the ledger row and the live pointer are both
    written again by later attempts, and the pass that finishes this landing
    must merge the revision a review read rather than whichever head the run
    happened to reach before the record was read.
    """
    if not project.strip():
        return
    path = _record_path(run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "project": project,
                    "written_at": time.time() if now is None else now,
                    "attempts": 0,
                    **({"pointer": dict(pointer)} if pointer else {}),
                    **({"head": head} if head else {}),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        return


def _cited_head(record: Mapping[str, Any]) -> str:
    """The revision the landing cited when it wrote this record, or "".

    A record written before the cited head was kept carries none, and falls back
    to the head the promoted row names.
    """
    return str(record.get("head") or "").strip()


def clear_pending(run_id: str) -> None:
    """Remove a run's record once its head is on the branch. Best-effort."""
    try:
        _record_path(run_id).unlink(missing_ok=True)
    except OSError:
        return


def _settled_landing(
    run_id: str, head: str, *, repository: Path | None = None, project: str = ""
) -> str:
    """Refuse repair after a discard or remote integration, retiring its record.

    Local ancestry alone leaves a push outstanding, so only the remote primary
    can establish that no landing remains. An unreadable remote keeps the
    pending record; a recorded discard still prevents resuming its worker. A
    checkout that configures no remote has no push outstanding, and its own
    branch is the primary the landing would have moved, so there the branch's
    ancestry is the whole question.
    """
    from reckon.crew import merge, promotion

    disposition = "discarded" if promotion.discard_recorded(run_id) else ""
    if not head:
        return disposition
    try:
        repository = repository or merge.project_repository(project)
        remote = merge.head_on_remote_branch(repository, head)
        on_branch = remote["on_branch"]
        if on_branch is None and remote["cause"] == merge.REMOTE_UNCONFIGURED:
            on_branch = merge._is_ancestor(
                repository, head, merge._current_branch(repository)
            )
    except (merge.MergeError, OSError):
        return disposition
    if on_branch is True:
        try:
            pointer = runs.read_pointer(run_id)
        except runs.CrewError:
            pointer = None
        if (
            pointer is not None
            and pointer.get("pending_landing_repair")
            and _release_marked_pointer(run_id, pointer)
        ):
            merge.release_landed_worktrees(
                repository,
                str(remote["remote_head"] or head),
                head=head,
                projects=[project or str(pointer.get("project") or "")],
            )
        clear_pending(run_id)
        return "already-landed"
    return disposition


#: The pointer fields that identify the attempt a landing mark belongs to.
_ATTEMPT_FIELDS = ("attempt", "phase", "pid", "pid_start_time")


def pointer_attempt(pointer: Mapping[str, Any] | None) -> dict[str, Any]:
    """The identity of the attempt a pointer belongs to, as the mark records it."""
    pointer = pointer or {}
    return {field: pointer.get(field) for field in _ATTEMPT_FIELDS}


def mark_pending_landing_repair(
    run_id: str, pointer: Mapping[str, Any]
) -> dict[str, Any]:
    """Write ``pointer`` as the run's live pointer, marked for the landing pass.

    One writer for the mark: a promotion that could not remove its pointer and a
    merge repair that rebuilds an absent one both call it, so the fields the
    mark carries and the attempt it names are decided in one place. The attempt
    is recorded because the pointer that is live when the mark is written may
    already belong to a newer attempt, and the pass must remove only the attempt
    it finished rather than whichever pointer happens to be there later.
    """
    marked = {
        **dict(pointer),
        "pending_landing_repair": True,
        "pending_landing_repair_attempt": pointer_attempt(pointer),
    }
    runs._write_json(runs.pointer_path(run_id), marked)
    return marked


def _release_marked_pointer(run_id: str, marked: Mapping[str, Any]) -> bool:
    """Remove a marked pointer only while it is still the attempt the mark names."""
    with runs._pointer_lock(run_id):
        try:
            current = runs.read_pointer(run_id)
        except runs.CrewError:
            return False
        if pointer_attempt(current) != marked.get("pending_landing_repair_attempt"):
            return False
        runs.pointer_path(run_id).unlink(missing_ok=True)
        return True


def _read(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _promoted_row(project: str, run_id: str) -> Mapping[str, Any] | None:
    """The run's ledger row in the project's mount, or None when it has none."""
    from reckon import ledger
    from reckon.crew import merge

    try:
        repository = merge.project_repository(project)
        path = ledger.run_path(project, run_id, repository)
        if not path.is_file():
            return None
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ledger.LedgerError, merge.MergeError):
        return None
    return row if isinstance(row, Mapping) else None


def _reviewed_head(row: Mapping[str, Any]) -> str:
    promoted = str(row.get("promoted_revision") or "").strip()
    if promoted:
        return promoted
    commits = [str(sha).strip() for sha in (row.get("commits") or ())]
    return next((sha for sha in reversed(commits) if sha), "")


def _promoted_row_absent(project: str, run_id: str) -> bool:
    """Whether the project's ledger proves the run has no row.

    A row that exists but cannot be read is not evidence of absence: retiring
    the record of a promoted-but-unmerged run would strand its head off the
    branch with nothing left to finish it.
    """
    from reckon import ledger
    from reckon.crew import merge

    try:
        repository = merge.project_repository(project)
        return not ledger.run_path(project, run_id, repository).is_file()
    except (OSError, ledger.LedgerError, merge.MergeError):
        return False


def _promotion_lease_held(run_id: str) -> bool:
    """Whether a promotion of this run still holds its host lease."""
    from reckon.crew import promotion
    from reckon.crew.host_lease import HostLease

    lease = HostLease(
        runs.crew_home(), promotion._promotion_lease_name(run_id), "", 0, ""
    )
    return lease.holder() is not None


def _land_attempt_unanswered(run_id: str) -> bool:
    """Whether the run's live pointer carries a land attempt still to be settled.

    ``attempting`` names an attempt whose promotion never produced a receipt.
    ``interrupted`` names one this pass retired on an earlier sweep that then
    ended before clearing the record, so the pair is retried rather than left
    behind; both are written only here or by the landing pass that refuses to
    retry them.
    """
    try:
        pointer = runs.read_pointer(run_id)
    except runs.CrewError:
        return False
    attempt = pointer.get("admission_landing")
    return isinstance(attempt, Mapping) and attempt.get("status") in (
        "attempting",
        "interrupted",
    )


def _retire_interrupted_landing(run_id: str) -> bool:
    """Retire the land attempt a promotion that ended never answered for.

    The landing pass refuses a run whose recorded attempt produced no receipt
    and raises an obligation instead of retrying it, which is right while the
    attempt's fate is unknown. A pending-landing record is that attempt's own
    evidence that the promotion stopped before its ledger row, so the marker is
    retired and the reflex is free to promote the run again.
    """

    def retire(pointer: dict[str, Any]) -> dict[str, Any]:
        attempt = pointer.get("admission_landing")
        if isinstance(attempt, Mapping) and attempt.get("status") == "attempting":
            pointer["admission_landing"] = {**dict(attempt), "status": "interrupted"}
        return pointer

    try:
        runs._mutate_pointer(run_id, retire)
    except runs.CrewError:
        return False
    return True


def _delivered_repair(
    pointer: Mapping[str, Any],
    original: str,
    conflict: Mapping[str, Any],
    repository: Path,
) -> str:
    """Read a fresh completed repair that retains the promoted head and primary."""
    from reckon.crew import merge, recovery
    from reckon.crew.queue_landing import _delivered_revision
    from reckon.crew.recovery_repair_dispatch import _reviewed_run_is_busy

    if _reviewed_run_is_busy(pointer):
        return ""
    classified = recovery.classify_pointer(pointer)
    if classified.get("manifest_status") != "complete":
        return ""
    head, _tree = recovery._review_head_and_tree(pointer)
    delivered = _delivered_revision(classified.get("manifest_commits") or ())
    if not head or head == original or not recovery.same_revision(head, delivered):
        return ""
    primary = str(conflict.get("main_revision") or "")
    if not primary or not all(
        merge._is_ancestor(repository, parent, head) for parent in (original, primary)
    ):
        return ""
    _require_repair_gate(pointer, head, repository)
    return head


def _require_repair_gate(
    pointer: Mapping[str, Any], head: str, repository: Path
) -> None:
    """Require the fresh repair manifest's passing capture for this exact head."""
    from reckon import ledger
    from reckon.crew.promotion_evidence import (
        _commit_canonical_id,
        _recorded_exit_status,
    )
    from reckon.crew.promotion_records import (
        _command_from_gate_log,
        _default_gate_evidence_from_manifest,
        _normalized_command,
    )
    from reckon.crew.reports import _GATE_LOG_REVISION

    check, _commits = _default_gate_evidence_from_manifest(
        pointer, verdict="passed", gate_check=None, commits=()
    )
    missing = ledger.gate_check_missing_fields(check)
    if missing:
        raise runs.CrewError("repair gate is missing " + ", ".join(missing))
    if check["exit_status"] != 0:
        raise runs.CrewError("repair gate did not pass")
    try:
        text = Path(check["log_path"]).read_text(encoding="utf-8")
    except (KeyError, OSError, UnicodeError) as exc:
        raise runs.CrewError("repair gate requires a readable captured log") from exc
    if _recorded_exit_status(text) != 0:
        raise runs.CrewError("repair gate log must end with EXIT=0")
    recorded_command = _command_from_gate_log(text)
    if _normalized_command(recorded_command) != _normalized_command(check["command"]):
        declared_command = _manifest_declared_command(pointer)
        raise runs.CrewError(
            _repair_gate_command_refusal(
                str(check["log_path"]), declared_command, recorded_command
            )
        )
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    revisions = (match.group(1) for match in _GATE_LOG_REVISION.finditer(first_line))
    if not any(_commit_canonical_id(repository, rev) == head for rev in revisions):
        raise runs.CrewError(f"repair gate log must name repaired revision {head}")


def _manifest_declared_command(pointer: Mapping[str, Any]) -> str:
    """The command a repair manifest's own ``tests:`` line states, or "".

    A repair gate compares the command the manifest declares with the one its
    cited log records, and a refusal has to name both to be actionable. The
    manifest is read through the same freshness reader the gate itself used, so
    the command the refusal quotes is the one that was compared rather than a
    later rewrite of the file.
    """
    from reckon.crew.promotion_checks import _fresh_manifest

    manifest = _fresh_manifest(pointer) or {}
    return str(manifest.get("tests") or "").strip()


def _repair_gate_command_refusal(
    log_path: str, declared_command: str, recorded_command: str
) -> str:
    """Why a repair gate refused: the log it read and the two commands.

    A repair gate refuses when the command its manifest declares is not the one
    its captured log records. The refusal states the log it read, the command
    the manifest declares and the command the log records — or that the log
    records none — because the operator's whole repair is editing one of the
    two, and a message naming neither leaves them with nothing to compare.
    """
    stated = (
        f"the manifest states {declared_command!r}"
        if declared_command
        else "the manifest states no tests: command"
    )
    held = (
        f"the log records {recorded_command!r}"
        if recorded_command
        else "the log records no command line"
    )
    return (
        f"repair gate command does not match its captured log {log_path!r}: "
        f"{stated}, {held}"
    )


def retire_stuck_review_pointers(*, only_run: str = "") -> list[dict[str, Any]]:
    """Retire a review run's pending-landing mark once its ledger row is written.

    A review run has nothing to land, so a ``pending_landing_repair`` mark on it
    names a review the landing pass can never finish: ``crew complete --wait``
    refuses a review, and the obligations hook lists the pointer as
    promotable-stale for good. The ledger row is the promotion's own evidence
    that it finished, so a marked review pointer beside a present row is retired
    here. Only the attempt the mark names is removed, the row is not rewritten,
    and nothing is re-promoted. The run's pending-landing record is cleared in
    the same step, so the pass never re-attempts a landing for the review.
    """
    from reckon.crew.promotion_requests import COMMAND_REPLY, PENDING, read_promotion
    from reckon.crew.recovery_review_delivery import _is_review_run

    retired: list[dict[str, Any]] = []
    for pointer in runs.list_live():
        if not pointer.get("pending_landing_repair"):
            continue
        run_id = str(pointer.get("run_id") or "")
        project = str(pointer.get("project") or "")
        if not run_id or not project or (only_run and run_id != only_run):
            continue
        if not _is_review_run(pointer):
            continue
        if (
            COMMAND_REPLY.get() is None
            and read_promotion(run_id).get("phase") in PENDING
        ):
            continue
        if _promoted_row(project, run_id) is None:
            continue
        if _promotion_lease_held(run_id):
            continue
        if not _release_marked_pointer(run_id, pointer):
            continue
        clear_pending(run_id)
        retired.append({"run_id": run_id, "status": "retired-review-landing"})
    return retired


def finish_pending_landings(
    *, now: float | None = None, land=None, live_run_ids=None, only_run: str = ""
) -> list[dict[str, Any]]:
    """Finish every promoted-but-unmerged landing, returning one row per record.

    ``land`` is ``merge.land``-shaped and ``live_run_ids`` the ids with a live
    pointer; both are parameters so a test can drive the pass.
    """
    from reckon.crew import merge

    moment = time.time() if now is None else now
    lander = land or merge.land
    results: list[dict[str, Any]] = retire_stuck_review_pointers(only_run=only_run)
    directory = pending_dir()
    if not directory.is_dir():
        return results
    live = (
        set(live_run_ids)
        if live_run_ids is not None
        else {str(pointer.get("run_id") or "") for pointer in runs.list_live()}
    )
    for path in sorted(directory.glob("*.json")):
        record = _read(path)
        if record is None:
            continue
        run_id = str(record.get("run_id") or "")
        project = str(record.get("project") or "")
        if not run_id or not project or (only_run and run_id != only_run):
            continue
        from reckon.crew.promotion_requests import (
            COMMAND_REPLY,
            PENDING,
            read_promotion,
        )

        if (
            COMMAND_REPLY.get() is None
            and read_promotion(run_id).get("phase") in PENDING
        ):
            results.append({"run_id": run_id, "status": "promotion-requested"})
            continue
        automatic_gate = (record.get("pointer") or {}).get("automatic_landing_gate")
        if automatic_gate and _promotion_lease_held(run_id):
            results.append({"run_id": run_id, "status": "promotion-running"})
            continue
        row = _promoted_row(project, run_id)
        cited = _cited_head(record)
        head = cited or (_reviewed_head(row) if row else "")
        settled = _settled_landing(run_id, head, project=project)
        if settled:
            results.append({"run_id": run_id, "status": settled, "head": head})
            continue
        if int(record.get("attempts") or 0) >= MAX_FINISH_ATTEMPTS and not record.get(
            "merge_conflict"
        ):
            results.append({"run_id": run_id, "status": "gave-up"})
            continue
        if row is None:
            age = moment - float(record.get("written_at") or moment)
            if run_id not in live:
                if age > UNPROMOTED_RECORD_SECONDS:
                    clear_pending(run_id)
                    results.append({"run_id": run_id, "status": "dropped-unpromoted"})
                continue
            # A promotion that ended between writing this record and its ledger
            # row leaves a live pointer carrying an unanswered land attempt, no
            # promoted row and no lease. Left alone it is a hold nothing lifts:
            # the record outlives every pass and the attempt refuses every
            # landing. Only that unanswered attempt is retried, because without
            # one the record blocks nothing and the reflex already re-promotes.
            # An attempt a previous sweep retired but could not clear its record
            # for is retried the same way, so the record does not outlive it.
            #
            # The record is cleared only after the attempt is retired, because the
            # record is the only evidence that this promotion stopped short. A
            # pass that ends between the two steps — or one whose pointer write
            # fails — therefore still leaves a record for the next pass to
            # retry; clearing first would leave the live pointer's attempt
            # unanswered with nothing left to read it.
            if (
                age > UNFINISHED_PROMOTION_SECONDS
                and _promoted_row_absent(project, run_id)
                and not _promotion_lease_held(run_id)
                and _land_attempt_unanswered(run_id)
            ):
                retired = _retire_interrupted_landing(run_id)
                if not retired:
                    # The pointer could not be written, so the record is the
                    # only evidence left that this promotion stopped short.
                    # Keep it: clearing it here would leave an attempt nothing
                    # can retire and nothing left to read it from.
                    results.append(
                        {
                            "run_id": run_id,
                            "status": "attempt-not-retired",
                            "attempt_retired": False,
                        }
                    )
                    continue
                clear_pending(run_id)
                results.append(
                    {
                        "run_id": run_id,
                        "status": "retried-unfinished",
                        "attempt_retired": retired,
                    }
                )
            continue
        conflict = record.get("merge_conflict") or {}
        pointer = None
        # A live pointer is read even without a recorded conflict, because the
        # promotion that could not remove it also reads it here: a marked
        # pointer names the attempt this landing finishes, and an unmarked one
        # the attempt a resume wrote after it moved. A record carrying the
        # pointer ``--land`` held is the promotion's own claim that it owed a
        # merge, so the merge runs even after the mark was overwritten; the
        # pointer is released only where a mark names it, and a record written
        # without a pointer leaves a live unmarked one to its own attempt.
        if conflict or run_id in live:
            try:
                pointer = runs.read_pointer(run_id)
            except runs.CrewError:
                pointer = None
        if (
            run_id in live
            and not (pointer or {}).get("pending_landing_repair")
            and not record.get("pointer")
        ):
            continue
        head = cited or _reviewed_head(row)
        if not head:
            continue
        repository = merge.project_repository(project)
        if conflict:
            try:
                repaired = (
                    _delivered_repair(pointer, head, conflict, repository)
                    if pointer
                    else ""
                )
            except runs.CrewError as exc:
                results.append(
                    {
                        "run_id": run_id,
                        "status": "repair-gate-refused",
                        "error": str(exc),
                    }
                )
                continue
            if not repaired:
                from reckon.crew.recovery_repair_dispatch import dispatch_merge_repair

                dispatch_merge_repair(
                    record.get("pointer") or row,
                    head=head,
                    primary=str(conflict.get("main_revision") or ""),
                    paths=conflict.get("paths") or (),
                    promoted=True,
                )
                results.append({"run_id": run_id, "status": "awaiting-merge-repair"})
                continue
            head = repaired

        replay_pointer = record.get("pointer") or {}

        def verify_gate(
            checkout,
            _base,
            _head,
            revision,
            pointer=replay_pointer,
            gate=automatic_gate,
        ):
            from reckon.crew.review_landing import replay_candidate

            checked = replay_candidate(
                {**pointer, "repo": str(checkout)}, revision, gate
            )
            if not checked["ok"]:
                raise merge.MergeError(checked["reason"])

        try:
            repository = merge.project_repository(project)
            landed = lander(
                repository,
                head=head,
                message=(
                    f"land({run_id}): finish a landing interrupted after its promotion"
                ),
                body=(
                    f"Run {run_id} was promoted with --land, but its process "
                    "ended before the reviewed head was merged. The landing "
                    f"pass finishes that merge of {head}."
                ),
                **({"verify": verify_gate} if automatic_gate else {}),
            )
        except merge.MergeConflictError as exc:
            from reckon.crew.recovery_repair_dispatch import dispatch_merge_repair

            repair = dispatch_merge_repair(
                record.get("pointer") or row,
                head=exc.head,
                primary=exc.base,
                paths=exc.paths,
                promoted=True,
            )
            results.append(
                {
                    "run_id": run_id,
                    "status": "merge-conflict",
                    "paths": exc.paths,
                    "main_revision": exc.base,
                    "repair": repair,
                }
            )
            continue
        except merge.MergeError as exc:
            record["attempts"] = int(record.get("attempts") or 0) + 1
            record["last_error"] = str(exc)
            with contextlib.suppress(OSError):
                path.write_text(json.dumps(record), encoding="utf-8")
            results.append({"run_id": run_id, "status": "failed", "error": str(exc)})
            continue
        # Only the attempt the mark names is released: a pointer a newer attempt
        # has since written belongs to work still in flight, and removing it or
        # its tree would strand that attempt mid-run.
        if (
            pointer is not None
            and pointer.get("pending_landing_repair")
            and _release_marked_pointer(run_id, pointer)
        ):
            merge.release_landed_worktrees(
                repository, str(landed["commit"]), head=head, projects=[project]
            )
        if automatic_gate:
            from reckon.crew.review_landing import finish_landing

            refusal = finish_landing(record["pointer"], run_id)
            if refusal:
                results.append(
                    {
                        "run_id": run_id,
                        "status": "declaration-refused",
                        "error": refusal,
                    }
                )
                continue
        clear_pending(run_id)
        results.append(
            {
                "run_id": run_id,
                "status": "finished",
                "head": head,
                "commit": str((landed or {}).get("commit") or ""),
            }
        )
    return results
