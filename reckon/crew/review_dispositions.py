"""Floors on a review's dimensions, and the dispositions that answer them.

A review's total is a sum, so a single dimension far below the others is
invisible in it: a review whose durability is 5 of 0-20 is indistinguishable
from one whose six dimensions are even. This module closes that gap. It reads
the floors a resolved flight config declares, derives the dimensions that sit
below theirs, and records the dispositions that answer them.

The floors live in flight configuration under ``gates.dimension_floors``, keyed
by dimension name, so the standard a stored score is read against travels with
the other gate settings and is readable by whoever is reading the record. A
dimension the map does not name carries no floor: a floor of zero would report
nothing, since every stored score is at or above zero, so an undeclared floor
is read as the absence of a standard rather than as a standard of nothing.

A sub-floor dimension is a finding with its own disposition, never a lower
total: the total remains the reviewer's own arithmetic and the dimension keeps
its own recorded score. The finding stands until a disposition in the closed
set is recorded against that dimension, by the code path that does the
deciding.

The record keys themselves live in :mod:`reckon.crew.review`, beside the store
schema the record is written under, because readers across the codebase import
them from there; this module imports the keys rather than redefining them. The
writer a disposition is persisted through, :func:`reckon.crew.review._write_record`,
also stays in that module, whose own remaining writers share it.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon.crew.findings import DEFERRED_DISPOSITION_KIND
from reckon.crew.review import (
    DIMENSION_DISPOSITION_KINDS,
    DIMENSION_DISPOSITIONS_KEY,
    DIMENSION_FLOORS_KEY,
    FINDING_DISPOSITIONS_KEY,
    REVIEW_DIMENSIONS,
    _write_record,
    commit_record_write,
    stored_record,
)

#: The rule a landing records when it carries a finding forward unjudged. It
#: names the mechanism rather than the caller, so a reader of the stored entry
#: sees why the run landed over the finding rather than who landed it.
DEFERRED_AT_LANDING_RULE = "landing-defers-non-blocking-findings"


def _floor_value(entry: Any) -> int | None:
    """Reduce one declared floor to an integer, or ``None`` when it declares none.

    Both spellings the flight schema accepts are read: the bare integer, and a
    mapping carrying the floor beside the dimension it names. A value that is
    not an integer is read as no floor rather than coerced, because a floor
    derived from an unreadable entry would report findings nobody declared.
    """
    if isinstance(entry, bool):
        return None
    if isinstance(entry, int):
        return entry
    if isinstance(entry, Mapping):
        value = entry.get("floor")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def declared_dimension_floors(config: Mapping[str, Any] | None) -> dict[str, int]:
    """Return the floors a resolved flight config declares, keyed by dimension.

    The map is read from ``gates.dimension_floors`` of the resolved
    configuration. Only the dimensions this schema defines are returned: a key
    the review schema does not define names nothing to measure, so it is
    ignored rather than carried as an unreadable floor. A missing section, a
    missing dimension and an unreadable floor all yield no floor for that
    dimension, which is the state in which no score can fall below it.
    """
    if not isinstance(config, Mapping):
        return {}
    gates = config.get("gates")
    declared = gates.get(DIMENSION_FLOORS_KEY) if isinstance(gates, Mapping) else None
    if not isinstance(declared, Mapping):
        return {}
    floors: dict[str, int] = {}
    for dimension in REVIEW_DIMENSIONS:
        floor = _floor_value(declared.get(dimension))
        if floor is not None:
            floors[dimension] = floor
    return floors


def _newest_delivered_review_run_id(
    project: str, run_id: str, *, rubric: str | None = None
) -> str:
    """The review run id of the run's newest delivered review, or ``""``.

    A rendered command names the round it answers, and the round the verb would
    select without the flag is the run's newest delivered review. A store that
    cannot be read yields the empty string, so the command renders as it did
    before rather than failing the reader that prints it.

    ``rubric`` selects the review family the round belongs to, so a figure
    review's own run is named rather than the text review stored beside it.
    """
    from reckon.crew import review_records

    try:
        record = review_records.newest_delivered_review(project, run_id, rubric=rubric)
    except (OSError, ValueError):
        return ""
    return str((record or {}).get("review_run_id") or "").strip()


def dimension_disposition_valid(entry: Any) -> bool:
    """Whether a recorded disposition answers a sub-floor finding.

    A fold must name the node id the finding was folded into; an exemption must
    carry its recorded reason. An entry that names neither is not a disposition
    — it is a row someone meant to fill in — and is treated as undisposed.
    """
    if not isinstance(entry, Mapping):
        return False
    kind = str(entry.get("kind") or "").strip()
    if kind == "folded":
        return bool(str(entry.get("node") or "").strip())
    if kind == "exempted":
        return bool(str(entry.get("reason") or "").strip())
    return False


def sub_floor_dimensions(
    record: Mapping[str, Any] | None,
    floors: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return one row per scored dimension that sits below its declared floor.

    Each row is ``{"dimension", "score", "floor"}``, ordered by the schema's
    own dimension order so a reader sees a stable list across calls. A
    dimension is absent from the result when it carries no declared floor, when
    the record scored nothing for it, or when a disposition in the closed set
    is recorded against it: the first two are not measurable against a floor,
    and the third has been answered. An undisposed finding stays in the result
    however high the review's total is, which is the whole point — the total
    cannot speak for a dimension averaged out of it.
    """
    if not isinstance(record, Mapping) or not isinstance(floors, Mapping):
        return []
    scores = record.get("scores")
    if not isinstance(scores, Mapping):
        return []
    dispositions = record.get(DIMENSION_DISPOSITIONS_KEY)
    recorded = dispositions if isinstance(dispositions, Mapping) else {}
    rows: list[dict[str, Any]] = []
    for dimension in REVIEW_DIMENSIONS:
        floor = _floor_value(floors.get(dimension))
        if floor is None:
            continue
        score = scores.get(dimension)
        if not isinstance(score, int) or isinstance(score, bool):
            continue
        if score >= floor:
            continue
        if dimension_disposition_valid(recorded.get(dimension)):
            continue
        rows.append({"dimension": dimension, "score": score, "floor": floor})
    return rows


def _carry_answer_to_delivered_copies(
    reviewed_run_id: str,
    record: Mapping[str, Any],
    key: str,
) -> list[Path]:
    """Write ``record``'s ``key`` field into every delivered copy of the record.

    A run review delivers its record into the reviewed run's own
    ``review-records`` directory, and the supervisor — or a promotion, for a run
    whose supervisor did not get that far — files those copies into the store
    again. That filing writes the copy's body over the store file, so a
    disposition recorded only on the store file is dropped by the next filing of
    the same review. The delivered copies are therefore written alongside the
    store copy, which is what keeps the answer on the body every later write of
    this review is made from.

    Which files those are is decided by ``review._records_naming_this_review``,
    the same selection the filing reads, narrowed to this record's rubric so the
    figure review stored beside a text review is never written to. The entry the
    caller just recorded is merged into each copy under the same key, the latest
    entry winning per name by the stamp it records, so a copy that already
    answers another finding keeps that answer and gains this one. A copy already
    carrying the same entry is left untouched, so recording a disposition twice
    writes nothing.
    """
    from reckon.crew.review import (
        _records_naming_this_review,
        _review_record_rubric,
        _write_record,
    )
    from reckon.crew.review_records import _answer_stamp
    from reckon.crew.runs import run_dir

    written_entries = record.get(key)
    if not isinstance(written_entries, Mapping):
        return []
    written: list[Path] = []
    for path in _records_naming_this_review(
        record,
        run_dir(reviewed_run_id),
        rubric=_review_record_rubric(record),
    ):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(body, dict):
            continue
        existing = body.get(key)
        merged = dict(existing) if isinstance(existing, Mapping) else {}
        changed = False
        for name, entry in written_entries.items():
            current = merged.get(name)
            if current is None or _answer_stamp(entry) >= _answer_stamp(current):
                if current != entry:
                    changed = True
                merged[name] = entry
        if not changed:
            continue
        body[key] = merged
        _write_record(path, body)
        written.append(path)
    return written


def _build_disposition_entry(
    kind: str | None, node: str | None, reason: str | None
) -> dict[str, Any]:
    """Validate one disposition's kind and field, and build its stored entry.

    Both the dimension writer and the finding writer store the same closed
    shape and refuse the same inputs, so the check and the build live here once.
    A kind outside :data:`DIMENSION_DISPOSITION_KINDS`, a fold naming no node,
    an exemption carrying no reason, and either kind supplied with the other's
    field all raise :class:`ValueError`. The gate is the reader's own predicate
    :func:`dimension_disposition_valid`, so an entry this returns is one the
    readers honour. The other kind's field is refused rather than dropped: a
    stored entry that carries a reason no fold can name, or a node no exemption
    can stand behind, reads as recorded on both sides of a question the caller
    only half answered.

    ``deferred`` is outside the closed set on purpose, and is refused by name
    rather than as an unknown word: a deferral records that nobody judged the
    finding, so only the landing rule may write one, through
    :func:`record_landed_deferrals`, and never through a verb a person calls.
    """
    disposition_kind = str(kind or "").strip().lower()
    named_node = str(node or "").strip()
    recorded_reason = str(reason or "").strip()
    if disposition_kind == DEFERRED_DISPOSITION_KIND:
        raise ValueError(
            "a deferred disposition is recorded only by the landing rule; "
            "answer the finding with --kind folded or --kind exempted instead"
        )
    if not dimension_disposition_valid(
        {"kind": disposition_kind, "node": named_node, "reason": recorded_reason}
    ):
        if disposition_kind not in DIMENSION_DISPOSITION_KINDS:
            raise ValueError(
                f"unknown disposition {kind!r}; "
                f"allowed kinds are {', '.join(DIMENSION_DISPOSITION_KINDS)}"
            )
        if disposition_kind == "folded":
            raise ValueError(
                "a folded disposition must name the node it was folded into"
            )
        raise ValueError("an exempted disposition must record its reason")
    if disposition_kind == "folded" and recorded_reason:
        raise ValueError(
            "a folded disposition carries no reason; record an exemption "
            "instead of folding a finding whose reason you are keeping"
        )
    if disposition_kind == "exempted" and named_node:
        raise ValueError(
            "an exempted disposition names no node; record a fold instead of "
            "exempting a finding that a node already answers"
        )
    entry: dict[str, Any] = {
        "kind": disposition_kind,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    if disposition_kind == "folded":
        entry["node"] = named_node
    else:
        entry["reason"] = recorded_reason
    return entry


def record_dimension_disposition(
    project: str,
    reviewed_run_id: str,
    dimension: str,
    *,
    kind: str,
    node: str | None = None,
    reason: str | None = None,
    base_dir: str | Path | None = None,
    reviewed_head_sha: str | None = None,
) -> Path:
    """Record the disposition one sub-floor dimension carries, and return its path.

    The record written beside the review's own content is what
    :func:`sub_floor_dimensions` reads back: a dimension with a valid entry in
    the closed set stops producing a row, and one without keeps producing it
    however often the session is read.

    A fold names the node id through ``node``; an exemption records its reason
    through ``reason``. Anything else is refused rather than stored: an unknown
    dimension, a kind outside :data:`DIMENSION_DISPOSITION_KINDS`, a fold
    naming no node, an exemption carrying no reason, a fold carrying a reason
    and an exemption naming a node all raise :class:`ValueError`. The last two
    are refused because the field they carry belongs to the other kind and
    would otherwise be dropped in silence while the call reported success. A
    run with no stored review is refused too — a disposition answers a finding,
    and there is none to answer.

    The record is written back to the file the review was read from, so a
    head-keyed review keeps its own file, a legacy record carrying the same
    head keeps its own, and a disposition recorded against one revision never
    speaks for another. Writing anywhere else would leave the copy the reader
    reads without the entry — the row standing while this call reported the
    disposition as recorded. The write is atomic and preserves every other
    field, including the reviewer's verbatim text. When the file is the
    committed record, the write is committed in one commit through
    :func:`reckon.crew.review.commit_record_write`, so a disposition recorded
    after promotion is not left as a modified tracked file; a staging write is
    left uncommitted.
    """
    dimension_name = str(dimension or "").strip().lower()
    if dimension_name not in REVIEW_DIMENSIONS:
        raise ValueError(
            f"unknown review dimension {dimension!r}; "
            f"known dimensions are {', '.join(REVIEW_DIMENSIONS)}"
        )
    disposition = _build_disposition_entry(kind, node, reason)

    path, record = stored_record(
        project,
        reviewed_run_id,
        base_dir=base_dir,
        reviewed_head_sha=reviewed_head_sha,
    )
    if record is None or path is None:
        raise ValueError(
            f"no stored review for run {reviewed_run_id!r} in project "
            f"{project!r} to record a disposition against"
        )
    dispositions = record.get(DIMENSION_DISPOSITIONS_KEY)
    merged = dict(dispositions) if isinstance(dispositions, Mapping) else {}
    merged[dimension_name] = disposition
    record[DIMENSION_DISPOSITIONS_KEY] = merged

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_record(path, record)
    # The reviewer's own copies in the review run's directory are filed into the
    # store again by a later filing of this review, so the answer is written
    # there too rather than only on the copy this call selected.
    _carry_answer_to_delivered_copies(
        reviewed_run_id, record, DIMENSION_DISPOSITIONS_KEY
    )
    # A disposition written to the committed record is a tracked change, so it
    # is committed here rather than left for the next reader to trip on; a
    # staging write is not tracked and is left where it was written.
    commit_record_write(project, path)
    return path


def code_review_identity(run_id: str) -> str:
    """The neighbourhood key a code review's identity names, or ``""``.

    A code review is keyed by the neighbourhood it read, and the run id it was
    dispatched and composed under carries the node id, which carries the store's
    own prefix. The key is therefore the suffix after that prefix, so the
    dispatched run id, the composer's run id and the store file's stem
    (``code-<key>``) all name one neighbourhood through one rule. A run id that
    names no code review yields the empty string, so a caller can tell a code
    review from a run review without a second lookup.
    """
    from reckon.crew import plan_review

    text = str(run_id or "").strip()
    if not text:
        return ""
    prefix = plan_review.CODE_REVIEW_NODE_PREFIX
    marker = text.rfind(f"-{prefix}")
    if marker >= 0:
        return text[marker + len(prefix) + 1 :]
    if text.startswith(prefix):
        return text[len(prefix) :]
    if text.startswith(plan_review.CODE_REVIEW_FILE_PREFIX):
        return text[len(plan_review.CODE_REVIEW_FILE_PREFIX) :]
    return ""


def code_review_record_for_run(
    project: str, run_id: str, *, base_dir: str | Path | None = None
) -> tuple[Path | None, dict[str, Any] | None]:
    """The stored code review a run names, or ``(None, None)`` when it names none.

    The record is resolved through the store's own path builder,
    :func:`reckon.crew.plan_review.code_review_path`, so a code review is read
    from the one place it is written and no second path rule is introduced. A
    run id that names no code review returns ``(None, None)``; one that names a
    code review whose record is not on disk returns the path it would occupy
    beside ``None``, so a caller can report the absence against the path.
    """
    key = code_review_identity(run_id)
    if not key:
        return None, None
    from reckon.crew import plan_review

    path = plan_review.code_review_path(project, key, base_dir)
    record = plan_review.code_review_record(project, key, base_dir=base_dir)
    return path, (dict(record) if record is not None else None)


def stored_record_for_disposition(
    project: str,
    run_id: str,
    *,
    base_dir: str | Path | None = None,
    reviewed_head_sha: str | None = None,
    rubric: str | None = None,
) -> dict[str, Any] | None:
    """The stored record a disposition rewrites, whatever kind the run names.

    A code review's record and a run review's record share one store root under
    different file families, so a reader that has already resolved a
    disposition's run needs one accessor that reaches whichever record the run
    names rather than selecting the family itself. A run that names no code
    review is read through :func:`reckon.crew.review.stored_record`, the
    selection every run-review reader shares.

    ``rubric`` names the review family the record belongs to: omitted reads the
    run's text review, and a named rubric reads that family's own record, so a
    caller answering a finding on a figure review reads back the record its
    disposition was written to rather than the text review stored beside it.
    """
    if code_review_identity(run_id):
        _path, record = code_review_record_for_run(project, run_id, base_dir=base_dir)
        return record
    _path, record = stored_record(
        project,
        run_id,
        base_dir=base_dir,
        reviewed_head_sha=reviewed_head_sha,
        rubric=rubric,
    )
    return record


def _record_code_finding_disposition(
    project: str,
    run_id: str,
    finding_index: int,
    disposition: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """Write one finding disposition into a stored code review's own record.

    The entry lands under the same key and in the same shape the run-review
    writer stores, so the findings reader reports a code review's disposition
    with no branch of its own. The index is validated against the same
    enumeration the findings reader prints, so the index a caller sees is the
    index this writer answers.
    """
    from reckon.crew import findings as findings_module

    path, record = code_review_record_for_run(project, run_id, base_dir=base_dir)
    if record is None or path is None:
        raise ValueError(
            f"no stored review for run {run_id!r} in project {project!r} to "
            "record a disposition against"
        )
    findings = findings_module._record_findings(record)
    if finding_index >= len(findings):
        raise ValueError(
            f"run {run_id!r} carries {len(findings)} findings; "
            f"--finding {finding_index} names none of them"
        )
    dispositions = record.get(FINDING_DISPOSITIONS_KEY)
    merged = dict(dispositions) if isinstance(dispositions, Mapping) else {}
    merged[str(finding_index)] = dict(disposition)
    record[FINDING_DISPOSITIONS_KEY] = merged

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_record(path, record)
    return path


def record_finding_disposition(
    project: str,
    reviewed_run_id: str,
    finding_index: int,
    *,
    kind: str,
    node: str | None = None,
    reason: str | None = None,
    base_dir: str | Path | None = None,
    reviewed_head_sha: str | None = None,
    rubric: str | None = None,
) -> Path:
    """Record the disposition one stored finding carries, and return its path.

    The entry is written under
    :data:`reckon.crew.review.FINDING_DISPOSITIONS_KEY`,
    keyed by the finding's index, which is where the findings reader looks it
    up. A fold names the node id through ``node``; an exemption records its
    reason through ``reason``. The same refusals the dimension writer raises
    apply: a kind outside :data:`DIMENSION_DISPOSITION_KINDS`, a fold naming no
    node, an exemption carrying no reason, and either kind supplied with the
    other's field all raise :class:`ValueError`. An index that names no finding
    in the record is refused too — a disposition answers a finding, and storing
    one for an index the record does not carry would read as answered while the
    reader reports nothing there.

    The record selected and rewritten is the same one the dimension disposition
    rewrites, so a head-keyed review keeps its own file and a disposition
    recorded against one revision never speaks for another.

    ``rubric`` selects the review family the finding belongs to: omitted reads
    the run's text review, and a named rubric reads that family's own record, so
    a finding a figure review raised is answered on the figure record and not on
    the text review stored beside it. The index is counted against the selected
    record's own findings, so the index a caller sees printed for a rubric is the
    index this writer keys against.
    """
    if (
        not isinstance(finding_index, int)
        or isinstance(finding_index, bool)
        or finding_index < 0
    ):
        raise ValueError(
            f"a finding disposition names a finding by its non-negative index; "
            f"got {finding_index!r}"
        )
    disposition = _build_disposition_entry(kind, node, reason)
    return _write_disposition_against_finding(
        project,
        reviewed_run_id,
        finding_index,
        disposition,
        base_dir=base_dir,
        reviewed_head_sha=reviewed_head_sha,
        rubric=rubric,
    )


def _write_disposition_against_finding(
    project: str,
    reviewed_run_id: str,
    finding_index: int,
    disposition: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    reviewed_head_sha: str | None = None,
    rubric: str | None = None,
) -> Path:
    """Store one already-built disposition entry against a finding, by index.

    The single place a finding disposition reaches the store, whichever rule
    built it: the verb a person calls builds its entry through
    :func:`_build_disposition_entry`, and the landing rule builds the entry it
    alone may write. Both then resolve the record, key the entry by index,
    rewrite it, carry the answer to the delivered copies and commit — one path,
    so a rule that writes a disposition cannot skip the copy a later reader
    re-reads or commit a record the verb would have committed.
    """
    if code_review_identity(reviewed_run_id):
        return _record_code_finding_disposition(
            project,
            reviewed_run_id,
            finding_index,
            dict(disposition),
            base_dir=base_dir,
        )

    path, record = stored_record(
        project,
        reviewed_run_id,
        base_dir=base_dir,
        reviewed_head_sha=reviewed_head_sha,
        rubric=rubric,
    )
    if record is None or path is None:
        raise ValueError(
            f"no stored review for run {reviewed_run_id!r} in project "
            f"{project!r} to record a disposition against"
        )
    # The findings are enumerated through the same reader the findings surface
    # uses, so the index the caller sees printed is the index this writer keys
    # against; a second enumeration could count differently and answer the wrong
    # finding.
    from reckon.crew import repair as repair_module

    findings = repair_module.review_findings(record)
    if finding_index >= len(findings):
        raise ValueError(
            f"run {reviewed_run_id!r} carries {len(findings)} findings; "
            f"--finding {finding_index} names none of them"
        )
    dispositions = record.get(FINDING_DISPOSITIONS_KEY)
    merged = dict(dispositions) if isinstance(dispositions, Mapping) else {}
    merged[str(finding_index)] = dict(disposition)
    record[FINDING_DISPOSITIONS_KEY] = merged

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_record(path, record)
    _carry_answer_to_delivered_copies(reviewed_run_id, record, FINDING_DISPOSITIONS_KEY)
    commit_record_write(project, path)
    return path


def record_landed_deferrals(
    project: str,
    reviewed_run_id: str,
    review: Mapping[str, Any],
    *,
    reviewed_head_sha: str | None = None,
    base_dir: str | Path | None = None,
) -> list[int]:
    """Record each finding the landing carried forward as ``deferred``.

    The landing pass admits a run whose review raises nothing blocking, so the
    findings it raised must not vanish with the pointer that carried them. They
    are not exempted either: an exemption is a judgement, and the pass makes
    none. Each one still open is written as a ``deferred`` entry naming the run
    it was deferred over and the rule that deferred it, which leaves the finding
    open work in the findings view until a fold or an exemption answers it.

    The findings deferred here are the ones the landing guard admits the run
    over: both read
    :func:`reckon.crew.findings.undisposed_finding_indexes`, so the set recorded
    cannot differ from the set the guard cleared. Each is stored through
    :func:`_write_disposition_against_finding`, the write path the dispose verb
    uses, so a deferral reaches the delivered copies and the record commit
    rather than restating that path here.

    The dispositions are read from the stored record rather than from the mapping
    the caller holds — a review parsed in memory carries no dispositions, and
    reading them there would report every finding unanswered and overwrite the
    answers the store already holds. The record rewritten is the one for the head
    this review read, so a deferral recorded against one revision never speaks
    for another. The returned indexes are the findings this call deferred.
    """
    from reckon.crew import findings as findings_module

    stored = stored_record_for_disposition(
        project,
        reviewed_run_id,
        base_dir=base_dir,
        reviewed_head_sha=reviewed_head_sha,
    )
    if stored is None:
        raise ValueError(
            f"no stored review for run {reviewed_run_id!r} in project "
            f"{project!r} to record a deferral against"
        )
    deferred = list(
        findings_module.undisposed_finding_indexes(stored, holding_landing=False)
    )
    entry = {
        "kind": DEFERRED_DISPOSITION_KIND,
        "run": reviewed_run_id,
        "rule": DEFERRED_AT_LANDING_RULE,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    for index in deferred:
        _write_disposition_against_finding(
            project,
            reviewed_run_id,
            index,
            entry,
            base_dir=base_dir,
            reviewed_head_sha=reviewed_head_sha,
        )
    return deferred


def dispose_commands(
    project: str,
    run_id: str,
    selector: str,
    *,
    review_run_id: str | None = None,
    rubric: str | None = None,
) -> tuple[str, str]:
    """The two commands that answer one duty, as ``(fold, exempt)``.

    The two spaces a disposition lives in — a review finding named by its index
    and a sub-floor dimension named by its name — reach one rendering here, so
    a duty row and a refusal cannot print the same command differently. The
    caller passes the selector in the verb's own spelling (``--finding 3``,
    ``--dimension fit``), which is the only part the two spaces do not share.

    The fold names the node the duty was folded into and the exemption its
    reason, the two shapes :func:`record_finding_disposition` and
    :func:`record_dimension_disposition` accept, so a reader handed either
    command is handed one the writer takes.

    The command names the review run it answers through ``--review``, so it
    runs rather than being refused for selecting an older round. A caller that
    holds the duty's own record passes its review run id in
    ``review_run_id``; omitted, the run's newest delivered review supplies it,
    which is the record the verb would otherwise select. A run carrying no
    staging record renders the command without the flag, exactly as before,
    because no newer round exists for the verb to prefer.

    ``rubric`` names the review family the finding belongs to, and each family
    is stored beside the others rather than in place of them: the verb selects
    a figure record only when the command carries ``--rubric figure``, so a
    command composed for a figure finding without it selects no record and is
    refused. Omitted, the command answers the run's text review and renders as
    it did before.
    """
    named_rubric = str(rubric or "").strip()
    named = str(review_run_id or "").strip()
    if not named:
        named = _newest_delivered_review_run_id(
            project, run_id, rubric=named_rubric or None
        )
    review_flag = f"--review {shlex.quote(named)} " if named else ""
    rubric_flag = f"--rubric {shlex.quote(named_rubric)} " if named_rubric else ""
    base = (
        f"reckon crew dispose --project {shlex.quote(project)} "
        f"--run {shlex.quote(run_id)} {review_flag}{rubric_flag}{selector}"
    )
    return (
        f"{base} --kind folded --node '<the node the finding went into>'",
        f"{base} --kind exempted --reason '<why this finding is not being acted on>'",
    )
