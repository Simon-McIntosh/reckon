"""A persisted per-project metadata index, keyed by each file's stat identity.

A page's first paint needs one row per document and figure — slug, href, type,
title, status, sprint, archive marker, stamps and figure dimensions — and
nothing derived. Building that list used to cost a parse of every file in the
tree on every cold process, so a restart re-read the whole project from the
shared filesystem before it could answer.

The index keeps one row per file on disk under the configuration home's cache
directory, together with the stat identity of the file the row came from. A
rebuild walks the tree, stats each file and re-parses only the ones whose
identity moved, so a restart costs stat calls rather than reads. The change
watch drops a tree's in-process rows when the kernel reports a change to it,
and the next read re-stats that tree and rebuilds the rows that moved.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from reckon import _plan_html, figures, resources
from reckon._store import _config_home, write_json_atomically
from reckon.file_memo import file_signature

LOGGER = logging.getLogger("reckon.metadata_index")

#: The fields a served row may carry, and the whole of what ``/_index``
#: returns. The stat identity that keys the persisted index is deliberately
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
_VERSION = 1
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
    #: Docs-relative paths whose row was recomputed this build.
    rebuilt: list[str] = field(default_factory=list)
    reused: int = 0
    added: int = 0
    removed: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.rebuilt or self.added or self.removed)


_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str], IndexBuild] = {}


def clear() -> None:
    """Drop every in-process index, as a process restart would."""

    with _LOCK:
        _CACHE.clear()


def invalidate_tree(docs_dir: Path) -> None:
    """Drop the in-process index of the tree the change watch reported."""

    root = str(Path(docs_dir).resolve())
    with _LOCK:
        for key in [key for key in _CACHE if key[1] == root]:
            del _CACHE[key]


def index_rows(docs_dir: Path, project: str) -> list[dict]:
    """Return the project's rows, rebuilding only a tree the watch dropped."""

    key = _cache_key(docs_dir, project)
    with _LOCK:
        build = _CACHE.get(key)
    if build is None:
        build = build_index(docs_dir, project)
        with _LOCK:
            _CACHE[key] = build
    return [dict(row) for row in build.rows]


def build_index(docs_dir: Path, project: str) -> IndexBuild:
    """Build one project's rows, re-parsing only what its stat identity moved."""

    docs_dir = Path(docs_dir)
    known = _load_persisted(docs_dir, project)
    build = IndexBuild(project=project, docs_dir=docs_dir)
    entries: list[dict] = []
    seen: set[str] = set()

    for relative, path in _covered_files(docs_dir):
        seen.add(relative)
        try:
            signature = list(file_signature(path))
        except OSError:
            # A file that vanished mid-walk is not a row; the next build sees
            # whatever replaced it.
            continue
        entry = known.get(relative)
        if entry is not None and entry.get("stat") == signature:
            build.reused += 1
            entries.append(entry)
            if "fields" in entry:
                build.rows.append(dict(entry["fields"]))
            continue
        if entry is None:
            build.added += 1
        else:
            build.rebuilt.append(relative)
        fields = _row_for(path, relative, docs_dir, project, signature)
        entry = {"path": relative, "stat": signature}
        if fields is None:
            entry["skip"] = True
        else:
            entry["fields"] = fields
            build.rows.append(fields)
        entries.append(entry)

    build.removed = len(set(known) - seen)
    if build.changed or not known:
        _store_persisted(docs_dir, project, entries)
    return build


def _cache_key(docs_dir: Path, project: str) -> tuple[str, str]:
    return (project, str(Path(docs_dir).resolve()))


def _covered_files(docs_dir: Path) -> list[tuple[str, Path]]:
    """Return (docs-relative posix path, path) for every file the index covers.

    Every HTML file anywhere in the tree, plus figure images under the
    top-level figures directory. Directory symlinks are not followed, matching
    the discovery walk.
    """

    figures_root = os.path.join(os.fspath(docs_dir), _FIGURE_DIR)
    found: dict[str, Path] = {}
    pending = [os.fspath(docs_dir)]
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
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
    return sorted(found.items())


def _row_for(
    path: Path,
    relative: str,
    docs_dir: Path,
    project: str,
    signature: list[int],
) -> dict | None:
    """Return the row for one file, or None when it is not a listed file."""

    if path.suffix in _FIGURE_SUFFIXES:
        return _figure_row(path, docs_dir, project, signature)
    return _resource_row(path, docs_dir, project, signature)


def _resource_row(
    path: Path, docs_dir: Path, project: str, signature: list[int]
) -> dict | None:
    try:
        resource = resources.identify_resource(docs_dir, path, project)
    except resources.ResourceCollision:
        return None
    if resource is None or resource.type not in _INDEX_TYPES:
        return None
    rec = _plan_html.parse_meta(path)
    href = str(
        (
            resource.relative_path
            if resource.legacy
            else resource.canonical_relative_path
        ).with_suffix("")
    )
    created, edited = _stat_stamps(signature)
    status = (rec.get("status") or "") if resource.type == "plan" else ""
    return {
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


def _figure_row(
    path: Path, docs_dir: Path, project: str, signature: list[int]
) -> dict | None:
    try:
        slug = path.relative_to(docs_dir / _FIGURE_DIR).as_posix()
    except ValueError:
        return None
    dims = figures._DIMS_BY_SUFFIX[path.suffix](path)
    capture, _caption = figures._capture_metadata(path)
    created, edited = _stat_stamps(signature)
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


def _stat_stamps(signature: list[int]) -> tuple[int, str]:
    """Return (created_unix_ts, edited_iso) from the file's own stat identity."""

    ctime_ns, mtime_ns = signature[4], signature[3]
    created = int(ctime_ns // 1_000_000_000)
    edited_ts = max(int(mtime_ns // 1_000_000_000), created)
    edited = datetime.fromtimestamp(edited_ts).isoformat(  # noqa: DTZ006
        timespec="seconds"
    )
    return created, edited


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


def _store_persisted(docs_dir: Path, project: str, entries: list[dict]) -> None:
    payload = {
        "schema": _SCHEMA,
        "version": _VERSION,
        "project": project,
        "docs_dir": str(Path(docs_dir).resolve()),
        "entries": entries,
    }
    try:
        write_json_atomically(
            _index_path(docs_dir, project), payload, fsync=False, ensure_ascii=False
        )
    except OSError as exc:
        LOGGER.warning("Could not persist the metadata index for %s: %s", project, exc)
