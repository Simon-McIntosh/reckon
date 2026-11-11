"""Amend one run's declared write claim through its live pointer.

A claim names the paths a worker may write. Widening adds paths only when the
recorded worker is not running, or when the run's own worker asks for them, and
checks peer claims before the pointer write. Narrowing and release require a
proven-local, proven-dead worker because they remove protection from paths
another worker may soon claim. Each operation preserves the pointer's other
fields and reports the paths it changed.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from reckon.crew.node import CrewError, unintegrated_claim_work
from reckon.crew.process_liveness import (
    LIVENESS_DEAD,
    LIVENESS_PLACED,
    LIVENESS_RECORDED_PID,
    LIVENESS_SESSION_WALK,
    record_process_liveness,
)
from reckon.crew.runs import (
    _write_json,
    pointer_path,
    read_pointer,
    record_process_alive,
)


class ClaimAmendmentRefusedError(CrewError):
    """A run's claim may not be amended while its worker can still write."""

    def __init__(self, run_id: str, reason: str) -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(f"cannot amend the write claim of run {run_id!r}: {reason}")


@dataclass(frozen=True)
class ClaimAmendment:
    """The outcome of one claim amendment, for a caller to report."""

    run_id: str
    previous: tuple[str, ...]
    current: tuple[str, ...]
    removed: tuple[str, ...]
    reason: str
    unintegrated: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return the stable report shape, matching the claim read model."""
        return {
            "run_id": self.run_id,
            "previous": list(self.previous),
            "current": list(self.current),
            "removed": list(self.removed),
            "reason": self.reason,
            "unintegrated": list(self.unintegrated),
        }


def declared_claim_paths(run_id: str) -> tuple[str, ...]:
    """Return the paths a run currently declares, in declaration order."""
    return tuple(_declared_paths(read_pointer(run_id)))


def _live_repair_round(pointer: Mapping[str, Any]) -> bool:
    """Whether the run's live attempt is a repair round the reflex opened.

    The predicate belongs to the module that classifies an attempt, so a widen
    and a refusal name the same round. Imported here rather than at module load
    because that module composes launches and this one is reached from them.
    """
    from reckon.crew.resumption import live_repair_round

    return live_repair_round(pointer)


def widen_claim(
    run_id: str,
    paths: Iterable[str],
    *,
    acceptance: bool = False,
    reason: str = "",
    self_widen: bool = False,
    grant_source: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Add paths to a run, checking peer claims under the pointer lock.

    A live worker keeps its claim fixed, because it is already writing against
    the boundary a widening would move. The exception is the run's own worker:
    it is the writer asking for its own boundary to move, so ``self_widen``
    admits it and records the grant, its reason and the fact that the worker
    made it. A repair round's live attempt is the other exception: the reflex
    started it as a turn of the reviewed run, its next attempt is fenced from
    this same claim, and a coordinator watching the round is the one who knows
    a path the finding needs. So the widen is recorded, and the round's next
    attempt mounts the added paths; a worker already running keeps the mounts
    it started with, because its sandbox was fixed when it began. Any other
    caller waits for the process to exit.

    ``grant_source`` names a caller other than the worker that grants a path on
    the run's behalf, and its grant is recorded the same way with that source.
    A repair round passes ``repair-round`` with the finding's id as the reason,
    so the path the round widened to reads back as a repair round's grant
    rather than as part of the declaration the run was dispatched with. A
    caller that names no source records no grant entry; the added path is then
    only part of the declaration.

    A promotion acceptance keeps the scope from before its grant. Promotion
    validates the stated reason against that scope while the widened claim
    remains on the pointer and in the committed run record.
    """
    from reckon.crew.dispatch import refuse_widen_scope_conflicts
    from reckon.crew.runs import _mutate_pointer

    requested = _distinct(path.strip() for path in paths if path.strip())
    if not requested:
        raise CrewError("widen_claim needs at least one path")
    stated_reason = str(reason).strip()
    source = str(grant_source).strip() or ("worker" if self_widen else "")
    if source and not stated_reason:
        raise CrewError("a recorded grant must state the reason for the grant")
    added: list[str] = []

    def amend(pointer: dict[str, Any]) -> dict[str, Any]:
        if (
            record_process_liveness(pointer).alive is True
            and not self_widen
            and not _live_repair_round(pointer)
        ):
            raise ClaimAmendmentRefusedError(
                run_id, "its worker is still running; wait for that process to exit"
            )
        node = _node(pointer)
        if not node:
            raise CrewError(
                f"live pointer for {run_id!r} records no node holding a write scope"
            )
        declared = _declared_paths(pointer)
        original = pointer.get("accepted_scope_before_widening")
        if acceptance and original is None:
            pointer["accepted_scope_before_widening"] = list(declared)
        for path in requested:
            if path not in declared:
                added.append(path)
                declared.append(path)
        if added:
            refuse_widen_scope_conflicts(
                pointer,
                added,
                set_aside_run_ids=_set_aside_on_acceptance(run_id, acceptance),
                regard_waiting=bool(source),
            )
        if source and added:
            _record_grant(node, added, stated_reason, source)
        if not acceptance and isinstance(original, list):
            original.extend(path for path in added if path not in original)
        node["write_paths"] = declared
        pointer["node"] = node
        return pointer

    return _mutate_pointer(run_id, amend), added


# The field a worker's fence is built from when its attempt is launched. A
# widen rewrites exactly this, and the fence reads it once at launch, so a
# worker already running cannot pick up a path added underneath it.
WIDEN_FENCE_FIELD = "node.write_paths"


def widen_takes_effect(pointer: Mapping[str, Any]) -> str:
    """Say when the paths a widen just added take effect, and from which field.

    A running worker's mounts are fixed when its sandbox starts, so a widened
    claim reaches it only on its next attempt. The result of a widen used to
    list the added paths and say nothing about this, so a coordinator read ``ok``
    as a fence that had already moved. The sentence here names the arm the
    request's liveness reading came from, so it states what the run's records
    actually support:

    * a worker read alive by its recorded pid is named with that pid;
    * a worker found only by the session walk is named by its session and never
      with a pid, because no pid this host read is the worker's;
    * a placed run's live worker is its scheduler job, and the recorded pid is
      the scheduler client's, never presented as the worker's;
    * a proven death says the next resume or launch mounts the paths;
    * an unreadable liveness says only that it could not be read on this host,
      and never claims a next launch will mount them.

    Every arm ends by naming the field the fence is built from, so a reader
    knows what the widen changed.
    """
    reading = record_process_liveness(pointer)
    attempt = pointer.get("attempt")
    numbered = f"attempt {attempt}" if isinstance(attempt, int) else ""
    if reading.arm == LIVENESS_RECORDED_PID:
        subject = f"worker {numbered}".strip() if numbered else "the run's worker"
        body = (
            f"{subject} is alive as pid {reading.pid}, so its mounts are "
            "unchanged and the added paths apply from the next attempt"
        )
    elif reading.arm == LIVENESS_SESSION_WALK:
        named = f" ({numbered})" if numbered else ""
        body = (
            f"a worker of this run was found by its session id "
            f"{reading.session_id!r}{named} and no pid is named for it, so its "
            "mounts are unchanged and the added paths apply from the next attempt"
        )
    elif reading.arm == LIVENESS_PLACED:
        job = f" {reading.job_id}" if reading.job_id else ""
        body = (
            f"the scheduler job{job} this placed run is charged to is still in "
            "the system; the recorded pid names the scheduler client rather than "
            "the worker, and its mounts are unchanged, so the added paths apply "
            "from the next attempt"
        )
    elif reading.arm == LIVENESS_DEAD:
        body = (
            "no worker of this run is alive on this host, so the next resume or "
            "launch mounts the added paths"
        )
    else:
        body = (
            "the worker's liveness could not be read on this host, so it is not "
            "known when the added paths take effect"
        )
    return f"{body}. The fence is built from {WIDEN_FENCE_FIELD} at launch."


def _record_grant(
    node: dict[str, Any], added: Iterable[str], reason: str, source: str
) -> None:
    """Record each path a caller granted the run, with its reason and source.

    The grant is kept beside the declaration it widened, so a reader can tell a
    path the run's boundary was widened to from one it was dispatched with, and
    which caller widened it: the run's own worker, or a repair round acting on
    the run's behalf.
    """
    from reckon.crew.dispatch_claims import WRITE_GRANTS_KEY
    from reckon.crew.runs import _utc_now

    recorded = node.get(WRITE_GRANTS_KEY)
    grants = list(recorded) if isinstance(recorded, list) else []
    known = {
        str(entry.get("path") or "") for entry in grants if isinstance(entry, Mapping)
    }
    for path in added:
        if path in known:
            continue
        known.add(path)
        grants.append(
            {
                "path": path,
                "reason": reason,
                "source": source,
                "at": _utc_now(),
            }
        )
    node[WRITE_GRANTS_KEY] = grants


def narrow_claim(
    run_id: str,
    *,
    keep: Sequence[str],
    reason: str,
) -> ClaimAmendment:
    """Retain only ``keep`` from a stopped run's declared claim.

    ``keep`` must name paths the run already declares — a path the pointer does
    not carry is refused rather than silently ignored, because a typo would
    otherwise read as a successful narrowing. The amendment must drop at least
    one path; narrowing to the same set is a caller mistake, and dropping every
    path is :func:`release_claim`'s job.
    """
    retained = _distinct(keep)
    if not retained:
        raise CrewError(
            "narrow_claim needs at least one retained path; dropping every path "
            "is release_claim"
        )
    pointer = read_pointer(run_id)
    _require_stopped_worker(run_id, pointer)
    current = _declared_paths(pointer)
    unknown = [path for path in retained if path not in current]
    if unknown:
        raise CrewError(
            f"run {run_id!r} does not declare {unknown!r}; it declares {current!r}"
        )
    kept = [path for path in current if path in set(retained)]
    if len(kept) == len(current):
        raise CrewError(
            f"narrow_claim would change nothing: run {run_id!r} already declares "
            f"exactly {kept!r}"
        )
    _amend(pointer, kept)
    return _amended(run_id, current, kept, reason, pointer)


def release_claim(
    run_id: str,
    *,
    paths: Iterable[str] | None = None,
    reason: str,
) -> ClaimAmendment:
    """Drop ``paths`` (or the whole claim) from a stopped run's declaration.

    The default drops every declared path, which is what makes a later dispatch
    onto those paths admissible: the claim registry derives what a run holds
    from exactly this list, so an emptied list holds nothing. Passing ``paths``
    drops only the named ones and leaves the rest declared.
    """
    pointer = read_pointer(run_id)
    _require_stopped_worker(run_id, pointer)
    current = _declared_paths(pointer)
    if paths is None:
        dropped = list(current)
    else:
        named = _distinct(paths)
        if not named:
            raise CrewError("release_claim was given an empty path list")
        unknown = [path for path in named if path not in current]
        if unknown:
            raise CrewError(
                f"run {run_id!r} does not declare {unknown!r}; it declares {current!r}"
            )
        dropped = list(named)
    if not dropped:
        raise CrewError(f"run {run_id!r} declares no paths to release")
    # A full release leaves the declaration empty; a partial one keeps the
    # undeclared remainder in its original order.
    remaining = [path for path in current if path not in set(dropped)]
    _amend(pointer, remaining)
    return _amended(run_id, current, remaining, reason, pointer)


# ── internals ────────────────────────────────────────────────────────────────


def _set_aside_on_acceptance(run_id: str, acceptance: bool) -> tuple[str, ...]:
    """The peers whose claims a promotion acceptance may walk past.

    A live claim protects a file from a second concurrent editor. A peer whose
    process has ended and whose manifest is terminal is editing nothing, so its
    claim protects nothing: its work in the worktree is fixed, and whichever run
    lands second merges on top. Only an acceptance is given that reading; a
    dispatch onto the same paths still refuses, which is why the judgement is
    made here rather than inside the shared claim check.
    """
    if not acceptance:
        return ()
    from reckon.crew.dispatch_claims import stopped_with_terminal_manifest
    from reckon.crew.runs import list_live

    return tuple(
        str(peer.get("run_id") or "")
        for peer in list_live()
        if str(peer.get("run_id") or "") != run_id
        and stopped_with_terminal_manifest(peer)
    )


def _distinct(values: Iterable[str]) -> list[str]:
    """Return the values as strings, order-preserving and de-duplicated."""
    return list(dict.fromkeys(str(value) for value in values))


def _node(pointer: Mapping[str, Any]) -> dict[str, Any]:
    """Return the pointer's node mapping, or an empty one when absent."""
    node = pointer.get("node")
    return dict(node) if isinstance(node, Mapping) else {}


def _declared_paths(pointer: Mapping[str, Any]) -> list[str]:
    """Return the run's declared write paths as strings, in order."""
    return [str(path) for path in _node(pointer).get("write_paths") or ()]


def _amended(
    run_id: str,
    previous: list[str],
    current: list[str],
    reason: str,
    pointer: Mapping[str, Any],
) -> ClaimAmendment:
    """Compose the report for one amendment, carrying lost-work evidence.

    A stopped run may still hold unintegrated work — an uncommitted delta, or a
    commit past its base — and releasing its claim does not recover that work.
    It is reported rather than refused: the caller is the authority an operator
    invoked, and the point of the surface is to let a judgement be made from
    what is on the record instead of from a hand-read pointer.
    """
    try:
        unintegrated = tuple(unintegrated_claim_work(pointer))
    except CrewError:
        unintegrated = ("could not be established from the recorded worktree",)
    return ClaimAmendment(
        run_id=run_id,
        previous=tuple(previous),
        current=tuple(current),
        removed=tuple(path for path in previous if path not in set(current)),
        reason=reason,
        unintegrated=unintegrated,
    )


def _amend(pointer: Mapping[str, Any], paths: Sequence[str]) -> None:
    """Rewrite the live pointer's declared write paths, preserving its keys."""
    from reckon.crew.dispatch_claims import WRITE_GRANTS_KEY

    payload = dict(pointer)
    node = _node(pointer)
    if not _node(pointer):
        raise CrewError("live pointer carries no node mapping to amend")
    node["write_paths"] = list(paths)
    grants = node.get(WRITE_GRANTS_KEY)
    if isinstance(grants, list):
        node[WRITE_GRANTS_KEY] = [
            entry
            for entry in grants
            if isinstance(entry, Mapping) and str(entry.get("path") or "") in set(paths)
        ]
    payload["node"] = node
    original = payload.get("accepted_scope_before_widening")
    if isinstance(original, list):
        payload["accepted_scope_before_widening"] = [
            path for path in original if path in paths
        ]
    _write_json(pointer_path(str(pointer.get("run_id") or "")), payload)


def _require_stopped_worker(run_id: str, pointer: Mapping[str, Any]) -> None:
    """Refuse the amendment unless the worker is proven stopped on this host."""
    alive, explanation = _worker_liveness(pointer)
    if alive is not False:
        raise ClaimAmendmentRefusedError(run_id, explanation)


def _worker_liveness(pointer: Mapping[str, Any]) -> tuple[bool | None, str]:
    """Judge whether a pointer's worker can still write, and why.

    Returns ``True`` when the worker is running, ``False`` only when it is
    proven stopped on this host, and ``None`` when the question cannot be
    answered here. Every non-``False`` answer refuses, so the two unprovable
    shapes (a foreign launching host, a pointer written before its worker was
    spawned) are closed rather than assumed dead.
    """
    host = pointer.get("launcher_host")
    reading_host = socket.gethostname()
    if host is not None and str(host) != reading_host:
        foreign = (
            f"it records a launching host {str(host)!r} that is not this host "
            f"{reading_host!r}, and a pid answers only on the machine that "
            "issued it"
        )
        return None, foreign
    pid = pointer.get("pid")
    if not pid:
        unborn = (
            "it records no worker process yet, and a pointer is written before "
            "its worker is spawned"
        )
        return None, unborn
    alive = record_process_alive(pointer)
    if alive is True:
        return True, f"its worker is still running as pid {pid}"
    if alive is None:
        return None, f"the liveness of pid {pid} could not be established"
    expected_start = pointer.get("pid_start_time")
    if expected_start is not None:
        from reckon.crew.runs import _process_start_time

        if _process_start_time(pid) == expected_start:
            return True, f"pid {pid} is alive and matches the recorded start tick"
    return False, ""
