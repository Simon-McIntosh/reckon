"""Bounded, append-only instants for a delivered run's handoffs.

The wire rows are compact arrays: epoch microseconds, stage index, attempt,
outcome and reason. Readers expand them to named fields. Existing bytes never
change, including after a retry. At capacity an overflow marker consumes the
reserved tail; later intervals remain unknown rather than losing old attempts.
Reasons have a UTF-8 byte bound and explicitly mark truncation. This keeps fifty
ordinary request/start/outcome triples below sixteen kilobytes. The hard byte
bound also holds for arbitrary input and arbitrarily many attempts.

A receipt proves the named event, not that a person read or acted on a pane.
Missing adjacent handoffs make the whole intervening interval unknown. Census
intervals are half-open and disjoint; their sum is the observed run duration.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew import runs
from reckon.node_exclusive_lock import exclusive_lock

MAX_BYTES = 16_000
STAGES = (
    "delivery_recorded",
    "delivery_shown",
    "review_requested",
    "review_admitted",
    "review_started",
    "review_record_written",
    "promotion_requested",
    "promotion_attempt",
    "promotion_outcome",
    "promotion_settled",
    "landing",
    "overflow",
)
_STAGE_NAMES = dict(enumerate(STAGES))
_DELIVERED = frozenset({"complete", "completed_unpromoted", "scoring", "promotable"})
_PREVIOUS = {
    "delivery_shown": "delivery_recorded",
    "review_requested": "delivery_recorded",
    "review_admitted": "review_requested",
    "review_started": "review_admitted",
    "review_record_written": "review_started",
    "promotion_requested": "review_record_written",
    "promotion_attempt": "promotion_requested",
    "promotion_outcome": "promotion_attempt",
    "promotion_settled": "promotion_outcome",
    "landing": "promotion_settled",
}
_PAIRED = frozenset({"review_started", "review_record_written", "promotion_outcome"})


def receipt_path(run_id: str) -> Path:
    """Resolve the journal through the run's durable archive location."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise ValueError(f"invalid receipt run id {run_id!r}")
    return runs.run_dir(run_id) / "stage-receipts.jsonl"


def _bounded(value: Any, limit: int) -> str:
    encoded = str(value or "").encode("utf-8")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    return encoded[: limit - 3].decode("utf-8", errors="ignore") + "…"


def _attempt_key(value: str) -> str:
    """Keep long review identities distinct when their head suffixes differ."""
    encoded = value.encode("utf-8")
    return (
        value if len(encoded) <= 96 else "sha256:" + hashlib.sha256(encoded).hexdigest()
    )


def _encode(row: list[Any]) -> bytes:
    while row and row[-1] == "":
        row.pop()
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def record_stage(
    run_id: str,
    stage: str,
    *,
    at: str | None = None,
    attempt: str = "",
    outcome: str = "",
    reason: str = "",
) -> bool:
    """Append and fsync an event once; return false if the journal is full.

    A stage/attempt/outcome identity deduplicates replayed observations without
    rewriting the event's first instant. A distinct attempt remains distinct.
    The shared record lock serialises writers across threads and hosts.
    """
    moment = datetime.now(UTC) if at is None else parse_utc(at)
    if moment is None:
        raise ValueError(f"invalid receipt instant {at!r}")
    code = STAGES.index(stage)
    stamp = round(moment.timestamp() * 1_000_000)
    row = [
        stamp,
        code,
        _attempt_key(attempt),
        _bounded(outcome, 48),
        _bounded(reason, 96),
    ]
    path = receipt_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(path.with_suffix(".lock")):
        data = path.read_bytes() if path.exists() else b""
        rows = _decode(data)
        if any(item["stage"] == "overflow" for item in rows):
            return False
        if any(
            (item["stage"], item["attempt"], item["outcome"]) == (stage, row[2], row[3])
            for item in rows
        ):
            return True
        payload = _encode(row)
        # Leave room for a single explicit saturation marker, including a
        # newline separating a partial append left by an interrupted writer.
        if len(data) + len(payload) > MAX_BYTES - 80:
            payload = _encode([stamp, STAGES.index("overflow")])
            accepted = False
        else:
            accepted = True
        if data and not data.endswith(b"\n"):
            payload = b"\n" + payload
        with path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return accepted


def _decode(data: bytes) -> list[dict[str, Any]]:
    result = []
    for line in data.splitlines():
        try:
            stamp, code, *fields = json.loads(line)
            stage = _STAGE_NAMES[code]
            at = datetime.fromtimestamp(stamp / 1_000_000, UTC).isoformat()
        except (ValueError, TypeError, IndexError, OverflowError, KeyError):
            # An interrupted append is an explicit gap, never a zero duration.
            result.append(
                {
                    "at": None,
                    "stage": "unknown",
                    "attempt": "",
                    "outcome": "",
                    "reason": "unreadable receipt",
                }
            )
            continue
        fields += [""] * (3 - len(fields))
        result.append(
            {
                "at": at,
                "stage": stage,
                "attempt": fields[0],
                "outcome": fields[1],
                "reason": fields[2],
            }
        )
    return result


def read_receipts(run_id: str) -> list[dict[str, Any]]:
    """Read events in instant order, preserving append order at equal times."""
    path = receipt_path(run_id)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    return sorted(_decode(data), key=lambda row: row["at"] or "")


def stage_census(run_ids: Iterable[str], *, until: str | None = None) -> dict[str, Any]:
    """Partition every observed interval once; missing handoffs stay unknown."""
    totals: Counter[str] = Counter()
    intervals = []
    unread = []
    for run_id in run_ids:
        rows = read_receipts(run_id)
        dated = [row for row in rows if row["at"] is not None]
        if len(dated) < 2:
            unread.append(run_id)
        if not dated:
            continue
        if dated[0]["stage"] != "delivery_recorded":
            unread.append(run_id)
        corrupt = len(dated) != len(rows)
        if dated[-1]["stage"] != "landing" and until is not None:
            end = parse_utc(until)
            if end is None or end < parse_utc(dated[-1]["at"]):
                raise ValueError("census end precedes the last receipt")
            dated.append({"at": end.isoformat(), "stage": "unknown", "attempt": ""})
        seen: set[tuple[str, str]] = set()
        shown = any(row["stage"] == "delivery_shown" for row in dated)
        pending_attempts: set[str] = set()
        for left, right in itertools.pairwise(dated):
            seen.add((left["stage"], left["attempt"]))
            if left["stage"] == "promotion_attempt":
                pending_attempts.add(left["attempt"])
            elif left["stage"] == "promotion_outcome":
                pending_attempts.discard(left["attempt"])
            duration = (parse_utc(right["at"]) - parse_utc(left["at"])).total_seconds()
            stage = left["stage"]
            expected = _PREVIOUS.get(right["stage"])
            present = (
                (expected, right["attempt"]) in seen
                if right["stage"] in _PAIRED
                else any(previous == expected for previous, _ in seen)
            )
            if (
                corrupt
                or not present
                or left["stage"] in {"overflow", "unknown"}
                or (left["stage"] == "delivery_recorded" and not shown)
                or (right["stage"] == "promotion_requested" and pending_attempts)
            ):
                stage = "unknown"
            totals[stage] += duration
            intervals.append(
                {
                    "run_id": run_id,
                    "stage": stage,
                    "start": left["at"],
                    "end": right["at"],
                    "seconds": duration,
                }
            )
    return {
        "seconds_by_stage": dict(totals),
        "unknown_seconds": totals["unknown"],
        "intervals": intervals,
        "unread_runs": list(dict.fromkeys(unread)),
    }


def note_delivery(event: Mapping[str, Any], *, shown: bool = False) -> None:
    """Record a delivered transition or its successful display to its owner."""
    run_id = str(event.get("run_id") or "")
    if run_id and event.get("to_state") in _DELIVERED:
        record_stage(run_id, "delivery_shown" if shown else "delivery_recorded")


def note_review(
    pointer: Mapping[str, Any], stage: str, *, at: str | None = None
) -> None:
    """Resolve a review's subject through the existing node-id resolver."""
    from reckon.crew.recovery_review_delivery import _resolved_reviewed_run_id

    subject = str(pointer.get("reviewed_run_id") or "") or _resolved_reviewed_run_id(
        pointer, str(pointer.get("project") or "")
    )
    if subject:
        record_stage(subject, stage, at=at, attempt=str(pointer.get("run_id") or ""))


def note_review_record(record: Mapping[str, Any]) -> None:
    """Record a completed review write against the run it actually reviewed."""
    subject = str(record.get("reviewed_run_id") or "")
    if subject:
        record_stage(
            subject,
            "review_record_written",
            attempt=str(record.get("review_run_id") or ""),
        )


def note_promotion_outcome(
    request: Mapping[str, Any], phase: str, result: Mapping[str, Any]
) -> None:
    """Retain each attempt outcome independently of the replaceable request."""
    run_id = str(request["run_id"])
    record_stage(
        run_id,
        "promotion_outcome",
        attempt=str(request.get("attempt_id") or request["request_id"]),
        outcome=phase,
        reason=str(result.get("error") or result.get("reason") or ""),
    )
    if phase == "promoted":
        record_stage(run_id, "promotion_settled")
        landed = ((result.get("result") or {}).get("steps") or {}).get("land") or {}
        if landed.get("ok"):
            record_stage(run_id, "landing")
