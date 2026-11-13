"""Durable promotion requests drained by the fleet's admission service.

The request file is authoritative until a terminal receipt is written in the
run directory. It is removed only after that receipt, so a service restart
finds either the request or its completed outcome. Promotion itself retains its
existing guards, lease and idempotent ledger/release path.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from reckon.crew import runs
from reckon.crew.host_lease import HostLease, local_host
from reckon.crew.node import CrewError

REQUESTED = "promotion-requested"
RUNNING = "promotion-running"
PROMOTED = "promoted"
FAILED = "promotion-failed"
REFUSED = "promotion-refused"
DEFERRED = "promotion-deferred"
# A request whose review record has not been stored yet is not a promotion
# attempt: the record is still being written. It is parked outside PENDING so
# the queue starts the requests behind it and no promotion slot is spent on it,
# and so a coordinator reading it is not told a promotion failed.
WAITING = "promotion-waiting"
PENDING = frozenset({REQUESTED, RUNNING})
ERRORS = frozenset({FAILED, REFUSED})

# A captured command uses the same callback and verdict writer as the CLI,
# with its response retained on the request instead of printed by a thread.
COMMAND_REPLY: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "promotion_command_reply", default=None
)
_CLAIMS: set[str] = set()
_CLAIMS_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _requests_dir() -> Path:
    return runs.crew_home() / "promotion-requests"


def _request_path(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise CrewError(f"invalid promotion run id {run_id!r}")
    return _requests_dir() / f"{run_id}.json"


def _receipt_path(run_id: str) -> Path:
    return runs.run_dir(run_id) / "promotion.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise CrewError(f"promotion record {path} does not hold an object")
    return value


def read_promotion(run_id: str) -> dict[str, Any]:
    """Read a pending request or its durable terminal outcome."""
    return _read(_request_path(run_id)) or _read(_receipt_path(run_id))


def _mark_pointer(request: Mapping[str, Any], phase: str) -> None:
    run_id = str(request["run_id"])
    try:
        with runs._pointer_lock(run_id, blocking=False):
            if not runs.pointer_path(run_id).exists():
                return
            pointer = runs.read_pointer(run_id)
            pointer["promotion_status"] = phase
            pointer["promotion_request"] = str(_request_path(run_id))
            pointer["phase"] = (
                phase
                if phase in {REQUESTED, PROMOTED}
                else str(request.get("previous_phase") or "complete")
            )
            runs._write_json(runs.pointer_path(run_id), pointer)
    except runs.PointerLockHeldError:
        # The durable request remains readable while another pointer mutation
        # holds its short lock; requesting never waits behind that mutation.
        return


def request_promotion(
    run_id: str,
    *,
    kind: str = "complete",
    options: Mapping[str, Any] | None = None,
    retry: bool = False,
) -> dict[str, Any]:
    """Record one request and return without doing any promotion work.

    A pending request wins over every later request for the same run, including
    one arriving through a different caller. Retrying a terminal refusal is an
    explicit coordinator action; automatic sweeps leave its obligation visible.
    """
    if kind not in {"command", "complete", "acceptance", "review-self"}:
        raise CrewError(f"unknown promotion request kind {kind!r}")
    path = _request_path(run_id)
    with runs._pointer_lock(f"promotion-request-{run_id}"):
        existing = read_promotion(run_id)
        if existing and (
            existing.get("phase") in PENDING
            or existing.get("phase") == PROMOTED
            or (existing.get("phase") in ERRORS and not retry)
        ):
            return {**existing, "duplicate": True}
        pointer = runs.read_pointer(run_id)
        request = {
            "ok": True,
            "run_id": run_id,
            "request_id": uuid4().hex,
            "phase": REQUESTED,
            "kind": kind,
            "project": str(pointer.get("project") or ""),
            "session": str(pointer.get("session") or ""),
            "node": (pointer.get("node") or {}).get("id", ""),
            "plan": str(pointer.get("plan") or ""),
            "previous_phase": str(pointer.get("phase") or "complete"),
            "requested_at": _now(),
            "options": json.loads(json.dumps(dict(options or {}), default=str)),
        }
        runs._write_json(path, request)
        _mark_pointer(request, REQUESTED)
        return request


def _review_self_waits(request: Mapping[str, Any]) -> tuple[bool, str]:
    """Whether this request is waiting on a review record still being written.

    Only a review's own promotion asks this: every other kind reads durable
    state that either exists or does not, so only ``review-self`` has a state
    in which the right answer is to wait rather than to drain or refuse.
    """
    if request.get("kind") != "review-self":
        return False, ""
    from reckon.crew import recovery_review_acceptance as acceptance

    try:
        pointer = runs.read_pointer(str(request.get("run_id") or ""))
    except CrewError:
        # A pointer that has gone is not waiting; draining it reports the
        # vanished run the way every other request kind does.
        return False, ""
    state, reason, _record = acceptance.review_self_state(pointer)
    if state != acceptance.REVIEW_SELF_WAITING:
        return False, ""
    # Only a reviewer that is still running holds the queue. One that is not
    # running is drained, so a run that ended without its record leaves the
    # refusal its coordinator has to see rather than a request that never
    # moves.
    if runs.record_process_alive(pointer) is True:
        return True, reason
    return False, ""


def _mark_waiting(request: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Park a request on the record its reviewer still owes.

    The pointer keeps the working phase its reviewer is in: a request waiting
    for a record must not read as a promotion the coordinator has to answer.
    The request is written outside ``PENDING`` so the next tick starts the
    requests behind it instead of spending a slot on this one.
    """
    run_id = str(request["run_id"])
    waiting = {
        **dict(request),
        "phase": WAITING,
        "waiting_reason": reason,
        "checked_at": _now(),
    }
    with runs._pointer_lock(f"promotion-request-{run_id}"):
        current = _read(_request_path(run_id))
        if current.get("request_id") != waiting["request_id"] or current.get(
            "phase"
        ) != request.get("phase"):
            # Another drain moved this request on while it was being judged.
            return {}
        runs._write_json(_request_path(run_id), waiting)
        _mark_pointer(waiting, WAITING)
    return waiting


class PromotionRequests:
    """A sweep's durable hand-off; no callable or worker lives on its caller."""

    def request(
        self, run_id: str, kind: str, *, config: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return request_promotion(run_id, kind=kind, options={"config": config})


def _execute_command(options: Mapping[str, Any]) -> dict[str, Any]:
    import click

    from reckon.crew_run_commands import crew_complete

    replies: list[dict[str, Any]] = []
    token = COMMAND_REPLY.set(replies)
    try:
        with click.Context(crew_complete) as context:
            context.invoke(crew_complete, **dict(options))
    except click.ClickException as exc:
        raise CrewError(exc.format_message()) from exc
    except click.exceptions.Exit as exc:
        if exc.exit_code:
            detail = replies[-1] if replies else {"error": "promotion command failed"}
            raise CrewError(json.dumps(detail)) from exc
    finally:
        COMMAND_REPLY.reset(token)
    if not replies:
        raise CrewError("promotion command produced no receipt")
    return replies[-1]


def _execute(request: Mapping[str, Any]) -> dict[str, Any] | None:
    run_id = str(request["run_id"])
    options = request.get("options") or {}
    if request["kind"] == "command":
        return _execute_command(options)
    if request["kind"] == "complete":
        from reckon.crew import promotion

        return promotion.complete(run_id, **options)
    from reckon.crew import recovery_review_acceptance as acceptance

    try:
        pointer = runs.read_pointer(run_id)
    except CrewError:
        if runs.pointer_path(run_id).exists():
            raise
        from reckon import ledger
        from reckon.crew import merge

        project = str(request["project"])
        path = ledger.run_path(project, run_id, merge.project_repository(project))
        row = _read(path)
        if not row:
            raise
        if row.get("run_id") != run_id:
            raise CrewError(f"ledger row names a different run: {path}") from None
        # Promotion removes its pointer after persisting this row. A service
        # receipt can be interrupted afterwards, so recover from that row
        # without repeating the completed review operation.
        return {
            "run_id": run_id,
            "promoted": True,
            "already_promoted": True,
            "record": row,
            "ledger": {"path": str(path)},
        }
    operation = (
        acceptance.accept_clean_review
        if request["kind"] == "acceptance"
        else acceptance.promote_review_run_itself
    )
    return operation(pointer, config=options.get("config"))


def _finish(request: Mapping[str, Any], phase: str, **result: Any) -> dict[str, Any]:
    run_id = str(request["run_id"])
    receipt = {**request, **result, "phase": phase, "finished_at": _now()}
    with runs._pointer_lock(f"promotion-request-{run_id}"):
        runs._write_json(_receipt_path(run_id), receipt)
        _mark_pointer(receipt, phase)
        path = _request_path(run_id)
        if phase in ERRORS:
            # Failed requests stay indexed for the session obligation reader.
            runs._write_json(path, receipt)
        else:
            path.unlink(missing_ok=True)
    return receipt


def _promoted_commits(result: Mapping[str, Any]) -> list[str]:
    promoted = result.get("promotion") or result
    record = promoted.get("record") or {}
    commits = record.get("commits") or result.get("commits")
    if commits:
        return list(commits)
    location = (result.get("ledger") or {}).get("path")
    if location:
        return list(_read(Path(location)).get("commits") or [])
    return []


class PromotionService:
    """Drain requested promotions at a bounded fleet-wide width.

    Slots and requests use renewable cross-host leases. An in-process claim is
    also needed because a lease reattaches when its holder has the same PID.
    The admission loop owns this instance; a tick starts work and never joins
    it. Tests and a manual service pass can explicitly settle the work.

    A service that replaces its process image must first stop starting work and
    let the work already running finish, because the replacement ends the
    threads it started. ``begin_drain`` refuses every later tick and
    ``wait_for_in_flight`` waits for the promotions this instance started.
    """

    def __init__(
        self,
        *,
        width: int = 2,
        execute: Callable[[Mapping[str, Any]], dict[str, Any] | None] | None = None,
    ) -> None:
        self.width = max(1, int(width))
        self.execute = execute or _execute
        self._threads: list[threading.Thread] = []
        self._draining = False

    def begin_drain(self) -> None:
        """Start no further promotion; work already running still finishes."""
        self._draining = True

    def wait_for_in_flight(self, timeout: float | None = None) -> list[str]:
        """Wait for this instance's promotions, returning those still running.

        A timeout is never used by the reload path: cutting a promotion off is
        the outcome the drain exists to prevent. It is offered so a caller that
        is not replacing the process can bound its own wait.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        still_running: list[str] = []
        for thread in list(self._threads):
            thread.join(
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if thread.is_alive():
                still_running.append(thread.name)
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        return still_running

    def _claim(self, name: str) -> HostLease | None:
        key = str(runs.crew_home() / name)
        with _CLAIMS_LOCK:
            if key in _CLAIMS:
                return None
            _CLAIMS.add(key)
        lease = HostLease(
            runs.crew_home(),
            name,
            local_host(),
            os.getpid(),
            os.environ.get("SLURM_JOB_ID", ""),
        )
        try:
            claimed = lease.claim()
        except BaseException:
            with _CLAIMS_LOCK:
                _CLAIMS.discard(key)
            raise
        if not claimed:
            with _CLAIMS_LOCK:
                _CLAIMS.discard(key)
            return None
        return lease

    @staticmethod
    def _release(lease: HostLease) -> None:
        try:
            lease.release()
        finally:
            with _CLAIMS_LOCK:
                _CLAIMS.discard(str(lease.path.parent.parent / lease.path.stem))

    def tick(self) -> list[str]:
        """Start available requests without waiting for any promotion."""
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        if self._draining:
            return []
        launched: list[str] = []
        for path in sorted(_requests_dir().glob("*.json")):
            request = _read(path)
            if request.get("phase") not in PENDING:
                continue
            waiting, reason = _review_self_waits(request)
            if waiting:
                # Nothing is owed here yet, so this request takes no slot and
                # the loop carries on to the ones behind it.
                _mark_waiting(request, reason)
                continue
            slot = next(
                (
                    lease
                    for number in range(self.width)
                    if (lease := self._claim(f"promotion-slot-{number}")) is not None
                ),
                None,
            )
            if slot is None:
                break
            owner = self._claim(f"promotion-request-owner-{request['run_id']}")
            if owner is None:
                self._release(slot)
                continue
            thread = threading.Thread(
                target=self._run,
                args=(request, owner, slot),
                name=f"promotion-request-{request['run_id']}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
            launched.append(str(request["run_id"]))
        return launched

    def _run(self, request: Mapping[str, Any], *leases: HostLease) -> None:
        from reckon.crew.promotion import _PromotionLeaseRenewer

        renewers = [_PromotionLeaseRenewer(lease) for lease in leases]
        for renewer in renewers:
            renewer.start()
        try:
            run_id = str(request["run_id"])
            with runs._pointer_lock(f"promotion-request-{run_id}"):
                current = _read(_request_path(run_id))
                receipt = _read(_receipt_path(run_id))
                if (
                    receipt.get("request_id") == request["request_id"]
                    and receipt.get("phase") == PROMOTED
                ):
                    _request_path(run_id).unlink(missing_ok=True)
                    return
                if (
                    current.get("request_id") != request["request_id"]
                    or current.get("phase") not in PENDING
                ):
                    return
                request = {**current, "phase": RUNNING, "started_at": _now()}
                runs._write_json(_request_path(run_id), request)
                _mark_pointer(request, RUNNING)
            result = self.execute(request)
            if result is None:
                _finish(request, DEFERRED, result=None)
                return
            if result.get("waiting"):
                # The reviewer is still writing the record: park the request
                # rather than refusing a run that is not finished.
                _mark_waiting(request, str(result.get("reason") or ""))
                return
            if (
                result.get("accepted") is False
                or result.get("promoted") is False
                or result.get("ok") is False
            ):
                _finish(
                    request,
                    REFUSED,
                    ok=False,
                    error=str(
                        result.get("reason")
                        or result.get("error")
                        or result.get("detail")
                        or "promotion refused"
                    ),
                    result=result,
                )
                return
            commits = _promoted_commits(result)
            _finish(request, PROMOTED, result=result, commits=commits)
        except Exception as exc:  # noqa: BLE001 - retain every run's own failure
            phase = REFUSED if isinstance(exc, CrewError) else FAILED
            _finish(request, phase, ok=False, error=f"{type(exc).__name__}: {exc}")
        finally:
            for renewer in renewers:
                renewer.stop()
            for lease in reversed(leases):
                self._release(lease)

    def settle(self, timeout: float = 300.0) -> None:
        """Wait within an explicit budget for work this instance started."""
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))


def failure_obligations(
    project: str, session: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Project the service's durable failures into the owning session's duties."""
    items: list[dict[str, Any]] = []
    for path in _requests_dir().glob("*.json"):
        request = _read(path)
        if request.get("phase") not in ERRORS:
            continue
        if request.get("project") != project or request.get("session") != session:
            continue
        items.append(
            {
                "kind": "promotion-failed",
                "run_id": request["run_id"],
                "node": request.get("node", ""),
                "plan": request.get("plan", ""),
                "age_seconds": max(
                    0,
                    int(
                        (
                            (now or datetime.now(UTC))
                            - datetime.fromisoformat(request["finished_at"])
                        ).total_seconds()
                    ),
                ),
                "reason": str(request.get("error") or "promotion refused"),
                "next_command": f"reckon crew complete --run {request['run_id']} --gate {((request.get('options') or {}).get('gate') or 'passed')}",
            }
        )
    return items
