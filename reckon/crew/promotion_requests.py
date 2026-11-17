"""Durable promotion requests drained by the fleet's admission service.

The request file is authoritative until a terminal receipt is written in the
run directory. It is removed only after that receipt, so a service restart
finds either the request or its completed outcome. Promotion itself retains its
existing guards, lease and idempotent ledger/release path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from contextvars import Context, ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from reckon import checkout_trust
from reckon._timestamps import parse_utc
from reckon.crew import runs, stage_receipts
from reckon.crew.host_lease import HostLease, local_host
from reckon.crew.node import CrewError
from reckon.crew.queue_order import _override, fair_queue_order_details

_LOGGER = logging.getLogger(__name__)

# Capture the imported image once: HEAD can advance during a judgement.
_JUDGING_CODE_REVISION = checkout_trust.running_code_identity(
    Path(__file__).resolve().parent.parent
)

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
# Parked reviews are rechecked without a slot. A run waiting on one waits
# rather than being refused for a review that is about to be stored.
QUEUED = PENDING | {WAITING}
ERRORS = frozenset({FAILED, REFUSED})
# A refusal whose run has since been promoted, landed, discarded or has no live
# pointer is no longer the coordinator's to answer: the phase records that the
# request is settled, and the file is kept so the refusal stays readable.
SETTLED = "promotion-settled"

# How long the drain waits for a wakeup before looking for requests another
# process queued. A freed slot wakes it immediately, so this only bounds the
# latency of a request nobody told it about.
REFILL_POLL_SECONDS = 2.0
# A parked request older than half an hour is owed to its owning session.
WAITING_OBLIGATION_SECONDS = 1800.0
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
PREPARATION_DONE: ContextVar[Callable[[], None] | None] = ContextVar(
    "promotion_preparation_done", default=None
)
PROMOTION_STAGE: ContextVar[Callable[[str], None] | None] = ContextVar(
    "promotion_stage", default=None
)


def claim_promotion_lease(name: str) -> HostLease | None:
    """Claim one renewable resource, including between threads in one process."""
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


def release_promotion_lease(lease: HostLease) -> None:
    """Release only this claim; a preparation slot may already have a successor."""
    try:
        lease.release()
    finally:
        with _CLAIMS_LOCK:
            _CLAIMS.discard(str(lease.path.parent.parent / lease.path.stem))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _current_code_revision() -> str:
    """The source identity the service's existing reloader would load."""
    return checkout_trust.source_code_identity(Path(__file__).resolve().parent.parent)


def _refusal_uses_replaced_code(request: Mapping[str, Any]) -> bool:
    """Retry a superseded verdict only once the current image is running.

    Content identities stay stable across commits that change no runtime code.
    An unstamped verdict proves no code change, and an unknown source identity
    proves no reload, so neither supplies authority to retry a terminal refusal.
    """
    judged = request.get("code_revision")
    return bool(
        request.get("phase") == REFUSED
        and isinstance(judged, str)
        and judged not in {"", checkout_trust.IDENTITY_UNKNOWN, _JUDGING_CODE_REVISION}
        and _JUDGING_CODE_REVISION != checkout_trust.IDENTITY_UNKNOWN
        and _current_code_revision() == _JUDGING_CODE_REVISION
        and runs.pointer_path(str(request["run_id"])).exists()
    )


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
        "code_revision": receipt.get("code_revision"),
        "session": receipt.get("session"),
        "requested_at": receipt.get("requested_at"),
        "finished_at": receipt.get("finished_at"),
        "waited_seconds": _waited_seconds(
            receipt.get("requested_at"), receipt.get("finished_at")
        ),
        "stage_seconds": receipt.get("stage_seconds", {}),
    }
    path = _drained_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError:
        return
    _trim_drained_log(path)


def _recent_drained(moment: datetime, window_seconds: float) -> list[dict[str, Any]]:
    """Read the bounded service history shared by fairness and the rate report."""
    try:
        lines = _drained_log_path().read_text().splitlines()
    except OSError:
        return []
    recent = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        stamp = parse_utc(row.get("finished_at"))
        if (
            stamp is not None
            and 0 <= (moment - stamp).total_seconds() <= window_seconds
        ):
            recent.append(row)
    return recent


def promotion_queue_report(
    *, now: datetime | None = None, window_seconds: float = 3600.0
) -> dict[str, Any]:
    """What the queue holds now and how fast it has drained recently.

    ``waiting`` counts requests whose preparation can start next, ``running``
    includes preparation, branch writes and their waits, and ``parked`` those
    held outside every slot
    because the record a reviewer still owes is not written yet. The oldest
    wait is measured from the request that has waited longest, so a queued
    request that is stuck shows up even while younger ones drain past it.
    """
    moment = now or datetime.now(UTC)
    waiting: list[dict[str, Any]] = []
    running = 0
    parked = 0
    for request in _pending_requests(now=moment):
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
    stages: Counter[str] = Counter()
    for row in _recent_drained(moment, window_seconds):
        for stage, elapsed in (row.get("stage_seconds") or {}).items():
            stages[str(stage)] += float(elapsed)
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
        "requests": [
            {
                "run_id": request["run_id"],
                "session": request.get("session", ""),
                "position": position,
                "reason": request["ordering_reason"],
            }
            for position, request in enumerate(waiting, 1)
        ],
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
        "successful_promotions_per_hour": (
            counted["promoted"] * 3600 / window_seconds if window_seconds > 0 else None
        ),
        "stage_seconds_last_window": dict(stages),
        "rate_bounded_by": max(stages, key=stages.get) if stages else None,
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
    record = _read(_request_path(run_id)) or _read(_receipt_path(run_id))
    if not record and runs.pointer_path(run_id).exists():
        pointer = runs.read_pointer(run_id)
        leader = str(pointer.get("landing_group") or "")
        if leader and leader != run_id:
            return _read(_request_path(leader)) or _read(_receipt_path(leader))
    return record


def _mark_pointer(
    request: Mapping[str, Any], phase: str, *, readiness_checked: bool = False
) -> None:
    run_id = str(request["run_id"])
    if phase == REQUESTED and not readiness_checked and _review_self_waits(request)[0]:
        phase = WAITING
    try:
        with runs._pointer_lock(run_id, blocking=False):
            if not runs.pointer_path(run_id).exists():
                return
            pointer = runs.read_pointer(run_id)
            pointer["promotion_status"] = phase
            pointer["promotion_request"] = str(
                _request_path(str(request.get("group_run_id") or run_id))
            )
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
    one arriving through a different caller. A refusal by superseded code is
    retried by the current image; other terminal refusals require an explicit
    coordinator action and retain their visible obligation.
    """
    if kind not in {
        "command",
        "complete",
        "acceptance",
        "review-self",
        "landing-group",
    }:
        raise CrewError(f"unknown promotion request kind {kind!r}")
    path = _request_path(run_id)
    with runs._pointer_lock(f"promotion-request-{run_id}"):
        existing = read_promotion(run_id)
        replaced_code = _refusal_uses_replaced_code(existing)
        if existing.get("phase") == WAITING:
            waiting, reason = _review_self_waits(existing)
            existing = {
                **existing,
                "phase": WAITING if waiting else REQUESTED,
                "checked_at": _now(),
                "waiting_reason": reason,
            }
            runs._write_json(path, existing)
            _mark_pointer(existing, existing["phase"])
        if existing and (
            existing.get("phase") in QUEUED
            or existing.get("phase") == PROMOTED
            or (existing.get("phase") in ERRORS and not retry and not replaced_code)
        ):
            return {**existing, "duplicate": True}
        pointer = runs.read_pointer(run_id)
        if replaced_code and not retry:
            kind = str(existing["kind"])
            options = existing.get("options") or {}
        request = {
            "ok": True,
            "run_id": run_id,
            "request_id": uuid4().hex,
            "phase": REQUESTED,
            "kind": kind,
            "project": str(pointer.get("project") or ""),
            "session": str(pointer.get("session") or ""),
            "priority_override": _override(pointer),
            "node": (pointer.get("node") or {}).get("id", ""),
            "plan": str(pointer.get("plan") or ""),
            "previous_phase": str(pointer.get("phase") or "complete"),
            "requested_at": _now(),
            "options": json.loads(json.dumps(dict(options or {}), default=str)),
        }
        if replaced_code:
            request["previous_verdict"] = {
                key: existing.get(key)
                for key in ("code_revision", "phase", "finished_at", "error")
            }
        if kind == "landing-group":
            priorities = [
                _override(member["pointer"])
                for member in request["options"].get("members") or ()
            ]
            request["priority_override"] = max(
                (priority for priority in priorities if priority is not None),
                default=request["priority_override"],
            )
        runs._write_json(path, request)
        stage_receipts.record_stage(run_id, "promotion_requested", at=request["requested_at"], attempt=request["request_id"])
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
    # Readiness includes the manifest, worker liveness and supervisor exit.
    # An ended review with no record is refused by that same verdict.
    return state == acceptance.REVIEW_SELF_WAITING, reason


def _mark_readiness(
    request: Mapping[str, Any], *, waiting: bool, reason: str
) -> dict[str, Any]:
    """Date a readiness verdict without claiming a promotion slot.

    A parked request keeps its reviewer's working phase. A ready one joins the
    requested population so queue ordering charges its session a future turn.
    The request identity and phase must still match the judged snapshot.
    """
    run_id = str(request["run_id"])
    refreshed = {
        **dict(request),
        "phase": WAITING if waiting else REQUESTED,
        "waiting_reason": reason,
        "checked_at": _now(),
    }
    with runs._pointer_lock(f"promotion-request-{run_id}"):
        current = _read(_request_path(run_id))
        if current.get("request_id") != refreshed["request_id"] or current.get(
            "phase"
        ) != request.get("phase"):
            # Another drain moved this request on while it was being judged.
            return {}
        runs._write_json(_request_path(run_id), refreshed)
        _mark_pointer(refreshed, refreshed["phase"], readiness_checked=True)
    return refreshed


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
    if request["kind"] == "landing-group":
        from reckon.crew_run_commands import _complete_landing_group

        return _complete_landing_group(request)
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
    receipt = {
        **request,
        **result,
        "phase": phase,
        "finished_at": _now(),
        "code_revision": request.get("code_revision", _JUDGING_CODE_REVISION),
    }
    stage_receipts.note_promotion_outcome(request, phase, result)
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
    for member in (request.get("options") or {}).get("members") or ():
        member_id = str(member["pointer"]["run_id"])
        if member_id != run_id:
            member_receipt = {
                **receipt,
                "run_id": member_id,
                "group_run_id": run_id,
                "session": str(member["pointer"].get("session") or ""),
                "priority_override": _override(member["pointer"]),
                "node": (member["pointer"].get("node") or {}).get("id", ""),
                "plan": str(member["pointer"].get("plan") or ""),
            }
            runs._write_json(_receipt_path(member_id), member_receipt)
            _mark_pointer(member_receipt, phase)
            _record_drained(member_receipt)
    return receipt


def _reviewed_run_id(
    request: Mapping[str, Any], *, pointers: Sequence[Mapping[str, Any]] | None = None
) -> str:
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

    project = str(request.get("project") or "")
    try:
        return _resolved_reviewed_run_id(
            {"node": {"id": node_id}, "project": project},
            project,
            pointers=(
                None
                if pointers is None
                else [p for p in pointers if str(p.get("project") or "") == project]
            ),
        )
    except Exception:  # noqa: BLE001 — ordering must never stop the drain
        return ""


def _reviewed_request_runs(
    requests: Iterable[Mapping[str, Any]],
) -> Iterator[tuple[Mapping[str, Any], str]]:
    """Resolve a pass lazily, sharing one listing across source-node reviews."""
    from reckon.crew.recovery_vocabulary import REVIEW_NODE_PREFIX
    from reckon.crew.repair import round_node_source

    pointers = None
    listing_failed = False
    for request in requests:
        source = round_node_source(str(request.get("node") or ""), REVIEW_NODE_PREFIX)
        needs_population = bool(source and not source.startswith("r-"))
        if needs_population and pointers is None:
            try:
                pointers = runs.list_live()
            except Exception:  # noqa: BLE001 — ordering must never stop the drain
                pointers, listing_failed = [], True
        yield (
            request,
            (
                ""
                if needs_population and listing_failed
                else _reviewed_run_id(request, pointers=pointers)
            ),
        )


def _pending_requests(
    *, now: datetime | None = None, window_seconds: float = 3600.0
) -> list[dict[str, Any]]:
    """Reviews, aged requests and priorities precede fair session turns.

    Each urgency class takes one request per session in turn, starting with
    the session served least in the recent window, then its oldest request.
    Taking a higher urgency class does not consume a lower class's turn in
    this snapshot. Completed promotions persist the service count across
    passes and process restarts; running requests reserve a turn too. Only
    requested rows consume simulated future turns: running rows are already
    charged, and parked rows take no slot.
    """
    moment = now or datetime.fromisoformat(_now())
    pending = []
    for path in _requests_dir().glob("*.json"):
        request = _read(path)
        if request.get("phase") in QUEUED:
            if "priority_override" not in request:
                # Requests written without a priority snapshot still inherit
                # the run's priority while its pointer is available.
                with suppress(CrewError):
                    request["priority_override"] = _override(
                        runs.read_pointer(str(request["run_id"]))
                    )
            pending.append(request)
    waiting = {str(request.get("run_id") or "") for request in pending}
    review_first = {
        str(request.get("run_id") or "")
        for request, reviewed in _reviewed_request_runs(pending)
        if reviewed in waiting
    }
    served = Counter(
        str(row["session"])
        for row in _recent_drained(moment, window_seconds)
        if row.get("phase") == PROMOTED and row.get("session")
    )
    served.update(
        str(request.get("session") or "")
        for request in pending
        if request.get("phase") == RUNNING
    )
    return [
        {**request, "ordering_reason": reason}
        for request, reason in fair_queue_order_details(
            pending,
            now=moment,
            joined_at=lambda request: parse_utc(request.get("requested_at")),
            is_review=lambda request: str(request.get("run_id") or "") in review_first,
            served=served,
            consumes_turn=lambda request: request.get("phase") == REQUESTED,
        )
    ]


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
        reviewed == run_id
        for _other, reviewed in _reviewed_request_runs(
            request
            for request in map(_read, _requests_dir().glob("*.json"))
            if request.get("phase") in QUEUED
        )
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
        stage_receipts.note_promotion_outcome(request, DEFERRED, {"reason": str(refusal)})
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
        return claim_promotion_lease(name)

    @staticmethod
    def _release(lease: HostLease) -> None:
        release_promotion_lease(lease)

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
        for path in _requests_dir().glob("*.json"):
            request = _read(path)
            if _refusal_uses_replaced_code(request):
                try:
                    request_promotion(str(request["run_id"]))
                except CrewError as exc:
                    _LOGGER.warning("could not rejudge promotion: %s", exc)
        pending = _pending_requests()
        parked = [request for request in pending if request.get("phase") == WAITING]
        for request in parked:
            waiting, reason = _review_self_waits(request)
            _mark_readiness(request, waiting=waiting, reason=reason)
        if parked:
            # Newly ready requests consume fair session turns before admission.
            pending = _pending_requests()
        launched: list[str] = []
        for request in pending:
            if request.get("phase") == WAITING:
                continue
            waiting, reason = _review_self_waits(request)
            if waiting:
                # Nothing is owed here yet, so this request takes no slot and
                # the loop carries on to the ones behind it.
                _mark_readiness(request, waiting=True, reason=reason)
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
            branch = None
            members = (request.get("options") or {}).get("members") or ()
            repository = ""
            if request.get("kind") == "landing-group" and members:
                repository = str(members[0]["pointer"]["repo"])
            elif (request.get("options") or {}).get("land"):
                with suppress(CrewError):
                    repository = str(
                        runs.read_pointer(str(request["run_id"])).get("repo") or ""
                    )
            if repository:
                identity = hashlib.sha256(repository.encode()).hexdigest()
                branch = self._claim(f"promotion-branch-{identity}")
                if branch is None:
                    self._release(owner)
                    self._release(slot)
                    continue
                if request.get("kind") != "landing-group":
                    # A group excludes new individual landings. Individuals
                    # prepare together and serialize only their branch writes.
                    self._release(branch)
                    branch = None
            thread = threading.Thread(
                target=Context().run,
                args=(self._run, request, owner, *([branch] if branch else []), slot),
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
        preparation_released = False

        def release_preparation() -> None:
            nonlocal preparation_released
            if preparation_released:
                return
            preparation_released = True
            renewers[-1].stop()
            self._release(leases[-1])
            self.wake()

        token = PREPARATION_DONE.set(release_preparation)
        stage = "preparation"
        stage_began = time.monotonic()
        stage_seconds: dict[str, float] = {}

        def mark_stage(name: str) -> None:
            nonlocal stage, stage_began
            now = time.monotonic()
            stage_seconds[stage] = stage_seconds.get(stage, 0.0) + now - stage_began
            stage, stage_began = name, now

        stage_token = PROMOTION_STAGE.set(mark_stage)
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
                    or current.get("phase") not in QUEUED
                ):
                    return
                request = {
                    **current,
                    "attempt_id": uuid4().hex,
                    "phase": RUNNING,
                    "started_at": _now(),
                    "checked_at": _now(),
                    "code_revision": _JUDGING_CODE_REVISION,
                    "waiting_reason": "",
                    "stage_seconds": stage_seconds,
                }
                runs._write_json(_request_path(run_id), request)
                stage_receipts.record_stage(run_id, "promotion_attempt", at=request["started_at"], attempt=request["attempt_id"])
                _mark_pointer(request, RUNNING)
            from reckon.crew.promotion_scope import _branch_write_scope

            with _branch_write_scope():
                result = self.execute(request)
            mark_stage("finished")
            if result is None:
                _finish(request, DEFERRED, result=None)
                return
            if result.get("waiting"):
                # The reviewer is still writing the record: park the request
                # rather than refusing a run that is not finished.
                stage_receipts.note_promotion_outcome(request, WAITING, result)
                _mark_readiness(
                    request, waiting=True, reason=str(result.get("reason") or "")
                )
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
            mark_stage("failed")
            request = {**request, "stage_seconds": stage_seconds}
            phase = REFUSED if isinstance(exc, CrewError) else FAILED
            if isinstance(exc, CrewError) and _defer_to_a_queued_review(request, exc):
                return
            _finish(request, phase, ok=False, error=f"{type(exc).__name__}: {exc}")
        finally:
            PREPARATION_DONE.reset(token)
            PROMOTION_STAGE.reset(stage_token)
            for index in reversed(range(len(leases))):
                if preparation_released and index == len(leases) - 1:
                    continue
                renewers[index].stop()
                self._release(leases[index])
            # The slot that just freed takes the next waiting request now,
            # not when the admission pass next comes round.
            self.wake()

    def settle(self, timeout: float = 300.0) -> None:
        """Wait within an explicit budget for work this instance started."""
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))


def _settled_state(request: Mapping[str, Any]) -> str:
    """Why a request's run no longer needs the duty, or "" while it does.

    A refusal is the coordinator's to answer only while the run it concerns is
    still waiting for one. A run promoted, landed or discarded has arrived where
    the refusal was asking it to go, and a run whose pointer has gone can no
    longer be promoted at all, so in each of those cases the next command the
    duty carries can only fail. The discard marker is read first because it is
    written before the pointer goes, then the ledger row, because a run that
    landed and whose pointer has since gone has still landed; only a run with
    neither a discard record nor a ledger row and no pointer is gone.
    """
    run_id = str(request.get("run_id") or "")
    if not run_id:
        return ""
    from reckon.crew import promotion

    try:
        if promotion.discard_recorded(run_id):
            return "discarded"
    except OSError:
        pass
    try:
        landed = runs._promoted_record_project(
            run_id, str(request.get("project") or "")
        )
    except (OSError, ValueError, CrewError):
        landed = None
    if landed:
        from reckon.crew.recovery_classification import _promoted_awaiting_merge

        return (
            "promoted"
            if _promoted_awaiting_merge(
                {**request, "project": landed}, moment=time.time()
            )
            else "landed"
        )
    if not runs.pointer_path(run_id).exists():
        return "gone"
    return ""


def _settle(request: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Record a refusal as settled in place, keeping everything it carried.

    The request file is rewritten rather than removed: the refusal's reason, the
    attempt it belongs to and the run it named stay readable, and every reader
    that projects a duty skips a phase outside :data:`ERRORS` without any second
    store to reconcile. A request another drain has since moved on is left to
    that drain.
    """
    run_id = str(request["run_id"])
    settled = {
        **dict(request),
        "phase": SETTLED,
        "settled": reason,
        "settled_at": _now(),
    }
    with runs._pointer_lock(f"promotion-request-{run_id}"):
        current = _read(_request_path(run_id))
        if current.get("request_id") != request.get("request_id"):
            return {}
        runs._write_json(_request_path(run_id), settled)
    stage_receipts.record_stage(run_id, "promotion_settled", at=settled["settled_at"], outcome=reason)
    return settled


def settle_requests_for(run_id: str, *, settled_as: str = "promoted") -> list[str]:
    """Settle every request still open for a run another path has finished.

    A promotion that succeeds in the caller's own process already answers the
    queued request for that run, and a discard or a landing answers it too. What
    is left behind is a request the drain would promote again or refuse for a
    reason that no longer applies, so it is settled here instead of being left
    to raise a duty. Only requests for this one run are touched.
    """
    settled: list[str] = []
    for path in _requests_dir().glob("*.json"):
        request = _read(path)
        if str(request.get("run_id") or "") != run_id:
            continue
        if request.get("phase") == SETTLED:
            continue
        if _settle(request, settled_as):
            settled.append(run_id)
    return settled


def failure_obligations(
    project: str, session: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Project failures and overdue waits into the owning session's duties.

    A waiting request takes no promotion slot, but after half an hour its
    owner needs its condition and last observation. Rechecking never resets
    the age, which is measured from the original request.
    """
    items: list[dict[str, Any]] = []
    for path in _requests_dir().glob("*.json"):
        request = _read(path)
        waiting = request.get("phase") == WAITING
        if request.get("phase") not in ERRORS and not waiting:
            continue
        if request.get("project") != project or request.get("session") != session:
            continue
        settled = _settled_state(request)
        if settled:
            # The run has arrived where the refusal asked it to go, or can no
            # longer go anywhere: the request is settled here rather than
            # raising a duty whose next command cannot apply.
            _settle(request, settled)
            continue
        if waiting:
            age = _waited_seconds(
                request.get("requested_at"), (now or datetime.now(UTC)).isoformat()
            )
            if age is None or age <= WAITING_OBLIGATION_SECONDS:
                continue
            checked_at = str(request.get("checked_at") or "unknown")
            items.append(
                {
                    "kind": WAITING,
                    "run_id": request["run_id"],
                    "node": request.get("node", ""),
                    "plan": request.get("plan", ""),
                    "age_seconds": int(age),
                    "checked_at": checked_at,
                    "reason": f"{request.get('waiting_reason') or 'promotion is waiting'}; last checked {checked_at}",
                    "next_command": f"reckon crew complete --run {request['run_id']} --gate {((request.get('options') or {}).get('gate') or 'not-run')}",
                }
            )
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
