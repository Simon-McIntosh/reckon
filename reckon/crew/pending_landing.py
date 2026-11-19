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
from collections.abc import Callable, Mapping
from contextvars import ContextVar
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

# The completion callback still owns publication. A group defers that callback
# until every member's ordinary promotion has succeeded.
LANDING_GROUP: ContextVar[dict[str, Any] | None] = ContextVar(
    "landing_group", default=None
)


def group_path(run_id: str) -> Path:
    return pending_dir() / "groups" / f"{run_id}.json"


def save_group(group: Mapping[str, Any]) -> None:
    from reckon._store import write_json_atomically

    write_json_atomically(group_path(str(group["run_id"])), dict(group))


def _release_integrated_pointer(
    pointer: Mapping[str, Any],
    repository: Path,
    primary: str,
    row: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any], str]:
    """Record a safe workspace release before retiring the unchanged pointer."""
    from reckon.crew import promotion, promotion_release
    from reckon.crew.review_landing import finish_landing

    run_id = str(pointer["run_id"])
    project = str(pointer["project"])
    refusal = ""
    outcome: dict[str, Any] = {}

    def release(current: Mapping[str, Any]) -> bool:
        nonlocal refusal, outcome
        if dict(current) != pointer:
            refusal = "the live pointer changed before workspace release"
            return False
        refusal = landed_workspace_refusal(current, repository, primary)
        if refusal:
            return False
        outcome = promotion_release._release_run_workspace(
            current,
            integration_repo=repository,
            integration_commit=primary,
            gate=str(row.get("gate") or "") if row is not None else "passed",
        )
        if row is not None:
            outcome, recorded = promotion._record_release_on_ledger(
                project=project,
                root=repository,
                run_id=run_id,
                release=outcome,
                checkout=repository,
                after_merge=True,
            )
            if (
                recorded is None
                or (outcome.get("landing_check") or {}).get("ok") is False
            ):
                refusal = "the workspace outcome could not be recorded"
                return False
        if not outcome.get("worktree_released"):
            refusal = str(
                outcome.get("worktree_withheld") or "workspace release withheld"
            )
            return False
        refusal = finish_landing(current, run_id)
        return not refusal

    retired = retire_pointer_if(run_id, release)
    return retired, outcome, refusal


def _release_integrated_group_member(
    run_id: str, member: Mapping[str, Any], primary: str
) -> None:
    """Release an integrated member without replaying its stored pointer."""
    from reckon.crew import promotion
    from reckon.crew.recovery_repair_dispatch import _reviewed_run_is_busy

    try:
        pointer = runs.read_pointer(run_id)
    except runs.CrewError:
        if runs.pointer_path(run_id).exists():
            raise
        clear_pending(run_id)
        return
    busy = _reviewed_run_is_busy(pointer)
    if busy:
        raise runs.CrewError(f"group member {run_id}: {busy}")
    repository = Path(str(member["pointer"]["repo"]))
    row = _promoted_row(str(member["pointer"]["project"]), run_id)
    retired, _outcome, refusal = _release_integrated_pointer(
        pointer, repository, primary, row
    )
    if retired == POINTER_HELD:
        raise runs.CrewError(f"group member {run_id}: {refusal or 'pointer held'}")
    clear_pending(run_id)
    promotion._release_waiters_of_the_landed_section(pointer)


def retire_integrated_group_members(group: dict[str, Any]) -> dict[str, Any] | None:
    """Remove primary ancestors before repair, measurement or pointer replay."""
    from reckon.crew import merge, queue_landing

    for run_id, member in list(group["members"].items()):
        pointer = member["pointer"]
        primary = queue_landing._primary_revision(pointer)
        if not merge._is_ancestor(
            Path(str(pointer["repo"])), str(member["head"]), primary
        ):
            continue
        _release_integrated_group_member(run_id, member, primary)
        group.setdefault("finished_members", {})[run_id] = {
            "head": member["head"],
            "primary": primary,
            "status": "already-landed",
        }
        del group["members"][run_id]
        (group.get("merge_repairs") or {}).pop(run_id, None)
        for field in ("head", "primary", "measurements", "refusal"):
            group.pop(field, None)
        save_group(group)
    if group["members"]:
        return None
    finished = group.get("finished_members") or {}
    outcome = {
        "ok": True,
        "run_id": group["run_id"],
        "members": list(finished),
        "candidate": queue_landing._primary_revision(
            group["request"]["options"]["members"][0]["pointer"]
        ),
        "commits": [member["head"] for member in finished.values()],
        "outcome": "all member heads are on the primary branch",
    }
    runs._write_json(runs.run_dir(str(group["run_id"])) / "landing-group.json", outcome)
    group_path(str(group["run_id"])).unlink()
    return outcome


def refresh_group_repairs(group: dict[str, Any]) -> None:
    """Accept completed member repairs through the ordinary fresh-gate reader."""
    from reckon.crew.recovery_repair_dispatch import dispatch_merge_repair

    for run_id, conflict in list((group.get("merge_repairs") or {}).items()):
        member = group["members"][run_id]
        try:
            pointer = runs.read_pointer(run_id)
        except runs.CrewError:
            if runs.pointer_path(run_id).exists():
                raise
            # The group journal can precede the repair owner's pointer rebuild.
            pointer = member["pointer"]
        repaired = _delivered_repair(
            pointer,
            str(conflict["head"]),
            conflict,
            Path(str(pointer["repo"])),
        )
        if not repaired:
            repair = dispatch_merge_repair(
                pointer,
                head=str(conflict["head"]),
                primary=str(conflict["main_revision"]),
                paths=conflict["paths"],
                promoted=bool(member.get("promotion")),
            )
            raise runs.CrewError(
                f"group member {run_id} is awaiting merge repair: "
                + str(repair.get("reason") or "repair has not delivered")
            )
        if member.get("promotion"):
            member.setdefault("promoted_head", member["head"])
        member.update(head=repaired, pointer=pointer)
        member["options"]["commits"] = list(
            dict.fromkeys([*(member["options"].get("commits") or ()), repaired])
        )
        del group["merge_repairs"][run_id]
        for field in ("head", "primary", "measurements", "refusal"):
            group.pop(field, None)
        save_group(group)


def finish_landing_groups(*, only_run: str = "") -> list[dict[str, Any]]:
    """Resume the same queued group after interruption, including its promotions."""
    from reckon.crew.promotion_requests import PENDING, read_promotion
    from reckon.crew_run_commands import _complete_landing_group

    results = []
    for path in sorted((pending_dir() / "groups").glob("*.json")):
        group = _read(path)
        if not group or (
            only_run
            and only_run not in group["members"]
            and only_run not in (group.get("finished_members") or {})
        ):
            continue
        run_id = str(group["run_id"])
        if read_promotion(run_id).get("phase") in PENDING:
            continue
        try:
            result = _complete_landing_group(group["request"])
        except Exception as exc:  # noqa: BLE001 - retain failed groups for recovery
            results.append(
                {"run_id": run_id, "status": "group-refused", "error": str(exc)}
            )
            continue
        results.extend(
            {
                "run_id": member,
                "status": "finished" if result.get("ok") else "group-refused",
            }
            for member in result.get("members", group["members"])
        )
    return results


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
        if pointer is not None and _release_journal_pointer(
            run_id, pointer, _read(_record_path(run_id)) or {}
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


#: The pointer fields that name one attempt of a run. The phase is not one of
#: them: a promotion request rewrites it while the attempt it belongs to keeps
#: running, so an attempt's identity is stable across a station mark.
ATTEMPT_FIELDS = ("attempt", "attempt_kind", "pid", "pid_start_time")

#: What a landing mark records: the attempt plus the phase it was written
#: against, so a station rewrite of the phase leaves the mark naming the pointer
#: state it was made for rather than whichever state is live when it is read.
#: The shape is the one marks already on disk carry, so it keeps the fields
#: those records were written with rather than growing with the identity above.
MARKED_FIELDS = ("attempt", "phase", "pid", "pid_start_time")

#: What a compare-under-lock-and-retire found.
POINTER_RETIRED = "retired"
POINTER_ABSENT = "absent"
POINTER_HELD = "held"


def _identity(
    pointer: Mapping[str, Any] | None, fields: tuple[str, ...]
) -> dict[str, Any]:
    pointer = pointer or {}
    return {field: pointer.get(field) for field in fields}


def attempt_identity(pointer: Mapping[str, Any] | None) -> dict[str, Any]:
    """``pointer``'s attempt identity, ignoring the phase the station rewrites."""
    return _identity(pointer, ATTEMPT_FIELDS)


def marked_identity(pointer: Mapping[str, Any] | None) -> dict[str, Any]:
    """The identity a landing mark names, including the phase it was made for."""
    return _identity(pointer, MARKED_FIELDS)


def pointer_attempt(pointer: Mapping[str, Any] | None) -> dict[str, Any]:
    """The identity of the attempt a pointer belongs to, as the mark records it."""
    return marked_identity(pointer)


def retire_pointer_if(run_id: str, matches: Callable[[Mapping[str, Any]], bool]) -> str:
    """Retire the live pointer under its lock while ``matches`` accepts it.

    One owner for compare-under-lock-and-retire: a promotion retiring the
    attempt it read, a review retiring a pointer that moved, and the landing
    pass releasing a review's or a repair's mark all reach the pointer here, so
    the lock, the absent case and an unlink that fails are decided once.
    """
    with runs._pointer_lock(run_id):
        path = runs.pointer_path(run_id)
        if not path.exists():
            return POINTER_ABSENT
        try:
            current = runs.read_pointer(run_id)
        except runs.CrewError:
            return POINTER_HELD
        if not matches(current):
            return POINTER_HELD
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return POINTER_HELD
        return POINTER_RETIRED


def retire_promoted_pointer(pointer: Mapping[str, Any]) -> str:
    """Retire a proved promotion with no journal, group or live workspace owner.

    Retirement never creates merge work. Journals keep their cited head, and
    existing workspaces finish through the integrated release path so its
    safety checks, receipts and section waiters precede pointer removal.
    """
    run_id = str(pointer.get("run_id") or "")
    project = str(pointer.get("project") or "")
    if not run_id or not project or pointer.get("landing_group"):
        return POINTER_HELD

    def promoted(current: Mapping[str, Any]) -> bool:
        if dict(current) != pointer or _record_path(run_id).exists():
            return False
        workspace = str(current.get("worktree") or "")
        if workspace and Path(workspace).exists():
            return False
        row = _promoted_row(project, run_id)
        if row is None or row.get("run_id") != run_id:
            return False
        return bool(row.get("promoted_revision") or row.get("promoted_by"))

    return retire_pointer_if(run_id, promoted)


def retire_promoted_pointers(
    *,
    project: str | None = None,
    session: str | None = None,
    only_run: str = "",
    release_workspaces: bool = True,
) -> list[dict[str, Any]]:
    """Release integrated workspaces, then retire ownerless promoted pointers."""
    from reckon.node_exclusive_lock import LockHeld, exclusive_lock

    retired = (
        release_merged_promotions(project=project, session=session, only_run=only_run)
        if release_workspaces
        else []
    )
    live = runs.list_live() if project is None else runs.list_live(project=project)
    selected = [
        pointer
        for pointer in live
        if (not only_run or pointer.get("run_id") == only_run)
        and (session is None or pointer.get("session") == session)
    ]
    groups = {
        str(pointer["landing_group"])
        for pointer in selected
        if pointer.get("landing_group")
    }
    for group_id in sorted(groups):
        path = group_path(group_id)
        try:
            with exclusive_lock(path.with_suffix(".lock"), blocking=False):
                group = _read(path)
                if group is not None:
                    present = {
                        run_id
                        for run_id in group["members"]
                        if runs.pointer_path(run_id).exists()
                    }
                    retire_integrated_group_members(group)
                    retired.extend(
                        {"run_id": run_id, "status": "retired-promoted-pointer"}
                        for run_id in sorted(present)
                        if not runs.pointer_path(run_id).exists()
                    )
        except LockHeld:
            continue
        except (runs.CrewError, OSError, ValueError) as exc:
            retired.append(
                {"run_id": group_id, "status": "group-refused", "error": str(exc)}
            )
    retired.extend(
        {"run_id": str(pointer["run_id"]), "status": "retired-promoted-pointer"}
        for pointer in selected
        if retire_promoted_pointer(pointer) == POINTER_RETIRED
    )
    return retired


def promoted_pointer_write_refusal(run_id: str, pointer: Mapping[str, Any]) -> str:
    """Name the journal requiring intervention rather than recreate a promoted run."""
    project = str(pointer.get("project") or "")
    if _promoted_row_absent(project, run_id):
        return ""
    state = (
        "is promoted"
        if _promoted_row(project, run_id) is not None
        else "has unreadable promotion evidence; row absence is unproved"
    )
    group_id = str(pointer.get("landing_group") or "")
    path = (
        group_path(group_id)
        if group_id and group_path(group_id).exists()
        else _record_path(run_id)
    )
    return f"run {run_id} {state}; pending-landing record {path} cannot write a live pointer"


def mark_pending_landing_repair(
    run_id: str, pointer: Mapping[str, Any], *, opening_repair: bool = False
) -> dict[str, Any]:
    """Write ``pointer`` as the run's live pointer, marked for the landing pass.

    One writer for the mark: a promotion that could not remove its pointer and a
    merge repair that rebuilds an absent one both call it, so the fields the
    mark carries and the attempt it names are decided in one place. The attempt
    is recorded because the pointer that is live when the mark is written may
    already belong to a newer attempt, and the pass must remove only the attempt
    it finished rather than whichever pointer happens to be there later.

    ``opening_repair`` is the one caller a promoted run may be rebuilt for: a
    delivery that was promoted and then met a merge conflict still owes that
    merge, and its retained worker is the only owner of the conflict, so the
    round it opens needs the pointer back to be resumed. Every other write is
    the journal replay this refusal exists for, where recreating the pointer of
    a promoted run would let a landing that already ran be read as live again.
    """
    if opening_repair and _record_path(run_id).is_file():
        refusal = ""
    else:
        refusal = promoted_pointer_write_refusal(run_id, pointer)
    if refusal:
        raise runs.CrewError(refusal)
    marked = {
        **dict(pointer),
        "pending_landing_repair": True,
        "pending_landing_repair_attempt": pointer_attempt(pointer),
    }
    runs._write_json(runs.pointer_path(run_id), marked)
    return marked


def _release_marked_pointer(run_id: str, marked: Mapping[str, Any]) -> bool:
    """Remove a marked pointer only while it is still the attempt the mark names."""
    recorded = marked.get("pending_landing_repair_attempt")
    return (
        retire_pointer_if(run_id, lambda current: marked_identity(current) == recorded)
        == POINTER_RETIRED
    )


def _release_journal_pointer(
    run_id: str, pointer: Mapping[str, Any], record: Mapping[str, Any]
) -> bool:
    """Retire only the attempt captured by the journal or an existing legacy mark."""
    expected = record.get("retained_attempt") or pointer.get(
        "pending_landing_repair_attempt"
    )
    if not isinstance(expected, Mapping):
        return False
    return (
        retire_pointer_if(
            run_id, lambda current: marked_identity(current) == dict(expected)
        )
        == POINTER_RETIRED
    )


def _read(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _promotion_evidence(
    project: str, run_id: str, *, root: str | Path | None = None
) -> Mapping[str, Any] | None:
    """Read the authoritative store before its legacy file, refusing unreadability."""
    from reckon import ledger, run_store
    from reckon.crew import merge

    rows = run_store.stored_run_rows(project, run_ids=[run_id])
    if rows:
        return rows[0]
    repository = merge.project_repository(project) if root is None else Path(root)
    path = ledger.run_path(project, run_id, repository)
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(row, Mapping) or row.get("run_id") != run_id:
        raise ValueError(f"invalid promotion evidence at {path}")
    return row


def _promoted_row(
    project: str, run_id: str, *, root: str | Path | None = None
) -> Mapping[str, Any] | None:
    """The run's promoted row, or None when absent or unreadable."""
    from reckon import ledger
    from reckon.crew import merge

    try:
        return _promotion_evidence(project, run_id, root=root)
    except (OSError, ValueError, RuntimeError, ledger.LedgerError, merge.MergeError):
        return None


def _reviewed_head(row: Mapping[str, Any]) -> str:
    promoted = str(row.get("promoted_revision") or "").strip()
    if promoted:
        return promoted
    commits = [str(sha).strip() for sha in (row.get("commits") or ())]
    return next((sha for sha in reversed(commits) if sha), "")


def _promoted_row_absent(project: str, run_id: str) -> bool:
    """Whether successful store and legacy-file reads prove the run has no row.

    A row that exists but cannot be read is not evidence of absence: retiring
    the record of a promoted-but-unmerged run would strand its head off the
    branch with nothing left to finish it.
    """
    from reckon import ledger
    from reckon.crew import merge

    try:
        return _promotion_evidence(project, run_id) is None
    except (OSError, ValueError, RuntimeError, ledger.LedgerError, merge.MergeError):
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
    _require_repair_gate(pointer, head, repository, primary)
    return head


def pending_repair_head(pointer: Mapping[str, Any]) -> str:
    """The completed, gated head a promoted merge repair now owes a review of."""
    run_id = str(pointer.get("run_id") or "")
    if not run_id:
        return ""
    pending = _read(_record_path(run_id)) or {}
    conflict = pending.get("merge_conflict") or {}
    if not conflict:
        return ""
    original = str(conflict.get("head") or _cited_head(pending))
    try:
        return _delivered_repair(
            pointer, original, conflict, Path(str(pointer.get("repo") or ""))
        )
    except runs.CrewError:
        return ""


def _recorded_review_waiver(pointer: Mapping[str, Any]) -> str:
    """The reason the delivery's own promotion waived its review, or "".

    A run promoted under a waiver landed without the review its delivery owed,
    on the waiver's stated reason. The repair round then finishes that landing's
    merge and owes no review the promotion did not: a repaired head held for a
    review the operator already waived is the run held by a rule its own
    promotion record answered.
    """
    project = str(pointer.get("project") or "")
    run_id = str(pointer.get("run_id") or "")
    if not project or not run_id:
        return ""
    row = _promoted_row(project, run_id) or {}
    waiver = row.get("review_waiver")
    if not isinstance(waiver, Mapping):
        return ""
    return str(waiver.get("reason") or "").strip()


def _repair_review_wait(pointer: Mapping[str, Any], head: str) -> dict[str, Any] | None:
    """Read every review owed at the repair head through the lander's readers."""
    from reckon.crew import queue_landing
    from reckon.crew.recovery_review_dispatch import dispatch_review_for_run

    run_id, project = str(pointer["run_id"]), str(pointer["project"])
    tree = Path(str(pointer.get("worktree") or pointer["repo"]))
    if _recorded_review_waiver(pointer):
        return None
    for tier in queue_landing._owed_review_tiers(pointer):
        try:
            review, stale = queue_landing._review_for_tier(
                project, run_id, head, tree, tier
            )
        except (OSError, ValueError) as exc:
            review, stale, error = None, "", str(exc)
        else:
            error = ""
        added = queue_landing._review_added_failure_guard(review)
        if (
            not stale
            and not added
            and queue_landing._review_tier_is_complete(tier, review)
        ):
            continue
        report = {}
        if review is None and not error:
            report = dispatch_review_for_run(pointer)
        refused = review is not None or bool(error) or bool(report.get("refused"))
        reason = error or str(report.get("reason") or "")
        if review is not None:
            from reckon.crew import promotion_checks, repair

            findings = promotion_checks._describe_blocking_findings(
                repair.blocking_findings(review)
            )
            reason = (
                added or findings or f"the stored {tier} review is incomplete at {head}"
            )
        guard = f"repair {tier} review: {reason or 'awaiting review of ' + head}"
        current = runs.read_pointer(run_id)
        if refused:
            queue_landing._record_attempt(
                run_id,
                head=head,
                guard=guard,
                status="refused",
                code_stamp=queue_landing._landing_code_stamp(),
                review_stamp=queue_landing._review_stamp(review),
                previous=current.get("admission_landing"),
            )
        else:
            queue_landing._record_awaiting_review(
                run_id,
                current,
                head=head,
                code_stamp=queue_landing._landing_code_stamp(),
                review_stamp=queue_landing._review_stamp(review),
                tier=tier,
                stale_head=stale,
                read_error=error,
            )
        return {
            "run_id": run_id,
            "session": str(pointer.get("session") or ""),
            "head": head,
            "status": "review-refused" if refused else "awaiting-review",
            "guard": guard,
        }
    return None


def _require_repair_gate(
    pointer: Mapping[str, Any], head: str, repository: Path, primary: str = ""
) -> None:
    """Require the fresh repair manifest's capture to add no failing test.

    A gate that exited zero passes on its own receipt. A red gate is judged
    against the same command's own arm at the primary revision the merge
    conflict named, and refuses only on a failing id the primary does not
    fail. Absolute green made a repair whose gate files were already red on
    the primary branch unlandable however clean its own change, which is the
    opposite of what a repair round exists for: the fault it must clear is
    the one this run introduced, and the primary's redness is not this run's
    to fix. The delivery measure already decides a landing this way, and the
    comparison here reuses its reader rather than restating the rule.

    The primary arm is the fresh manifest's ``baseline_suite``: it is the arm
    the run records for the revision it measured before its change, and a
    repair round records it at the primary revision the conflict names. So
    the two sides are one command at two revisions. A refusal names the added
    ids and both logs, because the remedy is either the primary arm or the
    head's new failure, and a message naming neither leaves nothing to
    compare. ``primary`` is empty only for a caller holding no conflict
    record, and such a caller has no primary to compare against.
    """
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
    try:
        text = Path(check["log_path"]).read_text(encoding="utf-8")
    except (KeyError, OSError, UnicodeError) as exc:
        raise runs.CrewError("repair gate requires a readable captured log") from exc
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
    recorded_exit = _recorded_exit_status(text)
    if recorded_exit is None:
        raise runs.CrewError("repair gate log must end with EXIT=0")
    if recorded_exit == 0 and check["exit_status"] == 0:
        return
    if check["exit_status"] == 0:
        raise runs.CrewError("repair gate did not pass")
    _require_no_added_repair_failures(pointer, primary, text, check, repository)


def _require_no_added_repair_failures(
    pointer: Mapping[str, Any],
    primary: str,
    head_text: str,
    check: Mapping[str, Any],
    repository: Path,
) -> None:
    """Refuse a red repair gate only on a failing id the primary does not fail.

    The head's failing ids come from the log the gate read; the primary's from
    its own recorded arm, read again from that arm's log rather than taken from
    its ``failure_ids`` summary. Both sides go through the review reader's
    comparison, which knows that one test is written as a bare name in a
    summary and as a qualified node id in a ``FAILED`` line.
    """
    from reckon.crew import review

    primary_log, primary_ids = _repair_primary_arm(pointer, primary, check, repository)
    head_ids = review.gate_log_failure_ids(head_text)
    if head_ids is None:
        raise runs.CrewError(
            "repair gate log records no failing test id in a form the gate can "
            f"read, so the failures it added against primary {primary} are "
            f"unmeasured; log {check['log_path']!r}"
        )
    added = sorted(review.failure_ids_covered(primary_ids, head_ids))
    if not added:
        return
    raise runs.CrewError(
        f"repair gate adds failures primary {primary} does not fail: "
        f"{', '.join(added)}; head log {str(check['log_path'])!r}, "
        f"primary log {str(primary_log)!r}"
    )


def _repair_primary_arm(
    pointer: Mapping[str, Any],
    primary: str,
    check: Mapping[str, Any],
    repository: Path,
) -> tuple[Path, set[str]]:
    """The primary revision's own arm for the gate command, and its failures.

    The arm is read from the fresh manifest's ``baseline_suite``, which must
    name the conflict's primary revision, run the same command and declare
    its run complete. Each is refused by name rather than defaulted, because
    a comparison against an arm taken somewhere else is not the comparison
    the refusal reports: it would admit a head whose failures the primary
    never ran, and the log it names would be evidence of the wrong tree.
    """
    from reckon.crew import review
    from reckon.crew.promotion_checks import _fresh_manifest
    from reckon.crew.promotion_evidence import _commit_canonical_id
    from reckon.crew.promotion_records import (
        _command_from_gate_log,
        _manifest_relative_path,
        _normalized_command,
    )

    if not primary:
        raise runs.CrewError(
            "repair gate is red and the conflict records no primary revision to "
            "compare it with, so the failures it added are unmeasured"
        )
    manifest = _fresh_manifest(pointer) or {}
    manifest_path = str(pointer.get("manifest_path") or "")
    arm = manifest.get("baseline_suite")
    if not isinstance(arm, Mapping):
        raise runs.CrewError(
            "repair gate is red and the manifest records no baseline_suite arm "
            f"at primary {primary}, so the failures it added are unmeasured"
        )
    revision = str(arm.get("revision") or "").strip()
    named = _commit_canonical_id(repository, revision) if revision else None
    if named is None or named != _commit_canonical_id(repository, primary):
        raise runs.CrewError(
            "repair gate is red and its baseline_suite arm names "
            f"{revision or 'no revision'}, not primary {primary}, so the "
            "failures it added are unmeasured"
        )
    if arm.get("completed") is not True:
        raise runs.CrewError(
            "repair gate is red and its baseline_suite arm does not declare its "
            f"run complete, so its failures at primary {primary} are unmeasured"
        )
    declared_log = str(arm.get("log_path") or "").strip()
    if not declared_log:
        raise runs.CrewError(
            "repair gate is red and its baseline_suite arm names no log, so the "
            f"failures it added at primary {primary} are unmeasured"
        )
    log_path = _manifest_relative_path(declared_log, manifest_path=manifest_path)
    try:
        arm_text = log_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise runs.CrewError(
            "repair gate requires a readable primary arm log: "
            f"{str(log_path)!r} could not be read"
        ) from exc
    arm_command = _command_from_gate_log(arm_text) or str(arm.get("command") or "")
    if _normalized_command(arm_command) != _normalized_command(check["command"]):
        raise runs.CrewError(
            "repair gate is red and its primary arm log records "
            f"{arm_command or 'no command'!r} rather than the gate's "
            f"{str(check['command'])!r}, so the two arms are not one command"
        )
    ids = review.gate_log_failure_ids(arm_text)
    if ids is None:
        raise runs.CrewError(
            "repair gate requires a readable primary arm log: "
            f"{str(log_path)!r} records no failing test id in a form the gate "
            f"can read, so the failures it added at primary {primary} are "
            "unmeasured"
        )
    return log_path, ids


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


def landed_workspace_refusal(
    pointer: Mapping[str, Any], repository: Path, primary: str
) -> str:
    """Read the evidence that must survive before a merged run can be retired."""
    from reckon.crew import merge, routing
    from reckon.crew.promotion_checks import _fresh_manifest
    from reckon.crew.promotion_evidence import _commit_canonical_id

    manifest = _fresh_manifest(pointer)
    if manifest is None:
        return "the manifest is unreadable or does not belong to this attempt"
    if manifest.get("status") != "complete":
        return "the manifest does not declare a completed delivery"
    for citation in manifest.get("commits") or ():
        resolved = _commit_canonical_id(repository, str(citation))
        if not resolved:
            return f"manifest commit {citation!r} does not resolve"
        if not merge._is_ancestor(repository, resolved, primary):
            return f"manifest commit {citation!r} is not on the primary branch"
    value = str(pointer.get("worktree") or "")
    if not value or not Path(value).exists():
        return ""
    tree = Path(value)
    claims = [
        run_id
        for run_id in routing._live_pointer_worktrees().get(tree.resolve(), ())
        if run_id != pointer.get("run_id")
    ]
    inspected = routing._inspect_workspace(repository, tree, primary, claims)
    if inspected["dirty"]:
        return "worktree has uncommitted files: " + "; ".join(inspected["dirty"])
    if inspected["classification"] != "integrated":
        return "worktree cannot be released: " + str(inspected["classification"])
    return ""


def _recorded_process_proved_dead(pointer: Mapping[str, Any]) -> bool:
    """Whether the process a promoted run records is proved to have stopped.

    One read, through the crew's own liveness primitive, so the arm that
    answered is the one every other caller sees: the placed run's scheduler job
    first, then the pid the record wrote under the kernel start tick taken at
    registration, then the session walk. Only ``dead`` is proof; a record
    naming no process, a record whose pid cannot be read, and a scheduler that
    cannot be queried all leave the answer unknown, and unknown is not proof
    that a turn has finished.
    """
    from reckon.crew import process_liveness

    return process_liveness.record_process_liveness(pointer).alive is False


def _measured_workspace_refusal(
    pointer: Mapping[str, Any], repository: Path, head: str
) -> str:
    """What a promoted measurement run's worktree holds that must be kept.

    Integration ancestry cannot be asked of a measurement run — its head is
    never merged — so the tree is judged against the promoted head itself: a
    dirty tree, or one carrying commits beyond that head, is the only copy of
    work nobody recorded elsewhere and is kept with the reason.
    """
    from reckon.crew import merge, routing

    run_id = str(pointer.get("run_id") or "")
    value = str(pointer.get("worktree") or "")
    if not value or not Path(value).exists():
        return ""
    tree = Path(value)
    claims = [
        claim
        for claim in routing._live_pointer_worktrees().get(tree.resolve(), ())
        if claim != run_id
    ]
    inspected = routing._inspect_workspace(repository, tree, head, claims)
    if inspected["dirty"]:
        return "worktree has uncommitted files: " + "; ".join(inspected["dirty"])
    if not merge._is_ancestor(repository, str(inspected["head"]), head):
        return f"worktree carries commits beyond the promoted head {head}"
    return ""


def _release_measured_pointer(
    pointer: Mapping[str, Any],
    repository: Path,
    head: str,
    row: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any], str]:
    """Retire a promoted measurement run's pointer after releasing its tree.

    A measurement run's head is never merged: it replays work the primary
    branch already carries, so the integration ancestry every ordinary release
    proves cannot exist for it and requiring it kept measurement trees forever.
    Its promotion row states the measurement, which is the whole proof this
    path has; the tree leaves through the same removal promotion itself makes,
    and the outcome is recorded on the row before the pointer is retired.
    """
    from reckon.crew import promotion, promotion_release

    run_id = str(pointer["run_id"])
    project = str(pointer["project"])
    refusal = ""
    outcome: dict[str, Any] = {}

    def release(current: Mapping[str, Any]) -> bool:
        nonlocal refusal, outcome
        if dict(current) != pointer:
            refusal = "the live pointer changed before workspace release"
            return False
        refusal = _measured_workspace_refusal(current, repository, head)
        if refusal:
            return False
        removed = promotion._release_measurement_worktree(current)
        outcome = promotion_release._release_after_promotion(
            run_id,
            current,
            gate=str(row.get("gate") or "") if row is not None else "passed",
            integration_repo=repository,
            integration_commit=head,
        )
        if removed:
            outcome["measurement_worktree"] = removed
        if row is not None:
            outcome, recorded = promotion._record_release_on_ledger(
                project=project,
                root=repository,
                run_id=run_id,
                release=outcome,
                checkout=repository,
                after_merge=True,
            )
            if recorded is None:
                refusal = "the workspace outcome could not be recorded"
                return False
        if not outcome.get("worktree_released"):
            refusal = str(
                outcome.get("worktree_withheld") or "workspace release withheld"
            )
            return False
        return True

    retired = retire_pointer_if(run_id, release)
    return retired, outcome, refusal


def release_merged_promotions(
    *, only_run: str = "", project: str | None = None, session: str | None = None
) -> list[dict[str, Any]]:
    """Finish promoted live workspaces after an independently published merge.

    The promotion row proves delivery and the published branch proves integration.
    Refuse unsafe evidence before touching the workspace. Compare the complete
    pointer under its lock so a resumed attempt cannot lose its tree or pointer.
    The release receipt and section declaration precede pointer retirement; a
    failed write leaves the pointer for the next pass to finish.
    """
    from reckon.crew import merge, promotion
    from reckon.crew.promotion import _measurement_reason
    from reckon.crew.promotion_requests import COMMAND_REPLY, PENDING, read_promotion
    from reckon.crew.recovery_repair_dispatch import _reviewed_run_is_busy

    results: list[dict[str, Any]] = []
    for pointer in runs.list_live():
        if (project is not None and pointer.get("project") != project) or (
            session is not None and pointer.get("session") != session
        ):
            continue
        run_id = str(pointer.get("run_id") or "")
        run_project = str(pointer.get("project") or "")
        if not run_id or not run_project or (only_run and run_id != only_run):
            continue
        if not pointer.get("worktree") or pointer.get("landing_group"):
            continue
        # A journaled landing already has an owner in the pending-record pass.
        if _record_path(run_id).exists():
            continue
        if (
            COMMAND_REPLY.get() is None
            and read_promotion(run_id).get("phase") in PENDING
        ):
            continue
        row = _promoted_row(run_project, run_id)
        head = _reviewed_head(row) if row else ""
        if not head or _promotion_lease_held(run_id):
            continue
        # A repair mark names the attempt that wrote it, and the pointer may
        # since belong to a later one. A pointer that has moved on shows a turn
        # in progress for that later attempt, and so does a resumed turn its
        # stream never finished: both are finished once the process the pointer
        # records is proved dead, and only then is the rest of the release
        # allowed to look at them. Without that proof the run is kept — silently
        # where the mark alone is the evidence, since a newer attempt may simply
        # have superseded it, and reported once with the reason where a turn is
        # in progress and nothing can say whether it ended.
        marked = pointer.get("pending_landing_repair_attempt")
        superseded = bool(
            pointer.get("pending_landing_repair")
            and marked is not None
            and (
                not isinstance(marked, Mapping)
                or any(
                    pointer.get(field) != marked.get(field)
                    for field in MARKED_FIELDS
                    if field != "phase"
                )
            )
        )
        busy = _reviewed_run_is_busy(pointer)
        if (superseded or busy) and not _recorded_process_proved_dead(pointer):
            if busy:
                results.append(
                    {"run_id": run_id, "status": "release-held", "reason": busy}
                )
            continue
        # A run showing no turn in progress is released exactly as it always
        # was, whether or not it names a process at all.
        try:
            repository = merge.project_repository(run_project)
            if _measurement_reason(pointer) or _measurement_reason(row or {}):
                # A measurement run's head is never merged — not landing is what
                # the run is for — so its promotion alone is the whole proof.
                retired, release, refusal = _release_measured_pointer(
                    pointer, repository, head, row
                )
                if retired == POINTER_RETIRED:
                    clear_pending(run_id)
                    results.append(
                        {
                            "run_id": run_id,
                            "status": "already-landed",
                            "head": head,
                            "release": release,
                            "measured": True,
                        }
                    )
                elif refusal:
                    results.append(
                        {
                            "run_id": run_id,
                            "status": "release-refused",
                            "error": refusal,
                        }
                    )
                continue
            remote = merge.head_on_remote_branch(repository, head)
            primary = str(remote["remote_head"] or "")
            on_branch = remote["on_branch"]
            if on_branch is None and remote["cause"] == merge.REMOTE_UNCONFIGURED:
                primary = merge._rev_parse(
                    repository, merge._current_branch(repository)
                )
                on_branch = merge._is_ancestor(repository, head, primary)
            if on_branch is not True:
                continue
            retired, release, refusal = _release_integrated_pointer(
                pointer, repository, primary, row
            )
            if retired == POINTER_RETIRED:
                clear_pending(run_id)
                waiters = promotion._release_waiters_of_the_landed_section(pointer)
                results.append(
                    {
                        "run_id": run_id,
                        "status": "already-landed",
                        "head": head,
                        "release": release,
                        "waiters": waiters,
                    }
                )
            elif refusal:
                results.append(
                    {"run_id": run_id, "status": "release-refused", "error": refusal}
                )
        except (runs.CrewError, merge.MergeError, OSError, ValueError) as exc:
            results.append(
                {"run_id": run_id, "status": "release-refused", "error": str(exc)}
            )
    return results


def finish_pending_landings(
    *, now: float | None = None, land=None, live_run_ids=None, only_run: str = ""
) -> list[dict[str, Any]]:
    """Finish every promoted-but-unmerged landing, returning one row per record.

    ``land`` is ``merge.land``-shaped and ``live_run_ids`` the ids with a live
    pointer; both are parameters so a test can drive the pass.
    """
    from reckon.crew import merge
    from reckon.crew.resumption import release_judged_resume_holds

    moment = time.time() if now is None else now
    lander = land or merge.land
    results: list[dict[str, Any]] = release_judged_resume_holds(only_run=only_run)
    results.extend(finish_landing_groups(only_run=only_run))
    results.extend(retire_stuck_review_pointers(only_run=only_run))
    released = release_merged_promotions(only_run=only_run)
    results.extend(released)
    handled = {row["run_id"] for row in released}
    directory = pending_dir()
    if not directory.is_dir():
        return results + retire_promoted_pointers(
            only_run=only_run, release_workspaces=False
        )
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
        if (
            run_id in handled
            or not run_id
            or not project
            or (only_run and run_id != only_run)
        ):
            continue
        group_id = str((record.get("pointer") or {}).get("landing_group") or "")
        if group_id and group_path(group_id).exists():
            # A subset of a proved group need not pass its combined gate.
            # Only the group's completion callback may publish this record.
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
        from reckon.crew.resume import hold_reason

        try:
            held = hold_reason(runs.read_pointer(run_id), head)
        except runs.CrewError:
            held = ""
        if held:
            results.append({"run_id": run_id, "status": "held", "reason": held})
            continue
        if int(record.get("attempts") or 0) >= MAX_FINISH_ATTEMPTS and not record.get(
            "merge_conflict"
        ):
            results.append({"run_id": run_id, "status": "gave-up"})
            continue
        if row is None:
            age = moment - float(record.get("written_at") or moment)
            if run_id not in live:
                if age > UNPROMOTED_RECORD_SECONDS and _promoted_row_absent(
                    project, run_id
                ):
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
        repair_delivered = False
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
            and not _released_pending_delivery(pointer or {}, record, head)
        ):
            continue
        head = cited or _reviewed_head(row)
        if not head:
            continue
        repository = merge.project_repository(project)
        if conflict:
            try:
                repaired = (
                    _delivered_repair(
                        pointer,
                        str(conflict.get("head") or head),
                        conflict,
                        repository,
                    )
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

                repair = dispatch_merge_repair(
                    record.get("pointer") or row,
                    head=head,
                    primary=str(conflict.get("main_revision") or ""),
                    paths=conflict.get("paths") or (),
                    promoted=True,
                )
                if repair.get("record"):
                    results.append(
                        {
                            "run_id": run_id,
                            "status": "record-obligation",
                            "record": repair["record"],
                            "error": repair["reason"],
                        }
                    )
                else:
                    results.append(
                        {"run_id": run_id, "status": "awaiting-merge-repair"}
                    )
                continue
            head = repaired
            repair_delivered = True
            if _cited_head(record) != head:
                record.update(head=head, attempts=0)
            record["pointer"] = {**(record.get("pointer") or {}), **dict(pointer)}
            runs._write_json(path, record)
            from reckon.crew import queue_landing

            conflicts = queue_landing._merge_conflicts(pointer, head)
            if conflicts:
                from reckon.crew.recovery_repair_dispatch import dispatch_merge_repair

                primary = queue_landing._primary_revision(pointer)
                repair = dispatch_merge_repair(
                    pointer, head=head, primary=primary, paths=conflicts, promoted=True
                )
                if repair.get("record"):
                    results.append(
                        {
                            "run_id": run_id,
                            "status": "record-obligation",
                            "record": repair["record"],
                            "error": repair["reason"],
                        }
                    )
                    continue
                queue_landing._record_attempt(
                    run_id,
                    head=head,
                    guard="merge conflict: " + ", ".join(conflicts),
                    status="refused",
                    remedy=str(repair.get("advice") or ""),
                    previous=pointer.get("admission_landing"),
                )
                results.append(
                    {
                        "run_id": run_id,
                        "status": "merge-conflict",
                        "head": head,
                        "paths": conflicts,
                        "main_revision": primary,
                        "repair": repair,
                    }
                )
                continue
            waiting = _repair_review_wait(pointer, head)
            if waiting:
                results.append(waiting)
                continue

        replay_pointer = record.get("pointer") or {}

        def verify_gate(
            checkout,
            _base,
            _head,
            revision,
            objects=None,
            pointer=replay_pointer,
            gate=automatic_gate,
        ):
            from reckon.crew.review_landing import replay_candidate

            merge.import_built_merge(
                checkout, revision, objects, parents=[_base, _head]
            )
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
        # A delivered repair is compared with the pointer the reader accepted;
        # a retained promotion pointer is compared with its existing mark.
        retired = False
        if pointer is not None:
            if repair_delivered:
                retired = (
                    retire_pointer_if(
                        run_id,
                        lambda current, delivered=pointer: dict(current) == delivered,
                    )
                    == POINTER_RETIRED
                )
            else:
                retired = _release_journal_pointer(run_id, pointer, record)
        if retired:
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
    handled = {row["run_id"] for row in results}
    retired_pointers = retire_promoted_pointers(
        only_run=only_run, release_workspaces=False
    )
    return results + [row for row in retired_pointers if row["run_id"] not in handled]


def _released_pending_delivery(
    pointer: Mapping[str, Any], record: Mapping[str, Any], head: str
) -> bool:
    """A manual release can settle an unmarked journal's same delivered attempt."""
    from reckon._timestamps import parse_utc
    from reckon.crew import recovery
    from reckon.crew.promotion_checks import _fresh_manifest
    from reckon.crew.recovery_repair_dispatch import _reviewed_run_is_busy

    released = parse_utc((pointer.get("delivery_hold") or {}).get("released_at"))
    if released is None or released.timestamp() <= float(record.get("written_at") or 0):
        return False
    if _reviewed_run_is_busy(pointer):
        return False
    manifest = _fresh_manifest(pointer) or {}
    if manifest.get("status") != "complete":
        return False
    current_head, _tree = recovery._review_head_and_tree(pointer)
    return bool(head and recovery.same_revision(head, current_head))
