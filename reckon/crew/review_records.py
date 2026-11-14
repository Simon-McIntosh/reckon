"""Readers, writers and the index for the store.

A review record is stored once per round in the project's review store, and a
promotion commits one copy of that record into the project's committed reviews
tree. This module owns that family: reading a run's record from the committed
tree first and the staging store after, selecting the copy a named head names,
unioning the disposition and answer fields every copy of one review run holds,
writing a record and committing that write, and the store index that answers a
misfiled lookup and a delivered-plan-review lookup. Staging stores keep a
shared writer-maintained index; committed directories use a node-local cache.

The store resolves its directories through :mod:`reckon.crew.review` and shares
its schema constants and revision helpers with it, so those names are imported
from there rather than restated. The import sits at the foot of this module
because :mod:`reckon.crew.review` re-exports this family: each module imports
the names it needs once the other has finished defining them, which breaks the
cycle without either module owning a copy of what the other defines.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from reckon import file_memo
from reckon.crew import open_run_index

# Each producer tick freezes the store's names and indexed content hashes.
# Parsed bytes use the shared bounded memo; writer updates move the next tick's
# key without stat-ing every record.
_TICK_REVIEW_STORES: ContextVar[dict[Path, dict[Path, os.stat_result]] | None] = (
    ContextVar("tick_review_stores", default=None)
)
_TICK_REVIEW_INDEXES: ContextVar[dict[Path, Any] | None] = ContextVar(
    "tick_review_indexes", default=None
)
_TICK_INDEX_DOCUMENTS: ContextVar[dict[Path, Any] | None] = ContextVar(
    "tick_index_documents", default=None
)


@contextmanager
def review_store_tick() -> Iterator[None]:
    """Share one index snapshot per store among review readers in a tick."""
    stores_token = _TICK_REVIEW_STORES.set({})
    indexes_token = _TICK_REVIEW_INDEXES.set({})
    documents_token = _TICK_INDEX_DOCUMENTS.set({})
    try:
        yield
    finally:
        _TICK_INDEX_DOCUMENTS.reset(documents_token)
        _TICK_REVIEW_INDEXES.reset(indexes_token)
        _TICK_REVIEW_STORES.reset(stores_token)


def _tick_store(directory: Path) -> dict[Path, os.stat_result] | None:
    # Inside a tick the first answer for a directory serves every later reader,
    # the open-work selection as well as the full listing: selecting reads every
    # live pointer and stats every file it names, so taking it per record makes
    # one refresh quadratic in the store's size.
    stores = _TICK_REVIEW_STORES.get()
    if stores is not None and directory in stores:
        return stores[directory]
    # A staging store answers from its own index, so no record is stat'ed and
    # the directory is not listed. The index is probed once per store per tick,
    # and once per call outside one, which is the single directory stat the
    # freshness of a writer-maintained index costs.
    document = store_index_document(directory)
    if document is not None:
        return _document_stat_view(directory, document, stores)
    selected = open_run_index.review_paths(directory)
    if selected is not None:
        files = {}
        for path in selected:
            try:
                files[path] = path.stat()
            except OSError:
                continue
        if stores is not None:
            stores[directory] = files
        return files
    if stores is None:
        return None
    if directory not in stores:
        files = {}
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.name.endswith(".json"):
                        continue
                    try:
                        info = entry.stat()
                    except OSError:
                        continue
                    if stat.S_ISREG(info.st_mode):
                        files[directory / entry.name] = info
        except OSError:
            pass
        stores[directory] = files
    return stores[directory]


def _document_stat_view(
    directory: Path,
    document: Mapping[str, Any],
    stores: dict[Path, dict[Path, os.stat_result]] | None,
) -> dict[Path, os.stat_result]:
    """One reader's view of a store, read from its index rather than its files."""
    files: dict[Path, os.stat_result] = {}
    for name, raw in (document.get("entries") or {}).items():
        entry = raw if isinstance(raw, Mapping) else {}
        files[directory / str(name)] = _indexed_record_stat(entry)
    if stores is not None:
        stores[directory] = files
    return files


def _store_paths(directory: Path, pattern: str) -> list[Path]:
    files = _tick_store(directory)
    if files is None:
        held = list(directory.glob(pattern))
    else:
        held = [path for path in files if fnmatchcase(path.name, pattern)]
    return [path for path in held if not path.name.startswith(".")]


def _record_stat(path: Path) -> os.stat_result:
    return _listed_stat(_tick_store(path.parent), path)


def _listed_stat(
    files: dict[Path, os.stat_result] | None, path: Path
) -> os.stat_result:
    """One record's stat, read from a listing already taken of its store.

    A loop over a store takes the listing once and reads every file's stat from
    it. Asking ``_record_stat`` per file instead re-takes the listing for each
    one, and taking it means reading every live pointer and stat-ing every file
    open work selects, so the loop becomes quadratic in the store's size.
    """
    if files is None:
        return path.stat()
    if path not in files:
        raise FileNotFoundError(path)
    return files[path]


def _listed_paths(
    directory: Path, files: dict[Path, os.stat_result] | None, pattern: str
) -> list[Path]:
    if files is None:
        return list(directory.glob(pattern))
    return [path for path in files if fnmatchcase(path.name, pattern)]


def _record_exists(path: Path) -> bool:
    try:
        return stat.S_ISREG(_record_stat(path).st_mode)
    except OSError:
        return False


def _record_identity(path: Path) -> str:
    document = store_index_document(path.parent)
    if document is not None:
        entry = document.get("entries", {}).get(path.name)
        return str(entry.get("sha256") or "unreadable") if entry else "absent"
    # Committed records are outside the writer-maintained staging index.
    try:
        return ":".join(map(str, file_memo.signature_from_stat(path.stat())))
    except OSError:
        return "absent"


def _record_file(path: Path) -> Any:
    def read() -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    if _TICK_REVIEW_STORES.get() is None:
        return read()
    identity = _record_identity(path)
    return file_memo.memoized_key("tick_review_record", (str(path), identity), read)


# A disposition or an answer is written back to whichever copy of a record the
# reader selected, and a promotion commits one copy of that record: the one a
# reviewer delivered, or a sibling at another path. The fields such a write
# touches — the disposition map, the per-finding response map and the response
# event list — are read from every copy of the record a promotion commits
# rather than from the single file it happens to read.
def _answer_stamp(entry: Any) -> str:
    """The moment an answer entry records, for choosing the latest per key."""
    if isinstance(entry, Mapping):
        for key in ("recorded_at", "when"):
            stamp = str(entry.get(key) or "")
            if stamp:
                return stamp
    return ""


def _staging_copies_of_record(
    project: str,
    record: Mapping[str, Any],
    base_dir: str | Path | None = None,
) -> list[Mapping[str, Any]]:
    """Every staging file that is another copy of ``record``, oldest first.

    A record is copied whenever a sibling of it is written, and a disposition
    or an answer is recorded on whichever copy the reader selected. A promotion
    commits one copy, so the answers a sibling carries are read from here and
    merged into the committed record rather than left behind in the host store.
    Copies are the files keyed by the record's own review run — the one key
    every copy shares — so a record the store filed under a different subject
    name is still its own. A file that cannot be read is skipped.

    ``base_dir`` names the staging store to read, so a reader that resolved its
    selection from a specific store reads that store's copies rather than the
    configured one; omitted, the root resolves through
    :func:`review_store_root` as it always has.
    """
    review_run_id = str(record.get("review_run_id") or "").strip()
    if not review_run_id:
        return []
    directory = _review.review_store_root(base_dir) / project
    if not directory.is_dir():
        return []
    copies: list[Mapping[str, Any]] = []
    for entry in sorted(
        _review_run_index(directory).get(review_run_id) or [],
        # Copies merge oldest first. The store index knows a record's write
        # order, which is the age that matters here; a store answered from the
        # per-file cache instead carries the file's own modification time.
        key=lambda item: (
            int(item.get("entry_version") or 0),
            int(item.get("mtime_ns") or 0),
            str(item.get("name") or ""),
        ),
    ):
        path = entry["path"]
        try:
            stored = _record_file(path)
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(stored, Mapping)
            and str(stored.get("review_run_id") or "").strip() == review_run_id
            and _review._review_record_rubric(stored)
            == _review._review_record_rubric(record)
        ):
            copies.append(stored)
    return copies


def _committed_record_at(path: str | Path) -> Mapping[str, Any] | None:
    """The committed record already stored at ``path``, or None when none parses.

    A round is stored once, then re-stored whenever a promotion commits the
    subject's rounds again. The re-store writes the round's body, which carries
    no answer; the answer a write recorded on the committed copy lives only in
    the file being overwritten, so it is read here and merged back rather than
    dropped. A path that does not exist yet, or whose bytes do not parse, has no
    record to preserve.
    """
    try:
        stored = _record_file(Path(path))
    except (OSError, json.JSONDecodeError):
        return None
    return stored if isinstance(stored, Mapping) else None


def _merge_record_answers(sources: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Union the answer fields of several copies of one review record.

    The answer mappings merge by key with the latest entry winning, ordered by the
    stamp each entry records — ``recorded_at`` on a disposition, ``when`` on a
    response — so a re-answer of one finding supersedes the copy an earlier
    write left. The event list is the union of every copy's events in time
    order, deduplicated by content, so the history of what was answered and
    when survives whichever copy happened to hold it. A field present in no
    copy is absent from the result, so a record carrying no answer is not given
    an empty one.
    """
    dispositions: dict[str, Any] = {}
    finding_dispositions: dict[str, Any] = {}
    responses: dict[str, Any] = {}
    events: list[Any] = []
    seen_events: set[str] = set()
    for source in sources:
        for key, target in (
            (_review.DIMENSION_DISPOSITIONS_KEY, dispositions),
            (_review.FINDING_DISPOSITIONS_KEY, finding_dispositions),
            ("responses", responses),
        ):
            mapping = source.get(key)
            if not isinstance(mapping, Mapping):
                continue
            for name, entry in mapping.items():
                existing = target.get(name)
                if existing is None or _answer_stamp(entry) >= _answer_stamp(existing):
                    target[name] = entry
        for event in source.get("response_events") or ():
            if not isinstance(event, Mapping):
                continue
            marker = json.dumps(event, sort_keys=True, default=str)
            if marker in seen_events:
                continue
            seen_events.add(marker)
            events.append(event)
    events.sort(key=_answer_stamp)
    merged: dict[str, Any] = {}
    if dispositions:
        merged[_review.DIMENSION_DISPOSITIONS_KEY] = dispositions
    if finding_dispositions:
        merged[_review.FINDING_DISPOSITIONS_KEY] = finding_dispositions
    if responses:
        merged["responses"] = responses
    if events:
        merged["response_events"] = events
    return merged


def store_committed_review(
    record: dict[str, Any],
    *,
    project: str | None = None,
    root: str | Path | None = None,
    committed_root: str | Path | None = None,
) -> Path:
    """Persist one review record into the project's committed tree.

    The record must name ``project`` and ``review_run_id`` — the run that
    produced the review. ``project`` may be supplied by the caller instead,
    which is how a promotion commits a record whose own body omits the project:
    the promoting run knows the project it is landing into, so the committed
    record takes that project rather than the write being refused. The review
    run id keys the committed file and supplies the
    dispatch and completion times through :func:`resolve_record_times`, so the
    record carries when the review ran rather than when its file was stored; a
    record carrying no review run id is refused rather than filed under the run
    it reviews, because two review runs of one reviewed run would otherwise name
    the same committed file. The times are resolved from the run records, the
    record's own carried stamps, or the dispatch instant the review run id
    encodes — in that order — and the stage used is recorded under
    ``times_source``. The store clock is never substituted for a stamp: a
    record whose times resolve from no source is committed with both stamps
    empty and ``times_source`` ``"unknown"``, marking the absence rather than
    defaulting it to the clock, because the record has no time to record and is
    kept rather than lost.

    The body must be a review: it names the plan or the run it reviews (which
    the ``plan_slug``/``reviewed_run_id`` checks above already require) and it
    carries review material — findings, scores, a rubric or a reviewed
    revision. A body carrying none of those is refused here, so the committed
    store holds records only.

    ``committed_root`` names the tree directly (a caller that already resolved
    it, or a test); omitted, it resolves through :func:`committed_review_root`
    against ``root``. A run review — one naming ``reviewed_run_id`` — is written
    under ``run/<reviewed-run-id>/``, whatever ``plan_slug`` its body also
    carries, because a run review records the plan its reviewed run was carried
    under and that plan is not the review's subject. A plan review is written
    under ``plan/<slug>/`` and is routed there only when the body names no
    reviewed run and carries an integer ``plan_version``. A disposition or
    answer recorded on any copy of this record — a staging sibling or the
    committed file already at the target path — is merged into the committed
    body, so a re-store of a round carries the answers rather than overwriting
    them. The write is atomic and every other body field is preserved.
    """
    project = str(record.get("project") or "").strip() or str(project or "").strip()
    if not project:
        raise ValueError("review record is missing project")
    review_run_id = str(record.get("review_run_id") or "").strip()
    if not review_run_id:
        raise ValueError(
            "a committed review record needs the review run id that names it; "
            "refusing to file it under the reviewed run"
        )
    if not _review.names_a_review_subject(record):
        raise ValueError(
            "review record names neither a reviewed run nor a plan, so it has "
            "no subject to be filed under"
        )
    reviewed_run_id = str(record.get("reviewed_run_id") or "").strip()
    if not _review.carries_review_material(record):
        raise ValueError(
            "review record carries no review material — findings, scores, a "
            "rubric or a reviewed revision — so it is not a review and the "
            "committed store holds records only"
        )
    if committed_root is None:
        committed_root = _review.committed_review_root(project, root=root)
    if committed_root is None:
        raise ValueError(f"no committed reviews tree resolves for project {project!r}")

    dispatched, completed, times_source = _review.resolve_record_times(
        project, record, root=root
    )

    stored = dict(record)
    stored["project"] = project
    stored[_review.DISPATCH_TIME_KEY] = dispatched
    stored[_review.COMPLETION_TIME_KEY] = completed
    stored[_review.TIMES_SOURCE_KEY] = times_source
    if not stored.get("timestamp"):
        stored["timestamp"] = datetime.now(UTC).isoformat()

    plan_slug = str(record.get("plan_slug") or "").strip()
    if reviewed_run_id:
        path = _review.review_path(
            project,
            reviewed_run_id,
            committed_root=committed_root,
            review_run_id=review_run_id,
            reviewed_head_sha=str(record.get(_review.REVIEWED_HEAD_KEY) or "") or None,
            rubric=_review._review_record_rubric(record),
        )
    else:
        from reckon.crew.plan_review import plan_review_path

        path = plan_review_path(
            project,
            plan_slug,
            int(record.get("plan_version") or 0),
            committed_root=committed_root,
            review_run_id=review_run_id,
        )

    # A disposition or an answer may have been recorded on a sibling of the file
    # this call commits, because its writer wrote back to whichever copy the
    # reader selected. Every copy of the record is read for those fields and
    # merged in, so a promotion carries them into the committed record rather
    # than leaving them where only the host store could see them. The committed
    # copy already at the target path is a source too: once an answer is written
    # there, a later round of the same record is re-stored with a body that
    # carries no answer, so reading the committed copy is what keeps the answer
    # from being overwritten. It is merged last, so an answer the committed
    # record already holds wins a tie against a staging sibling's copy of it.
    existing = _committed_record_at(path)
    stored.update(
        _merge_record_answers(
            [
                stored,
                *_staging_copies_of_record(project, stored),
                *([existing] if existing is not None else []),
            ]
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _review._write_record(path, stored)
    from reckon.crew.findings import note_record_written

    note_record_written(path, project=project, root=root)
    return path


def commit_record_write(project: str, path: str | Path | Iterable[str | Path]) -> bool:
    """Commit a write to a review record that lives in the committed tree.

    A disposition or an answer written to the committed record is a tracked
    file in the project's checkout, so the write is committed through the same
    helper a promotion commits its state writes with — one path, one commit —
    rather than left as a modified tracked file the next reader would trip on.
    A staging write is not tracked and is left uncommitted. A project with no
    committed tree, or a path outside it, is not committed here and returns
    ``False``.

    ``path`` names one file, as a disposition writes, or a set of files that
    share the one commit: a repair rewrites a committed record and preserves
    the as-delivered copy beside it, and the two belong to the same write.
    """
    committed = _review.committed_review_root(project)
    if committed is None:
        return False
    root = Path(committed).resolve()
    if isinstance(path, (str, Path)):
        paths = [Path(path)]
    else:
        paths = [Path(item) for item in path]
    inside: list[Path] = []
    for item in paths:
        try:
            item.resolve().relative_to(root)
        except (OSError, ValueError):
            return False
        inside.append(item)
    if not inside:
        return False
    from reckon import ledger

    ledger._commit_state_write(
        project,
        "fix(review): commit an answer on a committed review record",
        "Commit the disposition or answer written to a committed review record "
        "so it travels with the tree rather than sitting as a modified file.",
        inside,
        "review",
    )
    return True


# A review record can be met mid-write: a review worker composes its record
# itself, so a reader can arrive while the file is still being written. Skipping
# such a file is a reading a reader has to see rather than a silent absence -- a
# truncated record would otherwise read as "this run has no review" and send the
# reader to write one beside the record already there.
#
# The skips belong to the call that made them rather than to the module. A read
# inside an open collection region appends to that region's own list, and a read
# with no region open names nowhere, so a worker's read of a record never
# surfaces as a finding about a file a later sweep never touched, and a process
# that never collects grows no list at all.
_READ_FAILURES: ContextVar[list[dict[str, str]] | None] = ContextVar(
    "reckon_review_read_failures", default=None
)


@contextmanager
def collect_read_failures() -> Iterator[list[dict[str, str]]]:
    """Collect the record files a read inside this region could not read.

    A caller that publishes a fleet-wide reading derives inside this region and
    names every file it could not read in what it publishes. The list belongs to
    the region, so it starts empty for every caller and nothing read outside it
    reaches a later reading.
    """
    failures: list[dict[str, str]] = []
    token = _READ_FAILURES.set(failures)
    try:
        yield failures
    finally:
        _READ_FAILURES.reset(token)


def _note_skipped_record(
    path: Path, error: Exception, *, into: list[dict[str, str]] | None = None
) -> None:
    """Record one unreadable record file with the reader that met it.

    The entries go to the collector the reader named, or to the collection
    region it is reading inside. A reader in neither has nobody to tell and the
    miss is dropped rather than pooled for whoever reads next.
    """
    target = _READ_FAILURES.get() if into is None else into
    if target is None:
        return
    entry = {"path": str(path), "error": f"{type(error).__name__}: {error}"}
    for index, existing in enumerate(target):
        if existing["path"] == entry["path"]:
            target[index] = entry
            return
    target.append(entry)


def _note_malformed_record(
    path: Path | None,
    record: Mapping[str, Any] | None,
    *,
    skipped: list[dict[str, str]] | None = None,
) -> None:
    """Report a selected record carrying any claimless finding as malformed.

    A record is malformed when any entry in its ``findings`` list states no
    claim: such a finding cannot be answered back, so the repair reader drops it
    and the record's remaining findings repair as usual. The file is named to
    the caller's ``skipped`` list, or to the collection region a
    :func:`collect_read_failures` caller opened around the read, so a sweep that
    opens the region turns the malformation into an ``unreadable-review-record``
    obligation rather than the finding silently vanishing from repair. A read
    that names neither is nobody's finding and is dropped, the same rule
    :func:`_note_skipped_record` applies. The reason carries the count so a
    reader sees how much of the record was unanswerable.
    """
    if path is None or record is None:
        return
    claimless = _review.claimless_findings(record)
    if not claimless:
        return
    _note_skipped_record(
        path,
        ValueError(f"{len(claimless)} finding(s) in this record state no claim"),
        into=skipped,
    )


def _record_reads_complete(record: Mapping[str, Any]) -> bool:
    """Whether a stored record carries every measure its rubric demands.

    The completeness rule has one owner, the recovery reader's
    :func:`_review_is_complete`, and it is imported at call time because that
    module reads this store and a module-level edge would close a cycle.

    The same read-time total derivation :func:`read_review` applies is applied
    first, so a record whose six scores were stored without a total is judged
    complete exactly as the gate will later read it.
    """
    from reckon.crew.recovery_review_subject import _review_is_complete

    candidate = record
    if not _review._has_integer_total(candidate):
        candidate = _review._derive_landed_total(candidate)
    return _review_is_complete(candidate)


def _select_stored_record(
    paths: Iterable[Path],
    reviewed_head_sha: str | None,
    *,
    rubric: str | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Select one record from candidate files by the store's shared rule.

    Every candidate file is read once. A named head keeps only a record whose
    normalised head carries it — a stored record's head is compared to the named
    one as a prefix in either direction, so a short sha names its full record
    and a full sha names its short preservation copy.

    A record whose review run was discarded — asked of
    :func:`reckon.crew.promotion.discard_recorded`, the rule's one owner — is
    not selected while another record exists at that head: the discard says the
    fleet gave that review up, and a later review of the same revision is the
    evidence a reader means. A lone record is still read, so a head whose only
    review was discarded still describes that review rather than reporting that
    no review exists.

    Among the remaining records a complete one is preferred over an incomplete
    one, whichever is newer, and a parsed one over one that measured nothing: a
    re-review that finished its scoring, or a figure review whose images it
    actually read, is the evidence the gate needs, and the file order that names
    the review runs says nothing about whether a run measured anything. Where no
    such record sits at the head, the first head-matching record is returned, so
    an incomplete review is still read and the gate still refuses on it rather
    than reporting that no review exists. Without a named head the newest file
    is returned. A file that cannot be read or does not parse — a record a
    review worker is composing by hand can be met mid-write — is skipped and
    named to the caller, because a truncated file would otherwise read as "this
    run has no review". ``(None, None)`` means no readable record among the
    candidates; the caller decides whether that settles the search or falls
    through to another tree.
    """
    records: list[tuple[Path, Any, int]] = []
    for path in paths:
        try:
            record = _record_file(path)
            mtime_ns = _record_stat(path).st_mtime_ns
        except (OSError, ValueError) as error:
            _note_skipped_record(path, error, into=skipped)
            continue
        if not _review._review_rubric_matches(record, rubric):
            continue
        records.append((path, record, mtime_ns))
    if not records:
        return None, None
    if reviewed_head_sha is None:
        path, record, _mtime_ns = max(records, key=lambda item: item[2])
        return path, record
    named = reviewed_head_sha.strip().lower()
    matches: list[tuple[Path, Any]] = []
    for path, record, _mtime_ns in records:
        _, _, carried_head, stored_head = _review.carried_revision_pair(record)
        if not carried_head or not stored_head:
            continue
        actual = stored_head.lower()
        if actual.startswith(named) or named.startswith(actual):
            matches.append((path, record))
    if not matches:
        return None, None
    from reckon.crew import promotion

    undiscarded = [
        item
        for item in matches
        if not promotion.discard_recorded(
            str(item[1].get("review_run_id") or "").strip()
        )
    ]
    pool = undiscarded or matches

    def rank(item: tuple[Path, Any]) -> tuple[bool, bool]:
        _path, record = item
        return (
            _record_reads_complete(record),
            str(record.get("status") or "") == "parsed",
        )

    return max(pool, key=rank)


def _record_with_merged_answers(
    project: str,
    reviewed_run_id: str,
    selected: tuple[Path | None, dict[str, Any] | None],
    *,
    committed: Path | None,
    base_dir: str | Path | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Return the selected record carrying every copy's answers.

    A review worker writes its record by hand, as JSON, to whichever paths its
    dispatch grants — the plain staging file and the head-keyed sibling — so no
    writer in reckon's code runs on that path and the record it writes carries
    no disposition recorded on another copy. The invariant that holds whatever
    wrote the file is read-side: the selected record carries the
    ``dimension_dispositions``, ``responses`` and ``response_events`` held by
    every copy of the same review run, staging and committed, unioned by the
    rules :func:`_merge_record_answers` applies. The selection itself — which
    copy is chosen for a head — is unchanged; only the answer fields are merged
    onto the chosen record, and nothing is written. The committed record at the
    path this review run is filed under is merged last, so a committed answer
    wins a tie against a staging sibling's copy of it, the order a re-store
    merges its own merge sources in.

    A merged record carrying any claimless finding is reported as malformed to
    the caller's ``skipped`` list or the open collection region, so a reader
    that opened :func:`collect_read_failures` names the file rather than the
    malformation vanishing from repair.
    """
    path, record = selected
    if record is None:
        return path, record
    sources: list[Mapping[str, Any]] = [
        record,
        *_staging_copies_of_record(project, record, base_dir),
    ]
    review_run_id = str(record.get("review_run_id") or "").strip()
    if committed is not None and review_run_id and reviewed_run_id:
        committed_record = _committed_record_at(
            _review.review_path(
                project,
                reviewed_run_id,
                reviewed_head_sha=_review.carried_revision_pair(record)[3],
                rubric=_review._review_record_rubric(record),
                committed_root=committed,
                review_run_id=review_run_id,
            )
        )
        if committed_record is not None:
            sources.append(committed_record)
    merged = dict(record)
    merged.update(_merge_record_answers(sources))
    _note_malformed_record(path, merged, skipped=skipped)
    return path, merged


def stored_record(
    project: str,
    reviewed_run_id: str,
    *,
    base_dir: str | Path | None = None,
    reviewed_head_sha: str | None = None,
    rubric: str | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Return the file a stored record was read from and the record it holds.

    The selection rule is shared by every reader and by the writer that records
    a disposition beside the review store's own content: a named head is
    matched against the record's normalised ``reviewed_head_sha`` rather than
    trusted from its filename, which keeps older short-sha preservation copies
    readable, and without a named head the newest record is returned for
    compatibility with callers that have not yet learned to state the revision
    they need.

    The record returned is the selected file's own content with the answer
    fields unioned from every copy of the same review run — see
    :func:`_record_with_merged_answers` — and never the read-time annotation
    :func:`read_review` adds, so a caller that writes the record back persists
    the answers the reader was shown rather than a derived view. One exception is a record found away
    from the paths its own run id names: it carries :data:`MISFILED_KEY`, a fact
    about where the file sits rather than a derivation from other data, and a
    writer that writes the record back writes that fact where it took the
    record from. The path returned is the file it came from — the legacy path,
    one keyed by a revision, or a record filed under another run id — and it is
    what a writer needs to keep writing where the reader looked: a store can
    hold a legacy copy beside a revision-keyed record of the same head, and the
    head-first reader takes the first candidate carrying the head, so a rewrite
    that re-derived its target from the record's own fields could land beside
    the file the reader reads and leave that file unchanged.

    The project's committed reviews tree is read first — the durable committed
    store where a promoted run's record lives — and the host staging store is
    read only for a run not yet promoted. A record present in both trees is
    answered from the committed copy, which is the one that travels with the
    plan and the ledger; the staging copy is the same record before its
    promotion. A committed run directory holding no record falls through to the
    staging store, which is the state of a run whose record exists only where
    its live review wrote it.

    A record sitting at one of the run's own paths — the bare path or one keyed
    by a revision — settles where this run's review is filed, so the search for
    a record another writer filed under a different run id does not run: a
    named head that matched none of them is a fact about the revision, not
    about the filing, and the caller that wants the newest record asks again
    without a head. The committed tree answers for a run it carries a record
    for, and the staging store is consulted when it does not: a committed tree
    holding a record for this run at some head but none at the named one
    returns no record, and the staging store is then searched for one at that
    head before the tree reports the absence. Once the committed tree carries a
    record for the run and a staging file of its own exists, the committed tree
    settles and a store-wide search does not run. Only a run with none of its
    own files in either tree reaches the store-wide search.

    A record file that cannot be read or does not parse — a review worker
    composing its record by hand can be met mid-write — is skipped rather than
    raised: the reader answers from the records it could read, and every file
    it could not is appended to the caller's ``skipped`` list, or to the
    collection region a :func:`collect_read_failures` caller opened around the
    read, so a publisher can name it instead of the run reading as one that has
    no review at all. A read that names neither is nobody's finding and is
    dropped.
    """
    committed = _review.committed_review_root(project)
    committed_selected: tuple[Path | None, dict[str, Any] | None] | None = None
    if committed is not None:
        run_directory = committed / _review.COMMITTED_RUN_DIRNAME / reviewed_run_id
        committed_paths = (
            sorted(_store_paths(run_directory, "*.json"))
            if run_directory.is_dir()
            else []
        )
        if committed_paths:
            committed_selected = _select_stored_record(
                committed_paths,
                reviewed_head_sha,
                rubric=rubric,
                skipped=skipped,
            )
            # A committed record answers unless the committed tree holds records
            # for this run and none at the named head: a review of another head
            # is still reachable by that head, so the staging store is searched
            # for one rather than the committed tree returning an absence the
            # staging store could fill.
            if committed_selected[1] is not None or reviewed_head_sha is None:
                return _record_with_merged_answers(
                    project,
                    reviewed_run_id,
                    committed_selected,
                    committed=committed,
                    base_dir=base_dir,
                    skipped=skipped,
                )
    directory = _review.review_store_root(base_dir) / project
    candidates = [
        _review.review_path(project, reviewed_run_id, base_dir, rubric=rubric)
    ]
    if directory.is_dir():
        candidates.extend(_store_paths(directory, f"{reviewed_run_id}*.at-*.json"))
    existing = {path.resolve(): path for path in candidates if _record_exists(path)}
    if existing:
        selected = _select_stored_record(
            list(existing.values()),
            reviewed_head_sha,
            rubric=rubric,
            skipped=skipped,
        )
        # The run's own files hold no record at the named head. A record of the
        # same run filed under another reviewer's run id can still sit at that
        # head, so the store is asked for one before the absence is reported: a
        # run's own file settles where its review is filed only for the heads
        # that file carries. A lookup naming no head is not searched, because
        # the newest record of the run is what it asked for and one of the
        # run's own files is that record.
        #
        # The store is asked through the index, which names the file the search
        # would find without reading any of them; a run whose entries carry no
        # record at this head is answered from the run's own files rather than
        # by walking the store, which is what keeps a miss on a head mismatch as
        # cheap as it was.
        if (
            selected[1] is None
            and reviewed_head_sha is not None
            and _indexed_at_head(directory, reviewed_run_id, reviewed_head_sha)
        ):
            filed = _record_filed_elsewhere(
                directory,
                reviewed_run_id,
                reviewed_head_sha,
                rubric=rubric,
                skipped=skipped,
            )
            selected = filed if filed[1] is not None else selected
        return _record_with_merged_answers(
            project,
            reviewed_run_id,
            selected,
            committed=committed,
            base_dir=base_dir,
            skipped=skipped,
        )
    if committed_selected is not None:
        # The committed tree holds records for this run and no staging file of
        # its own exists, so the committed tree settles the search: a record
        # already committed to this run is not displaced by a store-wide search.
        return _record_with_merged_answers(
            project,
            reviewed_run_id,
            committed_selected,
            committed=committed,
            base_dir=base_dir,
            skipped=skipped,
        )
    # No file of this run's own exists, so the store's records are searched for
    # one whose content names the run it reviews — the shape a hand-written
    # record takes when its worker keys the file on its own run id instead of
    # the reviewed one.
    return _record_with_merged_answers(
        project,
        reviewed_run_id,
        _record_filed_elsewhere(
            directory,
            reviewed_run_id,
            reviewed_head_sha,
            rubric=rubric,
            skipped=skipped,
        ),
        committed=committed,
        base_dir=base_dir,
        skipped=skipped,
    )


def stored_records_for_run(
    project: str,
    reviewed_run_id: str,
    *,
    base_dir: str | Path | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    """Return every readable staging record the named reviewed run carries.

    A run is reviewed once per round, so its staging directory can hold one
    record per round — at different heads, written by different review runs,
    beside a legacy copy — and none of them displaces another: each is the
    run's review of one revision. A caller that keeps the run's reviews rather
    than reading the one a by-head selection returns (a promotion committing
    every round into the project's tree) reads them all through this lookup.

    Only the staging store is enumerated: the committed tree holds what a
    promotion has already landed, not the live rounds this serves. The store's
    own index answers, so the walk is one pass per project directory and per
    process, and a record filed under another name but naming this run is
    included — the same content-keyed rule the by-head readers apply. Records
    come back oldest first by mtime, so two runs of the same store read them in
    the same order. A file that cannot be read or does not parse is skipped and
    named to the caller's ``skipped`` list or collection region, the same rule
    the by-head readers apply.

    Each record carries the answer fields of every copy of the same review run
    — its own staging copy, its head-keyed siblings and the committed record
    under the project's reviews tree — merged through
    :func:`_record_with_merged_answers`, so a finding disposition recorded after
    a promotion is read by every caller of this lookup rather than only by the
    by-head readers. The records returned, and their order, are unchanged: the
    merge adds answers to a selected record and selects nothing else.
    """
    directory = _review.review_store_root(base_dir) / project
    if not directory.is_dir():
        return []
    committed = _review.committed_review_root(project)
    records: list[tuple[Path, dict[str, Any]]] = []
    seen: set[str] = set()
    entries = sorted(
        _store_index(directory).get(reviewed_run_id) or [],
        key=_recency,
    )
    for entry in entries:
        path = entry["path"]
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            record = _record_file(path)
        except (OSError, json.JSONDecodeError) as error:
            _note_skipped_record(path, error, into=skipped)
            continue
        if not isinstance(record, Mapping):
            continue
        if str(record.get("reviewed_run_id") or "").strip() != reviewed_run_id:
            continue
        _merged_path, merged = _record_with_merged_answers(
            project,
            reviewed_run_id,
            (path, record),
            committed=committed,
            base_dir=base_dir,
            skipped=skipped,
        )
        if merged is None:
            continue
        records.append((path, merged))
    return records


def newest_delivered_review(
    project: str,
    reviewed_run_id: str,
    *,
    base_dir: str | Path | None = None,
    rubric: str | None = None,
) -> dict[str, Any] | None:
    """The run's newest review record the staging store holds, or ``None``.

    A delivered review is filed into the staging store when its worker ends,
    and it stays there until a promotion commits it into the project's tree. A
    run re-reviewed after a repair therefore has one record per round, and the
    newest of them is the review its findings are about now. A reader that
    selects a record by a head, or by descent from a head the work has moved
    past, can land on an earlier round; this is the answer that says which
    round is current.

    Only the staging store is read: it is where a round is delivered and where
    a record awaits promotion, which is the state the caller is asking about.
    A run with no staging record yields ``None``, so a run whose reviews have
    all been promoted is never refused for lacking a newer one. ``rubric``
    keeps a figure round out of a text-review answer, the rule every reader in
    this family applies.

    The rounds are ordered by when each review ran rather than by the file's
    modification time: a disposition recorded on an earlier round rewrites that
    file, which would otherwise move it past the newer round and make the older
    record read as the newest.
    """
    records = stored_records_for_run(project, reviewed_run_id, base_dir=base_dir)
    newest: dict[str, Any] | None = None
    newest_key: tuple[datetime, int] | None = None
    for path, record in records:
        if not _review._review_rubric_matches(record, rubric):
            continue
        key = _delivery_order(record, path)
        if newest_key is None or key > newest_key:
            newest, newest_key = record, key
    return newest


def _delivery_order(
    record: Mapping[str, Any], path: str | Path
) -> tuple[datetime, int]:
    """When a review round ran, for ordering one round against another.

    The record's own dispatch stamp is the first answer, and the dispatch
    instant its review run id encodes the second — a record delivered straight
    into the staging store carries the id but no stamp. The file's modification
    time is only the tie-break, and the whole answer for a record naming
    neither, because a rewrite in place moves it and it says when the file was
    last written rather than when the review ran.
    """
    stamp = str(record.get(_review.DISPATCH_TIME_KEY) or "").strip()
    if not stamp:
        stamp = _review.run_id_dispatch_time(str(record.get("review_run_id") or ""))
    moment: datetime | None = None
    if stamp:
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            moment = None
    try:
        modified = Path(path).stat().st_mtime_ns
    except OSError:
        modified = 0
    if moment is None:
        return datetime.min.replace(tzinfo=UTC), modified
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment, modified


def _record_filed_elsewhere(
    directory: Path,
    reviewed_run_id: str,
    reviewed_head_sha: str | None,
    *,
    rubric: str | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Return the run's review sitting at another record's path, flagged.

    A review worker writes its record by hand and may key the file on its own
    run id, so a record whose content names the run it reviews can sit where no
    reader keyed on the reviewed run looks. The store's records are matched on
    what each one says about itself — its ``reviewed_run_id``, and the head it
    read when one is named — and the newest match is returned with its real path
    and :data:`MISFILED_KEY` set. A record the store filed as an incomplete
    partial is skipped, because the store keeps those outside current-review
    selection by design and a content match must not undo that.

    The match is answered from the index below, which reads the store once per
    process rather than once per lookup: a store of thousands of records makes a
    whole-store pass per miss the dominant cost of a per-turn reader. The file
    the index selects is re-read from disk, so the answer is the record's
    current content — a rewrite in place, such as a recorded disposition, is
    returned rather than the content the index was built from.
    """
    if not directory.is_dir():
        return None, None
    named = None if reviewed_head_sha is None else reviewed_head_sha.strip().lower()
    candidates = _store_index(directory).get(reviewed_run_id) or []
    found = _filed_match(
        candidates, reviewed_run_id, named, rubric=rubric, skipped=skipped
    )
    if found is not None:
        return found
    # The index answers from the facts it read when it last walked the store,
    # and a record rewritten in place leaves the store's entries where they
    # were: revalidate this run's own entries from their files' stat identities
    # first, then -- for a run the index holds no entry for at all, the shape a
    # record rewritten to name another run leaves -- the whole store, once.
    found = _filed_match(
        _revalidated_entries(candidates),
        reviewed_run_id,
        named,
        rubric=rubric,
        skipped=skipped,
    )
    if found is not None:
        return found
    refreshed = _filed_match(
        _refresh_store_entries(directory).get(reviewed_run_id) or [],
        reviewed_run_id,
        named,
        rubric=rubric,
        skipped=skipped,
    )
    return refreshed if refreshed is not None else (None, None)


def _indexed_at_head(
    directory: Path, reviewed_run_id: str, reviewed_head_sha: str
) -> bool:
    """Whether the index holds any record of this run at the named head.

    Answered from the entries this run has, revalidated from their files' own
    stat identities so a record rewritten in place is seen, and from no other
    file: a store of thousands of records is not walked to answer a question
    about one run.
    """
    named = reviewed_head_sha.strip().lower()
    entries = _store_index(directory).get(reviewed_run_id) or []
    return any(
        _indexed_head_matches(entry, named) for entry in _revalidated_entries(entries)
    )


def _filed_match(
    candidates: Iterable[dict[str, Any]],
    reviewed_run_id: str,
    named: str | None,
    *,
    rubric: str | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    """Return the newest candidate whose current content answers this lookup.

    The file each candidate names is read again, so the answer is the record's
    current content rather than the facts the index holds for it, and its
    content is re-checked against what the index selected on: a record rewritten
    since the index was read must not answer a request it no longer matches. The
    newest candidate that does match is returned carrying
    :data:`MISFILED_KEY`, because a file sitting at another run's path is what
    this lookup exists to report.

    A record whose review run was discarded loses to one whose run was kept, the
    same rule the run's own files are selected under, so a discarded review's
    record filed under another run id cannot shadow a promoted one. With no
    undiscarded match a discarded record is still returned: the lookup answers
    what the store holds rather than reporting that it holds nothing.
    """
    from reckon.crew import promotion

    matches: list[tuple[Path, dict[str, Any]]] = []
    for entry in sorted(candidates, key=_recency, reverse=True):
        if not _indexed_head_matches(entry, named):
            continue
        path = entry["path"]
        try:
            record = _record_file(path)
        except (OSError, json.JSONDecodeError) as error:
            _note_skipped_record(path, error, into=skipped)
            continue
        if not isinstance(record, Mapping):
            continue
        # The file's current content is re-checked against what the index
        # selected on: one rewritten since the index was built must not answer a
        # request its content no longer matches.
        if str(record.get("reviewed_run_id") or "") != reviewed_run_id:
            continue
        if not _review._review_rubric_matches(record, rubric):
            continue
        if named is not None:
            _, _, carried_head, stored_head = _review.carried_revision_pair(record)
            if not carried_head or not stored_head:
                continue
            actual = stored_head.lower()
            if not (actual.startswith(named) or named.startswith(actual)):
                continue
        filed = dict(record)
        filed[_review.MISFILED_KEY] = True
        matches.append((path, filed))
    if not matches:
        return None
    undiscarded = [
        item
        for item in matches
        if not promotion.discard_recorded(
            str(item[1].get("review_run_id") or "").strip()
        )
    ]
    return (undiscarded or matches)[0]


# ── The store index ─────────────────────────────────────────────────────────
# Choosing the file to answer a misfiled lookup from needs two facts per record
# file: the run id it names and the head it read. Reading every file to learn
# them costs a whole-store pass, and a session whose live pointers carry no
# record pays one pass per pointer. The index below holds those two facts — not
# the record content — and is built at most once per store directory and per
# process, then reused while the directory's stat identity is unchanged, which
# is exactly when its set of entries can have moved. The last element says
# whether the whole-store revalidation is settled for the entries held: true
# for an index adopted from a persisted document whose store has not moved,
# false for one this process built or updated itself.

_STORE_INDEXES: dict[
    Path,
    tuple[
        tuple[int, ...],
        dict[str, list[dict[str, Any]]],
        dict[str, list[dict[str, Any]]],
        list[dict[str, Any]],
        bool,
    ],
] = {}

# The run maps one store document's entries were last turned into, held per
# process against the document's version: a version moves on every indexed write
# and on every reconciled foreign write, so an entry keyed by it is current
# exactly while no write has been made.
_DOCUMENT_MAPS: dict[
    Path,
    tuple[
        tuple[str, int],
        dict[str, list[dict[str, Any]]],
        dict[str, list[dict[str, Any]]],
    ],
] = {}


def node_local_cache_dir(project: str) -> Path:
    """Return the node-local directory a project's disposable caches live in.

    A cache every process on the node can share has to sit where this node's
    processes all see it and no other node does, which is the runtime directory
    when the session has one and a per-uid directory under ``/tmp`` when it does
    not. Nothing is created here: a reader that finds no cache directory has no
    cache, and a writer materialises the path it writes into.
    """
    runtime = str(os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    base = (
        Path(runtime) / "reckon" if runtime else Path("/tmp") / f"{os.getuid()}-reckon"  # noqa: S108 - node-local
    )
    return base / project


# A persisted cache is meaningful only to the code that would read it: a build
# written before the reader changed can hold facts a later reader would derive
# differently, and the selection rules it was written under can differ too. The
# stamp is the content identity of the modules those facts and rules are
# defined in, so it moves for an uncommitted edit as readily as for a commit.
# The process-wide code stamp is not used here: it costs about eleven seconds on
# a cold process, reading a whole package and asking git about the tree, which
# is the cost a persisted cache exists to remove.
#
# Every cache on the node files itself the same way, so the stamp, the path and
# the document read and write live here once: a reader that spells them out for
# itself drifts from this one, and the drift is silent until a document written
# by one is trusted by the other.
_CACHE_CODE_STAMPS: dict[tuple[str, ...], str] = {}


def cache_code_stamp(sources: Iterable[Path]) -> str:
    """The content digest a persisted cache is filed under and checked against.

    Each kind of cache names the modules whose content fixes what its entries
    mean. Empty when a source cannot be read, which disables the persisted
    cache for this process rather than filing one under a stamp nothing can be
    compared with.
    """
    names = tuple(str(source) for source in sources)
    held = _CACHE_CODE_STAMPS.get(names)
    if held is not None:
        return held
    digest = hashlib.sha256()
    try:
        for source in names:
            digest.update(Path(source).read_bytes())
    except OSError:
        return ""
    stamp = digest.hexdigest()
    _CACHE_CODE_STAMPS[names] = stamp
    return stamp


def forget_cache_code_stamps() -> None:
    """Drop the stamps this process holds, as a newly started process has none.

    Both memos go: the shared one every cache files under, and this store's own
    held stamp, which is consulted before the sources are read. Clearing only
    the first would leave a caller that pinned the store's stamp — or that
    edited a source under test — still answering with the value it held.
    """
    _CACHE_CODE_STAMPS.clear()
    _INDEX_CODE_STAMP.clear()


def cache_document_path(project: str, kind: str, key: str) -> Path:
    """The node-local file one cache document is persisted in.

    ``key`` names the thing the document is about — a store directory, a pair
    of directories and a checkout — rather than the project alone, because a
    worktree's or a scratch tree's store shares its project name with the main
    checkout and the two must never answer from one another's entries.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return node_local_cache_dir(project) / f"{kind}-{digest}.json"


def read_cache_document(path: Path) -> dict[str, Any] | None:
    """One cache document as another process left it, or ``None`` when unusable.

    A missing, half-written or malformed document is not a cache, so the caller
    builds from scratch; every caller makes the same reading rather than each
    wrapping ``json.loads`` in its own except clause.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def write_cache_document(path: Path, document: Mapping[str, Any]) -> None:
    """Write one cache document where this node's processes can start from it.

    A disposable cache: an unwritable directory, a full disk or a reader that
    arrives mid-write costs a later process a from-scratch build rather than
    the cache, so a write that fails is not an error the caller reports.
    """
    try:
        from reckon._store import write_json_atomically

        write_json_atomically(
            path, document, fsync=False, mode=None, create_parents=True
        )
    except OSError:
        return


def _index_sources() -> tuple[Path, ...]:
    """The modules whose content fixes what this store's index means."""
    return (Path(__file__).resolve(), Path(_review.__file__).resolve())


# The stamp this store's index is filed under, held per process. A held value
# is consulted before the sources are read, so a caller can pin one — the state
# a document written by other code presents — without a source read.
_INDEX_CODE_STAMP: list[str] = []


def _index_code_stamp() -> str:
    """The stamp this store's persisted index is filed under."""
    if not _INDEX_CODE_STAMP:
        stamp = cache_code_stamp(_index_sources())
        if not stamp:
            return ""
        _INDEX_CODE_STAMP.append(stamp)
    return _INDEX_CODE_STAMP[0]


_RESOLVED_DIRECTORIES: dict[str, str] = {}


def _resolved(directory: Path) -> str:
    """The store directory's resolved absolute path, or "" when unresolvable.

    Held per process: this is the key every read compares a document against,
    so resolving the same store directory on each lookup would spend a path
    walk per read to answer a value that cannot change while the process runs.
    """
    key = str(directory)
    held = _RESOLVED_DIRECTORIES.get(key)
    if held is not None:
        return held
    try:
        resolved = str(directory.resolve())
    except OSError:
        resolved = ""
    _RESOLVED_DIRECTORIES[key] = resolved
    return resolved


def _index_cache_path(directory: Path) -> Path:
    """The node-local file one store directory's index is persisted in."""
    return cache_document_path(directory.name, "store-index", _resolved(directory))


def _stat_identity(info: os.stat_result) -> tuple[int, int, int]:
    """The identity of one record file: its inode, its size and its mtime."""
    return (info.st_ino, info.st_size, info.st_mtime_ns)


def _store_directory_identity(directory: Path) -> tuple[int, ...] | None:
    """Return the stat identity of ``directory``, or ``None`` when unreadable.

    Adding, removing or atomically replacing a file moves this identity, so it
    is what tells a rebuilt index from a current one. A rewrite that leaves the
    directory's entries alone does not move it, which is why the file the index
    selects is read again rather than served from the index.
    """
    try:
        info = directory.stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _index_entry(path: Path, info: os.stat_result) -> dict[str, Any] | None:
    """Read one record file once and return the facts the index selects on.

    The facts are the file's name, path, modification time and stat identity,
    the run its content reviews, the review run that produced it, and the head
    it carries. Never the whole record: the file the index selects is re-read
    from disk so a rewrite in place is answered current. A file that does not
    parse, does not hold a mapping, or is an incomplete partial -- which the
    store keeps outside current-review selection by design -- yields ``None``
    and no entry.
    """
    if _review._INCOMPLETE_RECORD_MARK in path.name or path.name.startswith("."):
        return None
    try:
        record = _record_file(path)
    except (OSError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    carried_head, head_sha = _review._first_carried_revision(
        record, _review.HEAD_REVISION_FIELDS
    )
    return {
        "name": path.name,
        "path": path,
        "mtime_ns": info.st_mtime_ns,
        "identity": _stat_identity(info),
        "reviewed_run_id": str(record.get("reviewed_run_id") or ""),
        "review_run_id": str(record.get("review_run_id") or "").strip(),
        "head_carried": bool(carried_head),
        "head_sha": head_sha,
    }


def _recency(entry: Mapping[str, Any]) -> tuple[int, int]:
    """An entry's age, newest largest.

    An entry the store's index holds knows the order its record was written in,
    which is the age a reader selecting "the newest" wants; an entry read from
    a file's stat carries that file's modification time instead.
    """
    return (int(entry.get("entry_version") or 0), int(entry.get("mtime_ns") or 0))


def _index_maps(
    entries: Iterable[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Key one entry per file by the run it reviews and by its reviewer.

    One scan serves both lookups: the misfiled lookup answers for the run a
    record's content reviews, and the delivered-plan-review lookup answers for
    the review run that produced the record. Both maps hold the same entry
    object, so whichever lookup reads one reads one set of facts.
    """
    reviewed: dict[str, list[dict[str, Any]]] = {}
    review_runs: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        reviewed.setdefault(entry["reviewed_run_id"], []).append(entry)
        review_run = entry["review_run_id"]
        if review_run:
            review_runs.setdefault(review_run, []).append(entry)
    return reviewed, review_runs


def _index_files(
    reviewed: Mapping[str, list[dict[str, Any]]],
    review_runs: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Recover the per-file entries the two keyed maps were built from."""
    files: dict[str, dict[str, Any]] = {}
    for group in (*reviewed.values(), *review_runs.values()):
        for entry in group:
            files.setdefault(entry["name"], entry)
    return list(files.values())


def _build_store_entries(directory: Path) -> list[dict[str, Any]]:
    """Read each record file in the store once, in name order.

    A file the index cannot build an entry from answers no lookup, so it is
    left out rather than named.
    """
    entries: list[dict[str, Any]] = []
    listing = _tick_store(directory)
    for path in sorted(_listed_paths(directory, listing, "*.json")):
        try:
            info = _listed_stat(listing, path)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        entry = _index_entry(path, info)
        if entry is not None:
            entries.append(entry)
    return entries


def _build_store_indexes(
    directory: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Read each record file once, keyed by the run it reviews and by its reviewer.

    The from-scratch build: every file the store holds is parsed. A process
    that finds a persisted index for this store directory starts from that
    instead and parses only what has changed since, which is what
    :func:`_update_store_entries` does.
    """
    return _index_maps(_build_store_entries(directory))


def _update_store_entries(
    directory: Path, previous: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Update an index from one listing, parsing only the files that changed.

    One directory listing names the files the store holds now. A file is parsed
    only when its name is new to the index, or when its stat identity -- its
    inode, its size and its modification time -- differs from the entry the
    index holds for it; every other file keeps the facts already read for it.
    An entry whose file the listing no longer names is dropped, so a deleted
    record stops answering, and a file that became unreadable drops its entry
    the same way.
    """
    held: dict[str, dict[str, Any]] = {entry["name"]: entry for entry in previous}
    kept: dict[str, dict[str, Any]] = {}
    # One listing answers both the names and their stats, so the store is
    # selected once per refresh whether or not a tick is sharing it.
    listing = _tick_store(directory)
    try:
        if listing is None:
            candidates = sorted(
                path
                for path in directory.glob("*.json")
                if not path.name.startswith(".")
            )
        else:
            candidates = sorted(
                path
                for path in listing
                if fnmatchcase(path.name, "*.json") and not path.name.startswith(".")
            )
    except OSError:
        candidates = []
    for path in candidates:
        try:
            info = path.stat() if listing is None else listing[path]
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        prior = held.get(path.name)
        if prior is not None and prior["identity"] == _stat_identity(info):
            prior["path"] = path
            prior["mtime_ns"] = info.st_mtime_ns
            kept[path.name] = prior
            continue
        entry = _index_entry(path, info)
        if entry is not None:
            kept[path.name] = entry
    return list(kept.values())


def _persisted_index_document(
    directory: Path,
    entries: Iterable[dict[str, Any]],
    identity: tuple[int, ...] | None,
) -> dict[str, Any]:
    """The document one store directory's index is persisted as.

    Each entry carries the stat identity a later reader compares against the
    file it finds, so that reader can tell a file it may reuse from one it must
    parse, and the facts both lookups select on. The file's path is not stored:
    the store directory is in the document, and a name inside it resolves
    there. The directory's own identity stored beside them is the one taken
    *before* the listing those entries came from, never a later stat: a record
    filed between the listing and this write is absent from the entries, and a
    stored identity that covered it would let a cold reader adopt an index that
    does not hold the record at all. An identity no reader can compare is
    stored as none, which makes the next reader list the store.
    """
    return {
        "code_stamp": _index_code_stamp(),
        "store": _resolved(directory),
        "directory_identity": list(identity or ()),
        "entries": [
            {
                "name": entry["name"],
                "identity": list(entry["identity"]),
                "mtime_ns": entry["mtime_ns"],
                "reviewed_run_id": entry["reviewed_run_id"],
                "review_run_id": entry["review_run_id"],
                "head_carried": bool(entry["head_carried"]),
                "head_sha": entry["head_sha"],
            }
            for entry in entries
        ],
    }


def _persist_store_entries(
    directory: Path,
    entries: list[dict[str, Any]],
    identity: tuple[int, ...] | None,
) -> None:
    """Write the store's index where this node's processes can start from it.

    ``identity`` is the store directory's identity as it stood before the
    listing ``entries`` were read from. A disposable cache: an unwritable
    directory, a full disk or a reader that arrives mid-write costs a later
    process a from-scratch build rather than the cache, so a write that fails
    is not an error a lookup reports.
    """
    if not _resolved(directory):
        return
    write_cache_document(
        _index_cache_path(directory),
        _persisted_index_document(directory, entries, identity),
    )


def _entry_from_persisted(directory: Path, raw: Any) -> dict[str, Any] | None:
    """One persisted entry, bound to the file its name resolves to now."""
    if not isinstance(raw, Mapping):
        return None
    name = str(raw.get("name") or "")
    identity = raw.get("identity")
    if not name or not isinstance(identity, list) or len(identity) != 3:
        return None
    try:
        stat_identity = (int(identity[0]), int(identity[1]), int(identity[2]))
        mtime_ns = int(raw.get("mtime_ns") or 0)
    except (TypeError, ValueError):
        return None
    return {
        "name": name,
        "path": directory / name,
        "mtime_ns": mtime_ns,
        "identity": stat_identity,
        "reviewed_run_id": str(raw.get("reviewed_run_id") or ""),
        "review_run_id": str(raw.get("review_run_id") or ""),
        "head_carried": bool(raw.get("head_carried")),
        "head_sha": raw.get("head_sha"),
    }


def _load_persisted_entries(
    directory: Path,
) -> tuple[list[dict[str, Any]], tuple[int, ...] | None] | None:
    """The store's index as another process left it, or ``None`` when unusable.

    An index is usable only when this checkout's reader wrote it for this store
    directory: a document stamped with other code's identity, or naming another
    store, is ignored rather than trusted, and the caller builds from scratch.
    A missing, half-written or malformed document gets the same answer, because
    a cache that cannot be read is not a cache. The directory's identity at the
    moment the document's entries were read comes back with them, so the caller
    can tell a store whose set of files has not moved since from one that has;
    a document written before that identity was recorded carries none, and the
    caller then treats it as one whose store it must look at.
    """
    store = _resolved(directory)
    if not store:
        return None
    document = read_cache_document(_index_cache_path(directory))
    if document is None:
        return None
    if str(document.get("code_stamp") or "") != _index_code_stamp():
        return None
    if str(document.get("store") or "") != store:
        return None
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        return None
    raw_identity = document.get("directory_identity")
    recorded: tuple[int, ...] | None = None
    if isinstance(raw_identity, list) and raw_identity:
        try:
            recorded = tuple(int(item) for item in raw_identity)
        except (TypeError, ValueError):
            return None
    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        entry = _entry_from_persisted(directory, raw)
        if entry is None:
            return None
        entries.append(entry)
    return entries, recorded


def _store_indexes(
    directory: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Return the store's two indexes, keyed by reviewed run and by reviewer.

    A staging store answers from the index document its writers maintain beside
    the records: every entry already carries the run it reviews, the review run
    that produced it and the head it holds, so the maps are built from the
    document alone -- no record is stat'ed, and the store is not listed.

    A directory that keeps no document -- the committed reviews tree, which is
    tracked by the project's checkout, or a store the writer index does not own
    -- answers through the per-process cache below, which is keyed on the
    directory's stat identity.
    """
    document = store_index_document(directory)
    if document is not None:
        return _document_index_maps(directory, document)
    return _full_store_indexes(directory)


def _full_store_indexes(directory: Path):
    """The established cached reader for history and projection recovery."""
    tick_indexes = _TICK_REVIEW_INDEXES.get()
    if tick_indexes is not None:
        if directory not in tick_indexes:
            tick_indexes[directory] = _build_store_indexes(directory)
        return tick_indexes[directory]
    identity = _store_directory_identity(directory)
    if identity is None:
        _STORE_INDEXES.pop(directory, None)
        return {}, {}
    cached = _STORE_INDEXES.get(directory)
    if cached is not None and cached[0] == identity:
        return cached[1], cached[2]
    settled_identity: tuple[int, ...] | None = None
    if cached is not None:
        entries: list[dict[str, Any]] | None = cached[3]
    else:
        loaded = _load_persisted_entries(directory)
        entries, settled_identity = loaded if loaded is not None else (None, None)
    if entries is None:
        reviewed, review_runs = _build_store_indexes(directory)
        entries = _index_files(reviewed, review_runs)
    elif settled_identity == identity:
        reviewed, review_runs = _index_maps(entries)
        _STORE_INDEXES[directory] = (
            identity,
            reviewed,
            review_runs,
            entries,
            True,
        )
        return reviewed, review_runs
    else:
        entries = _update_store_entries(directory, entries)
        reviewed, review_runs = _index_maps(entries)
    _STORE_INDEXES[directory] = (identity, reviewed, review_runs, entries, False)
    _persist_store_entries(directory, entries, identity)
    return reviewed, review_runs


# A record rewritten in place -- a disposition recorded on a stored review, a
# record re-filed under another run id -- keeps its name, its inode and the
# store's entries, so the directory's stat identity does not move and the index
# above would go on answering from the facts it first read. The two refreshes
# below key validity on each file's own stat identity instead: one for the files
# a lookup already holds entries for, which is a handful of stats, and one for
# the whole store for the miss no entry explains.


def _revalidated_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``entries`` with the ones whose file moved re-read from disk.

    Each file is stat'ed once and compared against the identity the index holds
    for it, so a record rewritten in place is re-read and a record whose file
    has gone stops answering. A file that no longer parses yields no entry,
    the same rule the build and update passes apply.
    """
    refreshed: list[dict[str, Any]] = []
    listings: dict[Path, dict[Path, os.stat_result] | None] = {}
    for entry in entries:
        if entry.get("indexed"):
            # An entry the store's own index holds is already current: a write
            # by a known writer advanced the version, and a foreign replacement
            # moved the directory identity the document is probed against.
            refreshed.append(entry)
            continue
        path = entry["path"]
        if path.parent not in listings:
            listings[path.parent] = _tick_store(path.parent)
        try:
            info = _listed_stat(listings[path.parent], path)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if entry["identity"] == _stat_identity(info):
            refreshed.append(entry)
            continue
        reread = _index_entry(path, info)
        if reread is not None:
            refreshed.append(reread)
    return refreshed


def _refresh_store_entries(
    directory: Path,
) -> dict[str, list[dict[str, Any]]]:
    """Revalidate every entry from its file's identity, once per process.

    This is the pass a store of thousands of records cannot afford per lookup:
    it stats every record file, and on a store of several thousand records that
    costs seconds. It is therefore paid at most once per store directory and per
    process, and only for a lookup no entry the index holds can explain -- a
    record rewritten in place to name a run the index has no entry for at all.
    Every later miss answers from the refreshed index. An index adopted from a
    persisted document whose store has not moved holds that pass as already
    paid: its entries were read from a store no writer has changed since, and
    the pass would re-derive them at a cost that grows with the store.

    The identity persisted with the refreshed entries is read before the
    listing that produced them, never the one the cache held and never a later
    stat, so a record filed between that listing and this write is missing from
    the entries and the next reader lists the store rather than adopting them.
    """
    if _TICK_REVIEW_STORES.get() is not None:
        return _store_index(directory)
    cached = _STORE_INDEXES.get(directory)
    if cached is None:
        return _store_index(directory)
    _, reviewed, review_runs, entries, refreshed = cached
    if refreshed:
        return reviewed
    identity = _store_directory_identity(directory)
    entries = _update_store_entries(directory, entries)
    reviewed, review_runs = _index_maps(entries)
    _STORE_INDEXES[directory] = (identity or (), reviewed, review_runs, entries, True)
    _persist_store_entries(directory, entries, identity)
    return reviewed


def _store_index(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the reviewed-run index, loaded and updated rather than rebuilt.

    A process holding no index for the store directory starts from the one
    another process on this node persisted for it, and a move of the directory's
    stat identity is answered by updating that index from one listing of the
    store, so only a file new to the index or changed since it was read costs a
    parse. A document that is missing, unreadable or stamped by other code is
    not usable, and only then is the index is built from scratch. The sequence is
    :func:`_store_indexes`, which owns it for both indexes.
    """
    return _store_indexes(directory)[0]


# ── The writer-maintained store index ───────────────────────────────────────
# The cache above is keyed on the store directory's stat identity, so a record
# written for any run moves it and the next read lists the store and stats every
# file it names. On a store of thousands of records that is seconds of GPFS per
# refresh, and it is paid once per duty item.
#
# This index replaces that freshness key with the writers themselves. One
# document lives inside the store directory, beside the records and on the
# shared home, so a writer on any node advances a version every node reads. Per
# record it holds the inode and SHA-256 of the bytes written; per run a digest over that
# run's (name, hash) pairs; per store a monotonic version, a bounded change log
# of (version, run id) and the directory identity at the indexed write.
#
# A reader consults the version and the run digest. It never lists the store and
# never stats a record: an unchanged run costs one comparison. A record written
# outside the writer path is found by listing names and inodes -- never by stat-ing
# the records the index already holds -- and a missing or foreign-stamped
# document is rebuilt from one listing and one hash of each record.

_INDEX_DIRNAME = ".review-index"
_INDEX_NAME = "index.json"
_INDEX_LOCK_NAME = "lock"
_INDEX_LOG_LIMIT = 4096


def _index_document_path(directory: Path) -> Path:
    """The index document that belongs to one store directory.

    It lives in a subdirectory of the store rather than beside the records for
    one reason: the document is rewritten on every indexed write, and a write
    beside the records would move the store directory's own mtime after the
    writer had already recorded it, so every read would find the store moved
    and none could settle.
    """
    return directory / _INDEX_DIRNAME / _INDEX_NAME


def _index_holding(directory: Path) -> Path | None:
    """The store's index subdirectory, created when the store can hold one."""
    holding = directory / _INDEX_DIRNAME
    try:
        holding.mkdir(exist_ok=True)
    except OSError:
        return None
    return holding


@contextmanager
def _index_lock(directory: Path, *, wait: bool = True) -> Iterator[bool | None]:
    """Hold the store's index lock, or report that it could not be taken.

    The lock file sits beside the document so a writer on any node serialises
    against every other node's writer. A directory that cannot hold the lock
    file, or a filesystem without advisory locks, yields ``False`` and the
    caller writes the document unlocked.

    ``wait`` false is for a reader: a lock another process holds yields
    ``None`` at once rather than blocking, and the reader serves the document
    as last persisted. A producer, station or follower thread reading the store
    must never wait on another process's index write.
    """
    import fcntl

    holding = _index_holding(directory)
    if holding is None:
        yield False
        return
    try:
        handle = (holding / _INDEX_LOCK_NAME).open("a")
    except OSError:
        yield False
        return
    with handle:
        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            yield None
            return
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_digest(entries: Mapping[str, Any], run_id: str) -> str:
    """The digest over one run's (name, hash) pairs."""
    pairs = sorted(
        (name, str(entry.get("sha256") or ""))
        for name, entry in entries.items()
        if str(entry.get("reviewed_run_id") or "") == run_id
    )
    return _sha256_hex(json.dumps(pairs).encode())


def _document_is_usable(directory: Path, doc: Any) -> bool:
    if not isinstance(doc, Mapping):
        return False
    if str(doc.get("code_stamp") or "") != _index_code_stamp():
        return False
    if str(doc.get("store") or "") != _resolved(directory):
        return False
    entries = doc.get("entries")
    if not isinstance(entries, dict):
        return False
    return isinstance(doc.get("runs"), dict)


# The parsed document this process last read from each store, keyed on the
# document file's inode, size and modification time. Every writer replaces the
# document under a new inode, so an unchanged key means unchanged content and
# a repeat read costs one stat rather than a parse of megabytes. An entry is
# dropped as this process persists a document, and a writer that mutates one
# takes its own copy rather than the cached mapping.
_PARSED_INDEX_DOCUMENTS: dict[
    str, tuple[tuple[int, int, int], dict[str, Any] | None]
] = {}


def _forget_parsed_index_document(directory: Path) -> None:
    """Drop this process's parsed copy, so the next read takes the file."""
    _PARSED_INDEX_DOCUMENTS.pop(str(directory), None)


def _fresh_index_document(directory: Path) -> dict[str, Any] | None:
    """Read and parse the document afresh, neither consulting nor filling the cache.

    A writer mutates the document it reads, so it takes this private copy: the
    cached mapping is what other readers in this process are already holding.
    """
    try:
        raw = _index_document_path(directory).read_bytes()
    except OSError:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    return doc if _document_is_usable(directory, doc) else None


def _read_index_document(directory: Path) -> dict[str, Any] | None:
    """The store's index document, parsed once per unchanged file.

    A read costs one stat while the document is unchanged: the parsed mapping
    is kept keyed on the file's inode, size and modification time, and every
    write replaces the file under a new inode. A changed or unusable document
    -- another checkout's, another store's, half written or not JSON -- is
    parsed again and answers ``None`` exactly as a missing one does, because an
    index that cannot be read is not an index.
    """
    path = _index_document_path(directory)
    try:
        info = path.stat()
    except OSError:
        _forget_parsed_index_document(directory)
        return None
    key = (info.st_ino, info.st_size, info.st_mtime_ns)
    cached = _PARSED_INDEX_DOCUMENTS.get(str(directory))
    if cached is not None and cached[0] == key:
        # The stamp a document is filed under is process state, not file
        # content, so a held mapping is re-checked rather than trusted: an
        # unusable one answers nothing and the caller rebuilds.
        return cached[1] if _document_is_usable(directory, cached[1]) else None
    document = _fresh_index_document(directory)
    _PARSED_INDEX_DOCUMENTS[str(directory)] = (key, document)
    return document


def _persist_index_document(directory: Path, doc: Mapping[str, Any]) -> None:
    """Write the store's index document atomically in the store's index dir."""
    _forget_parsed_index_document(directory)
    try:
        from reckon._store import write_json_atomically

        write_json_atomically(
            _index_document_path(directory),
            dict(doc),
            indent=None,
            sort_keys=True,
            mode=None,
            fsync=False,
            create_parents=True,
        )
    except OSError:
        return


def _entry_facts(name: str, record: Mapping[str, Any], data: bytes) -> dict[str, Any]:
    """The facts one record's entry carries, all of them derivable without a stat."""
    carried_head, head_sha = _review._first_carried_revision(
        record, _review.HEAD_REVISION_FIELDS
    )
    return {
        "name": name,
        "reviewed_run_id": str(record.get("reviewed_run_id") or ""),
        "review_run_id": str(record.get("review_run_id") or "").strip(),
        "head_carried": bool(carried_head),
        "head_sha": head_sha,
        "sha256": _sha256_hex(data),
        "size": len(data),
    }


def _entries_from_listing(directory: Path) -> dict[str, Any]:
    """Build entries from directory-entry inodes and one read of each record."""
    entries: dict[str, Any] = {}
    try:
        with os.scandir(directory) as scanned:
            names = sorted(
                (entry.name, entry.inode())
                for entry in scanned
                if entry.name.endswith(".json") and not entry.name.startswith(".")
            )
    except OSError:
        return entries
    for name, inode in names:
        if _review._INCOMPLETE_RECORD_MARK in name:
            continue
        path = directory / name
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        try:
            record = json.loads(data)
        except ValueError:
            record = {}
        if not isinstance(record, Mapping):
            record = {}
        entries[name] = _entry_facts(name, record, data)
        entries[name]["inode"] = inode
    return entries


def _directory_mtime_ns(directory: Path) -> int | None:
    try:
        return directory.stat().st_mtime_ns
    except OSError:
        return None


def _rebuild_index_document(directory: Path) -> dict[str, Any]:
    """Rebuild the whole document from one listing of the store."""
    identity = _store_directory_identity(directory)
    entries = _entries_from_listing(directory)
    runs = {run: _run_digest(entries, run) for run in _runs_of(entries)}
    for index, name in enumerate(sorted(entries), start=1):
        entries[name]["entry_version"] = index
    return {
        "code_stamp": _index_code_stamp(),
        "store": _resolved(directory),
        "version": len(entries),
        "generation": _sha256_hex(json.dumps(entries, sort_keys=True).encode()),
        "log": [
            [index, entry["reviewed_run_id"]]
            for index, entry in enumerate(
                sorted(entries.values(), key=lambda item: item["name"]), start=1
            )
        ],
        "entries": entries,
        "runs": runs,
        "directory_identity": identity,
        "dir_mtime_ns": identity[3] if identity is not None else None,
    }


def _runs_of(entries: Mapping[str, Any]) -> set[str]:
    return {str(entry.get("reviewed_run_id") or "") for entry in entries.values()}


def _index_stored_record(path: Path, record: Mapping[str, Any]) -> None:
    """Advance the store's index for one record this process has just written."""
    directory = path.parent
    if not _indexable(directory):
        return
    try:
        with path.open("rb") as handle:
            data = handle.read()
            inode = os.fstat(handle.fileno()).st_ino
    except OSError:
        return
    entry = _entry_facts(path.name, record, data)
    entry["inode"] = inode
    with _index_lock(directory):
        doc = _fresh_index_document(directory)
        if doc is None:
            doc = _rebuild_index_document(directory)
        version = int(doc.get("version") or 0) + 1
        doc["version"] = version
        entries = doc.setdefault("entries", {})
        previous = entries.get(path.name)
        touched = {entry["reviewed_run_id"]}
        if previous is not None:
            touched.add(str(previous.get("reviewed_run_id") or ""))
        entry["entry_version"] = version
        entries[path.name] = entry
        runs = doc.setdefault("runs", {})
        log = doc.setdefault("log", [])
        for run_id in sorted(touched):
            runs[run_id] = _run_digest(entries, run_id)
            log.append([version, run_id])
        del log[:-_INDEX_LOG_LIMIT]
        identity = _store_directory_identity(directory)
        doc["directory_identity"] = identity
        doc["dir_mtime_ns"] = identity[3] if identity is not None else None
        _persist_index_document(directory, doc)


def record_written(path: Path, record: Mapping[str, Any]) -> None:
    """Write one review record and advance the store's index for it.

    This is the store's one writer path. The record is serialised to a sibling
    temporary and renamed over the destination, so a reader never observes a
    partial file, and the bytes that landed are hashed so the index describes
    what is on disk rather than what was meant to be. The entry, the run's
    digest, the version and the change log move in the same locked write, so a
    reader that sees the new version sees the new entry.
    """
    from reckon._store import write_json_atomically

    write_json_atomically(
        path,
        dict(record),
        indent=2,
        sort_keys=True,
        mode=None,
        fsync=False,
        create_parents=True,
    )
    _index_stored_record(path, record)


# The directory identities this process has reconciled and found nothing to
# change, each with the document version it was checked at. A short-lived file
# another writer creates and removes in the store moves the directory's
# identity without moving any record, and recording the new identity meant
# rewriting the whole document -- megabytes on a shared filesystem, every few
# seconds. Remembering the check here costs nothing on disk; a moved identity
# or a new document version is checked again.
_SETTLED_IDENTITIES: dict[str, tuple[tuple[int, ...], int]] = {}


def reconcile_foreign_write(directory: Path) -> int:
    """Pick up added, removed or atomically replaced records without file stats.

    A writer that bypassed :func:`record_written` moved the directory's mtime
    without moving the index, so the two disagree and the store must be looked
    at. Directory entries supply their inodes without stat-ing the files.
    Only added or replaced names are read; removed names are dropped. An
    external rewrite retaining its inode becomes visible at the next rebuild.
    """
    doc = _fresh_index_document(directory)
    if doc is None:
        return 0
    identity = _store_directory_identity(directory)
    if identity is None or identity == tuple(doc.get("directory_identity") or ()):
        return 0
    settled = (tuple(identity), int(doc.get("version") or 0))
    if _SETTLED_IDENTITIES.get(str(directory)) == settled:
        return 0
    changed = 0
    with _index_lock(directory, wait=False) as locked:
        if locked is None:
            # Another process holds the index: it is writing or reconciling,
            # and the document this reader already has is at most that one
            # write behind.
            return 0
        doc = _fresh_index_document(directory) or doc
        identity = _store_directory_identity(directory)
        if identity is None or identity == tuple(doc.get("directory_identity") or ()):
            return 0
        try:
            with os.scandir(directory) as scanned:
                present = {
                    entry.name: entry.inode()
                    for entry in scanned
                    if entry.name.endswith(".json") and not entry.name.startswith(".")
                }
        except OSError:
            return 0
        entries = doc.setdefault("entries", {})
        runs = doc.setdefault("runs", {})
        log = doc.setdefault("log", [])
        touched: set[str] = set()
        moved = {
            name
            for name, inode in present.items()
            if name not in entries or entries[name].get("inode") != inode
        }
        for name in sorted(moved):
            if _review._INCOMPLETE_RECORD_MARK in name:
                continue
            path = directory / name
            try:
                data = path.read_bytes()
            except OSError:
                data = b""
            try:
                record = json.loads(data)
            except ValueError:
                record = {}
            if not isinstance(record, Mapping):
                record = {}
            entry = _entry_facts(name, record, data)
            entry["inode"] = present[name]
            previous = entries.get(name)
            doc["version"] = int(doc.get("version") or 0) + 1
            entry["entry_version"] = doc["version"]
            entries[name] = entry
            log.append([doc["version"], entry["reviewed_run_id"]])
            touched.add(entry["reviewed_run_id"])
            if previous is not None:
                previous_run = str(previous.get("reviewed_run_id") or "")
                if previous_run != entry["reviewed_run_id"]:
                    log.append([doc["version"], previous_run])
                    touched.add(previous_run)
            changed += 1
        for name in sorted(set(entries) - present.keys()):
            gone = entries.pop(name)
            run_id = str(gone.get("reviewed_run_id") or "")
            doc["version"] = int(doc.get("version") or 0) + 1
            log.append([doc["version"], run_id])
            touched.add(run_id)
            changed += 1
        if not changed:
            # Nothing the index describes moved, so nothing is written.
            _SETTLED_IDENTITIES[str(directory)] = (
                tuple(identity),
                int(doc.get("version") or 0),
            )
            return 0
        for run_id in touched:
            runs[run_id] = _run_digest(entries, run_id)
        del log[:-_INDEX_LOG_LIMIT]
        doc["directory_identity"] = identity
        doc["dir_mtime_ns"] = identity[3]
        _persist_index_document(directory, doc)
    return changed


def store_index_document(directory: Path) -> dict[str, Any] | None:
    """The store's index document, as current as its writers have left it.

    The document is probed against the store directory's identity, which is the
    one stat a writer-maintained index costs: a known writer records the identity its
    own write left, so its write settles the probe and no listing follows. Only
    a write the index did not make moves the identity away from the recorded one,
    and that is answered by :func:`reconcile_foreign_write`, which lists names.
    A missing or unusable document is rebuilt from one listing.
    """
    if not _indexable(directory):
        return None
    tick = _TICK_INDEX_DOCUMENTS.get()
    if tick is not None and directory in tick:
        return tick[directory]
    document = _settled_index_document(directory)
    if tick is not None:
        tick[directory] = document
    return document


def _settled_index_document(directory: Path) -> dict[str, Any] | None:
    """Read the document and bring it up to date with the store it describes."""
    document = _read_index_document(directory)
    if document is not None:
        # Several directory writes can share one filesystem clock tick. Size
        # and ctime are part of the same stat and keep a new name observable.
        identity = _store_directory_identity(directory)
        if identity is not None and identity == tuple(
            document.get("directory_identity") or ()
        ):
            return document
        reconcile_foreign_write(directory)
        return _read_index_document(directory) or document
    try:
        if not directory.is_dir():
            return None
    except OSError:
        return None
    with _index_lock(directory):
        # Another process may have written while this one waited for the lock.
        document = _fresh_index_document(directory)
        if document is None:
            document = _rebuild_index_document(directory)
            _persist_index_document(directory, document)
    return document


def _document_entries(
    directory: Path, document: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The per-file entries the run maps are built from, as the index holds them."""
    entries: list[dict[str, Any]] = []
    for name, raw in (document.get("entries") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        entries.append(
            {
                "name": str(name),
                "path": directory / str(name),
                "reviewed_run_id": str(raw.get("reviewed_run_id") or ""),
                "review_run_id": str(raw.get("review_run_id") or "").strip(),
                "head_carried": bool(raw.get("head_carried")),
                "head_sha": raw.get("head_sha"),
                "entry_version": int(raw.get("entry_version") or 0),
                "indexed": True,
            }
        )
    return entries


def _document_index_maps(
    directory: Path, document: Mapping[str, Any]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Build the two run maps from the document, reusing them until it moves."""
    key = (str(document.get("generation") or ""), int(document.get("version") or 0))
    tick_indexes = _TICK_REVIEW_INDEXES.get()
    if tick_indexes is not None:
        held = tick_indexes.get(directory)
        if isinstance(held, tuple) and len(held) == 3 and held[0] == key:
            return held[1], held[2]
    else:
        held = _DOCUMENT_MAPS.get(directory)
        if held is not None and held[0] == key:
            return held[1], held[2]
    reviewed, review_runs = _index_maps(_document_entries(directory, document))
    if tick_indexes is not None:
        tick_indexes[directory] = (key, reviewed, review_runs)
    else:
        _DOCUMENT_MAPS[directory] = (key, reviewed, review_runs)
    return reviewed, review_runs


def _indexable(directory: Path) -> bool:
    """Whether ``directory`` is a staging store the index belongs inside.

    A committed reviews tree is tracked by the project's checkout, so an index
    document written into it would be a file the project carries. Only the
    staging store, which lives under the crew home, keeps one.
    """
    try:
        root = Path(_review.review_store_root()).resolve()
        return Path(directory).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def store_index_version(directory: Path) -> int:
    """The version of one store's index, as its writers last left it."""
    doc = store_index_document(directory) or {}
    return int(doc.get("version") or 0)


def store_index_changed_runs(directory: Path, version: int) -> set[str]:
    """The runs whose records were written or removed after ``version``."""
    doc = store_index_document(directory) or {}
    changed: set[str] = set()
    for entry in doc.get("log") or []:
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        try:
            stamp = int(entry[0])
        except (TypeError, ValueError):
            continue
        if stamp > version and str(entry[1] or ""):
            changed.add(str(entry[1]))
    return changed


def review_records_index_names(
    directory: Path, run_id: str, *, reload: bool = False
) -> list[str]:
    """The record names the index holds for one reviewed run, without a listing."""
    if reload:
        _DOCUMENT_MAPS.pop(directory, None)
        tick = _TICK_INDEX_DOCUMENTS.get()
        if tick is not None:
            tick.pop(directory, None)
    document = store_index_document(directory) or {}
    return sorted(
        str(name)
        for name, raw in (document.get("entries") or {}).items()
        if isinstance(raw, Mapping) and str(raw.get("reviewed_run_id") or "") == run_id
    )


def store_index_run_digest(directory: Path, run_id: str) -> str | None:
    """The digest over one run's indexed records, or ``None`` when it holds none."""
    doc = store_index_document(directory) or {}
    runs = doc.get("runs")
    if not isinstance(runs, Mapping):
        return None
    return runs.get(run_id)


class _IndexedStat:
    """The attributes a reader asks of a record's stat, answered from the index.

    A constructed :class:`os.stat_result` carries no ``st_mtime_ns``, and the
    store's own selection orders records by that reading, so this answers it
    from the write order the index records. Every field here is O(1) to obtain:
    the store's size and age are known without touching the file at all.
    """

    __slots__ = ("st_ctime_ns", "st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_size")

    def __init__(self, entry: Mapping[str, Any]) -> None:
        self.st_mode = stat.S_IFREG | 0o644
        self.st_dev = 0
        self.st_ino = int(entry.get("inode") or 0)
        self.st_size = int(entry.get("size") or 0)
        # Write order, in the field the store's selection orders by.
        self.st_mtime_ns = int(entry.get("entry_version") or 0)
        self.st_ctime_ns = self.st_mtime_ns


def _indexed_record_stat(entry: Mapping[str, Any]) -> _IndexedStat:
    """A stat-shaped view of one indexed record, read from the index alone."""
    return _IndexedStat(entry)


def review_entries_signature(project: str, reviewed_run_id: str) -> tuple[Any, ...]:
    """The current identities of one reviewed run's indexed record files.

    A staging record written through the store moves its run's digest. Another
    run's new record leaves this key unchanged. Records outside the indexed
    staging store use their file identities. The committed directory is included
    because promotion can replace staging as the source.
    """
    directory = _review.review_store_root() / project
    digest = store_index_run_digest(directory, reviewed_run_id)
    if digest is not None:
        current_entries: list[Any] = [digest]
    else:
        entries = _store_index(directory).get(reviewed_run_id) or []
        current_entries = []
        for entry in entries:
            try:
                signature = file_memo.file_signature(entry["path"])
            except OSError:
                signature = None
            current_entries.append((entry["name"], signature))
    committed = _review.committed_review_root(project)
    run_directory = (
        committed / _review.COMMITTED_RUN_DIRNAME / reviewed_run_id
        if committed is not None
        else None
    )
    committed_identity = (
        _store_directory_identity(run_directory) if run_directory is not None else None
    )
    return (
        str(directory),
        tuple(sorted(current_entries)),
        str(run_directory or ""),
        committed_identity,
    )


def _review_run_index(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the review-run index, loaded and updated rather than rebuilt.

    The same index and the same sequence as :func:`_store_index`, keyed by the
    review run that produced a record rather than by the run it reviewed.
    """
    return _store_indexes(directory)[1]


def reviewed_run_ids(project: str, *, base_dir: str | Path | None = None) -> list[str]:
    """Return the reviewed run ids the project's staging store holds records for.

    The store's own index answers, so the enumeration is one pass per project
    directory per process rather than a walk per caller, and the ids come back
    sorted for a stable order. A plan review names no reviewed run, so the index
    keys it under the empty run id; an empty key is not a run and is not
    returned. A project directory that does not exist, or one holding no record,
    yields an empty list.
    """
    directory = _review.review_store_root(base_dir) / project
    if not directory.is_dir():
        return []
    return sorted(run_id for run_id in _store_index(directory) if run_id)


def committed_record_paths_by_review_run(
    project: str, *, root: str | Path | None = None
) -> dict[str, list[Path]]:
    """Resolve every committed filing through the store's reviewer identity index.

    Run, plan and figure records share a content identity even when the path
    carries a rubric or a reviewed head. Index each subject directory through
    the same owner as staging lookups instead of inferring identity from a name.
    Callers retaining this map must re-read selected records before acting.
    """
    committed = _review.committed_review_root(project, root=root)
    if committed is None:
        return {}
    paths: dict[str, list[Path]] = {}
    try:
        directories = list(committed.glob("*/*"))
    except OSError:
        return paths
    for directory in directories:
        try:
            if not directory.is_dir():
                continue
            index = _review_run_index(directory)
        except (OSError, ValueError):
            continue
        for review_run_id, entries in index.items():
            paths.setdefault(review_run_id, []).extend(
                entry["path"] for entry in entries
            )
    return paths


def record_for_review_run(
    project: str, review_run_id: str, *, base_dir: str | Path | None = None
) -> tuple[Path, dict[str, Any]] | None:
    """Return the newest staging record the named review run produced, or ``None``.

    A plan review is stored by the plan-review store rather than as a scored run
    review, so a lookup by the reviewed run does not find it; both kinds name the
    review run that produced them, which is the one stable key they share. The
    answer is served by the store's index rather than by walking the project
    directory — a store of thousands of records makes a whole-store pass per
    lookup the dominant cost of the lookup's per-turn reader — and the file the
    index selects is re-read so a rewrite in place is returned current. A
    project directory that does not exist, or one holding no record for the
    named review run, yields ``None``.
    """
    name = str(review_run_id or "").strip()
    if not name:
        return None
    directory = _review.review_store_root(base_dir) / project
    if not directory.is_dir():
        return None
    if open_run_index.pointers() is None:
        entries = _review_run_index(directory).get(name)
    else:
        with open_run_index.read_scope(record={"run_id": name, "project": project}):
            entries = _review_run_index(directory).get(name)
    if not entries:
        return None
    for entry in sorted(entries, key=_recency, reverse=True):
        path = entry["path"]
        try:
            record = _record_file(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, Mapping):
            continue
        if str(record.get("review_run_id") or "").strip() != name:
            continue
        return path, dict(record)
    return None


def record_for_review_run_anywhere(
    project: str,
    reviewed_run_id: str,
    review_run_id: str,
    *,
    base_dir: str | Path | None = None,
    rubric: str | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """The stored record one review run produced for a run, staged or committed.

    A caller that names the review run whose record it means supplies both
    identities — the run reviewed and the review run that read it — because a
    review run id alone says nothing about the run it belongs to, and a write
    keyed on the wrong run's record would answer a finding raised on another
    piece of work. The committed tree is read first: a promoted review is the
    record that travels with the project, and the staging copy is the same
    record before its promotion. A review run that never reached the committed
    tree is answered from the staging store through
    :func:`record_for_review_run`.

    ``reviewed_run_id`` and ``rubric`` both filter what is returned, so a
    record of another run or of another rubric is never handed back as the one
    named. ``(None, None)`` means no readable record of that review run belongs
    to this run.
    """
    name = str(review_run_id or "").strip()
    if not name:
        return None, None
    for path, record in reversed(_committed_records_for_run(project, reviewed_run_id)):
        if str(record.get("review_run_id") or "").strip() != name:
            continue
        if not _review._review_rubric_matches(record, rubric):
            continue
        return path, dict(record)
    staged = record_for_review_run(project, name, base_dir=base_dir)
    if staged is None:
        return None, None
    path, record = staged
    if str(record.get("reviewed_run_id") or "").strip() != reviewed_run_id:
        return None, None
    if not _review._review_rubric_matches(record, rubric):
        return None, None
    return path, dict(record)


def review_run_record_rubrics(
    project: str,
    reviewed_run_id: str,
    review_run_id: str,
    *,
    base_dir: str | Path | None = None,
) -> list[str]:
    """The rubric families a review run's record for a run is stored under.

    A lookup that filters by rubric can only answer ``None`` when the record it
    was asked for is held under another family, and a caller reading that
    ``None`` cannot tell the two cases apart: no record at all, or a record it
    could reach by naming the family it is in. This answers the second question
    without the filter, so a refusal can name what the store does hold.

    The name returned is the record's own rubric, as the record carries it; a
    text review is the empty name, because it is the family every record
    without a rubric belongs to. The committed tree is read first and the
    staging store after it, the same order
    :func:`record_for_review_run_anywhere` reads. An empty list means no
    readable record of the named review run belongs to this run.
    """
    name = str(review_run_id or "").strip()
    if not name:
        return []
    families: list[str] = []

    def _note(record: Mapping[str, Any]) -> None:
        rubric = _review._review_record_rubric(record)
        if rubric not in families:
            families.append(rubric)

    for _path, record in _committed_records_for_run(project, reviewed_run_id):
        if str(record.get("review_run_id") or "").strip() == name:
            _note(record)
    staged = record_for_review_run(project, name, base_dir=base_dir)
    if staged is not None:
        _path, record = staged
        if str(record.get("reviewed_run_id") or "").strip() == reviewed_run_id:
            _note(record)
    return families


def _indexed_head_matches(entry: Mapping[str, Any], named: str | None) -> bool:
    """Whether an indexed record file can answer a lookup for head ``named``.

    ``None`` means no head was named and every indexed record of the run is a
    candidate. A record that carries no head is skipped when one is named, the
    same rule the lookup has always applied: a record naming no revision cannot
    be known to describe the one asked about.
    """
    if named is None:
        return True
    if not entry.get("head_carried"):
        return False
    head_sha = entry.get("head_sha")
    if not head_sha:
        return False
    actual = str(head_sha).lower()
    return actual.startswith(named) or named.startswith(actual)


def _committed_records_for_run(
    project: str,
    reviewed_run_id: str,
) -> list[tuple[Path, dict[str, Any]]]:
    """Every readable landed record for the run, oldest key first.

    The tree is resolved from the project's registered checkout, as the by-head
    selection resolves it, because that is where a promotion commits the run's
    reviews; a run's own worktree holds the work, not the passed record of it.
    Only the committed tree is enumerated. A staging review is a live round
    keyed to the one head it read: once the run's work has moved past that head,
    the round describes a revision the work is no longer at, and a disposition
    recorded against it would retire a finding on a diff nobody read of the work
    as it now stands. A committed review is the run's landed evidence, which is
    what a descent from a later head is there to reach: the repair moved the
    head on, and the review of the ancestor remains the record the finding
    belongs to.
    """
    committed = _review.committed_review_root(project)
    if committed is None:
        return []
    run_directory = committed / _review.COMMITTED_RUN_DIRNAME / reviewed_run_id
    if not run_directory.is_dir():
        return []
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(_store_paths(run_directory, "*.json")):
        record = _committed_record_at(path)
        if record is None:
            continue
        if str(record.get("reviewed_run_id") or "").strip() != reviewed_run_id:
            continue
        records.append((path, dict(record)))
    return records


def stored_review_descending_from(
    project: str,
    reviewed_run_id: str,
    head: str,
    *,
    repository: str | Path | None,
    rubric: str | None = None,
) -> tuple[Path | None, dict[str, Any] | None, str]:
    """Return a stored review whose head the run's head descends from.

    A repair moves the run's head past the revision its review read. The
    by-head selection then finds no record describing the new head, and a
    disposition recorded afterwards would answer nothing. A review of an
    ancestor is still the run's own evidence about the work under review, so a
    disposition against it stands: the finding it raises is work the repair was
    asked to answer, and the answers are written onto that record.

    The record must have been landed: only the project's committed tree is
    searched, because that is the run's evidence about its own work, while a
    staging round is a live round of a single head and stops describing the
    work once the head has moved past it. That tree resolves from the project's
    registered checkout, where a promotion commits the record, so the lookup
    works from a worker's worktree as well as from the main checkout.
    ``repository`` is the run's own tree and is used only for the ancestry
    check, because the run's new head exists nowhere else. The newest matching
    committed record wins, so a run reviewed at several rounds answers against
    the latest one whose head is still in the run's history.
    A head equal to the record's own is accepted without a git call. An empty
    ``head`` or no repository to check ancestry against yields nothing, which
    leaves the caller's refusal in place rather than accepting an unverifiable
    record. The third element is the reviewed head the accepted record names,
    so a caller can key its write and report the revision it answered.
    """
    current = str(head or "").strip()
    if not current or repository is None:
        return None, None, ""
    from reckon.crew import merge as merge_module
    from reckon.crew import recovery as recovery_module

    for path, record in reversed(_committed_records_for_run(project, reviewed_run_id)):
        if not _review._review_rubric_matches(record, rubric):
            continue
        _, _, carries, stored_head = _review.carried_revision_pair(record)
        if not (carries and stored_head):
            continue
        if recovery_module.same_revision(
            stored_head, current
        ) or merge_module._is_ancestor(Path(repository), stored_head, current):
            return path, dict(record), stored_head
    return None, None, ""


# ── The review module, whose helpers this family reads ──
# The import sits at the foot of the module so that review.py, which
# re-exports the family above, can finish defining the names below before
# this module asks for them, whichever module imports first. The helpers are
# read through the module object rather than bound as locals, so a caller
# that patches one on reckon.crew.review still reaches the code here that
# reads it.
from reckon.crew import review as _review  # noqa: E402
