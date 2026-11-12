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
against it, and ``deferred`` once a landing carried it forward without judging
it — which leaves it open work all the same.
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
# node it went into), ``exempted`` (naming the reason it is not being acted on)
# or ``deferred`` (naming the run whose landing carried it forward). The entry's
# presence is the disposition; a missing or unrecognised entry leaves the
# finding ``open`` rather than reporting it as closed. The key is declared once
# in :mod:`reckon.crew.review`, which writes the entry and is imported here, so
# the writer and the reader can never disagree on it.

#: The disposition kind the landing rule records for a finding it carried
#: forward unjudged. It is written only by that rule, never by the dispose verb:
#: a deferral says nobody decided, so a hand-written one would claim a decision
#: the rule did not make on a finding nobody read.
DEFERRED_DISPOSITION_KIND = "deferred"

#: The kinds that answer a finding, and so end its life as open work.
CLOSING_DISPOSITION_KINDS: frozenset[str] = frozenset({"folded", "exempted"})

#: Every kind an entry may carry and still be read as a recorded disposition.
RECORDED_DISPOSITION_KINDS: frozenset[str] = CLOSING_DISPOSITION_KINDS | {
    DEFERRED_DISPOSITION_KIND
}

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
    outside the set leaves the finding ``open`` — an unreadable entry is a row
    someone meant to fill in, not a closed finding.

    A deferred entry renders ``deferred:<run>``: the landing rule carried the
    finding forward when it landed the run, naming the run it deferred over. The
    finding is not closed by it — a deferral records that nobody judged the
    finding, so it stays open work until a fold or an exemption answers it —
    which is why :func:`parse_finding_disposition` reports it as open.
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
    if kind == DEFERRED_DISPOSITION_KIND:
        run_id = str(entry.get("run") or "").strip()
        return f"deferred:{run_id}" if run_id else "open"
    return "open"


def parse_finding_disposition(rendered: str) -> dict[str, str]:
    """Decode a rendered finding disposition into its kind and its target.

    :func:`finding_disposition` owns the encoding — a fold renders
    ``folded:<node>``, an exemption ``exempted:<reason>`` and a landing deferral
    ``deferred:<run>``. A caller that counts dispositions by kind, or that needs
    the node a fold names, reads them back through here rather than re-stating
    the prefixes. Anything that is not a recorded disposition, including
    ``open``, decodes to kind ``open`` with no target.
    """
    kind, separator, target = str(rendered or "").partition(":")
    target = target.strip()
    if not separator or not target or kind not in RECORDED_DISPOSITION_KINDS:
        return {"kind": "open", "target": ""}
    return {"kind": kind, "target": target}


def undisposed_finding_indexes(
    record: Mapping[str, Any] | None, *, holding_landing: bool
) -> tuple[int, ...]:
    """The indexes of findings nobody has answered, split by what they withhold.

    This is the one reading of the severities the landing rule makes.
    ``holding_landing=True`` names the findings that refuse the landing: one
    declaring the blocking severity, and one declaring none at all, since an
    unreadable severity is a judgement the reviewer left unstated.
    ``holding_landing=False`` names the rest — declared non-blocking and still
    unanswered — which the landing carries forward as deferred work. The guard
    that admits the run and the writer that records the deferral both read this
    function, so the run lands exactly over the findings it defers.

    The index is the finding's position in the record's own ``findings`` list,
    the index :func:`finding_disposition` keys an answer against, so a disposition
    written for a returned index answers the finding this function named. A fold
    or an exemption is its author's decision and is left alone by both halves.
    """
    if not isinstance(record, Mapping):
        return ()
    selected: list[int] = []
    for index, finding in enumerate(_record_findings(record)):
        if not str(finding.get("text") or "").strip():
            continue
        if finding_disposition(record, index) != "open":
            continue
        severity = _review.declared_severity(finding)
        blocks = severity is None or severity == _review.BLOCKING_FINDING_SEVERITY
        if blocks is holding_landing:
            selected.append(index)
    return tuple(selected)


def _record_rows(record: Mapping[str, Any], root: Path | None) -> list[dict[str, Any]]:
    """One row per finding on a record, carrying the record's own identity.

    Each row names the finding's path and line, its severity and text, the
    review run that produced the record, the run it reviewed, the head it read,
    its disposition and the answer recorded against it. The finding's index
    within its record is carried as ``finding`` so a disposition — keyed by that
    index — and a dispose call can name the same finding.

    ``disposition`` and ``response`` answer two different questions and are kept
    apart: a disposition is the fold, exemption or landing deferral recorded
    against the finding's index, and a response is the answer the answer verb
    records against the finding's id. A row answers ``None`` for the second
    until one has been recorded, rather than an empty answer, so a reader can
    tell an unanswered finding from one answered with nothing.

    ``open`` states whether the finding is still work somebody owes: a fold and
    an exemption close it, a landing deferral does not — it records that the
    rule carried the finding forward without judging it — so a deferred row is
    listed with its disposition beside ``open: true``.
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
    answers = record.get("responses")
    if not isinstance(answers, Mapping):
        answers = {}
    rows: list[dict[str, Any]] = []
    for index, finding in enumerate(_record_findings(record)):
        raw_path = str(finding.get("path") or "").strip()
        relative = _runs._repository_relative_scope(raw_path, root)
        answer = answers.get(str(finding.get("id") or "").strip())
        disposition = finding_disposition(record, index)
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
                "disposition": disposition,
                "open": parse_finding_disposition(disposition)["kind"]
                not in CLOSING_DISPOSITION_KINDS,
                "response": dict(answer) if isinstance(answer, Mapping) else None,
            }
        )
    return rows


# The findings index, one entry per (project, committed tree, checkout). A
# whole-store read is the cost a dispatch preflight cannot pay per call: the
# committed tree holds thousands of records across thousands of directories,
# and reading them all took minutes. Every dispatch is a fresh process, so an
# index that lives only in one process starts cold on each one and saves that
# process nothing.
#
# The index is therefore persisted to the node-local cache directory the review
# store already keeps its own index in — never the tracked tree, which would put
# thousands of derived rows under version control — and each process starts from
# the copy another process on this node left. Two levels of trust decide what is
# reused: a review directory whose own stat identity is unchanged is trusted
# whole, so it costs one stat and no record read; a directory that moved is
# rescanned record by record against each record's stat identity, so a record
# written or atomically replaced behind the index's back is still read. The
# document is also stamped with a digest of the modules that derive a row, so a
# change to how a row is read invalidates every entry rather than serving rows
# the present code would no longer produce.
_INDEX_LOCK = threading.Lock()
_INDEXES: dict[tuple[str, str, str], dict[str, Any]] = {}
_MAX_INDEXES = 4

# Directory stats are latency-bound on a shared filesystem — a serial pass over
# two thousand directories costs seconds — so they are asked for concurrently.
# The figure is a count of outstanding stats, not of cores used.
_STAT_WORKERS = 16
_PARALLEL_STAT_FLOOR = 8

_RecordEntries = tuple[tuple[str, tuple[int, ...], int], ...]

# The document format below changes shape only with this number: a later reader
# that meets an older document rebuilds rather than guessing at its fields.
_INDEX_VERSION = 1


def _scan_directory(directory: Path, pattern: str = "*.json") -> _RecordEntries:
    """The records one review directory holds, each with its stat identity.

    Only the identity is taken here: the record's rows are read by the caller
    for the records whose identity moved, so a directory re-scan does not
    re-read records that did not change. A partial write the store keeps
    outside current-review selection is skipped, as before.
    """
    entries: list[tuple[str, tuple[int, ...], int]] = []
    try:
        paths = sorted(directory.glob(pattern))
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


def _directory_signature(directory: Path) -> tuple[int, ...] | None:
    """The stat identity of one review directory, or ``None`` when unreadable.

    A record created, deleted or atomically renamed into a directory moves the
    directory's own identity, so this is the one stat whose move proves that the
    rows the index holds for that directory may have changed. An unreadable
    directory yields no signature and is never trusted.
    """
    try:
        return file_memo.signature_from_stat(directory.stat())
    except OSError:
        return None


def _directory_signatures(
    directories: Iterable[Path],
) -> dict[str, tuple[int, ...] | None]:
    """Stat each review directory once, concurrently once there are enough.

    A serial pass over a store of thousands of directories is latency-bound on a
    shared filesystem, so a wide listing is asked for through a pool. A handful
    is cheaper in the calling thread.
    """
    ordered = list(directories)
    if len(ordered) < _PARALLEL_STAT_FLOOR:
        return {
            str(directory): _directory_signature(directory) for directory in ordered
        }
    workers = min(_STAT_WORKERS, len(ordered))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        signatures = list(pool.map(_directory_signature, ordered))
    return {
        str(directory): signature
        for directory, signature in zip(ordered, signatures, strict=True)
    }


def _review_parents(committed: Path) -> list[Path]:
    """The two directories the committed tree files its review directories under."""
    return [committed / dirname for dirname in (_RUN_DIRNAME, _PLAN_DIRNAME)]


def _list_child_directories(parent: Path) -> list[Path]:
    """The review directories one parent holds, by name order.

    One stat per child is the cost of a fresh listing, so this is paid only for
    a parent whose own signature moved — a review directory that appeared or
    vanished — rather than on every read.
    """
    try:
        return sorted(child for child in parent.iterdir() if child.is_dir())
    except OSError:
        return []


def _review_directories(parents: Iterable[Path]) -> list[Path]:
    """The directories the committed tree files. One per reviewed run or plan."""
    directories: list[Path] = []
    for parent in parents:
        if parent.is_dir():
            directories.extend(_list_child_directories(parent))
    return directories


def _scan_directories(
    directories: Iterable[Path], pattern: str = "*.json"
) -> list[_RecordEntries]:
    """Scan several review directories, concurrently once there are enough.

    A handful of directories is cheaper to scan in the calling thread than to
    hand to a pool; a store of thousands is latency-bound and gains the pool.
    """
    ordered = list(directories)
    if len(ordered) < _PARALLEL_STAT_FLOOR:
        return [_scan_directory(directory, pattern) for directory in ordered]
    workers = min(_STAT_WORKERS, len(ordered))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda path: _scan_directory(path, pattern), ordered))


def _read_record_rows(
    record_path: Path, root_path: Path | None
) -> tuple[dict[str, Any], ...]:
    """Read one committed record and build its finding rows."""
    record = _review._committed_record_at(record_path)
    if record is None:
        return ()
    return tuple(_record_rows(record, root_path))


def _index_sources() -> tuple[Path, ...]:
    """The modules whose content fixes how a finding row is derived.

    A row is a projection of a record through these readers: which finding
    fields are pulled, how the path is scoped against the checkout, and how a
    persisted record is parsed. A change to any of them can change a row without
    touching the record, so the digest of all four is what a persisted index is
    filed under.
    """
    from reckon.crew import review_records

    return (
        Path(__file__).resolve(),
        Path(_review.__file__).resolve(),
        Path(review_records.__file__).resolve(),
        Path(_runs.__file__).resolve(),
    )


def _index_code_stamp() -> str:
    """The digest this index is filed under and checked against."""
    from reckon.crew.review_records import cache_code_stamp

    return cache_code_stamp(_index_sources())


def _index_cache_path(key: tuple[str, str, str]) -> Path | None:
    """The node-local file one store and checkout's index is persisted in.

    The store's own directory and the checkout key the file, not the project
    name alone: a worktree or a scratch tree shares its project name with the
    main checkout and the two must never answer from one another's entries.
    """
    from reckon.crew.review_records import cache_document_path

    project, committed_key, root_key = key
    if not committed_key and not root_key:
        return None
    return cache_document_path(
        project, "findings-index", f"{committed_key}\0{root_key}"
    )


def _signature_tuple(value: Any) -> tuple[int, ...] | None:
    """A stat identity read back from a document, or ``None`` when malformed."""
    if not isinstance(value, list) or not value:
        return None
    try:
        return tuple(int(item) for item in value)
    except (TypeError, ValueError):
        return None


def _records_state(raw: Any) -> dict[str, dict[str, Any]] | None:
    """The per-record entries of one directory read back from a document."""
    if not isinstance(raw, Mapping):
        return None
    held: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if not isinstance(entry, Mapping):
            return None
        identity = _signature_tuple(entry.get("identity"))
        rows = entry.get("rows")
        if identity is None or not isinstance(rows, list):
            return None
        if not all(isinstance(row, Mapping) for row in rows):
            return None
        held[str(name)] = {"identity": identity, "rows": [dict(row) for row in rows]}
    return held


def _directory_state(raw: Any) -> dict[str, Any] | None:
    """One directory's entry read back from a document, or ``None`` when malformed."""
    if not isinstance(raw, Mapping):
        return None
    records = _records_state(raw.get("records"))
    if records is None:
        return None
    return {"signature": _signature_tuple(raw.get("signature")), "records": records}


def _directory_document(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One directory's entry as it is persisted."""
    signature = entry.get("signature")
    return {
        "signature": list(signature) if signature is not None else None,
        "records": {
            name: {"identity": list(record["identity"]), "rows": record["rows"]}
            for name, record in entry["records"].items()
        },
    }


def _load_persisted_index(key: tuple[str, str, str]) -> dict[str, Any] | None:
    """The index another process on this node left for this store, or ``None``.

    A document is usable only when this checkout's reader wrote it for this
    store: one stamped with other code's identity, written in another format, or
    naming another store and checkout is ignored rather than trusted, and the
    caller builds the index itself. A missing, half-written or malformed
    document gets the same answer, because a cache that cannot be read is not a
    cache.
    """
    from reckon.crew.review_records import read_cache_document

    stamp = _index_code_stamp()
    path = _index_cache_path(key)
    if not stamp or path is None:
        return None
    document = read_cache_document(path)
    if document is None:
        return None
    if document.get("version") != _INDEX_VERSION:
        return None
    if str(document.get("code_stamp") or "") != stamp:
        return None
    if document.get("key") != list(key):
        return None
    raw_directories = document.get("directories")
    if not isinstance(raw_directories, Mapping):
        return None
    directories: dict[str, dict[str, Any]] = {}
    for name, raw in raw_directories.items():
        entry = _directory_state(raw)
        if entry is None:
            return None
        directories[str(name)] = entry
    code = _directory_state(document.get("code"))
    parents = _parents_state(document.get("parents"))
    if parents is None:
        return None
    return {
        "key": list(key),
        "directories": directories,
        "parents": parents,
        "code": code,
        "dirty": False,
    }


def _parents_state(raw: Any) -> dict[str, dict[str, Any]] | None:
    """The review-directory listings read back from a document.

    Each parent carries its own stat identity beside the directories it held, so
    a reader that finds the parent unchanged reuses that listing rather than
    paying one stat per child directory — the cost a store of thousands of
    reviews otherwise pays on every fresh process.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        return None
    held: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if not isinstance(entry, Mapping):
            return None
        children = entry.get("directories")
        if not isinstance(children, list) or not all(
            isinstance(child, str) for child in children
        ):
            return None
        held[str(name)] = {
            "signature": _signature_tuple(entry.get("signature")),
            "directories": list(children),
        }
    return held


def _persist_index(key: tuple[str, str, str], state: Mapping[str, Any]) -> None:
    """Write this store's index where this node's processes can start from it.

    A disposable cache: an unwritable directory, a full disk or a reader that
    arrives mid-write costs a later process a from-scratch build rather than the
    cache, so a write that fails is not an error the read reports.
    """
    stamp = _index_code_stamp()
    path = _index_cache_path(key)
    if not stamp or path is None:
        return
    code = state.get("code")
    document = {
        "version": _INDEX_VERSION,
        "code_stamp": stamp,
        "key": list(key),
        "parents": {
            name: {
                "signature": (
                    list(entry["signature"]) if entry["signature"] is not None else None
                ),
                "directories": entry["directories"],
            }
            for name, entry in state.get("parents", {}).items()
        },
        "directories": {
            name: _directory_document(entry)
            for name, entry in state["directories"].items()
        },
        "code": _directory_document(code) if code is not None else None,
    }
    from reckon.crew.review_records import write_cache_document

    write_cache_document(path, document)


def _directory_entry(
    previous: Mapping[str, Any] | None,
    entries: _RecordEntries,
    root_path: Path | None,
) -> dict[str, Any]:
    """One directory's entry, re-reading only the records whose identity moved.

    A record whose stat identity is the one the index holds keeps its rows, so a
    directory rescan costs a stat per record and parses only what changed. A
    file that no longer appears in the listing is dropped with it.
    """
    prior = previous["records"] if previous is not None else {}
    records: dict[str, dict[str, Any]] = {}
    for path_text, identity, _mtime_ns in entries:
        cached = prior.get(path_text)
        if cached is not None and cached["identity"] == identity:
            records[path_text] = cached
            continue
        records[path_text] = {
            "identity": identity,
            "rows": [
                dict(row) for row in _read_record_rows(Path(path_text), root_path)
            ],
        }
    return records


def _committed_directories(state: dict[str, Any], committed: Path) -> list[Path]:
    """The review directories the committed tree files, listing only what moved.

    A listing costs one stat per child directory, which a store of thousands of
    reviews cannot pay on every fresh process. Each parent's own stat identity is
    therefore kept beside the directories it held: while that identity is
    unchanged no review directory can have appeared or vanished, and the listing
    the index already holds is the whole set. A parent that moved is listed
    again, which is how a newly committed review's directory is found.
    """
    held_parents = state.setdefault("parents", {})
    directories: list[Path] = []
    for parent in _review_parents(committed):
        signature = _directory_signature(parent)
        entry = held_parents.get(str(parent))
        if (
            entry is not None
            and signature is not None
            and entry["signature"] == signature
        ):
            directories.extend(Path(name) for name in entry["directories"])
            continue
        children = _list_child_directories(parent)
        held_parents[str(parent)] = {
            "signature": signature,
            "directories": [str(child) for child in children],
        }
        directories.extend(children)
        state["dirty"] = True
    return directories


def _committed_rows(
    state: dict[str, Any], committed: Path, root_path: Path | None
) -> tuple[list[dict[str, Any]], int]:
    """Every finding row of the committed tree, and the record count.

    Each review directory is stat'ed once. One whose identity is the one the
    index holds is trusted whole — its records are reused without a listing or a
    read — and one that moved is rescanned record by record, so a record that
    appeared, vanished or was atomically replaced is read then. A directory the
    index holds and the tree no longer files leaves with its rows.
    """
    held = state["directories"]
    directories = _committed_directories(state, committed)
    signatures = _directory_signatures(directories)

    stale: list[Path] = []
    for directory in directories:
        entry = held.get(str(directory))
        signature = signatures.get(str(directory))
        if entry is None or signature is None or entry["signature"] != signature:
            stale.append(directory)
    scanned = _scan_directories(stale) if stale else []
    for directory, entries in zip(stale, scanned, strict=True):
        signature = signatures.get(str(directory))
        held[str(directory)] = {
            "signature": signature,
            "records": _directory_entry(held.get(str(directory)), entries, root_path),
        }
        state["dirty"] = True

    seen = {str(directory) for directory in directories}
    for name in [name for name in held if name not in seen]:
        del held[name]
        state["dirty"] = True

    rows: list[dict[str, Any]] = []
    count = 0
    for directory in directories:
        entry = held.get(str(directory))
        if entry is None:
            continue
        for record in entry["records"].values():
            rows.extend(record["rows"])
            count += 1
    return rows, count


def _code_review_directory(project: str) -> Path | None:
    """The store directory the project's code reviews are written to.

    The store's own resolver names it, so a reader here and the writer that
    stores a code review cannot drift onto two directories.
    """
    from reckon.crew import plan_review

    return plan_review._code_review_store_dir(project)


def _code_review_rows(
    state: dict[str, Any], project: str, root_path: Path | None
) -> tuple[list[dict[str, Any]], int]:
    """The finding rows of the project's stored code reviews, and their count.

    The crew-home code-review store is indexed the same way the committed tree
    is: an unchanged store directory is trusted whole, and a store that moved is
    rescanned against each record's own stat identity, so a code review written
    since the index was built is read then and no other is read at all.
    """
    from reckon.crew import plan_review

    directory = _code_review_directory(project)
    if directory is None:
        return [], 0
    signature = _directory_signature(directory)
    entry = state.get("code")
    if entry is not None and signature is not None and entry["signature"] == signature:
        rows = [row for record in entry["records"].values() for row in record["rows"]]
        return rows, len(entry["records"])

    entries = _scan_directory(directory, f"{plan_review.CODE_REVIEW_FILE_PREFIX}*.json")
    records = _directory_entry(entry, entries, root_path)
    state["code"] = {"signature": signature, "records": records}
    state["dirty"] = True
    rows = [row for record in records.values() for row in record["rows"]]
    return rows, len(records)


def _index_for(key: tuple[str, str, str]) -> dict[str, Any]:
    """The live index for one store and checkout, created on first use.

    A process that holds none starts from the document another process on this
    node persisted for the same store, and builds its own only when there is
    none to start from.
    """
    index = _INDEXES.get(key)
    if index is not None:
        return index
    index = _load_persisted_index(key)
    if index is None:
        index = {
            "key": list(key),
            "parents": {},
            "directories": {},
            "code": None,
            "dirty": True,
        }
    with _INDEX_LOCK:
        _INDEXES[key] = index
        while len(_INDEXES) > _MAX_INDEXES:
            _INDEXES.pop(next(iter(_INDEXES)))
    return index


def _indexed_rows(
    project: str, root_path: Path | None
) -> tuple[list[dict[str, Any]], int]:
    """Every stored finding row of a project, refreshed by stat identity.

    Both of the roots a finding is read from are indexed: the committed reviews
    tree, and the crew-home store a code review is written to. The refreshed
    index is persisted for the next process on this node whenever a directory
    had to be rescanned, so a store that did not change costs a stat per
    directory and no write.
    """
    committed = _review.committed_review_root(project, root=root_path)
    committed_key = str(committed) if committed is not None else ""
    key = (project, committed_key, str(root_path))
    state = _index_for(key)

    rows: list[dict[str, Any]] = []
    count = 0
    if committed is not None and committed.is_dir():
        committed_rows, committed_count = _committed_rows(state, committed, root_path)
        rows.extend(committed_rows)
        count += committed_count
    code_rows, code_count = _code_review_rows(state, project, root_path)
    rows.extend(code_rows)
    count += code_count
    if state["dirty"]:
        _persist_index(key, state)
        state["dirty"] = False
    return rows, count


def note_record_written(
    path: str | Path, *, project: str, root: str | Path | None = None
) -> None:
    """Refresh a project's persisted findings index after a record was written.

    This is the whole of the writer side: the index is a disposable cache of
    rows derived from records, so a refresh that fails costs a later reader one
    record read and is never a reason to fail a write that has already landed.
    """
    try:
        _refresh_index_for_record(Path(path), project=project, root=root)
    except OSError:
        return


def _refresh_index_for_record(
    record_path: Path, *, project: str, root: str | Path | None
) -> None:
    """Rewrite this record's entry in the persisted index.

    The writers that change a record — the committed-store writer, an answer
    recorded on a committed record, and the code-review store — call this once
    their write has landed, so the next process to read the index does not have
    to open the record to learn what it now says. The record's directory is left
    with no trusted signature: the entry's rows are current here, but a
    concurrent write could land between this read and the directory's stat, and
    a signature recorded then would make a later reader trust an entry missing
    that write. A reader that meets an untrusted directory rescans it against
    each record's identity, which reuses this entry without opening the file.

    A writer never creates the document a reader builds: an index covering part
    of a store is worse than none, because every entry it holds would look
    trustworthy while the rest of the store went unread.

    This process's own index is deliberately left alone. It is keyed on the
    directory signatures this process observed, and its owner is the read path;
    a second writer of that structure would put the writer's ordering of a
    record write against the reader's stat beside the reader's own. The
    directory's signature has moved either way, so the next read in this process
    rescans that one directory and reads the record it just wrote, which is one
    file.
    """
    try:
        root_path = _scope_root(project, root)
        committed = _review.committed_review_root(project, root=root)
    except OSError:
        return
    key = (
        project,
        str(committed) if committed is not None else "",
        str(root_path),
    )
    state = _load_persisted_index(key)
    if state is None:
        return

    try:
        identity = file_memo.signature_from_stat(record_path.stat())
    except OSError:
        identity = None
    rows = (
        [dict(row) for row in _read_record_rows(record_path, root_path)]
        if identity is not None
        else []
    )
    target = None
    if committed is not None and _within(record_path, committed):
        target = state["directories"].setdefault(
            str(record_path.parent), {"signature": None, "records": {}}
        )
    else:
        directory = _code_review_directory(project)
        if directory is None or not _within(record_path, directory):
            return
        if state.get("code") is None:
            state["code"] = {"signature": None, "records": {}}
        target = state["code"]
    if identity is not None:
        target["records"][str(record_path)] = {"identity": identity, "rows": rows}
    else:
        target["records"].pop(str(record_path), None)
    target["signature"] = None
    _persist_index(key, state)


def _within(path: Path, directory: Path) -> bool:
    """Whether ``path`` lies inside ``directory`` once both are resolved."""
    try:
        path.resolve().relative_to(directory.resolve())
    except (OSError, ValueError):
        return False
    return True


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
