"""The choices one presenting session puts to the lead.

The read in :mod:`reckon.crew.lead_decisions` sorts every open decision into
its own list. This module turns those lists into the choices a session shows
the lead, in the order the lead answers them, and holds each one for the
session presenting it so two presenting sessions cannot offer the same
decision twice.

A choice is the record plus the two facts a reader needs and the record does
not carry: whether it is being offered at all (a decision pending on a run or
taken under a standing rule is not), and what the answer releases. A decision
written only in a plan's decisions block has no owning session, no recorded
time and no held work; each is reported absent by name and the value is null,
never an empty string or a decision of a count that reads as recorded-and-zero.

The plan-block decision is offered after the lead's ready work, because it
holds nothing back and answering it locks a plan section rather than releasing
a request. Presenting one is still a choice: the lead may answer it here and
the answer lands in the plan.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon.crew.run_paths import crew_home

#: The read's lists a presenting session offers, in the order it offers them,
#: as ``(read key, state)``. A pending decision waits on a run or gate and a
#: taken-and-reversible one has already been chosen, so neither is a choice the
#: lead is asked to make now.
PRESENTED_LISTS = (("decisions", "ready"), ("plan_block", "plan-block"))

#: The fields a plan-block choice reports as absent, in the read's own names.
ABSENT_FIELDS = ("session", "recorded_at", "held_work")


def _releases(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """What an answer releases, or ``None`` for a decision that holds nothing.

    The plan-block decision holds no work at all — the read reports that as
    absent, not as zero — and its release payload follows the same rule, so a
    reader cannot mistake "nothing recorded" for "nothing to release".
    """
    if row.get("held_work_count") is None:
        return None
    return {
        "requests": list(row.get("held_requests") or ()),
        "runs": list(row.get("held_runs") or ()),
        "sections": list(row.get("held_sections") or ()),
        "count": row["held_work_count"],
    }


def _option(item: Mapping[str, Any], recommended: str) -> dict[str, Any]:
    value = str(item.get("value") or "")
    return {
        "value": value,
        "label": str(item.get("label") or value),
        "recommended": bool(recommended) and value == recommended,
    }


def choice(row: Mapping[str, Any]) -> dict[str, Any]:
    """One decision as the lead is shown it.

    The options arrive recommendation-first from the read and keep that order
    here. The recommendation is marked rather than reordered, so the lead sees
    which one the owner or reviewer proposed without the option list losing the
    order the owner recorded.
    """
    recommended = str(row.get("recommended") or "")
    absent = [name for name in ABSENT_FIELDS if name in (row.get("absent") or ())]
    return {
        "id": row["id"],
        "state": row["state"],
        "repository": row["project"],
        "project": row["project"],
        "plan": row.get("plan"),
        "section": row.get("section"),
        "question": row.get("question") or "",
        "context": row.get("context") or "",
        "options": [
            _option(option, recommended) for option in row.get("options") or ()
        ],
        "recommended": recommended,
        "recommended_by": row.get("recommended_by") or "",
        "recommendation_source": row.get("recommendation_source") or "",
        "session": None if "session" in absent else row.get("session"),
        "sessions": [] if "session" in absent else list(row.get("sessions") or ()),
        "recorded_at": None if "recorded_at" in absent else row.get("recorded_at"),
        "held_work_count": (
            None if "held_work" in absent else row.get("held_work_count")
        ),
        "releases": None if "held_work" in absent else _releases(row),
        "plan_has_live_session": bool(row.get("plan_has_live_session")),
        "lead_action": row.get("lead_action") or "",
        "risk": row.get("risk") or "",
        "absent": absent,
    }


def _read(project: str | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The read, or a per-project read when one document refuses the whole fleet.

    A fleet read is one call and is used whenever it succeeds. A project whose
    own documents cannot be read refuses the whole call — correctly, since an
    unread authoritative record is not an absent one — but that refusal must not
    blind the lead to the other projects' decisions. So the fallback reads each
    mounted project on its own, keeps every project that answered, and names the
    projects it could not read with the refusal's own words, which the
    presentation reports rather than swallows.
    """
    from reckon.crew.lead_decisions import read_decisions

    try:
        return read_decisions(project), []
    except ValueError as exc:
        if project and project != "*":
            raise
        first_refusal = str(exc)
    from reckon.crew.lead_decisions import held_work_order
    from reckon.flight import mounted_project_docs

    merged: dict[str, Any] = {
        "ok": True,
        "view": "lead-choices",
        "projects": [],
        "count": 0,
        "total_count": 0,
        "decisions": [],
        "pending": [],
        "taken_and_reversible": [],
        "plan_block": [],
    }
    unread: list[dict[str, str]] = []
    owners = sorted(mounted_project_docs())
    for owner in owners:
        try:
            one = read_decisions(owner)
        except ValueError as exc:
            unread.append({"project": owner, "detail": str(exc)})
            continue
        merged["projects"].append(owner)
        for key in ("decisions", "pending", "taken_and_reversible", "plan_block"):
            merged[key].extend(one.get(key) or ())
    # A merged list is one list: the order the read put each project's rows in
    # is re-applied across the whole fleet, so the lead still answers largest
    # held work first rather than project by project.
    for key in ("decisions", "pending", "taken_and_reversible", "plan_block"):
        merged[key].sort(key=held_work_order)
    if unread and len(unread) == len(owners):
        raise ValueError(first_refusal)
    return merged, unread


def choices(project: str | None = None) -> dict[str, Any]:
    """The choices one presenting session shows the lead, held work first.

    Ready decisions come in the read's own order, which is largest held work
    first, and the plan-block decisions follow them. Pending and
    taken-and-reversible decisions are read but not offered: neither is a
    question the lead is asked to answer now.
    """
    view, unread = _read(project)
    offered = [row for key, _state in PRESENTED_LISTS for row in view.get(key) or ()]
    return {
        "ok": True,
        "view": "lead-choices",
        "project": project,
        "projects": view.get("projects") if project in (None, "*") else [project],
        "count": len(offered),
        "choices": [choice(row) for row in offered],
        "not_offered": {
            "pending": len(view.get("pending") or ()),
            "taken_and_reversible": len(view.get("taken_and_reversible") or ()),
        },
        "unread_projects": unread,
    }


def _holder(decision_id: str) -> str:
    """The session presenting one decision, or "" when nobody presents it."""
    from reckon.crew.decision_answers import _read_presentation

    record = _read_presentation(decision_id)
    return str((record or {}).get("session") or "")


def present(*, session: str, project: str | None = None) -> dict[str, Any]:
    """Hold every offerable decision for one session and return its choices.

    The claim is taken per decision before the choice is returned, so a second
    session running this at the same time is handed only what the first did not
    take. A decision already presented by another session is reported with the
    session holding it rather than silently dropped: a choice that vanished
    would read as a decision that no longer waits, which is the opposite of
    what a competing presenter is showing the lead.
    """
    from reckon.crew.decision_answers import claim_presentation

    session = str(session or "").strip()
    if not session:
        return {
            "ok": False,
            "view": "lead-choices",
            "error": "presentation_error",
            "detail": "presenting the lead's decisions needs the presenting session",
        }
    survey = _survey_before_presenting(session)
    view = choices(project)
    held: list[dict[str, Any]] = []
    elsewhere: list[dict[str, Any]] = []
    for item in view["choices"]:
        try:
            claim_presentation(item["id"], session=session)
        except ValueError as exc:
            elsewhere.append(
                {
                    "id": item["id"],
                    "presented_by": _holder(item["id"]),
                    "detail": str(exc),
                }
            )
            continue
        held.append(item)
    return {
        **view,
        "session": session,
        "count": len(held),
        "choices": held,
        "claimed_elsewhere": elsewhere,
        **survey,
    }


def _survey_before_presenting(session: str) -> dict[str, Any]:
    """The survey's list, or the reason it could not be taken.

    A presentation must not be lost because the harness keeps no session
    records on this host: the choices are what the lead came for, and an ask
    that could not be written is reported beside them rather than raised over
    them.
    """
    try:
        step = survey(session=session)
    except (OSError, ValueError, TypeError) as exc:
        return {"survey_error": str(exc)}
    return {
        key: step[key]
        for key in ("asked", "already_asked", "unreached", "replied")
        if key in step
    }


def choose(
    *, session: str, decision_id: str, option: str, note: str = ""
) -> dict[str, Any]:
    """Record the lead's answer to one choice this session presents."""
    from reckon.crew.decision_answers import answer_decision

    return answer_decision(decision_id, session=session, choice=option, note=note)


def release(*, session: str, decision_id: str | None = None) -> dict[str, Any]:
    """Give up the claims one presenting session holds.

    One decision when ``decision_id`` names it, otherwise every decision the
    session is presenting. A claim outliving the presentation would leave the
    decision unpresentable by any other session, so the skill releases what it
    did not answer.
    """
    from reckon.crew.decision_answers import (
        presenting_claims,
        release_presentation,
    )

    session = str(session or "").strip()
    if not session:
        return {
            "ok": False,
            "error": "presentation_error",
            "detail": "releasing a claim needs the presenting session",
        }
    if decision_id:
        targets = [decision_id]
    else:
        targets = [
            str(claim["decision_id"])
            for claim in presenting_claims()
            if str(claim.get("session") or "") == session and claim.get("decision_id")
        ]
    released: list[str] = []
    for target in targets:
        try:
            outcome = release_presentation(target, session=session)
        except ValueError as exc:
            return {
                "ok": False,
                "error": "presentation_error",
                "detail": str(exc),
                "released": released,
            }
        if outcome.get("released"):
            released.append(target)
    return {"ok": True, "session": session, "released": released}


# ── The survey that precedes the presentation ──────────────────────────────


def ask_path(session: str) -> Path:
    """The file holding the one ask sent to one live session.

    A session's own id is not a filename — it may carry slashes and capitals —
    so the readable part is stripped to safe characters and a digest keeps two
    ids that strip alike apart.
    """
    readable = re.sub(r"[^a-z0-9.-]+", "-", str(session).lower()).strip("-")
    digest = hashlib.sha256(str(session).encode("utf-8")).hexdigest()[:8]
    return (
        crew_home()
        / "decisions"
        / "asks"
        / f"{(readable or 'session')[-60:]}-{digest}.json"
    )


def survey(*, session: str) -> dict[str, Any]:
    """Ask each live session once, in writing, for the decision it has not recorded.

    A session that holds work for the lead without a record cannot appear in the
    read, so the presentation opens with one ask per live session. The ask is a
    record, not a message a session may miss while busy: it is written once per
    session, and a second presentation in the same session reports the first ask
    rather than sending a second.

    A reply is a decision recorded by that session after the ask, read back from
    the store rather than assumed. A session whose harness record names no live
    process, or names no peer to reach it by, is reported unreached with the
    reason — the skill names those rather than passing over them.
    """
    from reckon._store import write_json_atomically
    from reckon.crew import harness_sessions

    session = str(session or "").strip()
    if not session:
        return {
            "ok": False,
            "view": "lead-survey",
            "error": "presentation_error",
            "detail": "a survey needs the session asking",
        }
    addresses = harness_sessions.addresses()
    asked: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []
    unreached: list[dict[str, Any]] = []
    recorded_by = _recorded_by_session()
    for target, address in sorted(addresses.items()):
        if target == session:
            continue
        if address.get("reachable") is not True or not address.get("name"):
            unreached.append(
                {
                    "session": target,
                    "name": address.get("name"),
                    "detail": (
                        "no harness record names a live process for this session"
                        if address.get("reachable") is not True
                        else "the harness record names no peer to reach it by"
                    ),
                    "recorded_decision": recorded_by.get(target, []),
                }
            )
            continue
        path = ask_path(target)
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            already.append(
                {
                    "session": target,
                    "asked_at": existing.get("asked_at"),
                    "asked_by": existing.get("asked_by"),
                    "recorded_decision": recorded_by.get(target, []),
                }
            )
            continue
        ask = {
            "session": target,
            "name": address.get("name"),
            "asked_by": session,
            "asked_at": datetime.now(UTC).isoformat(),
            "message": (
                "If any decision waits on the lead and is not yet recorded, "
                "record it with `reckon crew decision record --record -` so the "
                "presenting session can put it to the lead. No reply is needed "
                "when nothing waits."
            ),
        }
        write_json_atomically(path, ask, fsync_directory=True)
        asked.append(
            {
                "session": target,
                "name": address.get("name"),
                "asked_at": ask["asked_at"],
                "recorded_decision": recorded_by.get(target, []),
            }
        )
    return {
        "ok": True,
        "view": "lead-survey",
        "session": session,
        "asked": asked,
        "already_asked": already,
        "unreached": unreached,
        "replied": sorted(
            target for target, ids in recorded_by.items() if ids and target != session
        ),
    }


def _recorded_by_session() -> dict[str, list[str]]:
    """Each session's recorded decisions, keyed by the session that recorded them."""
    recorded: dict[str, list[str]] = {}
    for path in sorted((crew_home() / "decisions").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, Mapping) or not record.get("session"):
            continue
        if record.get("answered_at") or record.get("withdrawn_by"):
            continue
        recorded.setdefault(str(record["session"]), []).append(
            str(record.get("id") or path.stem)
        )
    return recorded
