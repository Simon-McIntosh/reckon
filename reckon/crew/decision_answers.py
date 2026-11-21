"""The lead's answer: presented by one session, stored once, released on it.

A decision is answered by the lead through the one session presenting it. The
claim this module writes is what makes that path a path rather than a
convention: an answer must name the session that took the claim, so a worker,
or a coordinator reading its own plan, cannot answer anything: the owning
session is refused by name, and any other session is refused by the claim it
does not hold.

Both kinds of decision are answered here. A crew question stores the chosen
option, the note and the time in its own record. A plan-authored decision is
answered by locking it in its plan, and an answer record is written beside the
questions, because that record is what an admission pass reads to know which
held requests the answer released.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from reckon.crew.run_paths import crew_home

# The plan, store, run and flight modules are read where they are used rather
# than when this module is: the coordinator obligations view imports this module
# on the prompt path, and that path must reach no plan, backend or ledger
# derivation. Answering a decision imports them; reading an answer does not.

#: The row one answer appends to its project's monitor stream, and the duty its
#: owning session reads once the answer has reached it.
ANSWERED_EVENT_KIND = "decision-answered"
ANSWERED_DUTY_KIND = "decision-answered"

#: An answer landed in a plan's decisions block writes a record under this kind,
#: beside the questions rather than among them.
PLAN_ANSWER_KIND = "plan-answer"

#: A held request returns to the phase its hold interrupted; the phases a
#: request may return to are the one its own hold record names.
HELD_PHASE = "held"


def _claim_name(decision_id: str) -> str:
    """A filesystem name for one decision's presentation claim.

    A crew decision's identity is already safe as a filename. A plan's
    ``plan:<project>:<slug>:<key>`` identity is not, so its readable part is
    stripped to safe characters and a digest of the whole identity keeps apart
    two identities that strip alike.
    """
    readable = re.sub(r"[^a-z0-9.-]+", "-", str(decision_id).lower()).strip("-")
    digest = hashlib.sha256(str(decision_id).encode("utf-8")).hexdigest()[:8]
    return f"{(readable or 'decision')[-60:]}-{digest}"


def answer_path(answer_id: str) -> Path:
    """The file holding one plan decision's answer.

    A plan decision's answer is a record of its own, kept apart from the
    questions: the read of open questions refuses a file that is not one, so an
    answer written among them would make that read fail rather than hide the
    decision it answers.
    """
    return crew_home() / "answers" / f"{answer_id}.json"


def presenting_directory() -> Path:
    """The directory holding every decision's presentation claim."""
    return crew_home() / "decisions" / "presenting"


def presentation_path(decision_id: str) -> Path:
    """The file holding one decision's presentation claim."""
    return presenting_directory() / f"{_claim_name(decision_id)}.json"


def presenting_claims() -> list[dict[str, Any]]:
    """Every presentation claim on disk, read whole.

    A claim's decision id is carried in the file rather than parsed from its
    name, because the name holds a digest of the identity: two identities that
    read alike stay apart and a name-to-identity parse would have to re-derive
    the digest.
    """
    claims = []
    for path in sorted(presenting_directory().glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        if not isinstance(record, dict):
            raise TypeError(f"claim {path.name} is not an object")
        claims.append(record)
    return claims


def _plan_decision_parts(decision_id: str) -> tuple[str, str, str] | None:
    """``(project, slug, key)`` for a plan-authored decision identity."""
    text = str(decision_id)
    if not text.startswith("plan:"):
        return None
    parts = text[5:].split(":")
    if len(parts) != 3 or not all(parts):
        raise ValueError("a plan decision id reads plan:<project>:<slug>:<key>")
    return parts[0], parts[1], parts[2]


def _checked_identity(decision_id: str) -> tuple[str, str, str] | None:
    """The plan parts of an identity, or ``None`` for a crew question."""
    from reckon.crew.lead_decisions import decision_path

    parts = _plan_decision_parts(decision_id)
    if parts is None:
        decision_path(decision_id)
    return parts


def _read_presentation(decision_id: str) -> dict[str, Any] | None:
    """The claim presenting one decision, or ``None`` when nobody presents it."""
    try:
        text = presentation_path(decision_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    record = json.loads(text)
    if not isinstance(record, dict):
        raise TypeError(f"claim for {decision_id!r} is not an object")
    return record


def claim_presentation(decision_id: str, *, session: str) -> dict[str, Any]:
    """Take one decision for presentation, or name the claim that holds it."""
    session = str(session or "").strip()
    if not session:
        raise ValueError("a presentation claim must name the presenting session")
    _checked_identity(decision_id)
    existing = _read_presentation(decision_id)
    if existing is not None:
        holder = str(existing.get("session") or "")
        if holder == session:
            return existing
        raise ValueError(
            f"decision {decision_id!r} is presented by {holder!r}, so "
            f"{session!r} cannot present it: a decision has one presenter"
        )
    claim = {
        "decision_id": decision_id,
        "session": session,
        "claimed_at": datetime.now(UTC).isoformat(),
    }
    from reckon._store import write_json_atomically

    write_json_atomically(presentation_path(decision_id), claim, fsync_directory=True)
    return claim


def release_presentation(decision_id: str, *, session: str) -> dict[str, Any]:
    """Give up one decision's presentation claim, when this session holds it."""
    existing = _read_presentation(decision_id)
    if existing is None:
        return {"decision_id": decision_id, "released": False}
    holder = str(existing.get("session") or "")
    if holder != str(session or "").strip():
        raise ValueError(
            f"decision {decision_id!r} is presented by {holder!r}, so "
            f"{session!r} cannot release its claim"
        )
    presentation_path(decision_id).unlink(missing_ok=True)
    return {"decision_id": decision_id, "released": True}


# ── The answer ─────────────────────────────────────────────────────────────


def _append_answer_row(
    *, project: str, decision_id: str, choice: str, session: str, at: str
) -> None:
    """Append the one row an answer contributes to its project's monitor."""
    from reckon.crew import runs as crew_runs

    crew_runs._append_watch_lines(
        crew_runs.watch_stream_path(project),
        [
            {
                "kind": ANSWERED_EVENT_KIND,
                "at": at,
                "project": project,
                "decision_id": decision_id,
                "choice": choice,
                "by": session,
            }
        ],
    )


def _read_question(decision_id: str) -> dict[str, Any]:
    """One crew question, read whole, refusing an identity that is not one."""
    from reckon.crew.lead_decisions import decision_path

    path = decision_path(decision_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"no decision {decision_id!r} is recorded") from exc
    record = json.loads(text)
    if not isinstance(record, dict):
        raise TypeError(f"decision {decision_id!r} is not an object")
    return record


def answer_decision(
    decision_id: str, *, session: str, choice: str, note: str = ""
) -> dict[str, Any]:
    """Record the lead's answer to one presented decision.

    The answer stores the chosen option, the note the lead attached and the
    time. It is accepted only from the session holding the presentation claim.
    """
    session = str(session or "").strip()
    if not session:
        raise ValueError("an answer must name the presenting session")
    choice = str(choice or "").strip()
    if not choice:
        raise ValueError("an answer must name the chosen option")
    claim = _read_presentation(decision_id)
    if claim is None:
        raise ValueError(
            f"decision {decision_id!r} is not being presented, so an answer is "
            f"refused: only the presenting session may answer"
        )
    presenter = str(claim.get("session") or "")
    if presenter != session:
        raise ValueError(
            f"decision {decision_id!r} is presented by {presenter!r}, so "
            f"{session!r} cannot answer it"
        )
    parts = _plan_decision_parts(decision_id)
    if parts is not None:
        return _answer_plan_decision(
            decision_id, parts, session=session, choice=choice, note=note
        )
    from reckon.crew.lead_decisions import close_decision_at, recorded_decision

    # The recorder's own validation is the gate: an already-answered or
    # withdrawn record is refused by the rule that admitted it, and the read
    # that follows is the raw record the answer is written onto.
    recorded_decision(decision_id)
    record = _read_question(decision_id)
    offered = [str(option["value"]) for option in record.get("options") or ()]
    if choice not in offered:
        raise ValueError(f"choice {choice!r} is not one of {offered}")
    held = _held_behind(str(record.get("project") or ""), decision_id)
    record.update(
        choice=choice,
        held_requests=held,
        answered_at=datetime.now(UTC).isoformat(),
        answered_by=session,
        answer_note=str(note or "").strip(),
        answer_via="presenting-session",
    )
    record = close_decision_at(record, decision_id=decision_id)
    presentation_path(decision_id).unlink(missing_ok=True)
    _append_answer_row(
        project=str(record["project"]),
        decision_id=decision_id,
        choice=choice,
        session=session,
        at=str(record["answered_at"]),
    )
    return record


def _answer_plan_decision(
    decision_id: str,
    parts: tuple[str, str, str],
    *,
    session: str,
    choice: str,
    note: str,
) -> dict[str, Any]:
    """Answer a plan-authored decision by locking it in its plan."""
    project, slug, key = parts
    from reckon.flight import mounted_project_docs

    docs = mounted_project_docs().get(project)
    if docs is None:
        raise ValueError(f"project {project!r} is not mounted")
    path = docs / "plans" / f"{slug}.html"
    if not path.is_file():
        raise ValueError(f"plan {slug!r} is not readable under {project!r}")
    from reckon._plan_html import read_state_file

    state = read_state_file(path)
    authored = (state.get("decisions") or {}).get(key)
    if authored is None:
        raise ValueError(f"plan {slug!r} names no decision {key!r}")
    if not isinstance(authored, Mapping):
        raise TypeError(f"plan {slug!r} decision {key!r} is not an object")
    if str(authored.get("choice") or "").strip():
        raise ValueError(f"decision {decision_id!r} is already locked in its plan")
    values = [str(value) for value in authored.get("choices") or ()]
    if values and choice not in values:
        raise ValueError(f"choice {choice!r} is not one of {values}")
    held = _held_behind(project, decision_id)
    from reckon.mcp_edit_plan import _lock_decision

    locked = _lock_decision(
        project,
        slug,
        key,
        choice,
        str(note or "").strip(),
        f"the lead (via {session})",
        int(state.get("version") or 0),
    )
    if not locked.get("ok"):
        raise ValueError(
            f"decision {decision_id!r} was not locked: "
            + str(locked.get("detail") or locked.get("error"))
        )
    answered_at = datetime.now(UTC).isoformat()
    sections = [str(item) for item in authored.get("sections") or ()]
    record = {
        "kind": PLAN_ANSWER_KIND,
        "id": str(uuid4()),
        "decision_id": decision_id,
        "source": "plan",
        "project": project,
        "plan": slug,
        "decision_key": key,
        "sections": sections,
        "holds_whole_plan": not sections,
        "choice": choice,
        "answer_note": str(note or "").strip(),
        "answered_at": answered_at,
        "answered_by": session,
        "held_requests": held,
    }
    from reckon._store import write_json_atomically

    write_json_atomically(answer_path(record["id"]), record, fsync_directory=True)
    presentation_path(decision_id).unlink(missing_ok=True)
    _append_answer_row(
        project=project,
        decision_id=decision_id,
        choice=choice,
        session=session,
        at=answered_at,
    )
    return record


def answered_decisions() -> list[dict[str, Any]]:
    """Every answer the lead has recorded, crew questions and plan decisions.

    One reader serves the duty an owning session reads and the release an
    admission pass performs, so the row a session is told about and the hold
    the fleet releases come from one reading. A file that is not an answer,
    or one that cannot be parsed, is skipped.
    """
    answers: list[dict[str, Any]] = []
    for pattern in ("answers", "decisions"):
        for path in sorted((crew_home() / pattern).glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, Mapping):
                continue
            kind = str(record.get("kind") or "").strip()
            if kind == PLAN_ANSWER_KIND:
                answers.append(_plan_answer(record))
            elif not kind and record.get("answered_at"):
                answers.append(_crew_answer(record))
    return answers


def _crew_answer(record: Mapping[str, Any]) -> dict[str, Any]:
    """One crew question's answer, in the shape both consumers read."""
    return {
        "decision_id": str(record.get("id") or ""),
        "source": "crew",
        "project": str(record.get("project") or ""),
        "plan": str(record.get("plan") or ""),
        "decision_key": str(record.get("decision_key") or ""),
        "sections": [str(item) for item in record.get("held_sections") or ()],
        "holds_whole_plan": False,
        "choice": str(record.get("choice") or ""),
        "note": str(record.get("answer_note") or "")[:200],
        "answered_at": str(record.get("answered_at") or ""),
        "answered_by": str(record.get("answered_by") or "")[:200],
        "owner_session": str(record.get("session") or ""),
        "held_requests": [str(item) for item in record.get("held_requests") or ()],
    }


def _plan_answer(record: Mapping[str, Any]) -> dict[str, Any]:
    """One plan decision's answer, in the same shape."""
    sections = [str(item) for item in record.get("sections") or ()]
    return {
        "decision_id": str(record.get("decision_id") or ""),
        "source": "plan",
        "project": str(record.get("project") or ""),
        "plan": str(record.get("plan") or ""),
        "decision_key": str(record.get("decision_key") or ""),
        "sections": sections,
        "holds_whole_plan": not sections,
        "choice": str(record.get("choice") or ""),
        "note": str(record.get("answer_note") or "")[:200],
        "answered_at": str(record.get("answered_at") or ""),
        "answered_by": str(record.get("answered_by") or "")[:200],
        "owner_session": "",
        "held_requests": [str(item) for item in record.get("held_requests") or ()],
    }


def answered_items(
    project: str, session: str, *, now: datetime
) -> list[dict[str, Any]]:
    """The answers whose duty belongs to one owning session, in its project.

    A crew question belongs to the session that asked it: that session is
    waiting on the answer and reads it as its own duty. A plan decision belongs
    to no session, so it releases its held requests without a duty row.
    """
    from reckon._timestamps import parse_utc

    items: list[dict[str, Any]] = []
    for answer in answered_decisions():
        if answer["project"] != project:
            continue
        if answer["owner_session"] != session:
            continue
        moment = parse_utc(answer["answered_at"])
        age = max(0, int((now - moment).total_seconds())) if moment else 0
        items.append(
            {
                "kind": ANSWERED_DUTY_KIND,
                "run_id": (answer["held_requests"] or [""])[0],
                "run_ids": list(answer["held_requests"]),
                "age_seconds": age,
                "observed_at": answer["answered_at"],
                "decision_id": answer["decision_id"],
                "choice": answer["choice"],
                "reason": (
                    f"the lead answered {answer['decision_id']} choosing "
                    f"{answer['choice']!r}: the work it held is released"
                ),
            }
        )
    return items


def _held_behind(project: str, decision_id: str) -> list[str]:
    """The requests held behind one decision, read from the one join defining it.

    The answer records the set at the moment it is given, because that is the
    decision's answer rather than a later reading of the pointers: a request
    held when the lead answered is a request the answer releases, even if a
    coordinator holds another behind the same question afterwards.
    """
    from reckon.crew.lead_decisions import read_decisions

    view = read_decisions(project)
    for row in (
        list(view.get("decisions") or ())
        + list(view.get("pending") or ())
        + list(view.get("taken_and_reversible") or ())
        + list(view.get("plan_block") or ())
    ):
        if str(row.get("id") or "") == str(decision_id):
            return [str(run) for run in row.get("held_requests") or ()]
    return []


def release_answered_holds(live: list[dict[str, Any]]) -> list[str]:
    """Lift the hold on every request held behind an answered decision.

    The hold is written on the request's own pointer, so the pass is what takes
    it back out. The step is idempotent: a released request is no longer held,
    so the next pass finds nothing to do. One pointer that cannot be read is
    skipped rather than ending the pass, and is retried by the next one.
    """
    from reckon.crew.queue_edit import release

    pointers = {
        str(pointer.get("run_id") or ""): pointer
        for pointer in live
        if str(pointer.get("phase") or "") == HELD_PHASE
    }
    released: list[str] = []
    for answer in answered_decisions():
        for run_id in answer["held_requests"]:
            pointer = pointers.get(run_id)
            if pointer is None:
                continue
            try:
                changed = release(
                    run_id, reason=f"the lead answered {answer['decision_id']}"
                )
            except Exception:  # noqa: BLE001, S112 - retried by the next pass
                continue
            # The pass goes on to read these pointers, so the released phase is
            # written back here as well as to the file it was read from: a
            # request whose pointer still reads `held` in memory is a request
            # this pass would judge as held after it released it.
            pointer["phase"] = str(changed["released"][0]["phase"])
            pointer.pop("queue_hold", None)
            del pointers[run_id]
            released.append(run_id)
    return released
