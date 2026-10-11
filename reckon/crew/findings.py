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

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reckon import _store
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
    """Whether a stored record is a run review or a plan review.

    A run review names the run it reviews; a plan review names no reviewed run
    and carries plan findings whose anchor is a plan path or a plan section.
    The reviewed run id, present or absent, is the one key the two populations
    differ on.
    """
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
    severity: a follow-on is a finding a reviewer raised, and dropping it here
    would hide exactly the rows most easily lost. The store spells the path a
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


def _record_findings(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every finding a stored record carries, whatever its subject kind."""
    if _record_kind(record) == "plan":
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


def _committed_record_paths(project: str, root: Path | None) -> list[Path]:
    """Every committed review file the project holds, run and plan alike.

    The project's committed reviews tree is walked directly rather than through
    the staging store, because a promoted review is a committed file and the
    staging root does not name it. A project with no resolvable committed tree
    has no records and yields an empty list rather than an invented path.
    """
    committed = _review.committed_review_root(project, root=root)
    if committed is None or not committed.is_dir():
        return []
    return [
        path
        for dirname in (_RUN_DIRNAME, _PLAN_DIRNAME)
        for path in sorted((committed / dirname).glob("*/*.json"))
        if path.is_file() and _review._INCOMPLETE_RECORD_MARK not in path.name
    ]


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
    records_read = 0
    for record_path in _committed_record_paths(project, root_path):
        record = _review._committed_record_at(record_path)
        if record is None:
            continue
        records_read += 1
        for row in _record_rows(record, root_path):
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
