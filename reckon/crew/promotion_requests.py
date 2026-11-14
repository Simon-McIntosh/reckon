"""Durable promotion requests drained by the fleet's admission service.

The request file is authoritative until a terminal receipt is written in the
run directory. It is removed only after that receipt, so a service restart
finds either the request or its completed outcome. Promotion itself retains its
existing guards, lease and idempotent ledger/release path.
"""

from __future__ import annotations

import json
import logging
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

_LOGGER = logging.getLogger(__name__)

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

# How long the drain waits for a wakeup before looking for requests another
# process queued. A freed slot wakes it immediately, so this only bounds the
# latency of a request nobody told it about.
REFILL_POLL_SECONDS = 2.0
DRAINED_LOG_NAME = "promotion-drained.jsonl"
_DRAINED_LOG_MAX_BYTES = 256_000
_DRAINED_LOG_KEEP_LINES = 500

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


def _drained_log_path() -> Path:
    return runs.crew_home() / DRAINED_LOG_NAME


def _waited_seconds(requested_at: Any, finished_at: Any) -> float | None:
    try:
        requested = datetime.fromisoformat(str(requested_at))
        finished = datetime.fromisoformat(str(finished_at))
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (finished - requested).total_seconds()), 3)


def _trim_drained_log(path: Path) -> None:
    try:
        if path.stat().st_size <= _DRAINED_LOG_MAX_BYTES:
            return
        lines = path.read_text().splitlines()[-_DRAINED_LOG_KEEP_LINES:]
    except OSError:
        return
    try:
        path.write_text("\n".join(lines) + ("\n" if lines else ""))
    except OSError:
        return


def _record_drained(receipt: Mapping[str, Any]) -> None:
    """Append one finished request to the log the queue report reads.

    A receipt lives in the run directory, which a promotion's release path
    removes, so the rate over a window cannot be read back from receipts once
    their runs are gone. The append is best-effort: bookkeeping must never fail
    a promotion whose outcome is already durable on its receipt.
    """
    row = {
        "run_id": receipt.get("run_id"),
        "phase": receipt.get("phase"),
        "requested_at": receipt.get("requested_at"),
        "finished_at": receipt.get("finished_at"),
        "waited_seconds": _waited_seconds(
            receipt.get("requested_at"), receipt.get("finished_at")
        ),
    }
    path = _drained_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError:
        return
    _trim_drained_log(path)


def promotion_queue_report(
    *, now: datetime | None = None, window_seconds: float = 3600.0
) -> dict[str, Any]:
    """What the queue holds now and how fast it has drained recently.

    ``waiting`` counts the requests a slot would take up next, ``running`` the
    ones holding a slot, and ``parked`` the ones held outside every slot
    because the record a reviewer still owes is not written yet. The oldest
    wait is measured from the request that has waited longest, so a queued
    request that is stuck shows up even while younger ones drain past it.
    """
    moment = now or datetime.now(UTC)
    waiting: list[dict[str, Any]] = []
    running = 0
    parked = 0
    for path in _requests_dir().glob("*.json"):
        request = _read(path)
        phase = request.get("phase")
        if phase == REQUESTED:
            waiting.append(request)
        elif phase == RUNNING:
            running += 1
        elif phase == WAITING:
            parked += 1
    oldest = None
    for request in waiting:
        try:
            requested_at = datetime.fromisoformat(str(request.get("requested_at")))
        except (TypeError, ValueError):
            continue
        if oldest is None or requested_at < oldest[0]:
            oldest = (requested_at, request)
    counted = {"promoted": 0, "refused": 0, "failed": 0, "deferred": 0}
    try:
        finished = _drained_log_path().read_text().splitlines()
    except OSError:
        finished = []
    cutoff = moment.timestamp() - window_seconds
    for line in finished:
        try:
            row = json.loads(line)
            stamp = datetime.fromisoformat(str(row.get("finished_at")))
        except (TypeError, ValueError):
            continue
        if stamp.timestamp() < cutoff:
            continue
        key = {
            PROMOTED: "promoted",
            REFUSED: "refused",
            FAILED: "failed",
            DEFERRED: "deferred",
        }.get(str(row.get("phase")))
        if key is not None:
            counted[key] += 1
    return {
        "waiting": len(waiting),
        "running": running,
        "parked": parked,
        "oldest_wait_seconds": (
            None
            if oldest is None
            else round(max(0.0, (moment - oldest[0]).total_seconds()), 3)
        ),
        "oldest_wait_run_id": None if oldest is None else oldest[1].get("run_id"),
        "window_seconds": window_seconds,
        "finished_last_window": sum(counted.values()),
        "promoted_last_window": counted["promoted"],
        "refused_last_window": counted["refused"],
        "failed_last_window": counted["failed"],
        "deferred_last_window": counted["deferred"],
    }


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
    _record_drained(receipt)
    return receipt


def _reviewed_run_id(request: Mapping[str, Any]) -> str:
    """The run a queued review promotion reviews, or empty for any other request.

    A review run's node id carries the run it scored, so the queue can tell a
    review's own promotion from the promotion of the work being reviewed. The
    check on the node id's prefix comes first because it settles most requests
    without touching the store: only a review request pays for a lookup.
    """
    from reckon.crew.recovery_vocabulary import REVIEW_NODE_PREFIX

    node_id = str(request.get("node") or "")
    if not node_id.startswith(REVIEW_NODE_PREFIX):
        return ""
    from reckon.crew.recovery_review_delivery import _resolved_reviewed_run_id

    try:
        return _resolved_reviewed_run_id(
            {"node": {"id": node_id}, "project": str(request.get("project") or "")},
            str(request.get("project") or ""),
        )
    except Exception:  # noqa: BLE001 — ordering must never stop the drain
        return ""


def _pending_requests() -> list[dict[str, Any]]:
    """Every queued request, in the order a drain takes them up.

    A run whose review is queued beside it can only be refused for the absent
    review, and that refusal sends a coordinator back to re-request work that
    was about to become promotable. So a review's own promotion goes ahead of
    the run it reviews whenever that run is waiting too; every other request
    keeps the plain name order the queue has always drained in.
    """
    pending = []
    for path in sorted(_requests_dir().glob("*.json")):
        request = _read(path)
        if request.get("phase") in PENDING:
            pending.append(request)
    waiting = {str(request.get("run_id") or "") for request in pending}
    review_first = {
        str(request.get("run_id") or "")
        for request in pending
        if _reviewed_run_id(request) in waiting
    }
    return sorted(
        pending,
        key=lambda request: (
            str(request.get("run_id") or "") not in review_first,
            str(request.get("run_id") or ""),
        ),
    )


def _defer_to_a_queued_review(
    request: Mapping[str, Any], refusal: BaseException | str
) -> bool:
    """Requeue a run refused only for a review that is itself still queued.

    The refusal is true when it is written and wrong a minute later: the review
    the run needs is in this same queue, so the promotion is waiting on the
    queue rather than on the coordinator. Writing the request back as waiting
    keeps it in the drain, where the pass after the review promotes it without
    anyone re-requesting it. A run refused for anything else, or one
    whose review is neither queued nor stored, is refused exactly as before.

    The refusal itself is passed rather than its text: an aggregated refusal
    carries the parts it was composed from, and a run refused for a failing
    suite beside a missing review must be refused, not requeued. Flattening the
    message first would hide the second reason inside one string.
    """
    from reckon.crew.promotion_checks import refusal_awaits_a_review

    if not refusal_awaits_a_review(refusal):
        return False
    run_id = str(request["run_id"])
    if not any(
        other.get("phase") in PENDING and _reviewed_run_id(other) == run_id
        for other in (_read(path) for path in _requests_dir().glob("*.json"))
    ):
        return False
    with runs._pointer_lock(f"promotion-request-{run_id}"):
        current = _read(_request_path(run_id))
        if current.get("request_id") != request["request_id"]:
            # A newer request owns the run now; it decides for itself.
            return True
        requeued = {
            **current,
            "phase": REQUESTED,
            "deferred_at": _now(),
            "deferred_reason": str(refusal),
        }
        requeued.pop("started_at", None)
        runs._write_json(_request_path(run_id), requeued)
        _mark_pointer(requeued, REQUESTED)
    return True


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
        code_stamp: Callable[[], str] | None = None,
    ) -> None:
        self.width = max(1, int(width))
        self.execute = execute or _execute
        self.code_stamp = code_stamp
        self._threads: list[threading.Thread] = []
        self._tick_lock = threading.Lock()
        self._draining = False
        self._refill_wakeup = threading.Event()
        self._refill_stop = threading.Event()
        self._refill_thread: threading.Thread | None = None
        self._refill_armed_stamp: str | None = None

    def start_refill(self) -> None:
        """Start the thread that takes the next request as soon as a slot frees.

        An admission pass runs every 60 to 80 seconds, and longer under load,
        while one promotion holds its slot for minutes. A queue refilled only by
        the pass leaves a freed slot empty for the rest of that interval, so the
        drain is driven by the promotions themselves. ``crew admit --once`` and
        a test that drives single passes explicitly start nothing here.
        """
        if self._refill_thread is not None and self._refill_thread.is_alive():
            return
        self._refill_armed_stamp = (
            None if self.code_stamp is None else self.code_stamp()
        )
        self._refill_stop.clear()
        self._refill_thread = threading.Thread(
            target=self._refill,
            name="promotion-refill",
            daemon=True,
        )
        self._refill_thread.start()

    def stop_refill(self) -> None:
        """Stop taking new requests; work already running still finishes."""
        self._refill_stop.set()
        self._refill_wakeup.set()
        thread = self._refill_thread
        if thread is not None:
            thread.join(5.0)
        self._refill_thread = None

    def wake(self) -> None:
        """A slot has freed or a request has been queued: refill without a pass."""
        self._refill_wakeup.set()

    def _code_moved(self) -> bool:
        """Whether the checkout's code has moved past the image this runs.

        A replacement image ends this process's threads, so a request started
        here would be cut off mid-promotion by that replacement. An unreadable
        stamp is not evidence of a move, and must never stop the drain.
        """
        if self.code_stamp is None:
            return False
        try:
            return self.code_stamp() != self._refill_armed_stamp
        except Exception:  # noqa: BLE001 - an unreadable stamp is not a move
            return False

    def _refill(self) -> None:
        """Read the queue and start what fits, on its own clock.

        One failure is recorded and the loop carries on: a queue that could not
        be read this second, or a promotion that raised while starting, must not
        end the only thread that refills a freed slot for the rest of the
        service's life.
        """
        while not self._refill_stop.is_set():
            self._refill_wakeup.wait(REFILL_POLL_SECONDS)
            self._refill_wakeup.clear()
            if self._refill_stop.is_set() or self._draining:
                return
            try:
                if self._code_moved():
                    # Hand the replacement to the admission loop: it waits for
                    # the promotions in flight and re-executes the image.
                    self.begin_drain()
                    return
                self.tick()
            except Exception:  # one failed refill must not stop the drain
                _LOGGER.exception("promotion refill failed; the next tick retries")

    def begin_drain(self) -> None:
        """Start no further promotion; work already running still finishes.

        The decision is taken under the tick lock, so it cannot land between a
        tick reading the queue and that tick starting a promotion: this call
        returns only once no tick is in flight, and every tick after it refuses.
        """
        with self._tick_lock:
            self._draining = True
        self._refill_wakeup.set()

    def wait_for_in_flight(self, timeout: float | None = None) -> list[str]:
        """Wait for this instance's promotions, returning those still running.

        A timeout is never used by the reload path: cutting a promotion off is
        the outcome the drain exists to prevent. It is offered so a caller that
        is not replacing the process can bound its own wait.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        still_running: list[str] = []
        # Read under the tick lock: a tick that was in flight when the drain was
        # decided has appended its threads before this snapshot is taken, and no
        # later tick can append one.
        with self._tick_lock:
            in_flight = list(self._threads)
        for thread in in_flight:
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
        """Start available requests without waiting for any promotion.

        Requests are taken in the order :func:`_pending_requests` gives, so a
        review starts ahead of the run it reviews when both are waiting.

        Two callers tick this instance — the admission pass and the refill
        thread — so a tick holds ``_tick_lock`` for its whole body. A drain
        decision taken between two ticks would otherwise be made while a tick
        was half done, and that tick would start a promotion after the reload
        had already been decided, and after the set of threads the reload waits
        for had been read.
        """
        with self._tick_lock:
            return self._tick_locked()

    def _tick_locked(self) -> list[str]:
        """One tick, with the drain decision serialised against it."""
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        if self._draining:
            return []
        launched: list[str] = []
        for request in _pending_requests():
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
                reason = str(
                    result.get("reason")
                    or result.get("error")
                    or result.get("detail")
                    or "promotion refused"
                )
                if _defer_to_a_queued_review(request, reason):
                    return
                _finish(
                    request,
                    REFUSED,
                    ok=False,
                    error=reason,
                    result=result,
                )
                return
            commits = _promoted_commits(result)
            _finish(request, PROMOTED, result=result, commits=commits)
        except Exception as exc:  # noqa: BLE001 - retain every run's own failure
            phase = REFUSED if isinstance(exc, CrewError) else FAILED
            if isinstance(exc, CrewError) and _defer_to_a_queued_review(request, exc):
                return
            _finish(request, phase, ok=False, error=f"{type(exc).__name__}: {exc}")
        finally:
            for renewer in renewers:
                renewer.stop()
            for lease in reversed(leases):
                self._release(lease)
            # The slot that just freed takes the next waiting request now,
            # not when the admission pass next comes round.
            self.wake()

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
