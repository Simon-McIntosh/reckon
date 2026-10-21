# ruff: noqa: I001, UP035
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from reckon._timestamps import parse_utc
from reckon.crew import review as review_module
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    CrewError,
)
from reckon.crew.reports import (
    ManifestParseError,
    manifest_status_is_template,
    parse_manifest,
)
from reckon.crew.repair import round_node_head_suffix, round_node_source
from reckon.crew.routing import _signal_process_group
from reckon.crew.runs import (
    _mutate_pointer,
    _utc_now,
    list_live,
    run_dir,
    runs_dir,
)


# ── A delivered review ends its turn ────────────────────────────────────────
# A review's entire deliverable is the record it stores for the run it reviews,
# so once that record parses and the reviewer's own manifest reads complete
# there is nothing left to produce. Measured 2026-09-23: two reviews stored
# their records and complete manifests, then kept streaming for another 56 and
# 40 minutes — not stuck, but inventing further verification after the
# deliverable existed and spending a lane the fleet needed, at a moment the
# lane read 180% of its computed budget. The grace below is measured from the
# later of the two writes that make a review delivered, so a reviewer that
# stored its record and is still finishing its own turn is left alone until its
# stream has genuinely outlived the delivery rather than the watcher's notice.
DELIVERED_REVIEW_GRACE_SECONDS = 300.0


def _is_review_run(record: Mapping[str, Any]) -> bool:
    """Whether a run is a reviewer, by its role or by its minted node id."""
    return _is_review_node(record) or _pointer_role(record) == REVIEW_ROLE


def _review_store_record_paths(record: Mapping[str, Any], project: str) -> list[Path]:
    """The paths a reviewer was told to write its record to, in order.

    The dispatch composes them from the reviewed run, so they name the record
    this run is meant to deliver. A run review declares them inside a directory
    of the run it reviews, which is where they are resolved: the reviewed run's
    directory is the identity the composer and this reader both hold, so naming
    it here is what keeps the reader off the store until the record has been
    filed into it. A declaration that is not one of these is still read as a
    store path when it sits under the store's own project directory, so a
    pointer written before the record moved still names the record it was
    dispatched for.

    A path is selected by where it sits rather than by its filename, because
    the head-keyed and legacy spellings differ only in a suffix the reader must
    not have to guess.
    """
    node = record.get("node") or {}
    directory = review_module.review_store_root() / project
    run_directory = _review_record_delivery_directory(record, project)
    paths: list[Path] = []
    for declared in node.get("write_paths") or ():
        landed = _delivered_record_location(declared, run_directory)
        if landed is not None:
            paths.append(landed)
            continue
        candidate = Path(str(declared))
        if candidate.parent == directory:
            paths.append(candidate)
    return paths


def _is_run_review_node(record: Mapping[str, Any]) -> bool:
    """Whether a review run reads a run, rather than a plan.

    The question is which subject a review node id was minted from, and the
    node id grammar owns that answer: ``round_node_source`` recovers the source
    a round id carries, and only a run review's id carries one under the review
    prefix — a plan review's id carries its slug under the plan-review prefix
    instead, so the same call returns nothing for it. Neither predicate that
    already spells a review-versus-plan distinction answers this question:
    ``recovery_review_subject._is_review_node`` is true of both kinds, which is
    what a caller asking whether a run is a review at all needs, and
    ``review_lifecycle._is_run_review`` asks it of a stored review's subject
    rather than of a dispatch record, which is the only thing a delivery
    predicate is handed. So the narrower reading lives here, on the owner of
    the id grammar, rather than as a second copy of the prefix test.
    """
    node = record.get("node") or {}
    node_id = node.get("id") if isinstance(node, Mapping) else node
    return bool(round_node_source(str(node_id or ""), REVIEW_NODE_PREFIX))


def _declared_head_keyed_record(
    record: Mapping[str, Any], project: str
) -> tuple[Path, str] | None:
    """The head-keyed record path a review declares, and the head it spells.

    The head a review was dispatched to read is the head of the ``.at-`` write
    path its dispatch composed, read with the same parser the dispatch's own
    subject reader uses so the composer's spelling and this reader's are one.
    A review declaring only the legacy path names no head, which is exactly the
    ambiguity a delivered record cannot be told apart under.
    """
    for path in _review_store_record_paths(record, project):
        _, head = _review_record_subject(path)
        if head:
            return path, head
    return None


def _review_dispatch_instant(record: Mapping[str, Any], project: str) -> float | None:
    """When this review run was dispatched, as a POSIX instant, or None.

    Read from the run's own record where one exists — the committed row or the
    live pointer — and from the stamp the run id encodes otherwise, so a
    delivery is dated against the dispatch the fleet recorded rather than
    against a definition of dispatch invented here.
    """
    run_id = str(record.get("run_id") or "")
    if not run_id:
        return None
    dispatched, _completed = review_module.run_record_times(project, run_id)
    if not dispatched:
        dispatched = review_module.run_id_dispatch_time(run_id)
    if not dispatched:
        return None
    parsed = parse_utc(dispatched)
    if parsed is None:
        return None
    return parsed.timestamp()


def _delivered_record_location(
    declared: Any, run_directory: Path | None
) -> Path | None:
    """Where a declared path lands when it is a run review's own record.

    The reviewed run's directory is checked first, because it is the identity
    the composer and most readers both hold. A reader that cannot resolve it —
    a review whose node id does not spell the run it reviews, as a re-review's
    does not — still recognises the declaration by its shape, and the shape is
    anchored on the runs root so a repository path that happens to carry the
    directory name is not read as a delivery. Without that second rule the
    reader would fall through to the store, where a live review's record has
    not been filed yet, and report the run as owing a review it is owed none of.
    """
    if run_directory is not None:
        located = review_module.run_review_record_location(declared, run_directory)
        if located is not None:
            return located
    candidate = Path(str(declared))
    if candidate.parent.name != review_module.RUN_REVIEW_RECORD_DIRNAME:
        return None
    if candidate.parent.parent.parent != runs_dir():
        return None
    return candidate


def _review_record_delivery_directory(
    record: Mapping[str, Any], project: str
) -> Path | None:
    """Where a review run delivers its record: the reviewed run's directory.

    Resolved from the run the review's node id spells. A review whose id does
    not spell one — a re-review, or a hand dispatch under a coordinator's own
    id — names the run in the record paths it declares instead, which is read
    as the fallback rather than being dropped: the declaration is the run's own
    record of where its deliverable goes.
    """
    reviewed = _resolved_reviewed_run_id(record, project)
    if reviewed:
        return run_dir(reviewed)
    return _declared_record_directory(record)


def _declared_record_directory(record: Mapping[str, Any]) -> Path | None:
    """The run directory a review's own declarations name, or None.

    ``<runs>/<run id>/review-records/<record>.json`` spells its run in the path
    itself, so a reader that cannot resolve the reviewed run from the node id
    recovers it here without guessing.
    """
    for declared in (record.get("node") or {}).get("write_paths") or ():
        located = _delivered_record_location(declared, None)
        if located is not None:
            return located.parent.parent
    return None


def file_review_records_into_store(record: Mapping[str, Any]) -> list[Path]:
    """File a review run's delivered records into the store, return their paths.

    The supervisor calls this when its worker exits and ``crew complete`` calls
    it for a run whose supervisor did not get that far, so both end-of-run
    paths file a delivered record. A run that is not a review declares no
    record path, so it files nothing and pays one pointer read.
    """
    project = str(record.get("project") or "")
    directory = _review_record_delivery_directory(record, project)
    if directory is None:
        return []
    try:
        return review_module.file_delivered_run_reviews(record, run_directory=directory)
    except (OSError, ValueError):
        return []


def _resolved_reviewed_run_id(
    record: Mapping[str, Any], project: str, *, refuse_ambiguous: bool = False
) -> str:
    """The run a reviewer reviews, resolved from its minted node id, or empty.

    The review node id carries what its dispatch reviewed — the source run's id
    or, where it had one, its node id — so the store can be read even when the
    pointer predates the write paths that name the record directly. A source
    that is not a run id is matched against the live fleet's node ids, which is
    the only place the reviewed run's id is recoverable once its own node id
    was resolved to a run id at dispatch.

    ``refuse_ambiguous`` is set only by the dispatch grant that fences a
    reviewer's record directory. A paired dispatch launches two arms under one
    node id, so a bare node id matched against the live fleet finds two runs
    and the first match is one arm chosen by listing order rather than by
    intent — the grant would then reach whichever arm the listing happened to
    return first. With the flag set, more than one matching pointer returns the
    bare source itself, which names no run directory, so the grant is refused
    rather than given to the wrong run. One matching pointer, and every reader
    that leaves the flag false, resolves as before.
    """
    node_id = str((record.get("node") or {}).get("id") or "")
    source = round_node_source(node_id, REVIEW_NODE_PREFIX)
    if not source:
        return ""
    if source.startswith("r-"):
        return source
    matches = [
        str(pointer.get("run_id") or "")
        for pointer in list_live(project=project)
        if str((pointer.get("node") or {}).get("id") or "") == source
    ]
    if not matches:
        return _durable_reviewed_run_id(node_id, source, project)
    if refuse_ambiguous and len(matches) > 1:
        return source
    return matches[0]


def _durable_reviewed_run_id(node_id: str, source: str, project: str) -> str:
    """The one durable run a review node id names, or its bare source.

    A review composed against a run the fleet has already let go has no live
    pointer to match against, so the node id is resolved over the durable rows
    carrying its source node id instead. The head suffix the node id mints
    chooses among them: a row matches when the head a review of that run reads
    begins with the suffix, and a node id carrying no suffix matches every row
    with the source, which is the single-row spelling written before rounds
    were head-keyed.

    Exactly one match resolves. No match and more than one both return the bare
    source, which names no run directory, so a grant refuses rather than
    pointing at a run the id does not identify — the same answer the ambiguous
    live case gives. ``project`` scopes the rows whenever the caller has one,
    and for the dispatch grant, which holds none, the selection spans projects.

    A durable read that raises is answered with the bare source and never
    propagates: a store this cannot read leaves the resolution exactly as it
    stood before the fallback existed, rather than turning a missing pointer
    into a dispatch failure.
    """
    from reckon import run_store
    from reckon.crew.recovery_review_dispatch import _run_head_for_review

    try:
        records = run_store.runs_for_node(source, project=project or None)
    except Exception:  # noqa: BLE001 — an unreadable store is not a resolution
        return source
    suffix = round_node_head_suffix(node_id, REVIEW_NODE_PREFIX)
    named = {
        str(record.get("run_id") or "")
        for record in records
        if not suffix or _run_head_for_review(record).lower().startswith(suffix.lower())
    }
    named.discard("")
    if len(named) != 1:
        return source
    return named.pop()


def _complete_manifest_write_time(path: Path) -> float | None:
    """When a run's manifest last read complete, or None when it does not.

    Read directly rather than through the classifier: the classifier defers a
    live process's terminal report, and the whole point of this reader is a
    review whose process is still alive while its manifest has already reached
    its verdict. A status still carrying the dispatch template is not a verdict,
    so a placeholder never reads as a delivery.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = parse_manifest(text)
    except ManifestParseError:
        return None
    status = str(data.get("status") or "").strip().lower()
    if manifest_status_is_template(status) or status != "complete":
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _read_review_record(path: Path) -> tuple[Mapping[str, Any], float] | None:
    """The record at ``path`` and when it landed, without judging it complete.

    Kept separate from the completeness judgement so a refusal can tell a
    record that cannot be read from one that reads and is not a complete
    review: the two call for different remedies, and a reader that folds them
    reports a corrupt file where a good one is merely missing a total.
    """
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(stored, Mapping):
        return None
    try:
        return stored, path.stat().st_mtime
    except OSError:
        return None


def _review_delivery_verdict(record: Mapping[str, Any], project: str) -> dict[str, Any]:
    """The single evaluation of the conditions a review's delivery depends on.

    A run review is dispatched to read one revision, and the record that
    delivers it is the one filed at the head-keyed path that dispatch names. A
    record delivers only when all six of the conditions the returns below name
    hold together: the run declares a head-keyed record path; a record there
    reads as a review; it is a complete review; the head it describes is the
    head the path names, compared with the owners the promotion gate and the
    classifier already share; its ``review_run_id``, when it carries one, names
    this review run; and it was written after this run was dispatched. A record
    failing any of the six is another round's verdict standing where this run's
    would be — the defect measured on 2026-10-18, when a run that never wrote
    its record was promoted on the unsuffixed path an earlier round had left
    complete. The legacy path and the any-head ``.at-`` glob are therefore no
    delivery at all.

    The conditions are written once, here, so :func:`_delivered_review_record`
    and :func:`review_delivery_refusal` are its two projections and no reader
    restates them. A refusal returns the one condition that failed, the record
    path it concerns when there is one, and the remedy that condition calls for,
    so a coordinator is told which condition to act on rather than that the
    record cannot be read. The watcher adds a refusal this cannot express — the
    one naming the head when a run review declares no head-keyed path at all, a
    run this sees only as having stored nothing.
    """
    declared = _declared_head_keyed_record(record, project)
    if declared is None:
        return {
            "delivered": False,
            "condition": "no head-keyed record path",
            "path": "",
            "detail": (
                "declares no head-keyed record path, so the head it was "
                "dispatched to read is unnamed"
            ),
            "remedy": (
                "re-dispatch the review so the record path it is granted "
                "carries the head it read"
            ),
        }
    path, head = declared
    read = _read_review_record(path)
    if read is None:
        return {
            "delivered": False,
            "condition": "record cannot be read",
            "path": str(path),
            "detail": f"stored no record that reads as a review at {path}",
            "remedy": "store the review record there, or pass --outcome",
        }
    body, mtime = read
    missing = _review_missing_measures(body)
    if missing:
        listed = ", ".join(missing)
        return {
            "delivered": False,
            "condition": "record is not a complete review",
            "path": str(path),
            "detail": (
                f"stored a record at {path} that is not a complete review: it "
                f"lacks {listed}"
            ),
            "remedy": (
                "pass --outcome with the verdict the review reached, or "
                f"re-store the review with {listed}"
            ),
        }
    described = review_described_head(body, tree=None)
    if not same_revision(described, head):
        return {
            "delivered": False,
            "condition": "record describes a different head",
            "path": str(path),
            "detail": (
                f"stored a record at {path} describing head "
                f"{described or '(none)'} while its path names head {head}"
            ),
            "remedy": "re-dispatch the review against the head it read",
        }
    named = str(body.get("review_run_id") or "").strip()
    run_id = str(record.get("run_id") or "").strip()
    if named and named != run_id:
        return {
            "delivered": False,
            "condition": "record names a different review run",
            "path": str(path),
            "detail": (
                f"stores a record at {path} naming review run {named} while "
                f"this run is {run_id}"
            ),
            "remedy": "re-dispatch the review so it writes its own record",
        }
    dispatched = _review_dispatch_instant(record, project)
    if dispatched is None or mtime <= dispatched:
        written = datetime.fromtimestamp(mtime, UTC).isoformat()
        if dispatched is None:
            detail = (
                f"stores a record at {path} written at {written}, and this "
                "run's dispatch instant is unknown, so the record cannot be "
                "told from an earlier round's verdict"
            )
        else:
            dispatched_at = datetime.fromtimestamp(dispatched, UTC).isoformat()
            detail = (
                f"stores a record at {path} written at {written}, before this "
                f"run was dispatched at {dispatched_at}, so it is an earlier "
                "round's verdict"
            )
        return {
            "delivered": False,
            "condition": "record written before the dispatch",
            "path": str(path),
            "detail": detail,
            "remedy": "re-dispatch the review so it writes a fresh record",
        }
    return {"delivered": True, "mtime": mtime, "path": str(path)}


def _delivered_review_record(
    record: Mapping[str, Any], project: str
) -> tuple[float, Path] | None:
    """The stored review record a reviewer wrote and when it landed, or None.

    Whether a review has delivered is decided in :func:`_review_delivery_verdict`
    alone, and this is that decision's answer to the question every delivery
    reader asks; :func:`review_delivery_refusal` reads the same verdict to name
    the one condition that failed. The verdict carries the path as a string, so
    every branch names it one way; this reader returns it as a ``Path``, which
    is what its callers open.
    """
    verdict = _review_delivery_verdict(record, project)
    if not verdict["delivered"]:
        return None
    return verdict["mtime"], Path(verdict["path"])


def review_delivery_refusal(
    record: Mapping[str, Any], project: str
) -> dict[str, Any] | None:
    """The one delivery condition a review run failed, or None when it delivered.

    Every field a caller needs to act on the refusal is here: the condition's
    name, the record path it concerns when there is one, a statement of what was
    found, and the remedy that condition calls for. A record that delivers
    returns None, so a refusal reader and a delivery reader judge delivery from
    one verdict rather than from two copies of the conditions.
    """
    verdict = _review_delivery_verdict(record, project)
    if verdict["delivered"]:
        return None
    return verdict


def review_delivered(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The facts of a review run's delivery, or None while it has not delivered.

    A review delivers when the record it was minted to store parses and its own
    manifest reads complete; both are read from this run's own store path and
    manifest, never from a classifier that defers a live process's verdict. The
    returned mapping names both paths and both write times, so a caller
    measuring a grace reads the later of the two rather than guessing which
    write finished last. Public because more than the watcher asks whether a
    review is done: the fact is a property of the run, and any reader with the
    record can ask it.
    """
    if not _is_review_run(record):
        return None
    project = str(record.get("project") or "")
    manifest_path = Path(str(record.get("manifest_path") or ""))
    manifest_time = _complete_manifest_write_time(manifest_path)
    if manifest_time is None:
        return None
    stored = _delivered_review_record(record, project)
    if stored is None:
        return None
    record_time, record_path = stored
    return {
        "record_path": str(record_path),
        "manifest_path": str(manifest_path),
        "record_mtime": record_time,
        "manifest_mtime": manifest_time,
        "delivered_at": max(record_time, manifest_time),
    }


def _stop_delivered_reviews(
    pointers: Sequence[Mapping[str, Any]],
    *,
    grace_seconds: float = DELIVERED_REVIEW_GRACE_SECONDS,
    signal_run: Callable[..., None] | None = None,
) -> list[dict[str, Any]]:
    """Stop each review whose stream has outlived its delivery by the grace.

    A review that delivered and kept streaming is not still working: its
    deliverable is stored and its manifest is a verdict, so every further turn
    spends the lane the review holds and nothing else. The stop is signalled to
    the run's own recorded pid and nothing wider, because the watcher shares the
    login node with every peer session and a group signal reaches work this rule
    was never given. A stop that cannot be delivered is left for the next tick
    rather than recorded as one, because a record claiming a stopped process
    that is still running is worse than no record at all.
    """
    stopped: list[dict[str, Any]] = []
    for pointer in pointers:
        delivered = review_delivered(pointer)
        if not delivered:
            continue
        if str(pointer.get("phase") or "") in _TERMINAL_RUN_PHASES:
            continue
        stream_mtime = _run_stream_mtime(pointer)
        if stream_mtime is None:
            continue
        if stream_mtime - delivered["delivered_at"] <= grace_seconds:
            continue
        pid = pointer.get("pid")
        try:
            if signal_run is None:
                _signal_process_group(
                    int(pid),
                    pointer.get("pid_start_time"),
                    run_dir=_run_directory(pointer),
                    reason="delivered-review-outlived-grace",
                )
            else:
                signal_run(int(pid), pointer.get("pid_start_time"))
        except (
            CrewError,
            ProcessLookupError,
            PermissionError,
            OSError,
            TypeError,
            ValueError,
        ):
            continue
        run_id = str(pointer.get("run_id") or "")
        ended_at = _utc_now()
        detail = {
            "stopped_at": ended_at,
            "grace_seconds": grace_seconds,
            "record_path": delivered["record_path"],
            "manifest_path": delivered["manifest_path"],
        }

        def record(
            existing: dict[str, Any],
            *,
            _detail: Mapping[str, Any] = detail,
            _ended_at: str = ended_at,
        ) -> dict[str, Any]:
            existing["phase"] = "stopped"
            existing["stopped_at"] = _ended_at
            existing["ended_after_delivery"] = dict(_detail)
            return existing

        written = _mutate_pointer(run_id, record) if run_id else None
        if isinstance(pointer, dict) and written is not None:
            pointer.update(written)
        stopped.append(
            {
                "run_id": run_id,
                "pid": int(pid),
                "stopped_at": ended_at,
                "ended_after_delivery": dict(detail),
            }
        )
    return stopped


from .recovery_liveness import (  # noqa: E402
    _run_stream_mtime,
)
from .recovery_review_dispatch import (  # noqa: E402
    _review_record_subject,
)
from .recovery_review_subject import (  # noqa: E402
    _is_review_node,
    _review_missing_measures,
    review_described_head,
    same_revision,
)
from .recovery_vocabulary import (  # noqa: E402
    REVIEW_NODE_PREFIX,
    REVIEW_ROLE,
)
from .recovery_wait import (  # noqa: E402
    _run_directory,
)
from .recovery_watch import (  # noqa: E402
    _pointer_role,
)
