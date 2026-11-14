"""A persisted per-project metadata index, keyed by each file's content digest.

A page's first paint needs one row per document and figure — slug, href, type,
title, status, sprint, archive marker, stamps and figure dimensions — and
nothing derived. Building that list used to cost a parse of every file in the
tree on every cold process, so a restart re-read the whole project from the
shared filesystem before it could answer.

The index keeps one row per file on disk under the configuration home's cache
directory, together with a content digest of the file the row came from. A
rebuild walks the tree, hashes each file and re-parses only the ones whose
bytes changed, so a rebuild costs a hash per file rather than a parse; the hash
is an order of magnitude cheaper than the parse it stands in for. The change
watch drops a tree's in-process rows when the kernel reports a change to it,
and the next read checks that tree and rebuilds the rows that moved. A reader
with no watch compares each row with its digest on every call instead — see
``index_rows(..., revalidate=True)``. The process shares a hash while the file's
stat identity is unchanged and reads changed bytes again.

A plan's row also carries the two figures that cannot be read from a document's
``<meta>`` head: its open-followup count and its implementable-section count.
They are derived once, when the row is built, and reused until the file's bytes
change, so a reader that answers from the index — the drain's plan remainder,
:func:`plan_derivations` — never parses a plan it has already seen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from reckon import _plan_html, figures, file_memo, resources
from reckon._store import _config_home, write_json_atomically
from reckon.file_memo import (
    file_revision,
    file_signature,
    signature_from_stat,
)

#: A row's stamp source: a file and its stat identity in, ``(created, edited)``
#: out. The default derives both from the stat identity; a caller that can
#: reach the repository supplies the git-aware rule so the index and the served
#: discovery payload agree on one document's timestamps.
StampFn = Callable[[Path, list[int]], tuple[int, str]]

LOGGER = logging.getLogger("reckon.metadata_index")

#: The fields a served row may carry, and the whole of what ``/_index``
#: returns. The content digest that keys the persisted index is deliberately
#: not one of them: it is bookkeeping, not something a list renders.
ROW_FIELDS = (
    "slug",
    "href",
    "type",
    "title",
    "status",
    "sprint",
    "archived",
    "created",
    "edited",
    "width",
    "height",
    "revision",
    "impl",
)

#: Bumped when an entry gains a field, so an index written by the previous
#: shape is rebuilt rather than reused with the new field absent.
_VERSION = 6
_FIGURE_DIR = "figures"
_FIGURE_SUFFIXES = (".png", ".svg", ".gif")
_COVERED_PROJECT: ContextVar[tuple[str, str] | None] = ContextVar(
    "reckon_covered_project", default=None
)
_DISCOVERY_FILES: ContextVar[
    dict[
        tuple[str, str], tuple[list[tuple[str, Path]], dict[str, os.stat_result | None]]
    ]
    | None
] = ContextVar("reckon_discovery_files", default=None)
_DISCOVERY_DIRS: ContextVar[dict[str, list[int] | None] | None] = ContextVar(
    "reckon_discovery_dirs", default=None
)
_REUSE_COVERED: ContextVar[
    dict[
        tuple[str, str],
        tuple[list[tuple[str, Path]], dict[str, Path], dict[str, list[int]]] | None,
    ]
    | None
] = ContextVar("reckon_reuse_covered", default=None)
_RESOURCE_CANDIDATES: ContextVar[bool] = ContextVar(
    "reckon_resource_candidates", default=False
)


@contextmanager
def discovery_file_scope():
    """Share one covered-tree observation within a read.

    The region holds one covered-file listing, the stats each of its files is
    read for, and the identity each covered directory is checked for. A nested
    entry joins the region already open rather than replacing it, so one read's
    several readers observe the covered tree once between them; the outermost
    entry opens the region and the outermost exit closes it, so a change made
    between two reads is seen by the next.
    """

    if _DISCOVERY_FILES.get() is not None:
        yield
        return
    files_token = _DISCOVERY_FILES.set({})
    dirs_token = _DISCOVERY_DIRS.set({})
    reused_token = _REUSE_COVERED.set({})
    try:
        yield
    finally:
        _DISCOVERY_FILES.reset(files_token)
        _DISCOVERY_DIRS.reset(dirs_token)
        _REUSE_COVERED.reset(reused_token)


@contextmanager
def resource_candidate_scope():
    """Feed a resource scan only covered HTML paths it can classify."""

    token = _RESOURCE_CANDIDATES.set(True)
    try:
        yield
    finally:
        _RESOURCE_CANDIDATES.reset(token)


def _resource_candidates(files: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    return [
        (relative, path)
        for relative, path in files
        if relative.endswith(".html")
        and path.name not in resources.NON_RESOURCE_FILES
        and not any(
            part in resources.INFRA_DIRS for part in PurePosixPath(relative).parts[:-1]
        )
        and not resources._is_evidence_fragment(PurePosixPath(relative))
    ]


def covered_stat(
    docs_dir: Path, project: str, relative: str, path: Path
) -> os.stat_result | None:
    """Return a covered file's stat, reusing this pass's first observation."""

    scoped = _DISCOVERY_FILES.get()
    key = (os.fspath(docs_dir), project)
    observations = scoped[key][1] if scoped is not None and key in scoped else None
    if observations is not None and relative in observations:
        return observations[relative]
    try:
        observed = path.stat()
    except OSError:
        observed = None
    if observations is not None:
        observations[relative] = observed
    return observed


def covered_dir_identity(path: Path) -> list[int] | None:
    """Return a directory's stat identity, reusing this pass's first observation.

    A reader with no change watch checks every recorded directory to revalidate
    the covered file set, and a build recomputes the same identities as it
    records the new shape, so one pass would otherwise stat a directory once
    per reader that needs it. Inside :func:`discovery_file_scope` the first
    observation is reused; outside one the stat is taken as before.
    """

    scoped = _DISCOVERY_DIRS.get()
    key = os.fspath(path)
    if scoped is not None and key in scoped:
        return scoped[key]
    identity = _dir_identity(path)
    if scoped is not None:
        scoped[key] = identity
    return identity


def _stat_signature(observed: os.stat_result) -> list[int]:
    """Persist a content identity stable across hosts with differing device ids.

    The fields and their order are :func:`file_memo.signature_from_stat`'s. The
    leading device id is dropped, because a shared filesystem reports a
    different one for the same file on each host, so a persisted entry carrying
    it would be invalidated for a reader on any other host.
    """

    return list(signature_from_stat(observed))[1:]


def _same_stat_identity(recorded: object, observed: os.stat_result) -> bool:
    """Whether a persisted signature matches a file's observed stat identity.

    A write to the file moves at least one field of
    :func:`_stat_signature`'s identity, so a match says the file has not been
    written since the row was built and the row can be served without reading
    its bytes. A mismatch leaves the digest to decide, because a rewrite that
    restores the same bytes carries a new identity and must keep its row.
    """

    return recorded == _stat_signature(observed)


#: The types discovery keeps in an inventory, so the index answers with the
#: same row set the derived payload is merged into.
_INDEX_TYPES = frozenset({"plan", "research", "evidence"})
#: The row types a figure file derives, which carry a revision of its bytes.
_FIGURE_TYPES = frozenset({"figure", "gif"})


@dataclass
class IndexBuild:
    """One build's rows plus what it had to recompute to produce them."""

    project: str
    docs_dir: Path
    rows: list[dict] = field(default_factory=list)
    #: One record per plan file, carrying the figures a drain reads from it.
    plans: list[dict] = field(default_factory=list)
    #: Docs-relative paths whose row was recomputed this build.
    rebuilt: list[str] = field(default_factory=list)
    reused: int = 0
    added: int = 0
    removed: int = 0
    #: One discovery state per covered file, keyed by docs-relative path: the
    #: parsed ``<meta>`` record and the readiness state a discovery read needs
    #: beyond it. Persisted with the entry so a fresh process reuses it without
    #: parsing, and rebuilt only for a file whose bytes changed.
    discovery: dict[str, dict] = field(default_factory=dict)
    #: Each visited directory's stat identity, keyed by its docs-relative path.
    #: A later read compares these to decide whether the tree's *shape* — the
    #: set of directories and the files in them — still matches the persisted
    #: rows, so it can answer without walking the tree to rediscover them.
    dir_stamps: dict[str, list[int]] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.rebuilt or self.added or self.removed)


@dataclass(frozen=True)
class TreeChange:
    """The paths one reported change named, as the index can absorb them.

    ``files`` are docs-relative posix paths of covered files a source named,
    and ``dirs`` are docs-relative directories whose entry set moved. An empty
    change is a source saying it has nothing the index covers: the build is
    kept as it is. ``None`` is never a ``TreeChange``: a caller with no
    attributable paths drops the build instead, so the distinction between
    "nothing moved" and "nobody can say" survives to the read.
    """

    files: frozenset[str] = frozenset()
    dirs: frozenset[str] = frozenset()

    def __or__(self, other: TreeChange) -> TreeChange:
        return TreeChange(self.files | other.files, self.dirs | other.dirs)

    def __bool__(self) -> bool:
        return bool(self.files or self.dirs)


_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str, bool], IndexBuild] = {}
#: The paths each tree's next read must absorb, keyed by its resolved root. A
#: change marks the build rather than dropping it, so a change to one covered
#: file costs a read of that file and nothing else.
_MARKS: dict[str, TreeChange] = {}


class _ColdPath:
    """One cache-miss rebuild in progress, which other readers wait on.

    ``done`` is set when the build either stored its result or failed, so a
    reader that found the path in flight can read again through the cache. A
    path that fails sets ``failed`` and carries its ``error``, so the waiting
    readers can read for themselves rather than inheriting the failure.
    ``generation`` is the tree generation the path started against: a drop
    recorded while the path runs moves the tree's generation, which keeps that
    path's build out of the cache.
    """

    __slots__ = ("done", "error", "failed", "generation")

    def __init__(self, generation: int) -> None:
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.failed = False
        self.generation = generation


#: The cache-miss path in flight per cache key — one rebuild at a time per tree
#: and stamp source. A reader that arrives while one runs waits for it instead
#: of sweeping the same directories again.
_INFLIGHT: dict[tuple[str, str, bool], _ColdPath] = {}
#: Each tree's current generation, keyed by its resolved root. A drop moves it,
#: so a build that started before the drop cannot store the rows it read.
_GENERATIONS: dict[str, int] = {}

#: Discovery batches in flight, keyed by (resolved docs dir, project). A batch
#: holds the persisted entries as loaded when it opened; :func:`record_discovery`
#: updates them in memory, and one :func:`flush_discovery` writes the store, so a
#: pass that records hundreds of discoveries encodes and writes the whole store
#: once rather than once per document. The store is one flat JSON blob, so a
#: per-document write re-serialises every entry each time; accumulating the
#: entries and flushing at the end of the pass removes that quadratic cost.
_DISCOVERY_BATCHES: dict[tuple[str, str], dict[str, dict]] = {}


def clear() -> None:
    """Drop every in-process index, as a process restart would."""

    with _LOCK:
        roots = {key[1] for key in _CACHE} | set(_MARKS) | set(_GENERATIONS)
        roots |= {key[1] for key in _INFLIGHT}
        _CACHE.clear()
        _MARKS.clear()
        _MODULE_SOURCE_DIGESTS.clear()
        for root in roots:
            _GENERATIONS[root] = _GENERATIONS.get(root, 0) + 1
    with _PERSISTED_INDEX_LOCK:
        _PERSISTED_INDEX_CACHE.clear()


def invalidate_tree(docs_dir: Path) -> None:
    """Drop the in-process index of the tree the change watch reported.

    This is the fallback for a change no source could attribute: an event
    queue overflow, a commit that cannot be resolved, a rewritten history. The
    next read pays the full sweep, and a cache-miss path already running keeps
    its build out of the cache rather than storing rows read before the drop.
    """

    root = str(Path(docs_dir).resolve())
    with _LOCK:
        _GENERATIONS[root] = _GENERATIONS.get(root, 0) + 1
        for key in [key for key in _CACHE if key[1] == root]:
            del _CACHE[key]
        _MARKS.pop(root, None)


_COVERED_SUFFIXES = (".html", *_FIGURE_SUFFIXES)


def _may_be_covered(name: str) -> bool:
    """Whether a file name could be covered, decided from the name alone.

    The walk visits every entry in a directory and most of them are not
    covered, so it rejects by suffix before it builds a path or resolves one
    against the docs root. The suffixes here are the ones
    :func:`covered_name` accepts, so this is a rejection that can never drop a
    file the rule would keep.
    """

    return name.endswith(_COVERED_SUFFIXES)


def _skips_infra_dir(relative: str) -> bool:
    """Whether a docs-relative directory holds nothing the covered set keeps.

    ``resources.INFRA_DIRS`` names the directories that hold infrastructure
    rather than documents, and the covered set keeps no document from any of
    them — with one exception, the figure root, whose images the index does
    carry rows for. The walk and the revalidation both read the constant, so a
    directory added to it is skipped by both without a second list to maintain.
    """

    return resources.is_infra_path(PurePosixPath(relative).parts, keep=(_FIGURE_DIR,))


def covered_name(relative: str) -> bool:
    """Whether a docs-relative path is one the index builds a row for.

    The covered set is every HTML file anywhere in the tree plus figure images
    under the top-level figures directory, and a source that reports paths uses
    this same rule to decide which of them the index can absorb.
    """

    if not _may_be_covered(relative):
        return False
    if relative.endswith(".html"):
        return True
    parts = PurePosixPath(relative).parts
    return len(parts) > 1 and parts[0] == _FIGURE_DIR


def mark_tree_paths(docs_dir: Path, change: TreeChange) -> None:
    """Record the paths a source named for the next read to absorb.

    A tree with a cached build absorbs the mark on its next read. A tree with
    no cached build absorbs it too while a cache-miss path is in flight: the
    path consumes every mark recorded while it ran before it returns, so a
    change that lands after its walk passed a directory still reaches its rows.
    A mark is dropped when there is neither a cached build nor a path in flight,
    because the next read builds anyway and the persisted shape already tells it
    which directories moved. A change that names nothing covered is not
    recorded, so a write the index does not read costs neither a stat nor a
    rebuild.
    """

    if not change:
        return
    root = str(Path(docs_dir).resolve())
    with _LOCK:
        if not any(key[1] == root for key in _CACHE) and not any(
            key[1] == root for key in _INFLIGHT
        ):
            return
        recorded = _MARKS.get(root)
        _MARKS[root] = change if recorded is None else recorded | change


def marked_covered_changes(docs_dir: Path, project: str) -> TreeChange:
    """Mark the covered files whose stat identity moved since the index was built.

    The cross-node slow sweep already walks and stats every covered file; this
    is the comparison that walk was not making. A file the persisted index does
    not list was added, one it lists and the walk did not reach was removed, and
    one whose stat identity moved was replaced — each is a row the next read
    must rebuild. The state files that sweep also reads are not in the covered
    set and so mark nothing.
    """

    entries = _load_persisted(docs_dir, project)
    if not entries:
        return TreeChange()
    walked = dict(_covered_files_for_project(docs_dir, project))
    files = {
        relative
        for relative, path in walked.items()
        if (entry := entries.get(relative)) is None
        or (observed := covered_stat(docs_dir, project, relative, path)) is None
        or not _same_stat_identity(entry.get("signature"), observed)
    }
    files.update(relative for relative in entries if relative not in walked)
    return TreeChange(files=frozenset(files))


def index_rows(
    docs_dir: Path,
    project: str,
    *,
    repo_dir: Path | None = None,
    git_first: Mapping[str, int] | None = None,
    git_last: Mapping[str, int] | None = None,
    revalidate: bool = False,
) -> list[dict]:
    """Return the project's rows, rebuilding only a tree the watch dropped.

    A caller that supplies ``repo_dir`` and the git commit-time maps gets rows
    whose stamps use the same source the served discovery payload uses, so a
    reader merging the two never sees a document's timestamps move.

    ``revalidate`` checks every covered file against its digest and re-parses
    only the ones whose bytes changed. One process reuses a hash while the
    file's stat identity is unchanged. A caller the change watch serves gets
    that for free through
    :func:`invalidate_tree` and leaves the flag alone; a caller without a watch
    (the MCP and CLI processes) would otherwise answer from the first build it
    made for the life of its process and never see a later edit.
    """

    key = _cache_key(docs_dir, project, repo_dir is not None)
    while True:
        if not revalidate:
            with _LOCK:
                build = _CACHE.get(key)
            if build is not None:
                absorbed = _absorb_marked(
                    docs_dir, project, key, repo_dir, git_first, git_last
                )
                if absorbed is not None:
                    return [dict(row) for row in absorbed.rows]
                # The sources named paths this cached build cannot absorb,
                # which happens when its persisted shape is unusable; the
                # rebuild below is the honest answer to a mark that cannot be
                # applied.
        flight = _join_cold_path(key)
        if flight is None:
            return [
                dict(row)
                for row in _run_cold_path(
                    key,
                    docs_dir,
                    project,
                    repo_dir=repo_dir,
                    git_first=git_first,
                    git_last=git_last,
                ).rows
            ]
        if not _await_flight(flight):
            # The path failed: each waiting reader reads for itself.
            return [
                dict(row)
                for row in _run_cold_path(
                    key,
                    docs_dir,
                    project,
                    repo_dir=repo_dir,
                    git_first=git_first,
                    git_last=git_last,
                ).rows
            ]
        rows = _read_after_flight(key, docs_dir, project, repo_dir, git_first, git_last)
        if rows is not None:
            return rows
        # The path's build never reached the cache, because a drop moved the
        # tree's generation while it ran; read again for ourselves.
        revalidate = False


def _join_cold_path(key: tuple[str, str, bool]) -> _ColdPath | None:
    """Return the cache-miss path in flight for ``key``, or become its owner.

    A reader that finds none open registers one before it sweeps, so every
    later reader of the same key waits rather than repeating the sweep. A
    reader that finds one open gets it back and waits; it never registers a
    second.
    """

    with _LOCK:
        flight = _INFLIGHT.get(key)
        if flight is not None:
            return flight
        _INFLIGHT[key] = _ColdPath(_GENERATIONS.get(key[1], 0))
        return None


def _await_flight(flight: _ColdPath) -> bool:
    """Wait for a cache-miss path; whether its result reached the cache."""

    flight.done.wait()
    return not flight.failed


def _read_after_flight(
    key: tuple[str, str, bool],
    docs_dir: Path,
    project: str,
    repo_dir: Path | None,
    git_first: Mapping[str, int] | None,
    git_last: Mapping[str, int] | None,
) -> list[dict] | None:
    """Read the cache once a waited-for path finished, absorbing its marks.

    A path consumes the marks recorded while it ran before it returns, but a
    change can land between that and this read. Absorbing here is what keeps a
    waiting reader from being handed rows from before that change. ``None``
    means the path left no cached build — a drop moved the tree's generation
    while it ran — so the caller reads for itself.
    """

    with _LOCK:
        build = _CACHE.get(key)
    if build is None:
        return None
    absorbed = _absorb_marked(docs_dir, project, key, repo_dir, git_first, git_last)
    if absorbed is None:
        return None
    return [dict(row) for row in absorbed.rows]


def _run_cold_path(
    key: tuple[str, str, bool],
    docs_dir: Path,
    project: str,
    *,
    repo_dir: Path | None,
    git_first: Mapping[str, int] | None,
    git_last: Mapping[str, int] | None,
) -> IndexBuild:
    """Sweep one tree, store the build, and release every reader waiting on it.

    The build is stored against the generation the path started with, so a drop
    recorded while it ran keeps its result out of the cache. A failing path
    records its error and releases its waiters rather than handing them the
    failure; they read for themselves.
    """

    with _LOCK:
        flight = _INFLIGHT.get(key)
        if flight is None:
            # A reader running its own sweep after a failed path owns no
            # registered flight; it still needs a generation to store against.
            flight = _ColdPath(_GENERATIONS.get(key[1], 0))
    try:
        return _cold_build(
            key,
            docs_dir,
            project,
            generation=flight.generation,
            repo_dir=repo_dir,
            git_first=git_first,
            git_last=git_last,
        )
    except BaseException as exc:
        flight.error = exc
        flight.failed = True
        raise
    finally:
        with _LOCK:
            if _INFLIGHT.get(key) is flight:
                _INFLIGHT.pop(key, None)
        flight.done.set()


def _cold_build(
    key: tuple[str, str, bool],
    docs_dir: Path,
    project: str,
    *,
    generation: int,
    repo_dir: Path | None,
    git_first: Mapping[str, int] | None,
    git_last: Mapping[str, int] | None,
) -> IndexBuild:
    """Build one tree's rows, consuming every mark recorded while it ran.

    The identities :func:`_reuse_covered` read are handed to the build and to
    the shape it records, so the cold path stats each recorded directory once
    rather than once per reader that needs it. A mark recorded while the build
    ran is applied to the rows before they are returned, and the marks are
    taken before the build is published: a reader entering while the path runs
    must not find a build without them, because taking them there would leave
    this path answering from rows written before the change it recorded.
    """

    build: IndexBuild | None = None
    for _attempt in range(2):
        covered: list[tuple[str, Path]] | None = None
        dirs: dict[str, Path] | None = None
        identities: dict[str, list[int]] | None = None
        reused = _reuse_covered(docs_dir, project)
        if reused is not None:
            covered, dirs, identities = reused
        build = build_index(
            docs_dir,
            project,
            repo_dir=repo_dir,
            git_first=git_first,
            git_last=git_last,
            covered=covered,
            dirs=dirs,
            identities=identities,
        )
        # Take the marks first. Storing the build before consuming them lets a
        # reader that enters in the gap answer from, and consume the marks of,
        # a build this path is about to patch, leaving this path's own rows
        # without a change recorded while it ran.
        change = _pop_marks(key[1])
        if change is None:
            _store_cold_build(key, build, generation)
            return build
        patched = _patch_build(
            docs_dir, project, build, change, repo_dir, git_first, git_last
        )
        if patched is None:
            # The marked paths name a shape this build cannot absorb; the marks
            # are consumed, so one clean rebuild is the honest answer.
            continue
        _store_cold_build(key, patched, generation)
        return patched
    assert build is not None
    return build


def _pop_marks(root: str) -> TreeChange | None:
    """Take the marks a tree's sources recorded, consuming them."""

    with _LOCK:
        return _MARKS.pop(root, None)


def _store_cold_build(
    key: tuple[str, str, bool], build: IndexBuild, generation: int
) -> None:
    """Cache a cold path's build unless a drop moved the tree's generation."""

    with _LOCK:
        if _GENERATIONS.get(key[1], 0) == generation:
            _CACHE[key] = build


def _absorb_marked(
    docs_dir: Path,
    project: str,
    key: tuple[str, str, bool],
    repo_dir: Path | None,
    git_first: Mapping[str, int] | None,
    git_last: Mapping[str, int] | None,
) -> IndexBuild | None:
    """Patch every cached build of the tree with the paths its sources marked.

    A build's rows are served without a stat while it is held, so a change to
    one covered file must reach that build before the next read answers from
    it. Every cached variant of the tree is patched, because the marked paths
    belong to the tree rather than to one reader's stamp source; the variant
    being served takes the caller's git maps, and the others rebuild the marked
    rows from their own stat identity.
    """

    root = str(Path(docs_dir).resolve())
    with _LOCK:
        change = _MARKS.pop(root, None)
        builds = {
            cached: build for cached, build in _CACHE.items() if cached[1] == root
        }
    if change is None:
        return builds.get(key)
    patched: dict[tuple[str, str, bool], IndexBuild] = {}
    for cached, build in builds.items():
        if cached == key:
            fresh = _patch_build(
                docs_dir, project, build, change, repo_dir, git_first, git_last
            )
        else:
            fresh = _patch_build(docs_dir, project, build, change, None, None, None)
        if fresh is None:
            return None
        patched[cached] = fresh
    with _LOCK:
        _CACHE.update(patched)
    return patched.get(key)


def _patch_build(
    docs_dir: Path,
    project: str,
    build: IndexBuild,
    change: TreeChange,
    repo_dir: Path | None,
    git_first: Mapping[str, int] | None,
    git_last: Mapping[str, int] | None,
) -> IndexBuild | None:
    """Rebuild only the rows the marked paths reach, reusing every other row.

    Each marked directory is re-scanned, together with any subdirectory it
    gained, and a recorded directory that no longer exists drops its files and
    its descendants. A marked file is restatted and re-parsed, so its stamps
    come from the caller's git maps and a commit that only moves a document's
    commit date reaches its row. A file the re-scan reached whose stat identity
    is unchanged keeps its persisted row without being read, and every file no
    mark reaches is carried over without a stat.
    """

    entries = _load_persisted(docs_dir, project)
    if not entries:
        return None
    stamps = dict(build.dir_stamps) or _load_dir_stamps(docs_dir, project)
    if not stamps:
        return None
    recorded = set(stamps)
    marked = set(change.dirs) & recorded
    # A named file's own directory has moved too: its entry set changed when
    # the file was added, removed or replaced. Re-observing it is what records
    # the shape the next reader starts from.
    for relative in change.files:
        parent = PurePosixPath(relative).parent.as_posix()
        if parent in recorded:
            marked.add(parent)
    observed: set[str] = set()
    removed: set[str] = set()
    for relative in sorted(marked):
        identity = covered_dir_identity(docs_dir / relative)
        observed.add(relative)
        if identity is None:
            removed.add(relative)
            stamps.pop(relative, None)
        else:
            stamps[relative] = identity
    scanned = marked - removed
    # A marked directory under another marked directory is reached by that
    # one's scan; skipping every *unmarked* recorded directory is what lets the
    # scan descend into a marked child and into a subdirectory the tree gained.
    roots = [
        relative
        for relative in sorted(scanned)
        if not any(
            relative.startswith(other + "/") for other in scanned if other != relative
        )
    ]
    walked: list[tuple[str, Path]] = []
    scanned_files: list[tuple[str, Path]] = []
    if roots:
        scanned_files, walked = _walk_covered(
            docs_dir,
            [docs_dir / relative for relative in roots],
            frozenset(recorded - scanned),
        )
    for relative, path in walked:
        if relative in observed:
            continue
        identity = covered_dir_identity(path)
        if identity is not None:
            stamps[relative] = identity
    scanned_dirs = {relative for relative, _path in walked}
    files: dict[str, Path] = {}
    for relative in entries:
        if any(relative == gone or relative.startswith(gone + "/") for gone in removed):
            continue
        files[relative] = docs_dir / relative
    for relative in list(files):
        if PurePosixPath(relative).parent.as_posix() in scanned_dirs:
            del files[relative]
    files.update(scanned_files)

    stamp = _make_stamp(repo_dir, git_first, git_last)
    fresh = IndexBuild(project=project, docs_dir=docs_dir)
    fresh_entries: list[dict] = []
    seen: set[str] = set()
    for relative, path in sorted(files.items()):
        seen.add(relative)
        entry = entries.get(relative)
        reuse = False
        if entry is not None and _row_is_complete(entry):
            if relative in change.files:
                # A named file is read again even when its identity is
                # unchanged: a commit that touched it moves the stamp its row
                # carries, which no stat of the file can show.
                reuse = False
            elif PurePosixPath(relative).parent.as_posix() in scanned_dirs:
                current = covered_stat(docs_dir, project, relative, path)
                reuse = current is not None and _same_stat_identity(
                    entry.get("signature"), current
                )
            else:
                reuse = True
        if reuse:
            _reuse_entry(fresh, fresh_entries, relative, entry)
            continue
        try:
            digest = file_memo.content_digest(path)
        except OSError:
            digest = None
        if (
            relative not in change.files
            and entry is not None
            and digest is not None
            and entry.get("digest") == digest
            and _row_is_complete(entry)
        ):
            _reuse_entry(fresh, fresh_entries, relative, entry)
            continue
        try:
            signature = list(file_signature(path))
        except OSError:
            continue
        if entry is None:
            fresh.added += 1
        else:
            fresh.rebuilt.append(relative)
        fields, plan, discovery = _row_for(
            path, relative, docs_dir, project, signature, stamp
        )
        fresh_entry = {"path": relative, "digest": digest, "signature": signature[1:]}
        if fields is None:
            fresh_entry["skip"] = True
        else:
            fresh_entry["fields"] = fields
            fresh.rows.append(fields)
            if discovery is not None:
                fresh_entry["discovery"] = discovery
                fresh.discovery[relative] = discovery
            if plan is not None:
                if not plan["unreadable"]:
                    fresh_entry["plan"] = plan
                fresh.plans.append({"path": relative, **plan})
        fresh_entries.append(fresh_entry)

    fresh.removed = len(set(entries) - seen)
    fresh.dir_stamps = stamps
    # A re-scan that found no row change still learned the tree's new shape;
    # recording it is what stops the next reader scanning the same directory.
    if fresh.changed or stamps != build.dir_stamps or not entries:
        _store_persisted(docs_dir, project, fresh_entries, stamps)
    return fresh


def _reuse_covered(
    docs_dir: Path, project: str
) -> tuple[list[tuple[str, Path]], dict[str, Path], dict[str, list[int]]] | None:
    """Return the persisted file set, scanning only the directories that moved.

    A reader with no change watch checks every covered file to revalidate, and
    it needs the file set to do that. Rediscovering it walks the tree on every
    read. The persisted index already lists the files, and each recorded
    directory's stat identity says whether an entry was added to or removed
    from it since it was built — so a directory that has not moved keeps its
    persisted files and only the directories whose identity moved are scanned,
    together with any subdirectory found in one that the tree did not have
    before. A recorded directory that no longer exists drops its files and its
    descendants. An absent index returns ``None``, so the caller walks.

    The identities it read are returned with the file set, so a caller that
    goes on to build the index gets these directories' stats from this pass
    rather than statting every one of them again.

    A shape that moved is recorded here, before the caller reaches whatever it
    does with the file set, because the readers that want only the file set
    never build an index: a scan left unrecorded is a scan the next reader
    repeats. Recording it even when no row changed lets the next read start
    from the whole shape rather than scanning the same directory again.
    """

    memo = _REUSE_COVERED.get()
    memo_key = (os.fspath(docs_dir), project)
    if memo is not None and memo_key in memo:
        return memo[memo_key]
    result = _reuse_covered_once(docs_dir, project)
    if memo is not None:
        memo[memo_key] = result
    return result


def _reuse_covered_once(
    docs_dir: Path, project: str
) -> tuple[list[tuple[str, Path]], dict[str, Path], dict[str, list[int]]] | None:
    """Validate one docs tree against its persisted shape, once."""

    stamps = _load_dir_stamps(docs_dir, project)
    entries = _load_persisted(docs_dir, project)
    if not stamps or not entries:
        return None
    by_dir: dict[str, list[str]] = {}
    for relative in entries:
        by_dir.setdefault(PurePosixPath(relative).parent.as_posix(), []).append(
            relative
        )
    covered: dict[str, Path] = {}
    dirs: dict[str, Path] = {}
    moved: list[Path] = []
    identities: dict[str, list[int]] = {}
    for relative, identity in stamps.items():
        if _skips_infra_dir(relative):
            continue
        path = docs_dir / relative
        observed = covered_dir_identity(path)
        if observed is None:
            continue
        identities[relative] = observed
        if observed == identity:
            dirs[relative] = path
            for file_relative in by_dir.get(relative, ()):
                covered[file_relative] = docs_dir / file_relative
            continue
        moved.append(path)
    if moved:
        files, scanned = _walk_covered(docs_dir, moved, frozenset(stamps))
        covered.update(dict(files))
        dirs.update(dict(scanned))
        # Record the scan here rather than leaving it to the build that may
        # follow: a reader that only wants the file set never reaches one, so a
        # scan left unrecorded is a scan the next reader in the same read
        # repeats. The recorded directories keep the identities read above, so
        # only the ones the re-scan reached for the first time are statted.
        identities = _record_covered_shape(
            docs_dir,
            project,
            sorted(covered.items()),
            sorted(dirs.items()),
            identities,
        )
    return sorted(covered.items()), dirs, identities


def plan_derivations(docs_dir: Path, project: str) -> list[dict]:
    """Return one record per live plan file, revalidated by a stat of each.

    The records are what a closure drain reads a plan inventory for: the plan's
    docs-relative path, its open-followup count and its implementable-section
    count, the last of which is ``None`` when the plan carries no valid
    declaration, and ``unreadable`` for a plan file the build could not read.
    An archived plan (one the resource walk keeps out of the live inventory) is
    not returned. A caller that needs the served row fields — slug, href,
    title, stamps — reads :func:`index_rows` instead; these figures are
    deliberately outside ``ROW_FIELDS``.

    Every call checks each covered file against its digest, so a caller in a
    long-lived process sees a later edit. An unchanged stat identity reuses the
    hash, and only a file whose bytes changed is parsed again.
    """

    build = _build_and_cache(
        _cache_key(docs_dir, project, False), docs_dir, project, repo_dir=None
    )
    return [
        {
            "path": record["path"],
            "open_followups": record["open_followups"],
            "implementable_sections": record["implementable_sections"],
            "unreadable": record["unreadable"],
        }
        for record in build.plans
        if not record["archived"]
    ]


def _build_and_cache(
    key: tuple[str, str, bool],
    docs_dir: Path,
    project: str,
    *,
    repo_dir: Path | None = None,
    git_first: Mapping[str, int] | None = None,
    git_last: Mapping[str, int] | None = None,
    covered: list[tuple[str, Path]] | None = None,
    dirs: dict[str, Path] | None = None,
) -> IndexBuild:
    build = build_index(
        docs_dir,
        project,
        repo_dir=repo_dir,
        git_first=git_first,
        git_last=git_last,
        covered=covered,
        dirs=dirs,
    )
    with _LOCK:
        _CACHE[key] = build
    return build


def _reuse_entry(
    build: IndexBuild, entries: list[dict], relative: str, entry: Mapping
) -> None:
    """Carry a persisted entry's row, discovery state and plan record forward.

    The entry is kept as it stands and copied into the build, so a reused file's
    row reaches the caller without its bytes being read or parsed.
    """

    build.reused += 1
    entries.append(entry)
    if "fields" in entry:
        build.rows.append(dict(entry["fields"]))
    if isinstance(entry.get("discovery"), Mapping):
        build.discovery[relative] = entry["discovery"]
    if isinstance(entry.get("plan"), Mapping):
        build.plans.append({"path": relative, **entry["plan"]})


def build_index(
    docs_dir: Path,
    project: str,
    *,
    repo_dir: Path | None = None,
    git_first: Mapping[str, int] | None = None,
    git_last: Mapping[str, int] | None = None,
    covered: list[tuple[str, Path]] | None = None,
    dirs: dict[str, Path] | None = None,
    identities: Mapping[str, list[int]] | None = None,
) -> IndexBuild:
    """Build one project's rows, re-parsing only what its bytes changed.

    ``covered`` and ``dirs`` let a caller that has already established the
    tree's shape — by comparing the recorded directory identities — hand the
    file set in instead of walking the tree to rediscover it. With ``covered``
    absent the walk runs and both are derived from it. ``identities`` carries
    the directory stats the caller's shape check already read, so the build
    records their shape from those and stats only a directory it has not seen.

    A persisted row whose recorded stat identity still matches the file is
    reused without reading the file, so the first build after a restart costs a
    stat per file rather than a read and a hash of every one. A file whose
    identity moved takes the digest path, which decides from the bytes: a
    rewrite that restored the same content keeps its row, and any byte change
    is parsed again.
    """

    docs_dir = Path(docs_dir)
    stamp = _make_stamp(repo_dir, git_first, git_last)
    known = _load_persisted(docs_dir, project)
    if covered is None:
        files, walked_dirs = _walk_covered(docs_dir)
        dirs = dict(walked_dirs)
    else:
        files = covered
        dirs = dirs or {}
    build = IndexBuild(project=project, docs_dir=docs_dir)
    for relative, path in dirs.items():
        identity = identities.get(relative) if identities is not None else None
        if identity is None:
            identity = covered_dir_identity(path)
        if identity is not None:
            build.dir_stamps[relative] = identity
    entries: list[dict] = []
    seen: set[str] = set()

    for relative, path in files:
        seen.add(relative)
        entry = known.get(relative)
        if entry is not None and _row_is_complete(entry):
            observed = covered_stat(docs_dir, project, relative, path)
            if observed is not None and _same_stat_identity(
                entry.get("signature"), observed
            ):
                _reuse_entry(build, entries, relative, entry)
                continue
        try:
            digest = file_memo.content_digest(path)
        except OSError:
            # A file that cannot be read is still a row, built below with its
            # parse left unknown; a vanished one drops out and the next build
            # sees whatever replaced it.
            digest = None
        if (
            entry is not None
            and digest is not None
            and entry.get("digest") == digest
            and _row_is_complete(entry)
        ):
            _reuse_entry(build, entries, relative, entry)
            continue
        try:
            signature = list(file_signature(path))
        except OSError:
            # The file went away between the hash and this stat; the next build
            # sees whatever replaced it.
            continue
        if entry is None:
            build.added += 1
        else:
            build.rebuilt.append(relative)
        fields, plan, discovery = _row_for(
            path, relative, docs_dir, project, signature, stamp
        )
        entry = {"path": relative, "digest": digest, "signature": signature[1:]}
        if fields is None:
            entry["skip"] = True
        else:
            entry["fields"] = fields
            build.rows.append(fields)
            if discovery is not None:
                entry["discovery"] = discovery
                build.discovery[relative] = discovery
            if plan is not None:
                # A plan the build could not read keeps no record, so the next
                # call reads it again rather than reusing a row that never had
                # one; see _row_is_complete.
                if not plan["unreadable"]:
                    entry["plan"] = plan
                build.plans.append({"path": relative, **plan})
        entries.append(entry)

    build.removed = len(set(known) - seen)
    if build.changed or not known:
        _store_persisted(docs_dir, project, entries, build.dir_stamps)
    return build


def _row_is_complete(entry: Mapping) -> bool:
    """Whether a persisted entry carries everything its file type derives.

    A plan row is written without its plan record when the build could not read
    the file, so the row is rebuilt on the next call: a drain then reports the
    inventory unknown, as it does for a plan it cannot read, instead of
    answering from a row whose figures were never derived. An entry for a
    document discovery inventories must also carry its discovery state — the
    parsed head record and readiness — so a reader serving a discovery read from
    the index never reuses an entry written before that state was kept. A figure
    entry likewise carries the revision of its bytes, so an index written before
    the field existed is rebuilt rather than served with the row missing it.
    """

    fields = entry.get("fields")
    if not isinstance(fields, Mapping):
        return True
    if fields.get("type") in _INDEX_TYPES and not isinstance(
        entry.get("discovery"), Mapping
    ):
        return False
    if fields.get("type") in _FIGURE_TYPES:
        return "revision" in fields
    if fields.get("type") != "plan":
        return True
    return isinstance(entry.get("plan"), Mapping)


def _cache_key(docs_dir: Path, project: str, with_git: bool) -> tuple[str, str, bool]:
    return (project, str(Path(docs_dir).resolve()), with_git)


def _walk_covered(
    docs_dir: Path,
    roots: list[Path] | None = None,
    skip_subtrees: frozenset[str] = frozenset(),
) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    """Return the covered files and every visited directory, from one walk.

    The files are ``(docs-relative posix path, path)`` for every file
    :func:`covered_name` accepts, so the walk and the sources that report paths
    decide coverage by one rule. The directories are ``(docs-relative posix
    path, path)`` for
    every directory the walk reached, including ones that held no covered
    file: a directory's stat identity moves when an entry is added to or
    removed from it, so recording the set lets a later read tell whether the
    tree's shape changed without walking it again. Directory symlinks are not
    followed.

    ``roots`` starts the walk below ``docs_dir`` instead of at it, so a caller
    that already knows which directories moved reads only those. It is
    accompanied by ``skip_subtrees``, the docs-relative directories not to
    descend into: a directory the tree already had keeps its own identity
    check, so a rescan leaves it to that check rather than reading it twice,
    while a subdirectory the walk does descend into is one the tree did not
    have before and is read in full.
    """

    found: dict[str, Path] = {}
    directories: dict[str, Path] = {}
    pending = [os.fspath(root) for root in (roots or [docs_dir])]
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        try:
            relative_dir = Path(directory).relative_to(docs_dir).as_posix()
        except ValueError:
            continue
        directories[relative_dir] = Path(directory)
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        # The infra rule is decided from the directory's name
                        # and its parent's relative path, so a rejected
                        # directory costs no resolved path.
                        if _skips_infra_dir(
                            (PurePosixPath(relative_dir) / entry.name).as_posix()
                        ):
                            continue
                        try:
                            child = Path(entry.path).relative_to(docs_dir).as_posix()
                        except ValueError:
                            continue
                        if child in skip_subtrees:
                            continue
                        pending.append(entry.path)
                        continue
                except OSError:
                    continue
                if not _may_be_covered(entry.name):
                    continue
                path = Path(entry.path)
                try:
                    relative = path.relative_to(docs_dir).as_posix()
                except ValueError:
                    continue
                if not covered_name(relative):
                    continue
                found[relative] = path
    return sorted(found.items()), sorted(directories.items())


def _covered_files(docs_dir: Path) -> list[tuple[str, Path]]:
    """Return (docs-relative posix path, path) for every file the index covers.

    Every HTML file anywhere in the tree, plus figure images under the
    top-level figures directory. Directory symlinks are not followed. This is
    the single docs-tree walk: the discovery change signature sweeps the same
    function, so the index's covered set and discovery's counted set cannot
    drift into two implementations.

    The index records each visited directory's identity, so a tree whose shape
    is unchanged answers from the persisted file set without walking it. A tree
    whose shape moved, or one the index has not seen, walks; the walk's set and
    directory identities are recorded, so the next read skips the walk. A
    caller that already knows the project supplies it through
    :func:`_covered_files_for_project`, including for an unmounted checkout.
    """

    scoped = _COVERED_PROJECT.get()
    project = (
        scoped[1]
        if scoped is not None and scoped[0] == os.fspath(docs_dir)
        else _docs_dir_project(docs_dir)
    )
    if project is not None:
        reused = _reuse_covered(docs_dir, project)
        if reused is not None:
            return reused[0]
    files, dirs = _walk_covered(docs_dir)
    if project is not None:
        _record_covered_shape(docs_dir, project, files, dirs)
    return files


def _covered_files_for_project(docs_dir: Path, project: str) -> list[tuple[str, Path]]:
    """Use a known project while keeping the tree walk's one-argument entry."""

    scoped = _DISCOVERY_FILES.get()
    key = (os.fspath(docs_dir), project)
    if scoped is not None and key in scoped:
        files = scoped[key][0]
        return _resource_candidates(files) if _RESOURCE_CANDIDATES.get() else files
    token = _COVERED_PROJECT.set((os.fspath(docs_dir), project))
    try:
        files = _covered_files(docs_dir)
    finally:
        _COVERED_PROJECT.reset(token)
    if scoped is not None:
        scoped[key] = (files, {})
    return files


def _docs_dir_project(docs_dir: Path) -> str | None:
    """Return the project a docs tree is mounted as, from the mounts table.

    The discovery change signature sweeps :func:`_covered_files`, which is
    shared with the index and takes only the tree. Resolving the project from
    the mount that names it lets that reader reach the index's persisted
    walk-skip without a second directory-identity map. A tree no mount names —
    a dispatch worktree, an unregistered checkout — returns ``None`` and the
    reader walks as it always did.
    """

    from reckon._store import _mounts_path

    try:
        raw = json.loads(_mounts_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        target = str(Path(docs_dir).resolve())
    except OSError:
        return None
    for project in sorted(raw):
        path = raw[project]
        if not isinstance(project, str) or not isinstance(path, str):
            continue
        try:
            if str(Path(path).expanduser().resolve()) == target:
                return project
        except OSError:
            continue
    return None


def _record_covered_shape(
    docs_dir: Path,
    project: str,
    files: list[tuple[str, Path]],
    dirs: list[tuple[str, Path]],
    identities: Mapping[str, list[int]] | None = None,
) -> dict[str, list[int]]:
    """Persist the file set and directory identities one walk observed.

    Each covered file keeps the entry the index already holds for it, so a
    digest and the fields derived from it survive; a file the index has not
    seen keeps only its path and is parsed by the next index build. A file that
    left the tree drops out. Recording the shape keeps the walk-skip reusable
    without the parse a full index build performs.

    ``identities`` carries the stats a caller has already read, keyed by
    docs-relative directory. A directory it covers is recorded from that
    reading; only one the re-scan reached for the first time is statted here.
    The full set is returned, so a caller that goes on to build gets its
    directories' identities without reading them again.
    """

    known = _load_persisted(docs_dir, project)
    entries = [known.get(relative) or {"path": relative} for relative, _path in files]
    stamps: dict[str, list[int]] = {}
    for relative, path in dirs:
        identity = identities.get(relative) if identities is not None else None
        if identity is None:
            identity = covered_dir_identity(path)
        if identity is not None:
            stamps[relative] = identity
    _store_persisted(docs_dir, project, entries, stamps)
    return stamps


def _dir_identity(path: Path) -> list[int] | None:
    """Return the stat identity a directory's entry set changes with.

    mtime and ctime move when an entry is added to or removed from the
    directory, and the inode and size distinguish a replaced directory from a
    reused name. A reader that finds every recorded directory unchanged knows
    the file set is unchanged too and can skip the walk that would rediscover
    it; any difference falls back to the walk.
    """

    try:
        stat = path.stat()
    except OSError:
        return None
    return [
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
        int(stat.st_ino),
        int(stat.st_size),
    ]


def _row_for(
    path: Path,
    relative: str,
    docs_dir: Path,
    project: str,
    signature: list[int],
    stamp: StampFn,
) -> tuple[dict | None, dict | None, dict | None]:
    """Return one file's (row, plan record, discovery state), each None if absent.

    The plan record holds the figures a row cannot take from a ``<meta>`` head
    — the open-followup and implementable-section counts — and is present only
    for a plan the resource walk can identify. The discovery state is the full
    ``<meta>`` record and readiness a discovery read needs, present only for a
    document discovery inventories; it is what lets a fresh process answer a
    discovery read from the persisted index without parsing the file again.
    """

    if path.suffix in _FIGURE_SUFFIXES:
        row = _figure_index_row(path, docs_dir, project, signature, stamp)
        if row is None:
            return None, None, None
        # A figure row is derived whole by ``figures``; the index stores and
        # serves its declared shape, so the narrowing happens here, at the
        # boundary, rather than inside the builder of the row.
        return {field: row[field] for field in ROW_FIELDS if field in row}, None, None
    return _resource_row(path, docs_dir, project, signature, stamp)


def _resource_row(
    path: Path,
    docs_dir: Path,
    project: str,
    signature: list[int],
    stamp: StampFn,
) -> tuple[dict | None, dict | None, dict | None]:
    try:
        resource = resources.identify_resource(docs_dir, path, project)
    except resources.ResourceCollision:
        return None, None, None
    if resource is None or resource.type not in _INDEX_TYPES:
        return None, None, None
    rec = _plan_html.parse_meta(path)
    href = str(
        (
            resource.relative_path
            if resource.legacy
            else resource.canonical_relative_path
        ).with_suffix("")
    )
    created, edited = stamp(path, signature)
    status = (rec.get("status") or "") if resource.type == "plan" else ""
    row = {
        "slug": resource.slug,
        "href": href,
        "type": resource.type,
        "title": rec["title"],
        "status": status,
        "sprint": rec.get("sprint") or None,
        "archived": rec.get("archived") or ("1" if resource.archived else ""),
        "created": created,
        "edited": edited,
        "width": None,
        "height": None,
    }
    if resource.type == "plan":
        # The plan's progress figure rides the row so a page painting from the
        # index draws the bar without waiting for discovery. It comes from the
        # same parse the discovery payload reads, so the two rows agree.
        row["impl"] = rec["impl"]
    plan = _plan_record(path, resource.archived) if resource.type == "plan" else None
    discovery = {"meta": rec, "state": None, "readiness": None}
    try:
        state, text = _plan_html.read_state_and_text_file(path)
    except ValueError:
        pass
    else:
        discovery["state"] = state
        discovery["readiness"] = readiness_from_state(state, text)
    return row, plan, discovery


def _plan_record(path: Path, archived: bool) -> dict:
    """Return the derived figures one plan row carries beyond its meta fields.

    ``open_followups`` counts the followups a parsed read reports as anything
    other than resolved; ``implementable_sections`` is the declared remainder
    itself, ``None`` when the plan carries no valid declaration. ``archived``
    is the resource walk's path-based marker, so a drain that reads these
    records excludes the same plans the walk does.

    A file that cannot be read yields ``unreadable`` and no figure: a counted
    zero is indistinguishable from a plan that carries no open work, and the
    plan inventory as a whole is unknown rather than shorter. The figures
    derive from the shared per-file parse, so a plan another reader has already
    parsed is not parsed again; a readability read refuses an unreadable file
    first, because the shared parse reports one as an empty document — exactly
    the reading this record exists to refuse.
    """

    from reckon._schema import plan_executable_remainder

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return _unreadable_record(archived)
    try:
        state, shared_text = _plan_html.read_state_and_text_file(path)
        if shared_text != text:
            # The shared text is a read taken while the file was unreadable, so
            # it holds an empty document rather than this file's bytes; derive
            # the figures from the bytes just read instead of reporting an
            # empty plan.
            state = _plan_html.read_state(text)
    except ValueError:
        return _unreadable_record(archived)
    followups = state.get("followups") or []
    return {
        "archived": archived,
        "open_followups": sum(
            1
            for followup in followups
            if str(followup.get("status") or "") != "resolved"
        ),
        "implementable_sections": plan_executable_remainder(state),
        "unreadable": False,
    }


def readiness_from_state(state: Mapping, text: str) -> list:
    """Return the readiness a discovery row carries, from a parsed state.

    The named gates, the decision rows, the followups and the section
    declarations — the body state a roadmap explains readiness from, beyond a
    document's ``<meta>`` head. The gate list is drawn only when the document
    declares a gates section, matching the direct read.
    """

    has_gates = 'data-reckon="gates"' in text or "data-reckon='gates'" in text
    gates = list(state.get("gates") or []) if has_gates else []
    decisions = state.get("decisions") or {}
    decision_rows = [
        {"key": key, **(decision if isinstance(decision, dict) else {})}
        for key, decision in decisions.items()
    ]
    followups = list(state.get("followups") or [])
    declarations = (
        state.get("section_declarations")
        if isinstance(state.get("section_declarations"), dict)
        else {}
    )
    return [gates, decision_rows, followups, declarations]


def discovery_state(
    docs_dir: Path, project: str, *, build: bool = True
) -> dict[str, dict]:
    """Return each file's persisted discovery state whose bytes are unchanged.

    A discovery read needs, per file, the ``<meta>`` record and the parsed
    semantic state its inventory row carries. Both are parsed once, when the
    file's bytes first appear, and kept in the persisted metadata index beside
    its row and digest. This returns them, keyed by docs-relative path, for
    every file whose digest still matches the one recorded with it — so a fresh
    process answers a discovery read without parsing the tree it has already
    parsed.

    An entry whose digest moved is left out, and the caller parses that file and
    hands it to :func:`record_discovery`; an absent or unusable store is
    re-derived by a full build rather than read as a stale answer. A file the
    store does not cover is left out too, so the caller parses it rather than
    inventing an entry.

    The store is read afresh on every call rather than served from the in-process
    index cache: a caller that edits a plan and reads discovery again in the same
    process reports nothing to a change watch, so that cache still holds a parse
    of the file as it was. The digest check is what makes an entry current, and it
    runs only over the persisted entries.

    With ``build`` false an absent or unusable store answers empty rather than
    being rebuilt: a caller that seeds one document's parse from the store must
    not make a single read pay for parsing the whole tree.
    """

    key = _cache_key(docs_dir, project, False)
    known = _load_persisted(docs_dir, project)
    if not known:
        if not build:
            return {}
        built = _build_and_cache(key, docs_dir, project, repo_dir=None)
        return {relative: dict(entry) for relative, entry in built.discovery.items()}
    fresh: dict[str, dict] = {}
    changed_signatures = False
    for relative, entry in known.items():
        discovery = entry.get("discovery")
        if not isinstance(discovery, Mapping):
            continue
        path = docs_dir / relative
        observed = covered_stat(docs_dir, project, relative, path)
        if observed is None:
            continue
        signature = _stat_signature(observed)
        if entry.get("signature") == signature:
            fresh[relative] = dict(discovery)
            continue
        try:
            digest = file_memo.content_digest(path)
        except OSError:
            continue
        if entry.get("digest") == digest:
            fresh[relative] = dict(discovery)
            # Replace the entry so the shared persisted envelope stays read-only.
            known[relative] = {**entry, "signature": signature}
            changed_signatures = True
    if changed_signatures:
        _store_persisted(
            docs_dir, project, list(known.values()), _load_dir_stamps(docs_dir, project)
        )
    return fresh


def _batch_key(docs_dir: Path, project: str) -> tuple[str, str]:
    return (str(Path(docs_dir).resolve()), project)


def begin_discovery(docs_dir: Path, project: str) -> None:
    """Open a discovery batch: recorded entries accumulate, one flush writes.

    The batch holds the store as loaded now, so a pass that records a parse for
    each file whose digest moved updates that snapshot in memory and
    :func:`flush_discovery` writes it once when the pass ends. Opening a batch
    for a project that already has one replaces it rather than nesting.
    """

    docs_dir = Path(docs_dir)
    _DISCOVERY_BATCHES[_batch_key(docs_dir, project)] = _load_persisted(
        docs_dir, project
    )


def flush_discovery(docs_dir: Path, project: str) -> None:
    """Write the store once for everything the batch recorded; a no-op without one.

    The entries are the store as loaded when the batch opened, with each
    recorded parse written back under its file's new digest. A file left out of
    the store — one it does not cover — stays out, and the directory stamps are
    re-read here so a walk that ran during the pass is preserved.
    """

    docs_dir = Path(docs_dir)
    known = _DISCOVERY_BATCHES.pop(_batch_key(docs_dir, project), None)
    if known is None:
        return
    _store_persisted(
        docs_dir, project, list(known.values()), _load_dir_stamps(docs_dir, project)
    )


def record_discovery(docs_dir: Path, project: str, relative: str) -> None:
    """Write a file the caller has just read into the persisted index.

    A discovery read parses a file whose persisted entry its digest no longer
    matches, and this puts the parse that read made back under the file's new
    digest, so the next process reuses it instead of parsing it again. The
    caller has already read the file, so the entry is derived from the shared
    per-file parse rather than a second read. A path the store does not cover,
    or one whose digest already matches, is left alone: the file set and the
    directory stamps are the full build's to record.

    Inside a :func:`begin_discovery` batch the entry is accumulated and the
    write is deferred to :func:`flush_discovery`, so a pass that records one
    parse per moved file writes the whole store once instead of once per
    document. With no batch open the entry is written immediately, so a caller
    outside a pass still persists its parse at once.
    """

    docs_dir = Path(docs_dir)
    path = docs_dir / relative
    batch = _DISCOVERY_BATCHES.get(_batch_key(docs_dir, project))
    known = batch if batch is not None else _load_persisted(docs_dir, project)
    if relative not in known:
        return
    try:
        digest = file_memo.content_digest(path)
        if known[relative].get("digest") == digest:
            return
        signature = list(file_signature(path))
    except OSError:
        return
    fields, plan, discovery = _row_for(
        path,
        relative,
        docs_dir,
        project,
        signature,
        _make_stamp(None, None, None),
    )
    entry: dict = {"path": relative, "digest": digest, "signature": signature[1:]}
    if fields is None:
        entry["skip"] = True
    else:
        entry["fields"] = fields
        if discovery is not None:
            entry["discovery"] = discovery
        if plan is not None and not plan["unreadable"]:
            entry["plan"] = plan
    known[relative] = entry
    if batch is None:
        _store_persisted(
            docs_dir, project, list(known.values()), _load_dir_stamps(docs_dir, project)
        )


def _unreadable_record(archived: bool) -> dict:
    """Return the record a plan file that cannot be read contributes.

    No figure is reported and the entry keeps no plan record, so the next build
    reads the file again rather than answering unknown for as long as the index
    lives.
    """

    return {
        "archived": archived,
        "open_followups": None,
        "implementable_sections": None,
        "unreadable": True,
    }


def _figure_revision(path: Path) -> str:
    """Return the token that addresses this figure's bytes, or ``""``.

    The revision lets a page's first paint build a URL the server answers
    immutable from the row it already holds rather than one it must ask for. An
    unreadable file carries none, and its URL falls back to revalidation.
    """

    try:
        return file_revision(path.stat())
    except OSError:
        return ""


def _figure_index_row(
    path: Path,
    docs_dir: Path,
    project: str,
    signature: list[int],
    stamp: StampFn,
) -> dict | None:
    """Return one figure's index row: the figures row plus the index-only fields.

    :func:`reckon.figures.figure_row` owns every field a figure row derives from
    its path — the same builder the Figures inventory reads — so the index takes
    that row whole rather than rebuilding it and adds only what the index alone
    carries: the file's ``created`` and ``edited`` stamps, its ``revision``, and
    the empty ``status``, ``sprint`` and ``archived`` a figure row serves. What
    the index endpoint serves is :data:`ROW_FIELDS`, applied where the row is
    stored.
    """

    row = figures.figure_row(path, docs_dir, project)
    if row is None:
        return None
    created, edited = stamp(path, signature)
    row.update(
        {
            "status": "",
            "sprint": None,
            "archived": "",
            "created": created,
            "edited": edited,
            "revision": _figure_revision(path),
        }
    )
    return row


def _stat_stamps(path: Path, signature: list[int]) -> tuple[int, str]:
    """Return (created_unix_ts, edited_iso) from the file's own stat identity."""

    ctime_ns, mtime_ns = signature[4], signature[3]
    created = int(ctime_ns // 1_000_000_000)
    edited_ts = max(int(mtime_ns // 1_000_000_000), created)
    edited = datetime.fromtimestamp(edited_ts).isoformat(  # noqa: DTZ006
        timespec="seconds"
    )
    return created, edited


def stamps_for(
    path: Path,
    repo_dir: Path,
    git_first: Mapping[str, int],
    git_last: Mapping[str, int],
) -> tuple[int, str]:
    """Return (created_unix_ts, edited_iso) for one file by the discovery rule.

    ``created`` is the file's first commit time when it is tracked, falling back
    to its birth time (or ctime). ``edited`` is its last commit time, replaced by
    its working-tree mtime when the file is modified since that commit or
    untracked, and never earlier than ``created``. Both the served discovery
    payload and the persisted index derive their stamps here, so the two
    payloads cannot disagree on one document's timestamps.
    """

    stat = path.stat()
    try:
        rel = str(path.relative_to(repo_dir))
    except ValueError:
        rel = ""
    created = git_first.get(rel) or int(
        getattr(stat, "st_birthtime", None) or stat.st_ctime
    )
    last_commit = git_last.get(rel)
    mtime = int(stat.st_mtime)
    edited_ts = mtime if last_commit is None or mtime > last_commit else last_commit
    edited_ts = max(edited_ts, created)
    edited = datetime.fromtimestamp(edited_ts).isoformat(  # noqa: DTZ006
        timespec="seconds"
    )
    return created, edited


def _make_stamp(
    repo_dir: Path | None,
    git_first: Mapping[str, int] | None,
    git_last: Mapping[str, int] | None,
) -> StampFn:
    """Return the stamp source for a build.

    Without a repository the row's stamps come from its stat identity alone.
    With one, they follow the discovery rule so the index's timestamps match
    the ones the served payload merges over them.
    """

    if repo_dir is None:
        return _stat_stamps
    first = git_first or {}
    last = git_last or {}

    def stamp(path: Path, signature: list[int]) -> tuple[int, str]:
        return stamps_for(path, repo_dir, first, last)

    return stamp


def _persisted_cache_path(kind: str, version: int, *identity: str) -> Path:
    """Keep each persisted cache format in its own file."""

    digest = hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()
    return _config_home() / "cache" / kind / f"{digest}-v{version}.json"


#: Returned by :func:`persisted_memo` when nothing usable is stored and no
#: computation was asked for. A sentinel rather than ``None`` so a stored
#: ``null`` is a value like any other.
_PERSISTED_MISSING: object = object()

#: The envelope a persisted memo carries beside its value.
_PERSISTED_SCHEMA = "reckon.persisted-memo"

#: Source digests already known for a module's file, keyed by the file's path
#: and stat identity. A digest is a function of the file's bytes, so an
#: identity that has not moved answers the same digest: a read that revalidates
#: a persisted value states each producer's digest by comparing the source
#: file's identity against the one the value was stored with, rather than
#: hashing unchanged sources again. Keyed on the identity rather than the path
#: alone, so a source file that moves misses the entry and is hashed afresh.
#: :func:`clear` drops it with the other caches; the next persisted read seeds
#: it again from the store it validates.
_MODULE_SOURCE_DIGESTS: dict[tuple[str, tuple[int, ...]], str] = {}


@dataclass(frozen=True)
class _PersistedIndexPayload:
    path: Path
    signature: tuple[int, ...]
    source_bytes: int
    value: object


# Large indexes share one immutable envelope per tree. Source-byte accounting
# uses the stat already needed for invalidation, keeping hits independent of
# payload size; the LRU bounds both the number and total source size retained.
_MAX_PERSISTED_INDEXES = 32
_MAX_PERSISTED_INDEX_BYTES = 128 * 1024 * 1024
_PERSISTED_INDEX_LOCK = threading.Lock()
_PERSISTED_INDEX_CACHE: OrderedDict[tuple[str, str], _PersistedIndexPayload] = (
    OrderedDict()
)


def _read_index_envelope(path: Path, key: tuple[str, str]) -> object:
    """Share a read-only envelope, replacing a tree's previous file revision."""

    observed = path.stat()
    signature = signature_from_stat(observed)
    with _PERSISTED_INDEX_LOCK:
        previous = _PERSISTED_INDEX_CACHE.get(key)
        if (
            previous is not None
            and previous.path == path
            and previous.signature == signature
        ):
            _PERSISTED_INDEX_CACHE.move_to_end(key)
            return previous.value
    raw = file_memo._read_only(json.loads(path.read_text(encoding="utf-8")))
    if signature_from_stat(path.stat()) == signature:
        with _PERSISTED_INDEX_LOCK:
            # A later reader may have installed a newer envelope while this
            # one decoded; it keeps that entry rather than overwriting it.
            if _PERSISTED_INDEX_CACHE.get(key) is previous:
                _PERSISTED_INDEX_CACHE.pop(key, None)
                if observed.st_size <= _MAX_PERSISTED_INDEX_BYTES:
                    _PERSISTED_INDEX_CACHE[key] = _PersistedIndexPayload(
                        path, signature, observed.st_size, raw
                    )
                while (
                    len(_PERSISTED_INDEX_CACHE) > _MAX_PERSISTED_INDEXES
                    or sum(
                        item.source_bytes for item in _PERSISTED_INDEX_CACHE.values()
                    )
                    > _MAX_PERSISTED_INDEX_BYTES
                ):
                    _PERSISTED_INDEX_CACHE.popitem(last=False)
    return raw


def _module_source_path(module: object) -> Path | None:
    source = getattr(module, "__file__", None)
    return Path(source) if source else None


def _module_source_identity(module: object) -> list[int] | None:
    """Return a module's source file identity, or ``None`` when unreadable."""

    path = _module_source_path(module)
    if path is None:
        return None
    try:
        return _stat_signature(path.stat())
    except OSError:
        return None


def _module_source_digest(module: object) -> str:
    """Return the byte digest of the source file that defines ``module``.

    A persisted value is a reuse of the code that produced it, so the digest of
    that code rides beside it and a source change discards the value. A module
    with no source file digests as empty, which still compares equal to itself.

    A digest already known for the file's current stat identity is returned
    without reading it, so the digest a stored value was validated against is
    the one reused rather than a second hash of the same unchanged source.
    """

    path = _module_source_path(module)
    if path is None:
        return ""
    identity = _module_source_identity(module)
    key = (os.fspath(path), tuple(identity)) if identity is not None else None
    if key is not None:
        held = _MODULE_SOURCE_DIGESTS.get(key)
        if held is not None:
            return held
    try:
        digest = file_memo.content_digest(path)
    except OSError:
        return ""
    if key is not None:
        _MODULE_SOURCE_DIGESTS[key] = digest
    return digest


def persisted_memo(
    kind: str,
    version: int,
    identity: Sequence[str],
    modules: Sequence[object],
    *,
    compute: Callable[[], object] | None = None,
    store: object = _PERSISTED_MISSING,
) -> object:
    """Return a value persisted for ``identity``, guarded by its producers' sources.

    One entry per identity, in the cache file :func:`_persisted_cache_path`
    names, carrying the value together with the source digest of every module
    that produced it, so a value is only ever reused by the code that made it:
    an entry recorded by an older parser is treated as absent rather than read
    as a stale answer.

    With ``compute`` the value is derived, persisted and returned when no usable
    entry is stored. With ``store`` the given value is persisted and returned.
    With neither, the stored value is returned, or :data:`_PERSISTED_MISSING`
    when none is usable — a caller that must not build what it seeds from takes
    this form.
    """

    producers = {str(getattr(module, "__name__", module)): module for module in modules}
    path = _persisted_cache_path(kind, version, *identity)
    if store is not _PERSISTED_MISSING:
        sources = {
            name: _module_source_digest(module) for name, module in producers.items()
        }
        _write_persisted_memo(path, sources, _module_source_stats(producers), store)
        return store
    held = _read_persisted_memo(path, producers)
    if held is not _PERSISTED_MISSING:
        return held
    if compute is None:
        return _PERSISTED_MISSING
    sources = {
        name: _module_source_digest(module) for name, module in producers.items()
    }
    value = compute()
    _write_persisted_memo(path, sources, _module_source_stats(producers), value)
    return value


def _module_source_stats(
    producers: Mapping[str, object],
) -> dict[str, list[int] | None]:
    """Record each producer's source identity beside the digest it produced."""

    return {name: _module_source_identity(module) for name, module in producers.items()}


def _read_persisted_memo(
    path: Path,
    producers: Mapping[str, object],
    *,
    index_key: tuple[str, str] | None = None,
) -> object:
    """Return a stored value whose producer sources still match, else absent.

    A stored entry carries each producer's source identity as well as its
    digest. When a source file's identity has not moved, the digest recorded
    for it still describes the file, so it is reused as the file's digest
    rather than the file being hashed again — a restart revalidates its stored
    value by stat identity. A source that has moved is digested, and a value
    from older code is absent either way.

    Large index envelopes are shared read-only, with retention bounded per
    tree and across trees. Other memo values receive independent mutable
    copies. Producer digests are validated on every call against the payload.
    """

    try:
        raw = (
            _read_index_envelope(path, index_key)
            if index_key is not None
            else file_memo.memoized(
                "persisted_memo_payload",
                path,
                lambda: json.loads(path.read_text(encoding="utf-8")),
                mutable=True,
            )
        )
    except FileNotFoundError:
        return _PERSISTED_MISSING
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable persisted memo %s: %s", path, exc)
        return _PERSISTED_MISSING
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != _PERSISTED_SCHEMA
        or not isinstance(raw.get("sources"), dict)
        or "value" not in raw
    ):
        return _PERSISTED_MISSING
    recorded = raw["sources"]
    stats = raw.get("source_stats")
    sources = {
        name: _module_digest_for(module, name, recorded, stats)
        for name, module in producers.items()
    }
    if recorded != sources:
        return _PERSISTED_MISSING
    return raw["value"]


def _module_digest_for(
    module: object,
    name: str,
    recorded: Mapping[str, str],
    stats: object,
) -> str:
    """Return a producer's digest, reusing the recorded one when its source held.

    The recorded digest is adopted for the file's current identity only when
    the store also recorded that identity and it still matches, so a source
    that moved is digested and a store written before identities were recorded
    falls back to digesting as well.
    """

    identity = _module_source_identity(module)
    if (
        identity is not None
        and isinstance(stats, Mapping)
        and stats.get(name) == identity
        and isinstance(recorded.get(name), str)
    ):
        path = _module_source_path(module)
        if path is not None:
            _MODULE_SOURCE_DIGESTS[(os.fspath(path), tuple(identity))] = recorded[name]
    return _module_source_digest(module)


def _write_persisted_memo(
    path: Path,
    sources: Mapping[str, str],
    source_stats: Mapping[str, list[int] | None],
    value: object,
) -> None:
    """Persist ``value`` with the source digests of the modules that made it."""

    payload = {
        "schema": _PERSISTED_SCHEMA,
        "sources": dict(sources),
        "source_stats": dict(source_stats),
        "value": value,
    }
    try:
        write_json_atomically(path, payload, fsync=False, ensure_ascii=False)
    except (OSError, TypeError, ValueError) as exc:
        LOGGER.warning("Could not persist %s: %s", path, exc)


def _discovery_producers() -> tuple[object, ...]:
    """Return the modules whose source produces a persisted discovery parse."""

    import sys

    return (_plan_html, sys.modules[__name__])


def _index_path(docs_dir: Path, project: str) -> Path:
    return _persisted_cache_path(
        "metadata-index", _VERSION, project, str(Path(docs_dir).resolve())
    )


def _persisted_payload(docs_dir: Path, project: str) -> Mapping:
    """Return the store's whole persisted value, or empty when unusable.

    The store is one :func:`persisted_memo` value under the project and docs
    directory's identity, guarded by the source digests of the modules that
    parsed it, so an index written by a different parser is treated as absent
    and rebuilt rather than served as a stale parse.
    """

    held = _read_persisted_memo(
        _index_path(docs_dir, project),
        {module.__name__: module for module in _discovery_producers()},
        index_key=(project, str(Path(docs_dir).resolve())),
    )
    return held if isinstance(held, Mapping) else {}


def _load_persisted(docs_dir: Path, project: str) -> dict[str, dict]:
    """Return the persisted entries by relative path, or none when unusable."""

    entries: dict[str, dict] = {}
    for entry in _persisted_payload(docs_dir, project).get("entries") or []:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            entries[entry["path"]] = entry
    return entries


def _load_dir_stamps(docs_dir: Path, project: str) -> dict[str, list[int]]:
    """Return the persisted directories' stat identities, or none when absent."""

    stamps = _persisted_payload(docs_dir, project).get("dirs")
    if not isinstance(stamps, Mapping):
        return {}
    return {
        relative: identity
        for relative, identity in stamps.items()
        if isinstance(relative, str)
        and isinstance(identity, list)
        and all(isinstance(part, int) for part in identity)
    }


def _store_persisted(
    docs_dir: Path,
    project: str,
    entries: list[dict],
    dir_stamps: Mapping[str, list[int]] | None = None,
) -> None:
    persisted_memo(
        "metadata-index",
        _VERSION,
        (project, str(Path(docs_dir).resolve())),
        _discovery_producers(),
        store={"entries": entries, "dirs": dict(dir_stamps or {})},
    )
