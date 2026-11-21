"""Raise a lost session's implement work as one decision for the lead.

A session nothing has heard for the flight threshold is lost. Its reviews are
retired by the pass that finds them, because nothing composes a review under a
session that is gone. Implement work is different: no reflex composes it again,
so dropping it would lose the work silently. It is raised instead -- one open
decision per lost session, listing every record the session still owns with the
project, plan, section, phase and age, and the work each one holds back.

The decision is written once and updated in place as the list changes; a pass
that finds the same list writes nothing. It is answered per record: adopt moves
a record's ownership to a live session, retire removes a request with a discard
record or records a run's disposition without promoting it. Until it is
answered, the session's implement requests launch nothing and its landing groups
are skipped, without a refusal being recorded against any of them on each pass.

A record the classifier already reads as promoted needs no answer: its pointer
is retired by the pass, with the reason recorded, and it is left out of the
decision.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._store import write_json_atomically

from . import runs
from .run_paths import crew_home
from .runs import parse_utc

#: The two answers a decision takes per record.
ADOPT = "adopt"
RETIRE = "retire"
ACTIONS: tuple[str, ...] = (ADOPT, RETIRE)

#: The field that marks a decision as raised for a lost session.
SESSION_FIELD = "lost_session"

#: The decision's own option list, so the view can render it like any other.
OPTIONS = [
    {"value": ADOPT, "label": "Adopt into a live session"},
    {"value": RETIRE, "label": "Retire the work"},
]

#: Roles whose records a lost session owns but which the review rule retires.
REVIEW_ROLE = "review"

#: Phases a lost session's record may be in and still be raised.
RAISED_PHASES = (
    "queued",
    "awaiting-upstream",
    "blocked-upstream",
    "held",
    "blocked",
    "complete",
    "scoring",
    "promotable",
    "launch-failed",
    "started",
    "working",
    "stopped",
    "interrupted",
)


def decision_id(session: str) -> str:
    """A stable decision identity for one lost session.

    The identity is derived from the session so a pass that raises the same
    session's work again updates the same record instead of writing a second.
    The identity grammar admits lowercase letters, digits and hyphens only.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", str(session).lower()).strip("-")
    return f"lost-session-{slug or 'unknown'}"


def _decisions_dir() -> Path:
    return crew_home() / "decisions"


def decision_path(session: str) -> Path:
    return _decisions_dir() / f"{decision_id(session)}.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _is_open(record: Mapping[str, Any]) -> bool:
    """A raised decision is open until it carries a choice."""
    return bool(record.get(SESSION_FIELD)) and not str(record.get("choice") or "")


def open_decisions() -> list[dict[str, Any]]:
    """Every raised decision that has not been answered, newest name last."""
    found: list[dict[str, Any]] = []
    directory = _decisions_dir()
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        record = _read(path)
        if record is not None and _is_open(record):
            found.append(record)
    return found


def sessions_with_open_decision() -> set[str]:
    """The lost sessions whose implement work is still awaiting an answer."""
    return {
        str(record.get(SESSION_FIELD) or "")
        for record in open_decisions()
        if record.get(SESSION_FIELD)
    }


def _role(pointer: Mapping[str, Any]) -> str:
    node = pointer.get("node") or {}
    return str(pointer.get("role") or node.get("role") or "")


def _already_promoted(pointer: Mapping[str, Any]) -> bool:
    """A pointer the classifier already reads as promoted or landed elsewhere."""
    phase = str(pointer.get("phase") or "")
    if phase in {"promoted", "landed"}:
        return True
    if pointer.get("promoted_at"):
        return True
    return str(pointer.get("promotion_status") or "") in {"promoted", "landed"}


def _age_hours(pointer: Mapping[str, Any], moment: datetime) -> float | None:
    created = parse_utc(pointer.get("created_at") or pointer.get("queued_at"))
    if created is None:
        return None
    return round(max(0.0, (moment - created).total_seconds()) / 3600.0, 2)


def _identity(pointer: Mapping[str, Any]) -> tuple[str, str]:
    node = pointer.get("node") or {}
    options = pointer.get("dispatch_options") or {}
    plan = str(
        pointer.get("plan") or node.get("plan") or options.get("plan_slug") or ""
    )
    section = str(pointer.get("section") or node.get("section") or "").removeprefix("#")
    return plan, section


def _holds_back(pointer: Mapping[str, Any]) -> str:
    """What one record is holding back, in the terms the lead needs to decide."""
    phase = str(pointer.get("phase") or "")
    if phase in {"queued", "held", "launch-failed"}:
        return "a place in the queue that never launches under a lost session"
    if phase in {"awaiting-upstream", "blocked-upstream"}:
        detail = str(pointer.get("wait_detail") or "").strip()
        return "its section, which it waits on" + (f" ({detail})" if detail else "")
    if phase in {"blocked", "stopped", "interrupted", "started", "working"}:
        return "its section, which it can no longer finish"
    if phase in {"complete", "scoring", "promotable"}:
        return "a promotion nobody will request, and any review it owes"
    return "its section"


def _row(pointer: Mapping[str, Any], moment: datetime) -> dict[str, Any]:
    plan, section = _identity(pointer)
    return {
        "run_id": str(pointer.get("run_id") or ""),
        "project": str(pointer.get("project") or ""),
        "plan": plan,
        "section": section,
        "phase": str(pointer.get("phase") or ""),
        "age_hours": _age_hours(pointer, moment),
        "holds_back": _holds_back(pointer),
    }


def _discard(run_id: str, *, reason: str, fields: Mapping[str, Any]) -> str:
    """Discard one pointer through the shared departure path, keeping the reason."""
    from . import recovery_review_dispatch

    return recovery_review_dispatch._discard_stranded_review(
        run_id, reason=reason, fields=dict(fields)
    )


def _retire_promoted(pointer: Mapping[str, Any], *, moment: datetime) -> str:
    """Retire an already-promoted pointer of a lost session, with the reason."""
    run_id = str(pointer.get("run_id") or "")
    session = str(pointer.get("session") or "")
    reason = (
        f"retired by the lost-session pass: session {session!r} is lost and this "
        "pointer is already promoted, so it holds nothing for the lead to answer"
    )
    _discard(
        run_id,
        reason=reason,
        fields={
            SESSION_FIELD: session,
            "retired_reason": "already-promoted",
            "retired_at": moment.isoformat(),
        },
    )
    return run_id


def _signature(records: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    """The list of records without their moving age, for change detection."""
    return sorted(
        (
            record.get("run_id"),
            record.get("project"),
            record.get("plan"),
            record.get("section"),
            record.get("phase"),
            record.get("holds_back"),
        )
        for record in records
    )


def _group(live: Iterable[Mapping[str, Any]], session: str) -> list[Mapping[str, Any]]:
    return [pointer for pointer in live if str(pointer.get("session") or "") == session]


def raise_lost_session_work(
    live: Sequence[Mapping[str, Any]],
    *,
    answers: Iterable[Mapping[str, Any]],
    moment: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record or update one open decision per lost session's implement work.

    ``answers`` is the lost-session read the pass already took, one dict per
    session. Only a session read lost is raised. A review request of a lost
    session is left to the review rule; an already-promoted pointer is retired
    here, with the reason recorded; a request the pass retired as stranded is
    already absent from ``live`` and is not listed.

    Returns the decision ids written this pass, those already open and
    unchanged, the sessions whose decision is open (so the pass can suppress
    their launches), and the promoted pointers it retired.
    """
    now = datetime.now(tz=UTC) if moment is None else moment
    lost = {
        str(answer.get("session") or ""): answer
        for answer in answers
        if answer.get("lost") and str(answer.get("session") or "")
    }
    written: list[str] = []
    unchanged: list[str] = []
    retired: list[str] = []
    raised_sessions: list[str] = []
    for session, answer in sorted(lost.items()):
        rows: list[dict[str, Any]] = []
        rows_discarded: list[str] = []
        for pointer in _group(live, session):
            phase = str(pointer.get("phase") or "")
            if phase == "retired":
                continue
            run_id = str(pointer.get("run_id") or "")
            if not run_id or not runs.pointer_path(run_id).is_file():
                continue
            if _role(pointer) == REVIEW_ROLE:
                continue
            if _already_promoted(pointer):
                retired_id = _retire_promoted(pointer, moment=now)
                if retired_id:
                    retired.append(retired_id)
                    rows_discarded.append(retired_id)
                continue
            if phase not in RAISED_PHASES:
                continue
            rows.append(_row(pointer, now))
        if not rows:
            continue
        raised_sessions.append(session)
        path = decision_path(session)
        existing = _read(path)
        rows.sort(key=lambda row: row["run_id"])
        if existing is not None and _is_open(existing):
            # Age moves continuously, so it is not part of the identity of the
            # list: a decision whose records are the same ones is not rewritten
            # just because a later pass reads an older age. It is refreshed when
            # the list itself changes.
            if _signature(existing.get("records") or ()) == _signature(rows):
                unchanged.append(str(existing.get("id") or decision_id(session)))
                continue
            record = {
                **existing,
                "records": rows,
                "held_requests": [row["run_id"] for row in rows],
                "modified": now.isoformat(),
            }
        else:
            first = rows[0]
            record = {
                "id": decision_id(session),
                "source": "crew",
                "project": first["project"],
                "session": session,
                "plan": first["plan"],
                "section": first["section"],
                "question": (
                    f"Session {session!r} was last heard at "
                    f"{answer.get('last_heard_at')}, longer ago than "
                    "lost_session.after_hours. Its implement work is left with "
                    "nobody to answer it. Adopt each record into a live session, "
                    "or retire it."
                ),
                "context": (
                    "A dropped review is composed again by the reflex under a "
                    "live session; nothing composes a dropped implement request "
                    "again, so dropping it would lose the work silently."
                ),
                "options": list(OPTIONS),
                "recommended": RETIRE,
                "recommended_by": session,
                "recommendation_source": "unknown",
                "risk": "the work is lost if retired without adoption",
                "lead_action": (
                    "answer each record: adopt names a live session, retire "
                    "removes the request or records the run's disposition"
                ),
                "choice": "",
                "recorded_at": now.isoformat(),
                "modified": now.isoformat(),
                SESSION_FIELD: session,
                "last_heard_at": answer.get("last_heard_at"),
                "lost_since": answer.get("lost_since"),
                "records": rows,
                "held_requests": [row["run_id"] for row in rows],
                "held_runs": [],
                "held_sections": sorted(
                    {row["section"] for row in rows if row["section"]}
                ),
                "answers": {},
            }
            path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomically(path, record, fsync_directory=True)
        written.append(str(record["id"]))
    return {
        "written": written,
        "unchanged": unchanged,
        "sessions": raised_sessions,
        "retired": retired,
    }


def _answer_entries(answers: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(answers, Mapping):
        raise TypeError("answers must be an object keyed by run id")
    normalised: dict[str, dict[str, Any]] = {}
    for run_id, item in answers.items():
        if not isinstance(item, Mapping):
            raise TypeError(f"answer for {run_id!r} must be an object")
        action = str(item.get("action") or "").strip()
        if action not in ACTIONS:
            raise ValueError(f"answer for {run_id!r} names no action in {ACTIONS}")
        entry: dict[str, Any] = {"action": action}
        if action == ADOPT:
            adopter = str(item.get("session") or "").strip()
            if not adopter:
                raise ValueError(f"adopt for {run_id!r} must name a live session")
            entry["session"] = adopter
        normalised[str(run_id)] = entry
    return normalised


def answer_decision(
    record_id: str,
    answers: Mapping[str, Any],
    *,
    answered_by: str = "",
    moment: datetime | None = None,
) -> dict[str, Any]:
    """Answer a raised decision per record: adopt or retire each one.

    An adopt moves the record's ownership to the named live session, so that
    session's view lists the run and the lost session's does not. A retire
    removes a request with a discard record and records a run's disposition
    without promoting it. The record then carries its answers and is closed, so
    it leaves the open view; the answers themselves are the audit.
    """
    now = datetime.now(tz=UTC) if moment is None else moment
    path = _decisions_dir() / f"{record_id}.json"
    record = _read(path)
    if record is None:
        raise ValueError(f"no decision {record_id!r}")
    if not _is_open(record):
        raise ValueError(f"decision {record_id!r} is already answered")
    entries = _answer_entries(answers)
    session = str(record.get(SESSION_FIELD) or "")
    moved: list[str] = []
    retired: list[str] = []
    for run_id, entry in sorted(entries.items()):
        if entry["action"] == ADOPT:
            adopter = entry["session"]

            def adopt(current: dict[str, Any], _to: str = adopter) -> dict[str, Any]:
                current["session"] = _to
                current["adopted_from"] = session
                return current

            runs._mutate_pointer(run_id, adopt)
            moved.append(run_id)
        else:
            reason = (
                f"retired by the lead's answer to the lost-session decision "
                f"{record_id!r}: session {session!r} is lost and nobody adopted "
                "this record"
            )
            _discard(
                run_id,
                reason=reason,
                fields={
                    SESSION_FIELD: session,
                    "retired_reason": "answered-retire",
                    "decision_id": record_id,
                    "retired_at": now.isoformat(),
                },
            )
            retired.append(run_id)
    closed = {
        **record,
        "answers": entries,
        "choice": "answered",
        "answered_by": answered_by,
        "answered_at": now.isoformat(),
        "modified": now.isoformat(),
    }
    write_json_atomically(path, closed, fsync_directory=True)
    return {
        "ok": True,
        "decision_id": record_id,
        "adopted": moved,
        "retired": retired,
    }


def suppress_lost_session_requests(
    queued: Sequence[Mapping[str, Any]],
    *,
    sessions: Iterable[str] | None = None,
) -> tuple[list[Mapping[str, Any]], list[str]]:
    """Drop the implement requests of sessions whose decision is open.

    A request with an open decision launches nothing until the lead answers it.
    The drop is silent: no refusal is recorded against the request on each pass,
    because the decision is already the record of why it is held. Only requests
    are touched -- a run already in flight is not this pass's to stop.
    """
    open_sessions = (
        set(sessions) if sessions is not None else sessions_with_open_decision()
    )
    kept: list[Mapping[str, Any]] = []
    skipped: list[str] = []
    for pointer in queued:
        session = str(pointer.get("session") or "")
        if session in open_sessions and _role(pointer) != REVIEW_ROLE:
            skipped.append(str(pointer.get("run_id") or ""))
            continue
        kept.append(pointer)
    return kept, skipped


def skip_lost_session_landings(
    pointer: Mapping[str, Any],
    *,
    sessions: Iterable[str] | None = None,
) -> str:
    """Why a lost session's landing is skipped, or the empty string.

    A landing group containing a lost session's member is skipped as one group,
    with one reason, rather than refused on every pass: the work cannot be
    promoted by a session nobody can answer for, and the decision already names
    it.
    """
    open_sessions = (
        set(sessions) if sessions is not None else sessions_with_open_decision()
    )
    session = str(pointer.get("session") or "")
    if session and session in open_sessions:
        return (
            f"the run's session {session!r} is lost and its work waits on the "
            "lead's decision; nothing lands under it until the decision is answered"
        )
    return ""
