"""Serve every stored review finding by the repository path it names.

A review records its findings against the paths its reviewer read, but the
store keys records by the run or plan they are about, so the only way to ask
"what is already known about this file" is to read every record and pull the
paths out by hand. This module is that reader: it walks the committed run-review
and plan-review records of a project, pulls each finding's path and line out
through the record readers the store already has, groups the rows by the
repository-relative path they name, and counts the distinct reviews naming each
path — the recurrence a later reader needs to tell a fresh claim from one that
has been raised many times before.

A finding whose path is not inside the repository — a report, a manifest, a
staging store path — is kept, because it is still a finding, but under
``scope: artefact`` and out of the recurrence count: the paths such findings
name do not live in the tree, so counting them against a repository path would
report recurrence where none exists. The boundary that divides the two is
measured against the project's mounted checkout when no root is named, and an
absolute path inside it is reported repository-relative, so a reviewer that
cites the whole path still counts toward the path's recurrence.

Findings that declare a non-blocking severity are the ones most easily lost,
so the reader pulls the record's findings whole — every severity, not only the
blocking ones a repair is a candidate for. The finding's disposition is read
from the record: ``open`` until a later writer records a fold or an exemption
against it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from reckon import _store, file_memo
from reckon.crew import repair as _repair
from reckon.crew import review as _review
from reckon.crew import runs as _runs

# The record field a per-finding disposition is written under, keyed by the
# finding's index within its record — the same index the rows carry as
# ``finding``. The value is a mapping with a ``kind`` of ``folded`` (naming the
# node it went into) or ``exempted`` (naming the reason it is not being acted
# on). The entry's presence is the disposition; a missing or unrecognised entry
# leaves the finding ``open`` rather than reporting it as closed. The key is
# declared once in :mod:`reckon.crew.review`, which writes the entry and is
# imported here, so the writer and the reader can never disagree on it.

# The two scopes a row can carry. A repository path is one the record's own
# checkout holds; every other path — an absolute report or manifest path, a
# home-relative store path — is an artefact, kept but out of recurrence.
SCOPE_REPOSITORY = "repository"
SCOPE_ARTEFACT = "artefact"

# The committed tree's two subtrees: one directory per reviewed run, one per
# plan slug, each holding one file per review run that produced a record.
_RUN_DIRNAME = _review.COMMITTED_RUN_DIRNAME
_PLAN_DIRNAME = _review.COMMITTED_PLAN_DIRNAME


def _record_kind(record: Mapping[str, Any]) -> str:
    """Whether a stored record is a code review, a run review or a plan review.

    A code review names its ``subject`` as ``code``. A run review names the run
    it reviews; a plan review names no reviewed run and carries plan findings
    whose anchor is a plan path or a plan section. The subject key is consulted
    first because a code review names neither a reviewed run nor a plan, so it
    would otherwise be read as a plan review and its findings dropped.
    """
    if str(record.get("subject") or "").strip() == "code":
        return "code"
    if str(record.get("reviewed_run_id") or "").strip():
        return "run"
    return "plan"


def _split_anchor(anchor: str) -> tuple[str, str]:
    """Split a plan finding's anchor into its path and its line.

    A plan finding's anchor is a ``<file>:<line>`` reference or a bare
    ``<plan>#<section>`` one. A trailing ``:<line>`` is read as the line; an
    anchor shaped ``<plan>#<section>`` names no line, so it is returned whole as
    the path and, being no repository path, is scoped an artefact by the caller.
    """
    text = str(anchor or "").strip()
    if "#" in text:
        return text, ""
    head, separator, tail = text.rpartition(":")
    if separator and tail.strip():
        return head, tail.strip()
    return text, ""


def _scope_of(raw_path: str, root: Path | None) -> str:
    """Whether a finding's path lies inside the checkout, or names an artefact.

    The boundary is measured by the dispatch scope reader's own normalisation,
    so a path this reader calls repository-relative is the path a scope claim
    binds, and a symlinked spelling and a ``..``-bearing one land on the same
    row rather than two.
    """
    return (
        SCOPE_REPOSITORY
        if _runs._repository_relative_scope(raw_path, root) is not None
        else SCOPE_ARTEFACT
    )


def _scope_root(project: str, root: str | Path | None) -> Path | None:
    """The checkout the repository boundary is measured against.

    ``root`` names it explicitly — a worker's worktree — so the boundary is that
    worktree, not the registered checkout. Omitted, the committed tree
    resolves through the project's mounted docs directory, so the boundary is
    measured against that same mounted checkout and an absolute path inside it
    is repository-scoped rather than read as an artefact. A project with no
    resolvable docs directory has no boundary a path can be told against, and
    ``None`` is returned.
    """
    if root is not None:
        return Path(root).expanduser().resolve()
    docs_dir = _store._docs_dir_for_project(project)
    if docs_dir is None:
        return None
    return docs_dir.parent


def _run_findings(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The findings a run review carries, every severity alike.

    Read through the store's own finding reader so the claim field and the
    minted id are the ones a repair already answers, and never filtered by
    severity: a non-blocking finding is one a reviewer raised, and dropping it
    here would hide exactly the rows most easily lost. The store spells the path a
    finding names ``file``; it is surfaced as ``path`` so run and plan findings
    present one spelling to the row builder.
    """
    rows: list[dict[str, Any]] = []
    for finding in _repair.review_findings(record):
        row = dict(finding)
        row["path"] = str(finding.get("file") or "").strip()
        rows.append(row)
    return rows


def _plan_findings(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The findings a plan review carries, each with a path and line split out."""
    findings = record.get("findings")
    if not isinstance(findings, list):
        return []
    rows: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        path, line = _split_anchor(str(finding.get("anchor") or ""))
        rows.append(
            {
                "id": str(finding.get("id") or "").strip(),
                "path": path,
                "line": line,
                "text": str(finding.get("text") or "").strip(),
                "severity": str(finding.get("type") or "").strip() or None,
            }
        )
    return rows


def _code_findings(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The findings a code review carries, each with a path and line split out.

    A code review's finding names its location the same way a plan finding does
    — an ``anchor`` of ``<file>:<line>`` — so the split is the plan reader's.
    Its finding class is carried as ``severity``, the spelling the rows present,
    so the class a code reviewer assigned reads beside the text it qualifies.
    """
    findings = record.get("findings")
    if not isinstance(findings, list):
        return []
    rows: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        path, line = _split_anchor(str(finding.get("anchor") or ""))
        rows.append(
            {
                "id": str(finding.get("id") or "").strip(),
                "path": path,
                "line": line,
                "text": str(finding.get("text") or "").strip(),
                "severity": str(finding.get("class") or "").strip() or None,
            }
        )
    return rows


def _record_findings(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every finding a stored record carries, whatever its subject kind."""
    kind = _record_kind(record)
    if kind == "code":
        return _code_findings(record)
    if kind == "plan":
        return _plan_findings(record)
    return _run_findings(record)


def finding_disposition(record: Mapping[str, Any], index: int) -> str:
    """The disposition recorded against the finding at ``index``, or ``open``.

    A fold renders ``folded:<node>``; an exemption renders ``exempted:<reason>``.
    A record carrying no disposition map, no entry for the index, or an entry
    outside the closed set leaves the finding ``open`` — an unreadable entry is
    a row someone meant to fill in, not a closed finding.
    """
    dispositions = record.get(_review.FINDING_DISPOSITIONS_KEY)
    if not isinstance(dispositions, Mapping):
        return "open"
    entry = dispositions.get(str(index))
    if not isinstance(entry, Mapping):
        return "open"
    kind = str(entry.get("kind") or "").strip()
    if kind == "folded":
        node = str(entry.get("node") or "").strip()
        return f"folded:{node}" if node else "open"
    if kind == "exempted":
        reason = str(entry.get("reason") or "").strip()
        return f"exempted:{reason}" if reason else "open"
    return "open"


def parse_finding_disposition(rendered: str) -> dict[str, str]:
    """Decode a rendered finding disposition into its kind and its target.

    :func:`finding_disposition` owns the encoding — a fold renders
    ``folded:<node>`` and an exemption ``exempted:<reason>``. A caller that
    counts dispositions by kind, or that needs the node a fold names, reads them
    back through here rather than re-stating the prefixes. Anything that is not a
    closed disposition, including ``open``, decodes to kind ``open`` with no
    target.
    """
    kind, separator, target = str(rendered or "").partition(":")
    target = target.strip()
    if not separator or not target or kind not in ("folded", "exempted"):
        return {"kind": "open", "target": ""}
    return {"kind": kind, "target": target}


def _record_rows(record: Mapping[str, Any], root: Path | None) -> list[dict[str, Any]]:
    """One row per finding on a record, carrying the record's own identity.

    Each row names the finding's path and line, its severity and text, the
    review run that produced the record, the run it reviewed, the head it read
    and its disposition. The finding's index within its record is carried as
    ``finding`` so a disposition — keyed by that index — and a dispose call can
    name the same finding.
    """
    review_run_id = str(record.get("review_run_id") or "").strip()
    reviewed_run_id = str(record.get("reviewed_run_id") or "").strip()
    _carried_base, _base, carried_head, head = _review.carried_revision_pair(record)
    reviewed_head_sha = str(head or "").strip() if carried_head else ""
    # A code review names the revision it read ``reviewed_revision`` rather than
    # the run-review pair, so its head is that field; a run or plan review
    # resolves its head from the shared revision pair.
    if str(record.get("subject") or "").strip() == "code":
        reviewed_head_sha = str(record.get("reviewed_revision") or "").strip()
    rows: list[dict[str, Any]] = []
    for index, finding in enumerate(_record_findings(record)):
        raw_path = str(finding.get("path") or "").strip()
        relative = _runs._repository_relative_scope(raw_path, root)
        rows.append(
            {
                "path": relative if relative is not None else raw_path,
                "line": str(finding.get("line") or "").strip(),
                "scope": _scope_of(raw_path, root),
                "severity": finding.get("severity") or None,
                "text": str(finding.get("text") or "").strip(),
                "finding": index,
                "id": str(finding.get("id") or "").strip(),
                "review_run_id": review_run_id,
                "reviewed_run_id": reviewed_run_id,
                "reviewed_head_sha": reviewed_head_sha,
                "disposition": finding_disposition(record, index),
            }
        )
    return rows


# The findings index, one entry per (project, committed tree, checkout). A
# whole-store read is the cost a dispatch preflight cannot pay per call: the
# committed tree holds thousands of records across thousands of directories,
# and reading them all took minutes. The index keeps every record's rows from
# the call that read it, and finds the records that moved by comparing each
# review directory's stat identity — the one stat whose move a record's
# creation, deletion or atomic rewrite is guaranteed to cause.
_INDEX_LOCK = threading.Lock()
_INDEXES: dict[tuple[str, str, str], dict[str, Any]] = {}
_MAX_INDEXES = 4

# Directory stats are latency-bound on a shared filesystem — a serial pass over
# two thousand directories costs seconds — so they are asked for concurrently.
# The figure is a count of outstanding stats, not of cores used.
_STAT_WORKERS = 16
_PARALLEL_STAT_FLOOR = 8

_RecordEntries = tuple[tuple[str, tuple[int, ...], int], ...]


def _scan_directory(directory: Path) -> _RecordEntries:
    """The records one review directory holds, each with its stat identity.

    Only the identity is taken here: the record's rows are read by the caller
    for the records whose identity moved, so a directory re-scan does not
    re-read records that did not change. A partial write the store keeps
    outside current-review selection is skipped, as before.
    """
    entries: list[tuple[str, tuple[int, ...], int]] = []
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return ()
    for path in paths:
        if _review._INCOMPLETE_RECORD_MARK in path.name:
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        entries.append(
            (str(path), file_memo.signature_from_stat(info), info.st_mtime_ns)
        )
    return tuple(entries)


def _directory_records(directory: Path) -> _RecordEntries:
    """The records of one review directory, rescanned only when it moves.

    Keyed on the directory's own stat identity through
    :mod:`reckon.file_memo`: a directory whose entries changed — a record
    written, removed or atomically replaced — is rescanned, and one that did
    not costs a single stat.
    """
    return file_memo.memoized(
        "findings_directory_records", directory, lambda: _scan_directory(directory)
    )


def _review_directories(committed: Path) -> list[Path]:
    """The directories the committed tree files. One per reviewed run or plan.

    The listing of each top-level directory is memoised on its own identity, so
    a call re-lists the run or plan directories only when one of them appeared
    or vanished rather than on every read.
    """
    directories: list[Path] = []
    for dirname in (_RUN_DIRNAME, _PLAN_DIRNAME):
        parent = committed / dirname

        def list_children(target: Path = parent) -> tuple[Path, ...]:
            try:
                return tuple(
                    sorted(child for child in target.iterdir() if child.is_dir())
                )
            except OSError:
                return ()

        if not parent.is_dir():
            continue
        directories.extend(
            file_memo.memoized("findings_review_directories", parent, list_children)
        )
    return directories


def _scan_directories(directories: Iterable[Path]) -> list[_RecordEntries]:
    """Scan several review directories, concurrently once there are enough.

    A handful of directories is cheaper to scan in the calling thread than to
    hand to a pool; a store of thousands is latency-bound and gains the pool.
    """
    ordered = list(directories)
    if len(ordered) < _PARALLEL_STAT_FLOOR:
        return [_directory_records(directory) for directory in ordered]
    workers = min(_STAT_WORKERS, len(ordered))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_directory_records, ordered))


def _read_record_rows(
    record_path: Path, root_path: Path | None
) -> tuple[dict[str, Any], ...]:
    """Read one committed record and build its finding rows."""
    record = _review._committed_record_at(record_path)
    if record is None:
        return ()
    return tuple(_record_rows(record, root_path))


def _stored_code_reviews(project: str) -> list[dict[str, Any]]:
    """The project's stored code reviews, read from the store that holds them.

    A code review is written to the crew home's review store rather than the
    code-review tree the committed walk covers, so it is read through the
    store's own listing — the reader that already knows the file family and the
    key — rather than by a second walk of a directory this module would have to
    name.
    """
    from reckon.crew import plan_review

    return plan_review.list_code_reviews(project=project)


def _code_review_rows(
    project: str, root_path: Path | None
) -> tuple[list[dict[str, Any]], int]:
    """The finding rows of the project's stored code reviews, and their count.

    The rows are memoised against the record's own file, so a code review is
    read once and re-read only when its file moves. A record carrying no
    ``review_path`` cannot be keyed that way and is read here.
    """
    rows: list[dict[str, Any]] = []
    records = _stored_code_reviews(project)
    for record in records:
        path = str(record.get("review_path") or "").strip()
        if path:
            rows.extend(_read_record_rows(Path(path), root_path))
        else:
            rows.extend(tuple(_record_rows(record, root_path)))
    return rows, len(records)


def _index_for(key: tuple[str, str, str]) -> dict[str, Any]:
    """The live index entry for one store and checkout, created on first use."""
    index = _INDEXES.get(key)
    if index is None:
        index = {"records": {}}
        with _INDEX_LOCK:
            _INDEXES[key] = index
            while len(_INDEXES) > _MAX_INDEXES:
                _INDEXES.pop(next(iter(_INDEXES)))
    return index


def _indexed_rows(
    project: str, root_path: Path | None
) -> tuple[list[dict[str, Any]], int]:
    """Every stored finding row of a project, refreshed by stat identity.

    The committed tree is indexed rather than re-read: a record whose stat
    identity is unchanged contributes the rows the index already holds, and a
    record that appeared or moved is read then. A record that vanished from its
    directory leaves the index with the directory it was read from.
    """
    committed = _review.committed_review_root(project, root=root_path)
    committed_key = str(committed) if committed is not None else ""
    index = _index_for((project, committed_key, str(root_path)))
    records: dict[str, tuple[tuple[int, ...], tuple[dict[str, Any], ...]]] = index[
        "records"
    ]

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    count = 0
    if committed is not None and committed.is_dir():
        for entries in _scan_directories(_review_directories(committed)):
            for path_text, signature, _mtime_ns in entries:
                seen.add(path_text)
                count += 1
                cached = records.get(path_text)
                if cached is None or cached[0] != signature:
                    cached = (signature, _read_record_rows(Path(path_text), root_path))
                    records[path_text] = cached
                rows.extend(cached[1])
    for vanished in [name for name in records if name not in seen]:
        del records[vanished]

    code_rows, code_count = _code_review_rows(project, root_path)
    rows.extend(code_rows)
    return rows, count + code_count


def findings_by_path(
    project: str,
    *,
    path: str | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Group every stored finding of a project by the repository path it names.

    Each record in the project's committed reviews tree is read through the
    store's own record reader, its findings pulled whole, and each rendered as
    a row carrying its path, line, severity, text, review and reviewed run ids,
    reviewed head and disposition.

    Repository-scoped rows are grouped by path, each group carrying a
    ``recurrence`` — the number of distinct reviews naming that path — so a
    reader sees how often a path has been found against, not only how many rows
    it produced. Artefact-scoped rows are kept beside the groups under
    ``artefacts`` and add no recurrence.

    ``path``, when named, restricts the result to rows whose path begins with
    the named prefix; groups with no such row are omitted. ``root`` names the
    checkout the repository boundary and the committed tree are resolved
    against — a worker's worktree; omitted, both resolve against the project's
    mounted checkout, so an absolute path inside it is read repository-relative
    and counts toward recurrence rather than being kept as an artefact.
    """
    root_path = _scope_root(project, root)
    prefix = str(path or "").strip()

    groups: dict[str, dict[str, Any]] = {}
    artefacts: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    stored_rows, records_read = _indexed_rows(project, root_path)
    for row in stored_rows:
        if prefix and not row["path"].startswith(prefix):
            continue
        all_rows.append(row)
        if row["scope"] == SCOPE_ARTEFACT:
            artefacts.append(row)
            continue
        group = groups.get(row["path"])
        if group is None:
            group = {
                "path": row["path"],
                "scope": SCOPE_REPOSITORY,
                "rows": [],
                "reviews": set(),
            }
            groups[row["path"]] = group
        group["rows"].append(row)
        if row["review_run_id"]:
            group["reviews"].add(row["review_run_id"])

    grouped: list[dict[str, Any]] = [
        {
            "path": name["path"],
            "scope": name["scope"],
            "recurrence": len(name["reviews"]),
            "rows": name["rows"],
        }
        for name in sorted(groups.values(), key=lambda item: item["path"])
    ]
    return {
        "project": project,
        "path": prefix or None,
        "records": records_read,
        "paths": grouped,
        "artefacts": artefacts,
        "rows": all_rows,
    }
