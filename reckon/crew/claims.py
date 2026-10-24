"""Amend one run's declared write claim through its live pointer.

A claim names the paths a worker may write. Widening adds paths only when the
recorded worker is not running, and checks peer claims before the pointer write.
Narrowing and release require a proven-local, proven-dead worker because they
remove protection from paths another worker may soon claim. Each operation
preserves the pointer's other fields and reports the paths it changed.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from reckon.crew.node import CrewError, unintegrated_claim_work
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


def widen_claim(
    run_id: str, paths: Iterable[str], *, acceptance: bool = False
) -> tuple[dict[str, Any], list[str]]:
    """Add paths to a stopped run, checking peer claims under the pointer lock.

    A promotion acceptance keeps the scope from before its grant. Promotion
    validates the stated reason against that scope while the widened claim
    remains on the pointer and in the committed run record.
    """
    from reckon.crew.dispatch import refuse_widen_scope_conflicts
    from reckon.crew.runs import _mutate_pointer

    requested = _distinct(path.strip() for path in paths if path.strip())
    if not requested:
        raise CrewError("widen_claim needs at least one path")
    added: list[str] = []

    def amend(pointer: dict[str, Any]) -> dict[str, Any]:
        if record_process_alive(pointer) is True:
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
            )
        if not acceptance and isinstance(original, list):
            original.extend(path for path in added if path not in original)
        node["write_paths"] = declared
        pointer["node"] = node
        return pointer

    return _mutate_pointer(run_id, amend), added


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
    payload = dict(pointer)
    node = _node(pointer)
    if not _node(pointer):
        raise CrewError("live pointer carries no node mapping to amend")
    node["write_paths"] = list(paths)
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
