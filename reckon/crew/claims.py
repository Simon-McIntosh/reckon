"""Narrow or release one run's write claim without hand-editing its pointer.

A write claim is created at dispatch and, until this module, could only be
destroyed at promotion. ``--write-path`` exists on ``crew dispatch`` alone:
``crew resume`` takes run, advice, backend and reason, and no command amends a
live pointer's ``write_paths``. A coordinator whose worker blocked at its fence
therefore had two moves, both wrong — hand-edit the live pointer under the crew
config home, or redispatch and discard the worker's session with every turn of
context it holds. The consequence was measurable: a run terminal since
2026-09-15 held a claim on a source file that refused an unrelated promotion two
days later in a third session.

The mechanics are a rewrite of the pointer's ``node.write_paths``, so what this
module adds is the refusal that makes the rewrite safe rather than clever. A
claim is released only when the worker holding it can be *shown* to have
stopped, and "shown" is deliberately narrow here:

* a worker the process table still answers true for refuses — releasing a live
  worker's claim is how two workers end up writing one file;
* a pointer whose launching host is not this host refuses, because a pid is
  meaningful only on the machine that issued it and the crew config home is
  shared across login nodes, so a foreign answer would be fabricated;
* a pointer that records no process yet refuses, because the pointer is written
  before the worker is spawned and that window is exactly the gap a second
  misplaced writer is looking for.

Only a proven-local, proven-dead worker is amendable. That is the conservative
direction on purpose: refusing costs a wait, while admitting costs someone's
worker.

Nothing here is wired to a command. The caller owns the surface; the intended
entry point is a ``crew`` subcommand taking a run id plus an optional
``--keep``/``--drop`` path, delegating to :func:`narrow_claim` and
:func:`release_claim` and rendering :meth:`ClaimAmendment.as_dict`.
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
    process_alive,
    read_pointer,
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
    alive = process_alive(pid)
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
