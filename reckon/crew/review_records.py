"""Readers, writers and the index for the store.

A review record is stored once per round in the project's review store, and a
promotion commits one copy of that record into the project's committed reviews
tree. This module owns that family: reading a run's record from the committed
tree first and the staging store after, selecting the copy a named head names,
unioning the disposition and answer fields every copy of one review run holds,
writing a record and committing that write, and the store index that answers a
misfiled lookup and a delivered-plan-review lookup from one listing per store
directory, updated in place when a file is added, changed or removed and
persisted in a node-local cache the node's other processes start from.

The store resolves its directories through :mod:`reckon.crew.review` and shares
its schema constants and revision helpers with it, so those names are imported
from there rather than restated. The import sits at the foot of this module
because :mod:`reckon.crew.review` re-exports this family: each module imports
the names it needs once the other has finished defining them, which breaks the
cycle without either module owning a copy of what the other defines. No
behaviour changes here; the move leaves the store exactly as it was written.
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
from pathlib import Path
from typing import Any


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
        key=lambda item: item["mtime_ns"],
    ):
        path = entry["path"]
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
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
        stored = json.loads(Path(path).read_text(encoding="utf-8"))
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
    return path


def commit_record_write(project: str, path: str | Path) -> bool:
    """Commit a write to a review record that lives in the committed tree.

    A disposition or an answer written to the committed record is a tracked
    file in the project's checkout, so the write is committed through the same
    helper a promotion commits its state writes with — one path, one commit —
    rather than left as a modified tracked file the next reader would trip on.
    A staging write is not tracked and is left uncommitted. A project with no
    committed tree, or a path outside it, is not committed here and returns
    ``False``.
    """
    committed = _review.committed_review_root(project)
    if committed is None:
        return False
    try:
        Path(path).resolve().relative_to(Path(committed).resolve())
    except (OSError, ValueError):
        return False
    from reckon import ledger

    ledger._commit_state_write(
        project,
        "fix(review): commit an answer on a committed review record",
        "Commit the disposition or answer written to a committed review record "
        "so it travels with the tree rather than sitting as a modified file.",
        Path(path),
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
    and a full sha names its short preservation copy. Without a named head the
    newest file is returned. A file that cannot be read or does not parse — a
    record a review worker is composing by hand can be met mid-write — is
    skipped and named to the caller, because a truncated file would otherwise
    read as "this run has no review". ``(None, None)`` means no readable record
    among the candidates; the caller decides whether that settles the search or
    falls through to another tree.
    """
    records: list[tuple[Path, Any, int]] = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            mtime_ns = path.stat().st_mtime_ns
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
    for path, record, _mtime_ns in records:
        _, _, carried_head, stored_head = _review.carried_revision_pair(record)
        if not carried_head or not stored_head:
            continue
        actual = stored_head.lower()
        if actual.startswith(named) or named.startswith(actual):
            return path, record
    return None, None


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
            sorted(run_directory.glob("*.json")) if run_directory.is_dir() else []
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
        candidates.extend(directory.glob(f"{reviewed_run_id}*.at-*.json"))
    existing = {path.resolve(): path for path in candidates if path.is_file()}
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
        key=lambda item: item["mtime_ns"],
    )
    for entry in entries:
        path = entry["path"]
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
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
    """
    for entry in sorted(candidates, key=lambda item: item["mtime_ns"], reverse=True):
        if not _indexed_head_matches(entry, named):
            continue
        path = entry["path"]
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
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
        record = dict(record)
        record[_review.MISFILED_KEY] = True
        return path, record
    return None


# ── The store index ─────────────────────────────────────────────────────────
# Choosing the file to answer a misfiled lookup from needs two facts per record
# file: the run id it names and the head it read. Reading every file to learn
# them costs a whole-store pass, and a session whose live pointers carry no
# record pays one pass per pointer. The index below holds those two facts — not
# the record content — and is built at most once per store directory and per
# process, then reused while the directory's stat identity is unchanged, which
# is exactly when its set of entries can have moved.

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


# A persisted index is meaningful only to the code that would read it: a build
# written before the reader changed can hold facts a later reader would derive
# differently, and the selection rules it was written under can differ too. The
# stamp is the content identity of the two modules those facts and rules are
# defined in, so it moves for an uncommitted edit as readily as for a commit.
# The process-wide code stamp is not used here: it costs about eleven seconds on
# a cold process, reading a whole package and asking git about the tree, which
# is the cost this module's persisted cache exists to remove.
# A held stamp is one entry in a list rather than a module scalar so the memo
# costs no rebinding of a module global on the read path.
_INDEX_CODE_STAMP: list[str] = []


def _index_sources() -> tuple[Path, ...]:
    """The modules whose content fixes what a persisted index means."""
    return (Path(__file__).resolve(), Path(_review.__file__).resolve())


def _index_code_stamp() -> str:
    """The stamp a persisted index is filed under and checked against."""
    if not _INDEX_CODE_STAMP:
        digest = hashlib.sha256()
        try:
            for source in _index_sources():
                digest.update(source.read_bytes())
        except OSError:
            return ""
        _INDEX_CODE_STAMP.append(digest.hexdigest())
    return _INDEX_CODE_STAMP[0]


def _resolved(directory: Path) -> str:
    """The store directory's resolved absolute path, or "" when unresolvable."""
    try:
        return str(directory.resolve())
    except OSError:
        return ""


def _index_cache_path(directory: Path) -> Path:
    """The node-local file one store directory's index is persisted in.

    The store's own directory keys the file, not the project, because a
    worktree's or a scratch tree's store shares its project name with the main
    store and the two must never answer from one another's entries.
    """
    digest = hashlib.sha256(_resolved(directory).encode("utf-8")).hexdigest()[:32]
    return node_local_cache_dir(directory.name) / f"store-index-{digest}.json"


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
    if _review._INCOMPLETE_RECORD_MARK in path.name:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
    for path in sorted(directory.glob("*.json")):
        try:
            info = path.stat()
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
    try:
        candidates = sorted(directory.glob("*.json"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            info = path.stat()
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
    directory: Path, entries: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """The document one store directory's index is persisted as.

    Each entry carries the stat identity a later reader compares against the
    file it finds, so that reader can tell a file it may reuse from one it must
    parse, and the facts both lookups select on. The file's path is not stored:
    the store directory is in the document, and a name inside it resolves
    there.
    """
    return {
        "code_stamp": _index_code_stamp(),
        "store": _resolved(directory),
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


def _persist_store_entries(directory: Path, entries: list[dict[str, Any]]) -> None:
    """Write the store's index where this node's processes can start from it.

    A disposable cache: an unwritable directory, a full disk or a reader that
    arrives mid-write costs a later process a from-scratch build rather than
    the cache, so a write that fails is not an error a lookup reports.
    """
    if not _resolved(directory):
        return
    document = _persisted_index_document(directory, entries)
    try:
        from reckon._store import write_json_atomically

        write_json_atomically(
            _index_cache_path(directory),
            document,
            fsync=False,
            mode=None,
            create_parents=True,
        )
    except OSError:
        return


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


def _load_persisted_entries(directory: Path) -> list[dict[str, Any]] | None:
    """The store's index as another process left it, or ``None`` when unusable.

    An index is usable only when this checkout's reader wrote it for this store
    directory: a document stamped with other code's identity, or naming another
    store, is ignored rather than trusted, and the caller builds from scratch.
    A missing, half-written or malformed document gets the same answer, because
    a cache that cannot be read is not a cache.
    """
    store = _resolved(directory)
    if not store:
        return None
    try:
        document = json.loads(_index_cache_path(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(document, Mapping):
        return None
    if str(document.get("code_stamp") or "") != _index_code_stamp():
        return None
    if str(document.get("store") or "") != store:
        return None
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        return None
    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        entry = _entry_from_persisted(directory, raw)
        if entry is None:
            return None
        entries.append(entry)
    return entries


def _store_indexes(
    directory: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Return the store's two indexes, updated only when the directory moves.

    The index is held per store directory and per process and reused while the
    directory's stat identity is unchanged, which is exactly when its set of
    entries cannot have moved. When the identity does move the index is updated
    from one listing rather than rebuilt: the entries this process holds say
    which files changed, and a process holding none starts from the index
    another process persisted for this store directory. Every unchanged file
    keeps the facts already read for it, so a store of thousands of records
    costs one listing and one parse per changed file rather than a pass over
    all of them. The updated index is persisted for the next process on the
    node.
    """
    identity = _store_directory_identity(directory)
    if identity is None:
        _STORE_INDEXES.pop(directory, None)
        return {}, {}
    cached = _STORE_INDEXES.get(directory)
    if cached is not None and cached[0] == identity:
        return cached[1], cached[2]
    entries = cached[3] if cached is not None else _load_persisted_entries(directory)
    if entries is None:
        reviewed, review_runs = _build_store_indexes(directory)
        entries = _index_files(reviewed, review_runs)
    else:
        entries = _update_store_entries(directory, entries)
        reviewed, review_runs = _index_maps(entries)
    _STORE_INDEXES[directory] = (identity, reviewed, review_runs, entries, False)
    _persist_store_entries(directory, entries)
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
    for entry in entries:
        path = entry["path"]
        try:
            info = path.stat()
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
    Every later miss answers from the refreshed index.
    """
    cached = _STORE_INDEXES.get(directory)
    if cached is None:
        return _store_index(directory)
    identity, reviewed, review_runs, entries, refreshed = cached
    if refreshed:
        return reviewed
    entries = _update_store_entries(directory, entries)
    reviewed, review_runs = _index_maps(entries)
    _STORE_INDEXES[directory] = (identity, reviewed, review_runs, entries, True)
    _persist_store_entries(directory, entries)
    return reviewed


def _store_index(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the reviewed-run index, loaded and updated rather than rebuilt.

    A process holding no index for the store directory starts from the one
    another process on this node persisted for it, and a move of the directory's
    stat identity is answered by updating that index from one listing of the
    store, so only a file new to the index or changed since it was read costs a
    parse. A document that is missing, unreadable or stamped by other code is
    not usable, and only then is the index built from scratch. The sequence is
    :func:`_store_indexes`, which owns it for both indexes.
    """
    return _store_indexes(directory)[0]


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
    entries = _review_run_index(directory).get(name)
    if not entries:
        return None
    for entry in sorted(entries, key=lambda item: item["mtime_ns"], reverse=True):
        path = entry["path"]
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, Mapping):
            continue
        if str(record.get("review_run_id") or "").strip() != name:
            continue
        return path, dict(record)
    return None


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
    *,
    root: str | Path | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    """Every readable committed record for the run, oldest key first.

    Only the project's committed tree is enumerated. A staging review is a live
    round keyed to the one head it read: once the run's work has moved past that
    head, the round describes a revision the work is no longer at, and a
    disposition recorded against it would retire a finding on a diff nobody read
    of the work as it now stands. A committed review is the run's landed
    evidence, which is what a descent from a later head is there to reach: the
    repair moved the head on both sides, and the review of the ancestor remains
    the record the finding belongs to.
    """
    committed = _review.committed_review_root(project, root=root)
    if committed is None:
        return []
    run_directory = committed / _review.COMMITTED_RUN_DIRNAME / reviewed_run_id
    if not run_directory.is_dir():
        return []
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(run_directory.glob("*.json")):
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
    work once the head has moved past it. The newest matching committed record
    wins, so a run reviewed at several rounds answers against the latest one
    whose head is still in the run's history.
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

    for path, record in reversed(
        _committed_records_for_run(project, reviewed_run_id, root=repository)
    ):
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
