"""Compact read models joining live pointers with committed run records."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import ledger
from reckon.crew.node import normalize_section
from reckon.crew.recovery import classify_pointer
from reckon.crew.resumption import resolve_session
from reckon.crew.routing import mounted_repository_projects
from reckon.crew.runs import crew_home, list_live

DEFAULT_RUN_FIELDS = (
    "run_id",
    "node",
    "plan",
    "section",
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
    "member",
    "agent",
    "base_sha",
    "manifest_present",
    "commits",
    "commits_beyond_base",
    "log_age_seconds",
    "manifest_reported_status",
)

RUN_SOURCES = frozenset({"all", "ledger", "live"})
RUN_SCOPES = frozenset({"project", "workstation"})
WORKSTATION_RUN_FIELDS = ("project", "repo", "repo_exists")

#: A projected live row always carries the identity of the run it describes, so
#: a caller that asked for one field can still tell which run answered.
LIVE_ROW_ANCHOR = "run_id"


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
    """Return default fields plus validated opt-in fields, in stable order."""
    if fields is None:
        return DEFAULT_RUN_FIELDS
    requested = _requested_field_names(fields)
    _refuse_unknown_fields(requested)
    extras = tuple(field for field in OPTIONAL_RUN_FIELDS if field in requested)
    return (*DEFAULT_RUN_FIELDS, *extras)


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
    caller that does not narrow receives. An unknown field is refused here
    through the same accepted set the compact read model validates against.

    ``session`` marks which rows belong to the caller's own session, computed
    from the full classification so the marker survives projection.
    """
    classified = [classify_pointer(record) for record in records]
    if session is not None:
        for row in classified:
            row["mine"] = str(row.get("session") or "") == session
    if fields is None:
        return classified
    requested = _requested_field_names(fields)
    _refuse_unknown_fields(requested)
    selected = tuple(dict.fromkeys((LIVE_ROW_ANCHOR, *requested)))
    return [{field: row.get(field) for field in selected} for row in classified]


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


def _compact_row(
    record: Mapping[str, Any],
    *,
    source: str,
    project: str,
    checkout_path: str | None,
    repository: Path | None,
    selected_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Project one source record into the stable compact row shape."""
    classified = classify_pointer(record) if source == "live" else {}
    node_data = record.get("node")
    node_mapping = node_data if isinstance(node_data, Mapping) else {}
    node = classified.get("node") if source == "live" else record.get("node")
    plan = classified.get("plan") if source == "live" else record.get("plan")
    section = node_mapping.get("section") if source == "live" else record.get("section")
    worktree = record.get("worktree") or None
    worktree_exists = _path_exists(worktree, directory=True)
    transcript = record.get("transcript_path") or None
    session = resolve_session(
        str(record.get("run_id") or ""),
        record=record if source == "live" else None,
        project=project,
        root=checkout_path,
    )
    if source == "live":
        commits = classified.get("manifest_commits") or record.get("commits") or []
        manifest_present = classified.get("manifest_present", False)
    else:
        commits = record.get("commits") or []
        manifest_present = _path_exists(record.get("manifest_path"))
    process_alive = (
        classified.get("process_alive")
        if source == "live"
        else record.get("process_alive")
    )
    resumable, resumable_reason = _resumability(
        session,
        worktree_exists=worktree_exists,
        process_alive=process_alive,
    )
    complete = {
        "run_id": str(record.get("run_id") or ""),
        "node": str(node or ""),
        "plan": str(plan or ""),
        "section": normalize_section(section) if section else "",
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
        "worktree_exists": worktree_exists,
        "transcript_path": transcript,
        "transcript_exists": (
            bool(record.get("transcript_exists"))
            if "transcript_exists" in record
            else _path_exists(transcript)
        ),
        "resumable": resumable,
        "resumable_reason": resumable_reason,
        "project": project,
        "repo": str(repository) if repository is not None else "",
        "repo_exists": repository.is_dir() if repository is not None else False,
        "member": str(record.get("member") or ""),
        "agent": (
            dict(record["agent"]) if isinstance(record.get("agent"), Mapping) else {}
        ),
        "base_sha": str(record.get("base_sha") or ""),
        "manifest_present": bool(manifest_present),
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
    return {field: complete[field] for field in selected_fields}


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
) -> bool:
    """Apply every runs-view filter to one normalized row."""
    string_filters = {
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


def runs_view(
    project: str,
    *,
    checkout_path: str | None = None,
    source: str = "all",
    scope: str = "project",
    node: str | None = None,
    plan: str | None = None,
    section: str | None = None,
    session: str | None = None,
    member: str | None = None,
    classification: str | None = None,
    resumable: bool | None = None,
    newest_per_node: bool = False,
    fields: Iterable[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return compact live and committed run rows, newest first."""
    selected_source = str(source or "all").strip().lower()
    if selected_source not in RUN_SOURCES:
        raise RunQueryError(
            f"runs source must be one of {', '.join(sorted(RUN_SOURCES))}"
        )
    selected_scope = str(scope or "project").strip().lower()
    if selected_scope not in RUN_SCOPES:
        raise RunQueryError(
            f"runs scope must be one of {', '.join(sorted(RUN_SCOPES))}"
        )
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
    rows: list[dict[str, Any]] = []
    if selected_source in {"all", "live"}:
        live_records = list_live()
        if selected_scope == "project":
            live_records = [
                record
                for record in live_records
                if str(record.get("project") or "") == project
            ]
        for record in live_records:
            row_project = str(record.get("project") or "")
            repository = _record_repository(record, mounted_projects)
            rows.append(
                _compact_row(
                    record,
                    source="live",
                    project=row_project,
                    checkout_path=(
                        str(repository)
                        if selected_scope == "workstation" and repository is not None
                        else checkout_path
                    ),
                    repository=repository,
                    selected_fields=row_fields,
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
            rows.extend(
                _compact_row(
                    record,
                    source="ledger",
                    project=row_project,
                    checkout_path=ledger_root,
                    repository=repository,
                    selected_fields=row_fields,
                )
                for record in ledger.runs(row_project, ledger_root)
            )

    rows = [
        row
        for row in rows
        if _matches(
            row,
            node=node,
            plan=plan,
            section=section,
            session=session,
            member=member,
            classification=classification,
            resumable=resumable,
        )
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
    if "member" not in selected_fields:
        for row in rows:
            row.pop("member", None)
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
    downstream from one the producer recorded.
    """
    text = str(value or "").strip()
    if not text:
        return None, "unknown"
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None, "unknown"
    if parsed.tzinfo is None:
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
