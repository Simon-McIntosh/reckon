"""Readers, writers and the index for the store.

A review record is stored once per round in the project's review store, and a
promotion commits one copy of that record into the project's committed reviews
tree. This module owns that family: reading a run's record from the committed
tree first and the staging store after, selecting the copy a named head names,
unioning the disposition and answer fields every copy of one review run holds,
writing a record and committing that write, and the store index that answers a
misfiled lookup and a delivered-plan-review lookup from one scan per project
directory per process.

The store resolves its directories through :mod:`reckon.crew.review` and shares
its schema constants and revision helpers with it, so those names are imported
from there rather than restated. The import sits at the foot of this module
because :mod:`reckon.crew.review` re-exports this family: each module imports
the names it needs once the other has finished defining them, which breaks the
cycle without either module owning a copy of what the other defines. No
behaviour changes here; the move leaves the store exactly as it was written.
"""

from __future__ import annotations

import json
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
    directory = review_store_root(base_dir) / project
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
            and _review_record_rubric(stored) == _review_record_rubric(record)
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
            (DIMENSION_DISPOSITIONS_KEY, dispositions),
            (FINDING_DISPOSITIONS_KEY, finding_dispositions),
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
        merged[DIMENSION_DISPOSITIONS_KEY] = dispositions
    if finding_dispositions:
        merged[FINDING_DISPOSITIONS_KEY] = finding_dispositions
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
    if not names_a_review_subject(record):
        raise ValueError(
            "review record names neither a reviewed run nor a plan, so it has "
            "no subject to be filed under"
        )
    reviewed_run_id = str(record.get("reviewed_run_id") or "").strip()
    if not carries_review_material(record):
        raise ValueError(
            "review record carries no review material — findings, scores, a "
            "rubric or a reviewed revision — so it is not a review and the "
            "committed store holds records only"
        )
    if committed_root is None:
        committed_root = committed_review_root(project, root=root)
    if committed_root is None:
        raise ValueError(f"no committed reviews tree resolves for project {project!r}")

    dispatched, completed, times_source = resolve_record_times(
        project, record, root=root
    )

    stored = dict(record)
    stored["project"] = project
    stored[DISPATCH_TIME_KEY] = dispatched
    stored[COMPLETION_TIME_KEY] = completed
    stored[TIMES_SOURCE_KEY] = times_source
    if not stored.get("timestamp"):
        stored["timestamp"] = datetime.now(UTC).isoformat()

    plan_slug = str(record.get("plan_slug") or "").strip()
    if reviewed_run_id:
        path = review_path(
            project,
            reviewed_run_id,
            committed_root=committed_root,
            review_run_id=review_run_id,
            reviewed_head_sha=str(record.get(REVIEWED_HEAD_KEY) or "") or None,
            rubric=_review_record_rubric(record),
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
    _write_record(path, stored)
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
    committed = committed_review_root(project)
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
    claimless = claimless_findings(record)
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
        if not _review_rubric_matches(record, rubric):
            continue
        records.append((path, record, mtime_ns))
    if not records:
        return None, None
    if reviewed_head_sha is None:
        path, record, _mtime_ns = max(records, key=lambda item: item[2])
        return path, record
    named = reviewed_head_sha.strip().lower()
    for path, record, _mtime_ns in records:
        _, _, carried_head, stored_head = carried_revision_pair(record)
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
            review_path(
                project,
                reviewed_run_id,
                reviewed_head_sha=carried_revision_pair(record)[3],
                rubric=_review_record_rubric(record),
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
    committed = committed_review_root(project)
    committed_selected: tuple[Path | None, dict[str, Any] | None] | None = None
    if committed is not None:
        run_directory = committed / COMMITTED_RUN_DIRNAME / reviewed_run_id
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
    directory = review_store_root(base_dir) / project
    candidates = [review_path(project, reviewed_run_id, base_dir, rubric=rubric)]
    if directory.is_dir():
        candidates.extend(directory.glob(f"{reviewed_run_id}*.at-*.json"))
    existing = {path.resolve(): path for path in candidates if path.is_file()}
    if existing:
        return _record_with_merged_answers(
            project,
            reviewed_run_id,
            _select_stored_record(
                list(existing.values()),
                reviewed_head_sha,
                rubric=rubric,
                skipped=skipped,
            ),
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
    """
    directory = review_store_root(base_dir) / project
    if not directory.is_dir():
        return []
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
        records.append((path, dict(record)))
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
    candidates = _store_index(directory).get(reviewed_run_id)
    if not candidates:
        return None, None
    named = None if reviewed_head_sha is None else reviewed_head_sha.strip().lower()
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
        if not _review_rubric_matches(record, rubric):
            continue
        if named is not None:
            _, _, carried_head, stored_head = carried_revision_pair(record)
            if not carried_head or not stored_head:
                continue
            actual = stored_head.lower()
            if not (actual.startswith(named) or named.startswith(actual)):
                continue
        record = dict(record)
        record[MISFILED_KEY] = True
        return path, record
    return None, None


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
    ],
] = {}


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


def _build_store_indexes(
    directory: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Read each record file once, keyed by the run it reviews and by its reviewer.

    One scan serves both lookups: the misfiled lookup answers for the run a
    record's content reviews, and the delivered-plan-review lookup answers for
    the review run that produced the record. Each entry carries only the facts
    its lookup selects on, never the whole record, because the file the index
    selects is re-read from disk so a rewrite in place is answered current.
    """
    reviewed: dict[str, list[dict[str, Any]]] = {}
    review_runs: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(directory.glob("*.json")):
        if not path.is_file() or _INCOMPLETE_RECORD_MARK in path.name:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            mtime_ns = path.stat().st_mtime_ns
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, Mapping):
            continue
        carried_head, head_sha = _first_carried_revision(record, HEAD_REVISION_FIELDS)
        reviewed.setdefault(str(record.get("reviewed_run_id") or ""), []).append(
            {
                "path": path,
                "mtime_ns": mtime_ns,
                "head_carried": carried_head,
                "head_sha": head_sha,
            }
        )
        review_run = str(record.get("review_run_id") or "").strip()
        if review_run:
            review_runs.setdefault(review_run, []).append(
                {"path": path, "mtime_ns": mtime_ns}
            )
    return reviewed, review_runs


def _store_indexes(
    directory: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Return the store's two indexes, rebuilt only when the directory moves.

    Both indexes are built from one scan and cached under the directory's stat
    identity, so a lookup by a record's reviewed run and a lookup by the review
    run that produced it share the store's one pass and its one rebuild trigger.
    """
    identity = _store_directory_identity(directory)
    if identity is None:
        _STORE_INDEXES.pop(directory, None)
        return {}, {}
    cached = _STORE_INDEXES.get(directory)
    if cached is not None and cached[0] == identity:
        return cached[1], cached[2]
    reviewed, review_runs = _build_store_indexes(directory)
    _STORE_INDEXES[directory] = (identity, reviewed, review_runs)
    return reviewed, review_runs


def _store_index(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the reviewed-run index, rebuilt only when the directory moves."""
    return _store_indexes(directory)[0]


def _review_run_index(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the review-run index, rebuilt only when the directory moves."""
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
    directory = review_store_root(base_dir) / project
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
    directory = review_store_root(base_dir) / project
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


# ── The shared schema, resolution and revision helpers, owned by review ──
# The import sits at the foot of the module so that review.py, which re-exports
# the family above, can finish defining these names before this module asks for
# them, whichever module imports first.
from reckon.crew.review import (  # noqa: E402
    _INCOMPLETE_RECORD_MARK,
    COMMITTED_RUN_DIRNAME,
    COMPLETION_TIME_KEY,
    DIMENSION_DISPOSITIONS_KEY,
    DISPATCH_TIME_KEY,
    FINDING_DISPOSITIONS_KEY,
    HEAD_REVISION_FIELDS,
    MISFILED_KEY,
    REVIEWED_HEAD_KEY,
    TIMES_SOURCE_KEY,
    _first_carried_revision,
    _review_record_rubric,
    _review_rubric_matches,
    _write_record,
    carried_revision_pair,
    carries_review_material,
    claimless_findings,
    committed_review_root,
    names_a_review_subject,
    resolve_record_times,
    review_path,
    review_store_root,
)
