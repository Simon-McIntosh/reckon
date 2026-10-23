"""Compact read models joining live pointers with committed run records."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC
from pathlib import Path
from typing import Any

from reckon import ledger
from reckon._timestamps import parse_iso
from reckon.crew.node import CrewError, normalize_section
from reckon.crew.recovery import classify_pointer, group_terminal_lane_events
from reckon.crew.resumption import resolve_session
from reckon.crew.routing import mounted_repository_projects
from reckon.crew.runs import crew_home, list_live, read_pointer

DEFAULT_RUN_FIELDS = (
    "run_id",
    "node",
    "plan",
    "section",
    # A brief run names no plan section, so its authority is the stored brief it
    # read; its digest rides the row beside the null plan. A plan run has no
    # brief, so the key is absent there rather than empty — absence is what says
    # the row's authority is a plan section, not a digest.
    "brief_sha256",
    "source",
    "classification",
    "process_alive",
    "session_id",
    "session_id_source",
    "worktree",
    "worktree_exists",
    "transcript_path",
    "transcript_exists",
    "resumable",
    "resumable_reason",
)

OPTIONAL_RUN_FIELDS = (
    "lane",
    "model_key",
    "member",
    "agent",
    "base_sha",
    "manifest_present",
    "commits",
    "commits_beyond_base",
    "log_age_seconds",
    "manifest_reported_status",
)

#: Every field a runs row declares: the ones the view serves by default and the
#: optional ones a caller may ask for. The runs row is served whole, so a field
#: the view declares is on the row whether or not it holds a value. A key that
#: vanished when it was empty would make the row's shape depend on the run it
#: describes, and a caller comparing two rows — or reading one against the field
#: list this view documents — could not tell an unanswerable field from an
#: unanswered one.
RUN_ROW_FIELDS: tuple[str, ...] = (*DEFAULT_RUN_FIELDS, *OPTIONAL_RUN_FIELDS)

RUN_SOURCES = frozenset({"all", "ledger", "live"})
RUN_SCOPES = frozenset({"project", "workstation"})
WORKSTATION_RUN_FIELDS = ("project", "repo", "repo_exists")

#: A projected live row always carries the identity of the run it describes, so
#: a caller that asked for one field can still tell which run answered.
LIVE_ROW_ANCHOR = "run_id"
LIVE_OVERRUN_FIELDS = frozenset(
    {
        "budget_overrun_cause",
        "budget_overrun_rate",
        "budget_overrun_reference_rate",
        "budget_overrun_reference_runs",
        "budget_overrun_reference_source",
    }
)


#: The fields a queued row carries for where it stands in the lane's queue,
#: read from the admission pass's own order rather than recomputed here. A row
#: that is not queued carries no place, so the names join the live vocabulary
#: unconditionally: a caller asking where a queued run stands must not have its
#: request refused on a lane that happens to have no queued request in flight.
LIVE_PLACEMENT_FIELDS = ("position", "rank")


#: The keys :func:`reckon.crew.recovery_classification.classify_pointer`
#: answers for any record, so a live row carries them whatever pointer produced
#: it. Declared here rather than read from a classification because a
#: classification is not free: even an empty pointer sends it to the run's
#: ``worker.json``, ``exit.json`` and ``attempt.json``, so a caller that only
#: wants to judge an argument would have to wait on those reads to learn a set
#: that does not depend on them.
LIVE_CLASSIFICATION_FIELDS = (
    "backend",
    "budget_overrun",
    "budget_overrun_seconds",
    "budget_seconds",
    "classification",
    "commits_beyond_base",
    "detail",
    "effective_phase",
    "elapsed_seconds",
    "exit_record",
    "external_wait",
    "fleet_verdict",
    "interruption",
    "lane",
    "lane_cause",
    "lane_ended_at",
    "lane_event",
    "lifting_condition",
    "liveness_proven",
    "log_age_seconds",
    "log_fresh",
    "manifest_commits",
    "manifest_derived",
    "manifest_digest",
    "manifest_error",
    "manifest_file_present",
    "manifest_fresh",
    "manifest_path",
    "manifest_present",
    "manifest_reported_status",
    "manifest_status",
    "marker",
    "model_key",
    "needs_help_complete",
    "next_action",
    "node",
    "phase",
    "plan",
    "process_alive",
    "process_descendant_alive",
    "project",
    "recovery",
    "recovery_classification",
    "resets_at",
    "review_error",
    "review_present",
    "review_status",
    "run_id",
    "session",
    "stored_phase",
    "terminal_age_seconds",
    "terminal_at",
    "wait_age_seconds",
    "wait_condition_state",
    "wait_key_defect",
    "wait_observed",
    "wait_overdue",
    "worktree",
)


class RunQueryError(ValueError):
    """A runs-view request cannot be represented by the compact read model."""


def _path_exists(value: Any, *, directory: bool = False) -> bool:
    """Report whether a non-empty path exists with the requested kind."""
    text = str(value or "").strip()
    if not text:
        return False
    path = Path(text)
    return path.is_dir() if directory else path.is_file()


def _requested_field_names(fields: Iterable[str] | None) -> tuple[str, ...]:
    """Return the requested field names, normalized and de-duplicated in order."""
    if fields is None:
        return ()
    if isinstance(fields, str):
        parts = fields.split(",")
    else:
        parts = [str(field) for field in fields]
    return tuple(dict.fromkeys(part.strip() for part in parts if part.strip()))


def _refuse_unknown_fields(requested: Iterable[str]) -> None:
    """Reject field names outside the accepted set, naming what is accepted."""
    unknown = sorted(
        set(requested) - (set(DEFAULT_RUN_FIELDS) | set(OPTIONAL_RUN_FIELDS))
    )
    if not unknown:
        return
    raise RunQueryError(
        "unknown runs fields "
        + ", ".join(repr(field) for field in unknown)
        + "; optional fields are "
        + ", ".join(OPTIONAL_RUN_FIELDS)
    )


def _selected_fields(fields: Iterable[str] | None) -> tuple[str, ...]:
    """Return the fields a runs row is built with for one request.

    A request does not narrow a runs row. It adds the optional fields asked for
    to the row the view declares, so a caller receives the view's own row —
    every field this view is able to answer for, one of which it asked to be
    sure of — rather than a different, smaller row. The whole default row is
    built whatever was requested for a second reason: the view's own filters
    read ``classification``, ``session_id`` and ``resumable`` off the row before
    and after the projection, so a build narrowed to the request would answer
    those filters with an absent key and list nothing.

    What is refused here is a name no run row could carry, so the refusal is
    raised before the fleet is walked rather than after it.
    """
    if fields is None:
        return DEFAULT_RUN_FIELDS
    requested = _requested_field_names(fields)
    _refuse_unknown_fields(requested)
    return tuple(dict.fromkeys((*DEFAULT_RUN_FIELDS, *requested)))


def _live_field_vocabulary(
    classified: Sequence[Mapping[str, Any]], *, with_mine: bool
) -> set[str]:
    """Return the field names a live row carries.

    The accepted set is :data:`LIVE_FIELD_VOCABULARY`, the one declaration the
    live view and the MCP pre-flight share, so neither can answer for a name the
    other refuses. The rows in hand are unioned in as well, because a caller
    that reads the vocabulary from a served row must never be told a field the
    row carries is unknown. ``mine`` is a field of the request, not of the
    classification, so it joins only when a session was supplied.
    """
    vocabulary: set[str] = set(LIVE_FIELD_VOCABULARY)
    for row in classified:
        vocabulary.update(row)
    if with_mine:
        vocabulary.add("mine")
    return vocabulary


def _refuse_unknown_live_fields(
    requested: Iterable[str],
    classified: Sequence[Mapping[str, Any]],
    *,
    with_mine: bool,
) -> None:
    """Reject field names a live row does not carry, naming the live set."""
    vocabulary = _live_field_vocabulary(classified, with_mine=with_mine)
    unknown = sorted(set(requested) - vocabulary)
    if not unknown:
        return
    raise RunQueryError(
        "unknown live fields "
        + ", ".join(repr(field) for field in unknown)
        + "; live fields are "
        + ", ".join(sorted(vocabulary))
    )


#: The fields every list view's run rows carry, so a caller reads one
#: vocabulary wherever a run is listed however it is listed. ``section`` is the
#: name this set exists to guarantee: a run is dispatched for one section of one
#: plan, and every view that lists runs names it. A view routes its rows through
#: :func:`project_run_rows`, which fills ``section`` from the run's pointer and
#: leaves a field whose value is empty off the row.
COMMON_RUN_FIELDS: tuple[str, ...] = ("run_id", "node", "plan", "section")


#: Every field name a ``live`` row answers, as one declaration the live view,
#: the runs view and the MCP pre-flight all read. It is the union of the parts
#: above and not a set of its own, so a name added to one of them reaches every
#: caller at once: a pre-flight that restated the union could refuse a request
#: the view answers, which is a refusal the caller cannot act on.
LIVE_FIELD_VOCABULARY: frozenset[str] = (
    frozenset(LIVE_CLASSIFICATION_FIELDS)
    | LIVE_OVERRUN_FIELDS
    | frozenset(LIVE_PLACEMENT_FIELDS)
    | frozenset(COMMON_RUN_FIELDS)
)


def _canonical_section(value: Any) -> str:
    """Normalize a section identity, treating absence as the empty string."""

    return normalize_section(str(value)) if value else ""


def _run_section(record: Mapping[str, Any]) -> str:
    """Return the section a live pointer's run was dispatched for."""

    node = record.get("node")
    node_mapping = node if isinstance(node, Mapping) else {}
    return _canonical_section(node_mapping.get("section"))


def run_section_index(
    record_sets: Iterable[Iterable[Mapping[str, Any]]],
) -> dict[str, str]:
    """Map every run id in the given pointer sets to the section it names."""

    index: dict[str, str] = {}
    for records in record_sets:
        for record in records:
            run_id = str(record.get("run_id") or "")
            if not run_id:
                continue
            index.setdefault(run_id, _run_section(record))
    return index


def project_run_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    fields: Iterable[str] | None = None,
    view: str,
    vocabulary: Iterable[str] | None = None,
    sections: Mapping[str, str] | None = None,
    keep_empty: Iterable[str] = (),
    identity: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Route one list view's run rows through the single projection.

    Every list view that lists runs calls this, so one vocabulary, one refusal
    and one omission rule serve all of them. ``sections`` supplies the
    ``section`` for a row that does not carry it; a field a view does not carry
    is refused, naming the fields it has; and a field whose value is empty is
    left off the row rather than serialised as an empty string or a null. The
    run's identity is always kept, so a caller that asked for one field can
    still tell which run answered.

    ``identity`` is what a narrowed row carries whatever was asked for. A view
    of compact run rows names the shared run fields, so a caller reading one
    fact still sees which run, plan and section answered; a view that serves its
    rows whole, as the live view serves whole classifications, names none of
    them, because three fields riding along with a request for one is the shape
    that view's own contract refuses. Every narrowed row carries
    :data:`LIVE_ROW_ANCHOR` either way; omitting ``identity`` names
    :data:`COMMON_RUN_FIELDS`.

    ``keep_empty`` names the fields a view declares as part of its row, whose
    presence is itself the statement a reader makes: a brief run's row carries
    ``plan`` null, and the key's presence beside an absent digest is what
    distinguishes it. Such a field keeps its empty value.
    """

    shared = tuple(COMMON_RUN_FIELDS if identity is None else identity)
    rows = [dict(row) for row in rows]
    for row in rows:
        if not row.get("section") and sections is not None:
            section = sections.get(str(row.get("run_id") or ""))
            if section:
                row["section"] = section
        if row.get("section"):
            row["section"] = _canonical_section(row["section"])

    carried: set[str] = set(shared)
    for row in rows:
        carried.update(row)
    if vocabulary is not None:
        carried.update(vocabulary)

    declared = set(keep_empty)
    requested = _requested_field_names(fields)
    if requested:
        unknown = sorted(set(requested) - carried)
        if unknown:
            raise RunQueryError(
                f"unknown {view} fields "
                + ", ".join(repr(field) for field in unknown)
                + f"; {view} fields are "
                + ", ".join(sorted(carried))
            )
        selected = tuple(dict.fromkeys((*shared, *requested)))
    else:
        selected = ()

    projected: list[dict[str, Any]] = []
    for row in rows:
        names = selected or tuple(dict.fromkeys((*shared, *row)))
        narrowed = {name: row.get(name) for name in names if name in row}
        narrowed = {
            name: value
            for name, value in narrowed.items()
            if name == LIVE_ROW_ANCHOR
            or name in declared
            or value not in (None, "", [], {})
        }
        projected.append(narrowed)
    return projected


def project_live_rows(
    records: Iterable[Mapping[str, Any]],
    *,
    fields: Iterable[str] | None = None,
    session: str | None = None,
) -> list[dict[str, Any]]:
    """Classify each live pointer and project it to the requested fields.

    The live view reads whole classifications, so ``fields`` was accepted and
    ignored. A request now narrows each row to the fields asked for, always
    carrying :data:`LIVE_ROW_ANCHOR` so a projected row still names its run.
    Omitting ``fields`` returns the whole classification, which is what every
    caller that does not narrow receives. A requested field is validated
    against the names a live row actually carries — the classification keys,
    plus ``mine`` when a session is supplied — so a field like ``next_action``
    can be asked for where the compact runs vocabulary has no such name.

    ``session`` marks which rows belong to the caller's own session. The marker
    is computed from the full classification and kept on every projected row,
    because a session-scoped read that dropped ``mine`` would answer nothing
    about which rows are the caller's.

    A queued row carries where it stands in the lane's queue, as
    ``position`` and ``rank``, read from the admission pass's own ordering. The
    place is derived once, in :func:`reckon.crew.queue_admission
    .queued_placements`, so this view and any other surface that reports it
    answer from one order rather than each ordering the queue itself.
    """
    records = list(records)
    classified = group_terminal_lane_events(
        [classify_pointer(record) for record in records]
    )
    from reckon.crew.queue_admission import queued_placements

    placements = queued_placements(records)
    for row in classified:
        placement = placements.get(str(row.get(LIVE_ROW_ANCHOR) or ""))
        if placement is not None:
            row.update(placement)
    if session is not None:
        for row in classified:
            row["mine"] = str(row.get("session") or "") == session
    sections = run_section_index([records])
    with_mine = session is not None
    if fields is None:
        return project_run_rows(
            classified,
            view="live",
            vocabulary=_live_field_vocabulary(classified, with_mine=with_mine),
            sections=sections,
        )
    requested = _requested_field_names(fields)
    _refuse_unknown_live_fields(requested, classified, with_mine=with_mine)
    # ``mine`` is a field of the request rather than of the classification: the
    # projection narrows each row to what was asked for, and a narrowing drops
    # the ``session`` the marker is read from. The verdict is taken here, where
    # every classification is still whole, and carried across the projection by
    # run id, so a narrowed row still says which rows are the caller's.
    mine_by_run = {
        str(row.get(LIVE_ROW_ANCHOR) or ""): bool(row.get("mine")) for row in classified
    }
    rows = project_run_rows(
        classified,
        fields=fields,
        view="live",
        sections=sections,
        identity=(LIVE_ROW_ANCHOR,),
        vocabulary=_live_field_vocabulary(classified, with_mine=with_mine),
    )
    if with_mine:
        for row in rows:
            row["mine"] = mine_by_run.get(str(row.get(LIVE_ROW_ANCHOR) or ""), False)
    return rows


def _resumability(
    session: Mapping[str, Any],
    *,
    worktree_exists: bool,
    process_alive: Any,
) -> tuple[bool, str]:
    """Join the three independent facts that make a run recoverable."""
    if not session.get("resolved"):
        return False, str(
            session.get("detail")
            or "no session id in the pointer, stream or ledger; all three were consulted"
        )
    if not worktree_exists:
        return False, "worktree released by promotion"
    if process_alive is True:
        return False, "the run's process is alive"
    if process_alive is not False:
        return False, "process liveness is unknown"
    return True, "session resolved, worktree exists and process is not alive"


#: Where a compact row carries the inputs its path-derived fields are computed
#: from, when those fields are deferred. The key is removed before a row is
#: returned, so a reader never sees it.
_PATH_INPUTS = "_path_inputs"


def _path_derived(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Compute the fields derived from where a run's artifacts are.

    These are the expensive fields of a compact row — one filesystem probe each
    for the worktree, the manifest and the transcript — so a caller projecting a
    whole ledger defers them and computes them only for the rows it returns.
    Every value here is the one an eager projection produced.
    """
    record = inputs["record"]
    classified = inputs["classified"]
    worktree_exists = _path_exists(inputs["worktree"], directory=True)
    resumable, resumable_reason = _resumability(
        inputs["session"],
        worktree_exists=worktree_exists,
        process_alive=inputs["process_alive"],
    )
    transcript = inputs["transcript"]
    return {
        "worktree_exists": worktree_exists,
        "manifest_present": bool(
            classified.get("manifest_present", False)
            if inputs["source"] == "live"
            else _path_exists(record.get("manifest_path"))
        ),
        "transcript_exists": (
            bool(record.get("transcript_exists"))
            if "transcript_exists" in record
            else _path_exists(transcript)
        ),
        "resumable": resumable,
        "resumable_reason": resumable_reason,
    }


def _compact_row(
    record: Mapping[str, Any],
    *,
    source: str,
    project: str,
    checkout_path: str | None,
    repository: Path | None,
    selected_fields: tuple[str, ...],
    classified_live: Mapping[str, Any] | None = None,
    ledger_row: Any = None,
    with_path_fields: bool = True,
) -> dict[str, Any]:
    """Project one source record into the stable compact row shape.

    A brief run's authority is the stored brief it read, not a plan section, so
    its row carries ``plan`` null and the digest of that brief. The live pointer
    holds the digest on its node block; a promoted ledger row holds it in the
    ``brief`` block promotion writes. A plan run has neither, so ``plan`` is the
    section's plan and ``brief_sha256`` is left off the row (see the projection
    below) rather than carried empty.

    ``ledger_row`` is the committed row the caller already holds for this run,
    or a nullary callable returning it. A callable is resolved only if the
    pointer and the stream leave the session unanswered, so a view that never
    reaches the ledger source never loads it.

    ``with_path_fields`` false leaves the path-derived fields off the row and
    parks their inputs under :data:`_PATH_INPUTS` for :func:`_finalize_paths`,
    so a caller that will discard most of its rows pays no filesystem probe for
    them.
    """
    classified = classified_live or classify_pointer(record) if source == "live" else {}
    node_data = record.get("node")
    node_mapping = node_data if isinstance(node_data, Mapping) else {}
    node = classified.get("node") if source == "live" else record.get("node")
    plan = classified.get("plan") if source == "live" else record.get("plan")
    section = node_mapping.get("section") if source == "live" else record.get("section")
    brief_record = record.get("brief")
    brief_sha256 = (
        str(brief_record.get("sha256") or "")
        if source == "ledger" and isinstance(brief_record, Mapping)
        else str(node_mapping.get("brief_sha256") or "")
        if source == "live"
        else ""
    )
    worktree = record.get("worktree") or None
    transcript = record.get("transcript_path") or None
    session = resolve_session(
        str(record.get("run_id") or ""),
        record=record if source == "live" else None,
        ledger_row=record if source == "ledger" else ledger_row,
        project=project,
        root=checkout_path,
    )
    if source == "live":
        commits = classified.get("manifest_commits") or record.get("commits") or []
    else:
        commits = record.get("commits") or []
    process_alive = (
        classified.get("process_alive")
        if source == "live"
        else record.get("process_alive")
    )
    identity = ledger.normalize_identity(record)
    path_inputs = {
        "source": source,
        "record": record,
        "classified": classified,
        "session": session,
        "worktree": worktree,
        "transcript": transcript,
        "process_alive": process_alive,
    }
    # A deferred row carries ``None`` in each path-derived field until
    # :func:`_finalize_paths` fills it, so the projection below keeps the key
    # order and shape an eager row has.
    paths = _path_derived(path_inputs) if with_path_fields else {}
    complete = {
        "lane": identity.get("lane"),
        "model_key": identity.get("model_key"),
        "run_id": str(record.get("run_id") or ""),
        "node": str(node or ""),
        "plan": str(plan) if plan else None,
        # The section is carried as the source states it; the single
        # projection (project_run_rows) canonicalises it, so this row builder
        # holds no copy of that rule.
        "section": str(section) if section else "",
        "brief_sha256": brief_sha256,
        "source": source,
        "classification": (
            classified.get("classification")
            if source == "live"
            else record.get("classification")
        ),
        "process_alive": process_alive,
        "session_id": session["session_id"],
        "session_id_source": session["source"],
        "worktree": worktree,
        "worktree_exists": paths.get("worktree_exists"),
        "transcript_path": transcript,
        "transcript_exists": paths.get("transcript_exists"),
        "resumable": paths.get("resumable"),
        "resumable_reason": paths.get("resumable_reason"),
        "project": project,
        "repo": str(repository) if repository is not None else "",
        "repo_exists": repository.is_dir() if repository is not None else False,
        "member": str(record.get("member") or ""),
        "agent": (
            dict(record["agent"]) if isinstance(record.get("agent"), Mapping) else {}
        ),
        "base_sha": str(record.get("base_sha") or ""),
        "manifest_present": paths.get("manifest_present"),
        "commits": [str(commit) for commit in commits],
        # The three fields a coordinator checks first, drawn from the same
        # classification the live view already computes. A ledger row carries
        # whatever its committed record recorded, so a source that has not
        # stored them reports absence rather than inventing a value.
        "commits_beyond_base": (
            classified.get("commits_beyond_base", 0)
            if source == "live"
            else record.get("commits_beyond_base")
        ),
        "log_age_seconds": (
            classified.get("log_age_seconds")
            if source == "live"
            else record.get("log_age_seconds")
        ),
        "manifest_reported_status": (
            classified.get("manifest_reported_status")
            if source == "live"
            else record.get("manifest_reported_status")
        ),
    }
    row = {field: complete[field] for field in selected_fields}
    if not with_path_fields:
        row[_PATH_INPUTS] = path_inputs
    if identity.get("lane"):
        row.update(lane=identity["lane"], model_key=identity.get("model_key"))
    if classified.get("lane_event"):
        row["lane_event"] = classified["lane_event"]
    # Only a brief run carries a brief digest. Leaving the key off a plan run's
    # row keeps that row's shape what it was before brief runs existed, so a
    # reader tells the two authorities apart by the key's presence.
    if not row.get("brief_sha256"):
        row.pop("brief_sha256", None)
    return row


def _finalize_paths(row: dict[str, Any]) -> dict[str, Any]:
    """Fill a deferred row's path-derived fields in place, and return it.

    A row that already carries its path fields has nothing parked under
    :data:`_PATH_INPUTS`, so finalizing it is a no-op rather than a second
    round of filesystem probes.
    """
    inputs = row.pop(_PATH_INPUTS, None)
    if inputs is not None:
        derived = _path_derived(inputs).items()
        row.update({field: value for field, value in derived if field in row})
    return row


def _canonical_root(root: Any) -> str | None:
    """One key for one ledger, whatever spelling of its root a caller holds.

    The two arms reach the same project's ledger from different values — one
    from the checkout path the request carried, the other from the root a live
    record recorded — so a cache keyed on the raw value reads the same ledger
    twice and the "one load per project" bound holds only by accident.
    """
    text = str(root or "").strip()
    if not text:
        return None
    return str(Path(text).expanduser().resolve())


def _mounted_projects() -> dict[str, Path]:
    """Return the repository owning every project registered on this workstation."""
    return {
        project: repository
        for repository, projects in mounted_repository_projects().items()
        for project in projects
    }


def _record_repository(
    record: Mapping[str, Any], mounted_projects: Mapping[str, Path]
) -> Path | None:
    """Return a live run's recorded repository, falling back to its mount."""
    value = str(record.get("repo") or "").strip()
    if value:
        return Path(value).expanduser().resolve()
    return mounted_projects.get(str(record.get("project") or ""))


def _matches(
    row: Mapping[str, Any],
    *,
    node: str | None,
    plan: str | None,
    section: str | None,
    session: str | None,
    member: str | None,
    classification: str | None,
    resumable: bool | None,
    run_id: str | None = None,
) -> bool:
    """Apply every runs-view filter to one normalized row."""
    string_filters = {
        "run_id": run_id,
        "node": node,
        "plan": plan,
        "session_id": session,
        "member": member,
        "classification": classification,
    }
    if any(
        expected is not None and str(row.get(field) or "") != str(expected)
        for field, expected in string_filters.items()
    ):
        return False
    if section is not None and str(row.get("section") or "") != normalize_section(
        section
    ):
        return False
    return resumable is None or row.get("resumable") is resumable


def _live_records_for_run(
    run_id: str, project: str, *, scope: str
) -> list[dict[str, Any]]:
    """Read one run's live pointer without walking every other pointer.

    A single-run read must not pay for the fleet, so this reads the one pointer
    file and nothing else. An absent, malformed or foreign-project pointer each
    read as no live record rather than raising: the ledger arm may still answer
    for a run that was already promoted and released its pointer.
    """
    try:
        record = read_pointer(run_id)
    except (CrewError, OSError, ValueError):
        return []
    if scope == "project" and str(record.get("project") or "") != project:
        return []
    return [record]


def normalize_runs_source(source: str | None) -> str:
    """Return the runs view's own source, or raise on one it does not serve.

    Does no I/O, so a caller judging a request's arguments can ask what
    :func:`runs_view` accepts before the fleet is walked: the view calls this
    function, so the accepted set and the normalisation that decides it are one
    fact. A caller comparing the raw argument against :data:`RUN_SOURCES`
    instead would refuse ``'ALL'``, which the view serves.
    """

    selected = str(source or "all").strip().lower()
    if selected not in RUN_SOURCES:
        raise RunQueryError(
            f"runs source must be one of {', '.join(sorted(RUN_SOURCES))}"
        )
    return selected


def normalize_runs_scope(scope: str | None) -> str:
    """Return the runs view's own scope, or raise on one it does not serve.

    The counterpart of :func:`normalize_runs_source` for the scope argument, and
    read by the view and its pre-flight for the same reason.
    """

    selected = str(scope or "project").strip().lower()
    if selected not in RUN_SCOPES:
        raise RunQueryError(
            f"runs scope must be one of {', '.join(sorted(RUN_SCOPES))}"
        )
    return selected


def runs_view(
    project: str,
    *,
    checkout_path: str | None = None,
    source: str = "all",
    scope: str = "project",
    node: str | None = None,
    plan: str | None = None,
    section: str | None = None,
    run_id: str | None = None,
    session: str | None = None,
    member: str | None = None,
    classification: str | None = None,
    resumable: bool | None = None,
    newest_per_node: bool = False,
    fields: Iterable[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return compact live and committed run rows, newest first.

    ``run_id`` selects one run by identity. Identity is known before the read,
    so it narrows the read rather than filtering its result: the live arm reads
    that run's single pointer through :func:`read_pointer` instead of walking
    every pointer in the fleet, and the ledger arm keeps only that run's row
    before any compact row is built. The answer is the same rows the identity
    filter would have kept, without paying for the fleet to read one run.
    """
    selected_source = normalize_runs_source(source)
    selected_scope = normalize_runs_scope(scope)
    selected_run_id = str(run_id or "").strip()
    if run_id is not None and not selected_run_id:
        raise RunQueryError("runs run_id must be a non-empty run identifier")
    if resumable is not None and not isinstance(resumable, bool):
        raise RunQueryError("resumable must be true, false or omitted")
    if not isinstance(newest_per_node, bool):
        raise RunQueryError("newest_per_node must be true or false")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
    ):
        raise RunQueryError("runs limit must be a positive integer")

    selected_fields = _selected_fields(fields)
    if selected_scope == "workstation":
        selected_fields = (*selected_fields, *WORKSTATION_RUN_FIELDS)
    row_fields = tuple(dict.fromkeys((*selected_fields, "member")))
    mounted_projects = _mounted_projects() if selected_scope == "workstation" else {}
    # One committed-row read per project, shared by both arms. The live arm
    # reaches it only when a live row's pointer and stream leave the session
    # unanswered, so a view whose live rows all carry a session reads no ledger
    # at all, and one whose rows do not reads it once rather than once per row.
    committed: dict[tuple[str, str | None], dict[str, Mapping[str, Any]]] = {}

    def committed_rows(row_project: str, root: Any) -> dict[str, Mapping[str, Any]]:
        key = (row_project, _canonical_root(root))
        if key not in committed:
            committed[key] = {
                str(record.get("run_id") or ""): record
                for record in ledger.runs(row_project, key[1])
            }
        return committed[key]

    rows: list[dict[str, Any]] = []
    live_records: list[Mapping[str, Any]] = []
    if selected_source in {"all", "live"}:
        if selected_run_id:
            live_records = _live_records_for_run(
                selected_run_id, project, scope=selected_scope
            )
        else:
            live_records = list_live()
            if selected_scope == "project":
                live_records = [
                    record
                    for record in live_records
                    if str(record.get("project") or "") == project
                ]
        classified_live = group_terminal_lane_events(
            [classify_pointer(record) for record in live_records]
        )
        records_by_id = {
            str(record.get("run_id") or ""): record for record in live_records
        }
        for classified in classified_live:
            record = records_by_id[str(classified.get("run_id") or "")]
            row_project = str(record.get("project") or "")
            repository = _record_repository(record, mounted_projects)
            row_checkout = (
                str(repository)
                if selected_scope == "workstation" and repository is not None
                else checkout_path
            )
            live_run_id = str(record.get("run_id") or "")

            def live_ledger_row(
                _project: str = row_project,
                _record: Mapping[str, Any] = record,
                _checkout: str | None = row_checkout,
                _run_id: str = live_run_id,
            ) -> Mapping[str, Any]:
                """The live row's committed row, read only when the ledger is asked.

                The absence of a row answers with an empty mapping rather than
                ``None``, because ``None`` sends the ledger source to load the
                ledger itself and pay for a search this lookup already made.
                """
                root = _checkout if _checkout is not None else _record.get("repo")
                found = committed_rows(_project, root).get(_run_id)
                return found if found is not None else {}

            rows.append(
                _compact_row(
                    record,
                    source="live",
                    project=row_project,
                    checkout_path=row_checkout,
                    repository=repository,
                    selected_fields=row_fields,
                    classified_live=classified,
                    ledger_row=live_ledger_row,
                    with_path_fields=False,
                )
            )
    if selected_source in {"all", "ledger"}:
        ledger_projects = (
            mounted_projects.items()
            if selected_scope == "workstation"
            else ((project, Path(checkout_path).expanduser().resolve()),)
            if checkout_path is not None
            else ((project, None),)
        )
        for row_project, repository in ledger_projects:
            ledger_root = str(repository) if repository is not None else None
            ledger_rows = list(committed_rows(row_project, ledger_root).values())
            if selected_run_id:
                ledger_rows = [
                    record
                    for record in ledger_rows
                    if str(record.get("run_id") or "") == selected_run_id
                ]
            rows.extend(
                _compact_row(
                    record,
                    source="ledger",
                    project=row_project,
                    checkout_path=ledger_root,
                    repository=repository,
                    selected_fields=row_fields,
                    with_path_fields=False,
                )
                for record in ledger_rows
            )

    def keep(row: Mapping[str, Any], *, judged_resumable: bool) -> bool:
        return _matches(
            row,
            node=node,
            plan=plan,
            section=section,
            session=session,
            member=member,
            classification=classification,
            resumable=resumable if judged_resumable else None,
            run_id=selected_run_id or None,
        )

    rows = [row for row in rows if keep(row, judged_resumable=False)]
    # A caller judging on ``resumable`` pays the path probes for every row the
    # other filters left, because that is the fact it asked to be judged on;
    # every other caller pays them only for the rows it returns.
    if resumable is not None:
        rows = [
            row
            for row in (_finalize_paths(candidate) for candidate in rows)
            if keep(row, judged_resumable=True)
        ]
    rows.sort(key=lambda row: str(row.get("run_id") or ""), reverse=True)
    if newest_per_node:
        newest: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            node_id = str(row.get("node") or "")
            if node_id in seen:
                continue
            seen.add(node_id)
            newest.append(row)
        rows = newest
    if limit is not None:
        rows = rows[:limit]
    rows = [_finalize_paths(row) for row in rows]
    if "member" not in selected_fields:
        for row in rows:
            row.pop("member", None)
    # The rows are built, so they are routed through the one projection last,
    # exactly as the live, obligations and directory views route theirs. It
    # fills a section the row does not carry from the run's own pointer,
    # canonicalises every section to the one spelling, and refuses a field this
    # view cannot answer before the fleet is walked. This view holds no copy of
    # any of those rules.
    #
    # The row itself is served whole: every field this view declares is kept,
    # empty or not, so a runs row is the read model the crew tool documents
    # rather than whichever keys this run happened to fill. A brief run's null
    # plan is one of them, and the key's presence beside an absent digest is
    # what says that row's authority is a digest.
    rows = project_run_rows(
        rows,
        view="runs",
        vocabulary=row_fields,
        sections=run_section_index([live_records]),
        keep_empty=RUN_ROW_FIELDS,
    )
    return {
        "ok": True,
        "project": project,
        "view": "runs",
        "source": selected_source,
        "scope": selected_scope,
        "count": len(rows),
        "rows": rows,
    }


# --- Watch-event extraction -------------------------------------------------
#
# A watch stream records one row per observed fleet state, in two shapes: a
# transition (a node moved from one state to another) and a baseline (a node's
# state as first seen, carrying no source state because nothing moved). Both a
# dispatch that a follower first observes and a whole-fleet re-inventory after a
# follower restart are written as baseline rows, so neither event class answers
# the question a reader has — *which rows are arrivals*. Two filters that look
# natural are both wrong, measured against the live streams on this workstation:
#
#   * keeping only ``event == "transition"`` drops genuine arrivals. Across the
#     6 live streams at 2026-09-18T09:26Z (12446 rows, 3031 nodes) 2417 nodes
#     first appear as a baseline row and 23 never had a transition row at all,
#     so the filter re-dates most of the fleet and loses the rest.
#   * keeping every baseline row invents arrivals. A follower that re-attaches
#     re-inventories every live run in one instant, emitting one baseline row
#     per run: the largest such burst on disk covers 10 nodes in the same second,
#     every one of them already carrying earlier rows, and 204 of the 2621
#     baseline rows on disk belong to a burst.
#
# The discriminator is first appearance per node, not event class: the earliest
# row for a node is its arrival whatever its class, later transitions are real
# state changes, and later baselines are re-inventory. One refinement is needed
# for the case a follower restart produces and that rule alone gets wrong — a
# node that was dispatched before the recording began has no earlier row, so its
# re-inventory baseline reads as its arrival. A re-inventory burst is therefore
# recognised directly: a same-instant group of two or more baseline rows in one
# stream, at least one of whose members already has an earlier row, is one
# inventory snapshot of a fleet that was already running. Every member of such a
# group is re-inventory even when it is that node's only row. Genuine arrivals
# that share a second are left alone by that test, which matters because they
# exist: a measured 8-node wave and a 3-node wave were both first observed in a
# single poll, sharing a stamp but carrying no already-known member.
#
# Stamps are the other half. A stream is mixed-format: JSON rows carry
# ``observed_at`` in UTC, and rendered ticker rows carry a wall clock in the
# reader's LOCAL zone with no offset and no date. A reader that merges the two
# as if both were UTC manufactures an ordering that is not in the data — a
# coordinator reading the live streams did exactly that and turned one
# concurrent ascent into a descending limb. So every stamp this module returns
# is either explicit UTC or marked unknown; a rendered row's zone is never
# assumed. Ordering never depends on a stamp at all: rows are positioned by
# their position in the append-only stream, which is chronological by
# construction, so an unknown-zone row is ordered correctly and only its stamp
# is withheld.

WATCH_EVENT_SUFFIX = ".events"
_RENDERED_ROW = re.compile(r"^(?P<clock>\d{2}:\d{2}:\d{2})\s+(?P<body>.*)$")
_TRANSITION_ARROW = "\N{RIGHTWARDS ARROW}"
_BASELINE_ARROW = "\N{BULLET}"


def watch_event_paths(*, home: Path | None = None) -> list[Path]:
    """Return every project's watch stream, newest file written last."""
    root = (home or crew_home()) / "watch"
    if not root.is_dir():
        return []
    return sorted(root.glob(f"*{WATCH_EVENT_SUFFIX}"))


def _normalize_stamp(value: Any) -> tuple[str | None, str]:
    """Return ``(stamp_utc, zone)`` for a stored stamp.

    A row that carries an explicit offset is converted to UTC and reported as
    such. Anything else — a value with no zone, a wall clock, an unparseable
    string — is reported as unknown rather than assumed to be UTC, because a
    stamp invented at the row that does not carry one is indistinguishable
    downstream from one the producer recorded. Whether the stamp names a zone
    is read from the parsed result's ``tzinfo`` rather than from the text's
    shape, so an offset that carries seconds is told from a zoneless wall clock.
    """
    text = str(value or "").strip()
    if not text:
        return None, "unknown"
    parsed = parse_iso(text)
    if parsed is None or parsed.tzinfo is None:
        return None, "unknown"
    return parsed.astimezone(UTC).isoformat(), "utc"


def _project_of(path: Path) -> str:
    """Return the project a watch stream belongs to, from its file name."""
    stem = path.name[: -len(WATCH_EVENT_SUFFIX)]
    return stem.rsplit("-", 1)[0] if "-" in stem else stem


def _rendered_row(text: str) -> dict[str, Any] | None:
    """Parse one rendered ticker line into the fields it actually carries.

    The pane's columns are fixed-width, but this reads tokens rather than
    offsets: the role and session columns are conditionally populated, and a
    parser keyed to a column position would report the neighbouring field the
    first time one of them is empty. The node is therefore the last token
    before the arrow (the source state, if any, having been peeled off first).
    """
    match = _RENDERED_ROW.match(text)
    if match is None:
        return None
    body = match.group("body").strip()
    from_state: str | None = None
    # The pane draws a baseline with a bullet and no source state at all
    # (ticker.py: BASELINE_ARROW when is_baseline). No stream on disk carries
    # that shape yet — the rendered rows that exist all use the transition
    # arrow, including the one-token arrivals — but the live renderer can
    # produce it, so it is read rather than dropped.
    if _BASELINE_ARROW in body:
        left, _, right = body.partition(_BASELINE_ARROW)
        event = "baseline"
    elif _TRANSITION_ARROW in body:
        left, _, right = body.partition(_TRANSITION_ARROW)
        event = "transition"
        tokens = left.split()
        if not tokens:
            return None
        # One token left of the arrow is an arrival, not a malformed line: the
        # follower saw the node first, so there is no source state to print and
        # the node is the only token. Requiring two tokens here discards that
        # arrival and reports the node's next row in its place.
        if len(tokens) > 1:
            from_state = tokens[-1]
            left = left[: left.rfind(from_state)]
    else:
        return None
    right_tokens = right.split()
    if not right_tokens:
        return None
    left_tokens = left.split()
    if not left_tokens:
        return None
    return {
        "event": event,
        "node": left_tokens[-1],
        "from_state": from_state,
        "to_state": right_tokens[0],
    }


def parse_watch_row(
    line: str, *, path: Path, line_number: int
) -> dict[str, Any] | None:
    """Return one watch row in the stable shape, or None for an unusable line.

    Both renderings of the same stream land here: the JSON record the producer
    writes, and the rendered ticker line an older one left behind. They are
    returned in one shape so a caller cannot accidentally count both, and the
    rendered form is marked as carrying no establishable stamp rather than
    being dropped silently — dropping it would lose the only record of rows
    written before the format changed.
    """
    text = line.strip()
    if not text:
        return None
    project = _project_of(path)
    if text.startswith("{"):
        try:
            record = json.loads(text)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        stamp, zone = _normalize_stamp(record.get("observed_at"))
        return {
            "project": str(record.get("project") or project),
            "node": str(record.get("node") or ""),
            "run_id": record.get("run_id"),
            "session": record.get("session") or None,
            "event": str(record.get("event") or ""),
            "from_state": record.get("from_state"),
            "to_state": record.get("to_state"),
            "observed_at_utc": stamp,
            "observed_at_zone": zone,
            "rendered": False,
            "stream": str(path),
            "line": line_number,
        }
    rendered = _rendered_row(text)
    if rendered is None:
        return None
    return {
        "project": project,
        "node": str(rendered["node"]),
        "run_id": None,
        "session": None,
        "event": rendered["event"],
        "from_state": rendered["from_state"],
        "to_state": rendered["to_state"],
        "observed_at_utc": None,
        "observed_at_zone": "unknown",
        "rendered": True,
        "stream": str(path),
        "line": line_number,
    }


def read_watch_rows(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Return every usable row of the given streams, in stream order.

    Rows keep file-then-line order rather than being sorted by stamp, because
    the stamp is exactly what is missing from a rendered row and an
    append-only stream is already chronological.
    """
    rows: list[dict[str, Any]] = []
    for entry in paths:
        path = Path(entry)
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                row = parse_watch_row(line, path=path, line_number=number)
                if row is None or not row["node"]:
                    continue
                rows.append(row)
    return rows


def _re_inventory_bursts(rows: Sequence[Mapping[str, Any]]) -> set[tuple[str, int]]:
    """Return the stream positions belonging to a fleet re-inventory burst.

    A follower that attaches emits one baseline row per live run at a single
    instant, so the group shares a stamp; a group of two or more such rows one
    of whose members has an earlier row is inventory of a fleet that was
    already running, and not one of its members arrived by being in it. The
    already-known-member test is what keeps this from swallowing a genuine
    wave: a same-second group that is a wave's first observation has no member
    with an earlier row, and is left as arrivals.
    """
    first_line: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row["stream"]), str(row["node"]))
        first_line[key] = min(first_line.get(key, row["line"]), int(row["line"]))
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        if row["event"] != "baseline" or not row["observed_at_utc"]:
            continue
        groups.setdefault((str(row["stream"]), str(row["observed_at_utc"])), []).append(
            row
        )
    marked: set[tuple[str, int]] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        known = any(
            int(row["line"]) != first_line[(str(row["stream"]), str(row["node"]))]
            for row in group
        )
        if not known:
            continue
        for row in group:
            marked.add((str(row["stream"]), int(row["line"])))
    return marked


def extract_watch_arrivals(
    paths: Iterable[Path] | None = None, *, home: Path | None = None
) -> dict[str, Any]:
    """Return which watch rows are arrivals, which are state changes, and which
    are re-inventory, each stamped in UTC or explicitly unknown.

    This is the one answer every reader should ask for instead of writing its
    own filter over the event classes: a node's earliest row is its arrival
    whatever class the producer gave it, later transitions are the state
    changes it made, and later baselines plus every member of a re-inventory
    burst are inventory rather than news. Rows whose zone could not be
    established are still classified (position in the stream orders them) and
    are additionally listed, so a caller can see what it may not stamp.

    The entry point a command should expose is
    ``extract_watch_arrivals(project=...)`` returning this same mapping; no
    command wiring is added here because the CLI is owned by another surface.
    """
    selected = (
        [Path(entry) for entry in paths if str(entry)]
        if paths is not None
        else watch_event_paths(home=home)
    )
    rows = read_watch_rows(selected)
    burst = _re_inventory_bursts(rows)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((str(row["project"]), str(row["node"])), []).append(row)

    arrivals: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    for node_rows in grouped.values():
        node_rows.sort(key=lambda row: (str(row["stream"]), int(row["line"])))
        for index, row in enumerate(node_rows):
            position = (str(row["stream"]), int(row["line"]))
            if index == 0 and position not in burst:
                arrivals.append({**row, "kind": "arrival"})
            elif row["event"] == "transition":
                changes.append({**row, "kind": "state-change"})
            else:
                inventory.append({**row, "kind": "re-inventory"})

    for bucket in (arrivals, changes, inventory):
        bucket.sort(key=lambda row: (str(row["stream"]), int(row["line"])))
    unstamped = [row for row in rows if row["observed_at_zone"] != "utc"]
    return {
        "ok": True,
        "view": "watch-arrivals",
        "files": [str(path) for path in selected],
        "row_count": len(rows),
        "arrivals": arrivals,
        "state_changes": changes,
        "re_inventory": inventory,
        "stamps_unknown": unstamped,
        "counts": {
            "arrivals": len(arrivals),
            "state_changes": len(changes),
            "re_inventory": len(inventory),
            "stamps_unknown": len(unstamped),
            "rendered_rows": sum(1 for row in rows if row["rendered"]),
        },
    }
