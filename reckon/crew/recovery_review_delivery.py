# ruff: noqa: I001, UP035
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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
from reckon.crew.routing import _signal_process_group
from reckon.crew.runs import (
    _mutate_pointer,
    _utc_now,
    list_live,
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
    """The review-store paths a reviewer was told to write, in order.

    The dispatch composes them from the reviewed run, so they name the record
    this run is meant to deliver. A path is selected by where it sits — under
    the review store's own project directory — rather than by its filename,
    because the head-keyed and legacy spellings differ only in a suffix the
    reader must not have to guess.
    """
    node = record.get("node") or {}
    directory = review_module.review_store_root() / project
    paths: list[Path] = []
    for declared in node.get("write_paths") or ():
        candidate = Path(str(declared))
        if candidate.parent == directory:
            paths.append(candidate)
    return paths


def _resolved_reviewed_run_id(record: Mapping[str, Any], project: str) -> str:
    """The run a reviewer reviews, resolved from its minted node id, or empty.

    The review node id carries what its dispatch reviewed — the source run's id
    or, where it had one, its node id — so the store can be read even when the
    pointer predates the write paths that name the record directly. A source
    that is not a run id is matched against the live fleet's node ids, which is
    the only place the reviewed run's id is recoverable once its own node id
    was resolved to a run id at dispatch.
    """
    node_id = str((record.get("node") or {}).get("id") or "")
    if not node_id.startswith(REVIEW_NODE_PREFIX):
        return ""
    source = node_id[len(REVIEW_NODE_PREFIX) :]
    if not source:
        return ""
    if source.startswith("r-"):
        return source
    for pointer in list_live(project=project):
        if str((pointer.get("node") or {}).get("id") or "") == source:
            return str(pointer.get("run_id") or "")
    return source


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


def _read_complete_review(path: Path) -> tuple[float, Path] | None:
    """A stored review at ``path`` and when it landed, when it parses complete."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(stored, Mapping) or not _review_is_complete(stored):
        return None
    try:
        return path.stat().st_mtime, path
    except OSError:
        return None


def _delivered_review_record(
    record: Mapping[str, Any], project: str
) -> tuple[float, Path] | None:
    """The stored review record a reviewer wrote and when it landed, or None.

    The record the dispatch told this run to write is read first; the store
    lookup by the reviewed run is the fallback for a pointer that carried no
    write paths. Both judge a parse with the predicate promotion uses, so a
    watcher and a coordinator cannot disagree about whether a review counts as
    delivered.
    """
    for path in _review_store_record_paths(record, project):
        stored = _read_complete_review(path)
        if stored is not None:
            return stored
    reviewed = _resolved_reviewed_run_id(record, project)
    if not reviewed:
        return None
    candidates = [review_module.review_path(project, reviewed)]
    directory = review_module.review_store_root() / project
    if directory.is_dir():
        candidates.extend(sorted(directory.glob(f"{reviewed}.at-*.json")))
    for path in candidates:
        stored = _read_complete_review(path)
        if stored is not None:
            return stored
    return None


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
from .recovery_review_subject import (  # noqa: E402
    _is_review_node,
    _review_is_complete,
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
