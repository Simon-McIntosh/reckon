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
and the next read rehashes that tree and rebuilds the rows that moved. A
reader with no watch revalidates by digest on every call instead — see
``index_rows(..., revalidate=True)`` — so a long-lived process still sees a
later edit.

A plan's row also carries the two figures that cannot be read from a document's
``<meta>`` head: its open-followup count and its implementable-section count.
They are derived once, when the row is built, and reused until the file's bytes
change, so a reader that answers from the index — the drain's plan remainder,
:func:`plan_derivations` — never parses a plan it has already seen.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from reckon import _plan_html, figures, resources
from reckon._store import _config_home, write_json_atomically
from reckon.file_memo import file_signature

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
)

_SCHEMA = "reckon.metadata-index"
#: Bumped when an entry gains a field, so an index written by the previous
#: shape is rebuilt rather than reused with the new field absent.
_VERSION = 4
_FIGURE_DIR = "figures"
_FIGURE_SUFFIXES = (".png", ".svg", ".gif")
#: The types discovery keeps in an inventory, so the index answers with the
#: same row set the derived payload is merged into.
_INDEX_TYPES = frozenset({"plan", "research", "evidence"})


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
    #: Each visited directory's stat identity, keyed by its docs-relative path.
    #: A later read compares these to decide whether the tree's *shape* — the
    #: set of directories and the files in them — still matches the persisted
    #: rows, so it can answer without walking the tree to rediscover them.
    dir_stamps: dict[str, list[int]] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.rebuilt or self.added or self.removed)


_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str, bool], IndexBuild] = {}

#: A parse of a document's metadata, keyed by its bytes rather than its path.
#: One promotion reads the same documents from several trees — the main
#: checkout, the run's worktree and the tip tree — and the path-keyed memo in
#: parsing then parses each tree's copy of byte-identical content separately.
#: Keying on the content digest collapses those copies to one parse, while a
#: file whose bytes changed has a new digest and is parsed again.
_META_LOCK = threading.Lock()
_META_BY_DIGEST: dict[tuple[str, str | None], dict] = {}
_META_MAX_ENTRIES = 50_000


def clear() -> None:
    """Drop every in-process index, as a process restart would."""

    with _LOCK:
        _CACHE.clear()
    with _META_LOCK:
        _META_BY_DIGEST.clear()


def parse_meta_shared(path: Path, slug: str | None = None) -> dict:
    """Parse ``path``'s metadata, reusing a parse of byte-identical content.

    ``_plan_html.parse_meta`` memoises per path, so the same document reached
    through two trees is parsed once per tree. Keying the parse on the file's
    content digest instead lets a second tree's identical bytes reuse the first
    tree's parse; a changed file has a new digest and is parsed afresh. A file
    that cannot be hashed falls back to the path-keyed parse, so an unreadable
    file's error path is unchanged.
    """

    from reckon import _plan_html

    try:
        digest = _content_digest(path)
    except OSError:
        return _plan_html.parse_meta(path, slug)
    key = (digest, slug)
    with _META_LOCK:
        cached = _META_BY_DIGEST.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
    value = _plan_html.parse_meta(path, slug)
    with _META_LOCK:
        _META_BY_DIGEST[key] = copy.deepcopy(value)
        while len(_META_BY_DIGEST) > _META_MAX_ENTRIES:
            _META_BY_DIGEST.pop(next(iter(_META_BY_DIGEST)))
    return value


def invalidate_tree(docs_dir: Path) -> None:
    """Drop the in-process index of the tree the change watch reported."""

    root = str(Path(docs_dir).resolve())
    with _LOCK:
        for key in [key for key in _CACHE if key[1] == root]:
            del _CACHE[key]


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

    ``revalidate`` rehashes every covered file before answering and re-parses
    only the ones whose bytes changed — the cost a restart already pays. A
    caller the change watch serves gets that for free through
    :func:`invalidate_tree` and leaves the flag alone; a caller without a watch
    (the MCP and CLI processes) would otherwise answer from the first build it
    made for the life of its process and never see a later edit.
    """

    key = _cache_key(docs_dir, project, repo_dir is not None)
    if not revalidate:
        with _LOCK:
            build = _CACHE.get(key)
        if build is not None:
            return [dict(row) for row in build.rows]
    covered: list[tuple[str, Path]] | None = None
    dirs: dict[str, Path] | None = None
    if revalidate:
        reused = _reuse_covered(docs_dir, project)
        if reused is not None:
            covered, dirs = reused
    build = _build_and_cache(
        key,
        docs_dir,
        project,
        repo_dir=repo_dir,
        git_first=git_first,
        git_last=git_last,
        covered=covered,
        dirs=dirs,
    )
    return [dict(row) for row in build.rows]


def _reuse_covered(
    docs_dir: Path, project: str
) -> tuple[list[tuple[str, Path]], dict[str, Path]] | None:
    """Return the persisted file set when the tree's shape is unchanged.

    A reader with no change watch rehashes every covered file to revalidate,
    and it needs the file set to do that. Rediscovering it walks the tree on
    every read. The persisted index already lists the files, and the
    directories' stat identities say whether entry ``*was added or removed``
    since it was built — so a shape that has not moved is answered without the
    walk, and any difference (or an absent index) returns ``None`` so the
    caller walks and rebuilds.
    """

    stamps = _load_dir_stamps(docs_dir, project)
    entries = _load_persisted(docs_dir, project)
    if not stamps or not entries:
        return None
    dirs: dict[str, Path] = {}
    for relative, identity in stamps.items():
        path = docs_dir / relative
        if _dir_identity(path) != identity:
            return None
        dirs[relative] = path
    covered = [(relative, docs_dir / relative) for relative in sorted(entries)]
    return covered, dirs


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

    Every call rehashes each covered file, so a caller in a long-lived process
    sees a later edit, and only a file whose bytes changed is parsed again.
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


def build_index(
    docs_dir: Path,
    project: str,
    *,
    repo_dir: Path | None = None,
    git_first: Mapping[str, int] | None = None,
    git_last: Mapping[str, int] | None = None,
    covered: list[tuple[str, Path]] | None = None,
    dirs: dict[str, Path] | None = None,
) -> IndexBuild:
    """Build one project's rows, re-parsing only what its bytes changed.

    ``covered`` and ``dirs`` let a caller that has already established the
    tree's shape — by comparing the recorded directory identities — hand the
    file set in instead of walking the tree to rediscover it. With ``covered``
    absent the walk runs and both are derived from it.
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
        identity = _dir_identity(path)
        if identity is not None:
            build.dir_stamps[relative] = identity
    entries: list[dict] = []
    seen: set[str] = set()

    for relative, path in files:
        seen.add(relative)
        try:
            digest = _content_digest(path)
        except OSError:
            # A file that cannot be read is still a row, built below with its
            # parse left unknown; a vanished one drops out and the next build
            # sees whatever replaced it.
            digest = None
        entry = known.get(relative)
        if (
            entry is not None
            and digest is not None
            and entry.get("digest") == digest
            and _row_is_complete(entry)
        ):
            build.reused += 1
            entries.append(entry)
            if "fields" in entry:
                build.rows.append(dict(entry["fields"]))
            if isinstance(entry.get("plan"), Mapping):
                build.plans.append({"path": relative, **entry["plan"]})
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
        fields, plan = _row_for(path, relative, docs_dir, project, signature, stamp)
        entry = {"path": relative, "digest": digest}
        if fields is None:
            entry["skip"] = True
        else:
            entry["fields"] = fields
            build.rows.append(fields)
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
    answering from a row whose figures were never derived.
    """

    fields = entry.get("fields")
    if not isinstance(fields, Mapping) or fields.get("type") != "plan":
        return True
    return isinstance(entry.get("plan"), Mapping)


def _content_digest(path: Path) -> str:
    """Return the digest that changes exactly when ``path``'s bytes change.

    The row is keyed on the file's bytes rather than its stat identity: a
    rewrite that restores the same bytes — a checkout or a rebuild that only
    touches the mtime — reuses the parsed row, while any byte change re-parses
    it. A shared filesystem also reports a different device number for the same
    file on each host, which a byte digest is blind to, so the served process
    and the readers on other hosts share one persisted index instead of each
    re-parsing the whole project.
    """

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cache_key(docs_dir: Path, project: str, with_git: bool) -> tuple[str, str, bool]:
    return (project, str(Path(docs_dir).resolve()), with_git)


def _walk_covered(
    docs_dir: Path,
) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    """Return the covered files and every visited directory, from one walk.

    The files are ``(docs-relative posix path, path)`` for every HTML file
    anywhere in the tree plus figure images under the top-level figures
    directory. The directories are ``(docs-relative posix path, path)`` for
    every directory the walk reached, including ones that held no covered
    file: a directory's stat identity moves when an entry is added to or
    removed from it, so recording the set lets a later read tell whether the
    tree's shape changed without walking it again. Directory symlinks are not
    followed.
    """

    figures_root = os.path.join(os.fspath(docs_dir), _FIGURE_DIR)
    found: dict[str, Path] = {}
    directories: dict[str, Path] = {}
    pending = [os.fspath(docs_dir)]
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
                        pending.append(entry.path)
                        continue
                except OSError:
                    continue
                name = entry.name
                is_figure = name.endswith(_FIGURE_SUFFIXES) and (
                    directory == figures_root
                    or directory.startswith(figures_root + os.sep)
                )
                if not (name.endswith(".html") or is_figure):
                    continue
                path = Path(entry.path)
                try:
                    relative = path.relative_to(docs_dir).as_posix()
                except ValueError:
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
    """

    return _walk_covered(docs_dir)[0]


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
) -> tuple[dict | None, dict | None]:
    """Return one file's (row, plan record), each None when it has none.

    The plan record holds the figures a row cannot take from a ``<meta>`` head
    — the open-followup and implementable-section counts — and is present only
    for a plan the resource walk can identify.
    """

    if path.suffix in _FIGURE_SUFFIXES:
        return _figure_row(path, docs_dir, project, signature, stamp), None
    return _resource_row(path, docs_dir, project, signature, stamp)


def _resource_row(
    path: Path,
    docs_dir: Path,
    project: str,
    signature: list[int],
    stamp: StampFn,
) -> tuple[dict | None, dict | None]:
    try:
        resource = resources.identify_resource(docs_dir, path, project)
    except resources.ResourceCollision:
        return None, None
    if resource is None or resource.type not in _INDEX_TYPES:
        return None, None
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
    plan = _plan_record(path, resource.archived) if resource.type == "plan" else None
    return row, plan


def _plan_record(path: Path, archived: bool) -> dict:
    """Return the derived figures one plan row carries beyond its meta fields.

    ``open_followups`` counts the followups a parsed read reports as anything
    other than resolved; ``implementable_sections`` is the declared remainder
    itself, ``None`` when the plan carries no valid declaration. ``archived``
    is the resource walk's path-based marker, so a drain that reads these
    records excludes the same plans the walk does.

    A file that cannot be read yields ``unreadable`` and no figure: a counted
    zero is indistinguishable from a plan that carries no open work, and the
    plan inventory as a whole is unknown rather than shorter. The read here is
    the file's own, not the memoised text parse_meta shares — that one reports
    an unreadable file as an empty document, which is exactly the reading this
    record exists to refuse.
    """

    from reckon._schema import plan_executable_remainder

    try:
        text = path.read_text(encoding="utf-8")
        state = _plan_html.read_state(text)
    except (OSError, ValueError):
        return {
            "archived": archived,
            "open_followups": None,
            "implementable_sections": None,
            "unreadable": True,
        }
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


def _figure_row(
    path: Path,
    docs_dir: Path,
    project: str,
    signature: list[int],
    stamp: StampFn,
) -> dict | None:
    try:
        slug = path.relative_to(docs_dir / _FIGURE_DIR).as_posix()
    except ValueError:
        return None
    dims = figures._DIMS_BY_SUFFIX[path.suffix](path)
    capture, _caption = figures._capture_metadata(path)
    created, edited = stamp(path, signature)
    return {
        "slug": slug,
        "href": f"/{project}/figures/{slug}",
        "type": "gif" if path.suffix == ".gif" else "figure",
        "title": figures._titleize(capture)
        if capture
        else figures._titleize(path.stem),
        "status": "",
        "sprint": None,
        "archived": "",
        "created": created,
        "edited": edited,
        "width": dims[0] if dims else None,
        "height": dims[1] if dims else None,
    }


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


def _index_path(docs_dir: Path, project: str) -> Path:
    identity = f"{project}\0{Path(docs_dir).resolve()}".encode()
    digest = hashlib.sha256(identity).hexdigest()
    return _config_home() / "cache" / "metadata-index" / f"{digest}.json"


def _load_persisted(docs_dir: Path, project: str) -> dict[str, dict]:
    """Return the persisted entries by relative path, or none when unusable."""

    path = _index_path(docs_dir, project)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable metadata index %s: %s", path, exc)
        return {}
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != _SCHEMA
        or raw.get("version") != _VERSION
        or raw.get("project") != project
        or raw.get("docs_dir") != str(Path(docs_dir).resolve())
        or not isinstance(raw.get("entries"), list)
    ):
        LOGGER.warning("Ignoring incompatible metadata index %s", path)
        return {}
    entries: dict[str, dict] = {}
    for entry in raw["entries"]:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            entries[entry["path"]] = entry
    return entries


def _load_dir_stamps(docs_dir: Path, project: str) -> dict[str, list[int]]:
    """Return the persisted directories' stat identities, or none when absent."""

    path = _index_path(docs_dir, project)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable metadata index %s: %s", path, exc)
        return {}
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != _SCHEMA
        or raw.get("version") != _VERSION
        or raw.get("project") != project
        or raw.get("docs_dir") != str(Path(docs_dir).resolve())
    ):
        return {}
    stamps = raw.get("dirs")
    if not isinstance(stamps, dict):
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
    payload = {
        "schema": _SCHEMA,
        "version": _VERSION,
        "project": project,
        "docs_dir": str(Path(docs_dir).resolve()),
        "entries": entries,
        "dirs": dict(dir_stamps or {}),
    }
    try:
        write_json_atomically(
            _index_path(docs_dir, project), payload, fsync=False, ensure_ascii=False
        )
    except OSError as exc:
        LOGGER.warning("Could not persist the metadata index for %s: %s", project, exc)
