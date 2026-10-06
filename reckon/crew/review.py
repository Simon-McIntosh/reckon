"""Schema, parser and durable store for independent node reviews.

A promoted run today carries a gate the worker wrote itself, which is why
gate pass sits at 1.00 on every lane in the committed ledger and cannot
separate one lane from another. This module supplies the second opinion: the
artefacts an independent review worker emits, how they are parsed into one
six-dimension record, and where those records live so they survive the run
directory and the worktree that produced them.

The six dimensions and their meanings are authored once in two mirrored
places. The Python schema below names them; the review worker learns their
meaning from ``prompts/review.md``, which this module loads from disk on each
call. A dimension renamed in the schema and not in the prompt fails the
falsifier that checks the prompt text for every schema name, so the two
cannot drift.

The parser turns the emitted form the prompt asks for into a record that
carries a value for every schema dimension. It never averages, weights, ranks
or compares lanes: the total is the arithmetic sum of what it parsed and
nothing else, and when a dimension is missing the total is withheld rather
than silently taken over fewer dimensions — a total computed over four is a
lower score indistinguishable from a worse one.

Alongside the dimensions, the parser records one verdict per checklist item —
the six read targets the prompt enumerates — and names any item that carries
none, by the same rule: an item with no verdict is reported as absent and the
item count is withheld rather than taken over the items that happen to be
present. The item verdicts are recorded and their absence named, but they do
not enter the completeness predicate a promotion reads (see
:func:`reckon.crew.recovery._review_is_complete`), which checks all schema
dimensions: reviews stored before this line existed carry no item verdicts,
and folding them into that predicate would mark every one of them incomplete.

It also records the production call sites the reviewer verified against the
change. A literal ``CALL_SITES: none`` is an explicit zero; an omitted line is
left absent so a missing measurement cannot be mistaken for a measured zero.
A label with no usable value is also left absent, but carries a machine-readable
emission state so a partial answer is not confused with an omitted question.

A finding may state whether it blocks the node's landing, by opening with one
of the declared severities and a colon. The value is recorded beside the
finding's file, line and text; a finding that states none carries no severity
field, because a defaulted severity is a judgement nobody made and the
distinction a gate reads is exactly the one a default would erase.

It records the revisions the review read under the canonical pair
``reviewed_base_sha`` and ``reviewed_head_sha``. A review is evidence about a
diff between two revisions: once a repair lands, a stored verdict describes
code that no longer exists, so a guard that reads the presence of a review as
evidence about the code being promoted is reading a true statement that
stopped being the one required. The store spells the pair five ways —
``reviewed_head_sha``, ``reviewed_commit``, ``commits_read``,
``reviewed_base_sha`` and ``reviewed_base`` — and the parser normalises them
without collapsing base into head. A record lacking either half is marked
incomplete: one revision cannot say what diff was reviewed.

The parser is on the live path, not a library awaiting a caller: dispatch and
runs resolve the review store through :func:`review_store_root`,
``reckon/crew/promotion.py`` reduces a stored record to the ledger block it
writes, and ``reckon/crew/recovery.py`` decides ``promotable`` from the
parsed dimensions. A change here changes what all of them store or decide.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _store
from reckon._store import write_json_atomically

# ── The schema: six dimensions, one maximum ────────────────────────────────
# A dimension added later is added in exactly one place. The mirror lives in
# prompts/review.md, whose text the falsifier checks against these names.
#
# ``reuse`` is the sixth, and it is deliberately independent of the clone
# detector in ``reckon/clones.py``: that detector runs at promotion and sees
# textual copies, while this module's review judgement runs before promotion
# and reads capability duplication the detector cannot see — a re-implementation
# in different code of a mechanism the repository already owns.

REVIEW_DIMENSIONS: tuple[str, ...] = (
    "goal_fidelity",
    "evidence",
    "scope_discipline",
    "durability",
    "fit",
    "reuse",
)

REVIEW_MAX_SCORE = 20

# ── The checklist: six read targets, one verdict each ───────────────────────
# These mirror the "What to read" list in prompts/review.md. The first five
# use VERDICT lines and call_sites uses the dedicated CALL_SITES line. A
# reviewer that skips an item and summarises the rest produces a record
# indistinguishable from a thorough one unless the omission is named, which is
# what this list is for.
# The mirror is checked by the same falsifier that checks the dimension names.

REVIEW_ITEMS: tuple[str, ...] = (
    "goal",
    "done_when",
    "write_paths",
    "manifest",
    "diff",
    "call_sites",
)

# ── The severity a finding can state ────────────────────────────────────────
# A finding may say whether it blocks the node's landing, which is the
# distinction a gate reading findings has to make. The vocabulary is declared
# here once and mirrored in the emission form in prompts/review.md, held by the
# same falsifier that holds the dimension names. The blocking value is named on
# its own because a gate that acts on blocking findings alone compares against
# that name rather than against the literal it happens to hold.
FINDING_SEVERITIES: tuple[str, ...] = ("blocking", "follow-on")
BLOCKING_FINDING_SEVERITY = "blocking"


def declared_severity(finding: Mapping[str, Any]) -> str | None:
    """Return the severity a stored finding declares, or ``None``.

    The one reader of a stored finding's severity, so the promotion audit, the
    repair reflex and the parser cannot disagree about which findings carry a
    judgement. A finding either states one of :data:`FINDING_SEVERITIES` — in
    its declared spelling, surrounding whitespace trimmed — or it states
    nothing: an absent key, an empty string and a word outside the vocabulary
    all mean the same thing here, no declared severity. A value outside the
    vocabulary is never guessed at, because a defaulted severity is a judgement
    nobody made.
    """
    value = finding.get("severity")
    if value is None:
        return None
    word = str(value).strip()
    return word if word in FINDING_SEVERITIES else None


# ── The plan rubrics ────────────────────────────────────────────────────────
# A plan review reads a plan's authored content before it is built; a plan
# design review reads that plan against the codebase it would extend. Each item
# below is a check the reviewer must speak to, and each prompt file names the
# same items in the same spelling, so the same mirror that holds the code-review
# schema to its prompt holds these two. The rubrics are advisory: a finding is
# scored on whether it would have changed the plan, so the items are the checks
# a reviewer owes an answer to, not scores.
PLAN_REVIEW_ITEMS: tuple[str, ...] = (
    "wiring",
    "done_when",
    "single_goal",
    "evidence_paths",
    "anchors_resolve",
    "naming",
    "reasoning",
)

PLAN_DESIGN_REVIEW_ITEMS: tuple[str, ...] = (
    "reuse_search",
    "deep_module",
    "thin_wrapper",
    "duplicate_owner",
    "interface_budget",
)

# The two rubrics a plan report is emitted under. Declared here beside the item
# sets so the parser that reads both and the error it raises for an unknown
# rubric name one list.
PLAN_REVIEW_RUBRICS: tuple[str, ...] = ("plan_review", "plan_design_review")

# ── The revision pair a review read ─────────────────────────────────────────
# The store already carries these five spellings. Base spellings describe the
# tree before the reviewed work; head spellings describe the landed work. A
# commit list contributes its last entry as head but cannot invent a base.
REVIEWED_BASE_KEY = "reviewed_base_sha"
REVIEWED_HEAD_KEY = "reviewed_head_sha"
BASE_REVISION_FIELDS: tuple[str, ...] = (REVIEWED_BASE_KEY, "reviewed_base")
HEAD_REVISION_FIELDS: tuple[str, ...] = (
    REVIEWED_HEAD_KEY,
    "reviewed_commit",
    "commits_read",
)
REVISION_FIELDS: tuple[str, ...] = (
    "reviewed_commit",
    "reviewed_base",
    REVIEWED_HEAD_KEY,
    REVIEWED_BASE_KEY,
    "commits_read",
)
_REVISION_LABELS = frozenset({"revision", *REVISION_FIELDS})

# The prompt is a versioned, diffable file rather than a string inside this
# module, so editing it is a text change rather than a code change. It is read
# from disk on every call: the module holds the path, not the text.
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "review.md"

# The plan rubrics are the same shape: versioned, diffable files read from disk
# on every call, so editing a rubric is a text change rather than a code change.
_PLAN_REVIEW_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "plan_review.md"
)
_PLAN_DESIGN_REVIEW_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "plan_design_review.md"
)


def _sha_from(value: Any) -> str | None:
    """Reduce a stored or emitted revision value to one sha, or ``None``.

    A commit list reduces to its last entry, which is the head the review read;
    an empty string, ``None`` and an empty list all reduce to ``None``. The
    caller decides what that means — a spelling carried with no usable value is
    a recorded absence, which is why this returns ``None`` rather than raising.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        for item in reversed(value):
            if isinstance(item, str) and item.strip():
                return item.strip()
        return None
    return None


def _first_carried_revision(
    record: Mapping[str, Any], fields: tuple[str, ...]
) -> tuple[bool, str | None]:
    """Return the first carried revision without treating emptiness as absence."""
    for field in fields:
        if field in record:
            return True, _sha_from(record[field])
    return False, None


def carried_revision_pair(
    record: Mapping[str, Any],
) -> tuple[bool, str | None, bool, str | None]:
    """Resolve the base/head pair from the five spellings the store carries.

    Each boolean is key presence rather than truthiness. A carried empty value
    is therefore preserved as a recorded absence and never falls through to a
    lower-precedence spelling. ``commits_read`` contributes only the head: a
    list of landed commits does not identify the tree they were based on.
    """
    base_carried, base_sha = _first_carried_revision(record, BASE_REVISION_FIELDS)
    head_carried, head_sha = _first_carried_revision(record, HEAD_REVISION_FIELDS)
    return base_carried, base_sha, head_carried, head_sha


# ── Resolving the revisions a record carries ────────────────────────────────
# A review record's revision pair is typed by hand more often than not, and a
# sha that lost characters mid-value still looks like one. A record keyed to a
# revision that names no commit matches nothing a reader compares against, so
# the mistake survives every reader until the reviewed run's own promotion
# fails to find the review it was promised — and the whole review has to be
# bought again. The pair is therefore resolved against the reviewed run's own
# repository while its tree is readable, and a carried revision that names no
# commit there is refused at both the write and the review run's check.


def _reviewed_run_pointer(reviewed_run_id: str) -> Mapping[str, Any] | None:
    """The reviewed run's live pointer, or ``None`` when it is not readable.

    The import is local because the run registry and the recovery reflex both
    read this module, so a module-level edge back to them would be a cycle.
    """
    if not reviewed_run_id:
        return None
    from reckon.crew.node import CrewError
    from reckon.crew.runs import read_pointer

    try:
        return read_pointer(reviewed_run_id)
    except (CrewError, OSError, ValueError):
        return None


def unresolved_reviewed_revision(record: Mapping[str, Any]) -> str | None:
    """The refusal a record earns when a carried revision names no commit.

    The reviewed run's own live pointer supplies both facts the refusal rests
    on: the repository the carried revisions must resolve in — the tree the
    run worked in, whose object store holds every commit the review could have
    read — and the head that tree actually carries, so the refusal names the
    stored value beside the real one and the reviewer repairs its own record
    while it still holds its turn. A record carrying no revision at all is
    left to the gates that require the pair, and a reviewed run with no
    readable pointer or tree is left alone: there is nothing to resolve
    against, and a guess about a reclaimed tree would refuse records that are
    correct. Formatting belongs to the caller, so the same sentence serves the
    store's write and the manifest check from one definition.
    """
    base_carried, base_sha, head_carried, head_sha = carried_revision_pair(record)
    carried = (
        (REVIEWED_BASE_KEY, base_sha if base_carried else None),
        (REVIEWED_HEAD_KEY, head_sha if head_carried else None),
    )
    if not any(sha for _key, sha in carried):
        return None
    pointer = _reviewed_run_pointer(str(record.get("reviewed_run_id") or "").strip())
    if pointer is None:
        return None
    from reckon.crew.recovery import (
        _resolve_commit,
        _review_tree,
        _run_head_for_review,
    )

    tree = _review_tree(pointer)
    if tree is None:
        return None
    for key, sha in carried:
        if not sha or _resolve_commit(tree, sha):
            continue
        head = _run_head_for_review(pointer)
        actual = (
            f"the reviewed worktree's head is {head!r}"
            if head
            else "the reviewed worktree carries no readable head"
        )
        return (
            f"{key} {sha!r} does not resolve to a commit in the reviewed "
            f"repository {str(tree)!r}; {actual}"
        )
    return None


class ReviewScoreError(ValueError):
    """A score fell outside the allowed range and was refused, not clamped.

    The error message names the dimension and the offending value so the
    caller can show the reviewer what was rejected. Clamping would turn a
    wrong score into a plausible one; refusal keeps it visible.
    """

    def __init__(self, dimension: str, value: int) -> None:
        self.dimension = dimension
        self.value = value
        super().__init__(
            f"review score for {dimension} is {value}; "
            f"allowed range is 0..{REVIEW_MAX_SCORE}"
        )


# ── The prompt ──────────────────────────────────────────────────────────────


def load_review_prompt() -> str:
    """Read the persisted review prompt from disk at call time."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def load_plan_review_prompt() -> str:
    """Read the plan-review rubric prompt from disk at call time."""
    return _PLAN_REVIEW_PROMPT_PATH.read_text(encoding="utf-8")


def load_plan_design_review_prompt() -> str:
    """Read the plan-design-review rubric prompt from disk at call time."""
    return _PLAN_DESIGN_REVIEW_PROMPT_PATH.read_text(encoding="utf-8")


# ── The parser ──────────────────────────────────────────────────────────────
# The emitted form the prompt asks for is one line per element:
#     VERDICT <item>: <one sentence saying what was read and what was found>
#     SCORE <dimension>: <integer 0..20>
#     JUSTIFICATION <dimension>: <one sentence citing a path or a line>
#     FINDING <file>:<line> <what is wrong and why it matters>
#     FINDING <file>:<line> <severity>: <what is wrong and why it matters>
# The second finding form says whether the defect blocks the node's landing; the
# severity is one of the declared values above. A finding that states no
# severity carries no ``severity`` field: a defaulted one would be a judgement
# nobody made, which is the state the gate exists to distinguish.
# Lines in any other shape are ignored, so the reviewer may surround the
# emitted lines with prose without breaking the parse.

_SCORE_RE = re.compile(
    r"^SCORE\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\S+)\s*$", re.IGNORECASE
)
_JUST_RE = re.compile(
    r"^JUSTIFICATION\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", re.IGNORECASE
)
_VERDICT_RE = re.compile(
    r"^VERDICT\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", re.IGNORECASE
)
_CALL_SITES_RE = re.compile(r"^CALL_SITES\s*:\s*(.*)$", re.IGNORECASE)
_FIND_RE = re.compile(r"^FINDING\s+(\S+)\s*(.*)$", re.IGNORECASE)
_REVISION_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def _as_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _stated_severity(text: str) -> str | None:
    """Return the declared severity a finding's text states, or ``None``.

    A finding states its severity by opening with one of the declared values
    and a colon. The word is matched case-insensitively and recorded in its
    declared spelling. The vocabulary decision is :func:`declared_severity`'s,
    so a word outside the vocabulary states nothing this parser can read: it
    stays where the reviewer wrote it — in the text — and no severity is
    recorded, since a guess would put a judgement in the record that nobody
    made. The text itself is never rewritten, so a finding parses to the same
    ``file``, ``line`` and ``text`` it parsed to before this slot existed.
    """
    word, separator, _ = text.partition(":")
    if not separator:
        return None
    return declared_severity({"severity": word.strip().lower()})


def _review_text_lines(text: str) -> Iterator[str]:
    """Yield each stripped line of emitted reviewer text.

    Both reviewer-text grammars read line by line and ignore every line their
    own shape does not match, so prose around the emitted lines does not break
    the parse. The splitting and trimming live here once, so the code-review and
    plan-report grammars cannot drift in how they read the text they are given.
    """
    for raw in text.splitlines():
        yield raw.strip()


def parse_review(
    text: str, *, record: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Parse emitted review text into the complete dimension schema.

    Returns a record shaped like the stored one, without the run metadata a
    call site adds before persisting:

    - ``status`` — ``"parsed"`` when at least one SCORE line was recognised,
      ``"unparsed"`` when none of the emitted text parsed as a review.
    - ``scores`` — dimension to score, for the dimensions that were emitted.
    - ``absent`` — schema dimensions with no recognised SCORE line.
    - ``justifications`` — dimension to its one-sentence justification.
    - ``item_verdicts`` — checklist item to the verdict sentence it carried,
      for the items that were emitted with text.
    - ``absent_items`` — checklist items with no recognised VERDICT line, or
      with one carrying no text: a line that says nothing is not a verdict.
    - ``item_aggregate`` — how many checklist items carried a verdict, when
      every item did, otherwise ``None``. Withheld on the same rule as the
      total: a count over the items that happen to be present reads as a
      review that checked fewer, which is indistinguishable from one that
      skipped one.
    - ``call_sites`` and ``call_site_count`` — the production call sites the
      reviewer verified against the change and their parseable count. An
      explicit ``CALL_SITES: none`` records an empty list and zero; an omitted
      or empty line leaves both keys absent because omission and zero are
      different claims.
    - ``call_sites_emission`` — ``"empty"`` when the reviewer emitted the
      label but supplied no site, whitespace, or only separators. The count
      remains absent in this state because the line measured nothing.
    - ``findings`` — a list of ``{"file", "line", "text"}``, each carrying a
      ``severity`` key only when the finding stated one of the declared values;
      a finding that states none is recorded without the key, never defaulted.
    - ``total`` — the arithmetic sum of the parsed scores when every dimension
      is present, otherwise ``None``. The total is never computed over a
      subset: a total taken over fewer dimensions is a lower score
      indistinguishable from a worse one.
    - ``reviewed_base_sha`` and ``reviewed_head_sha`` — the revision pair the
      review read. Base and head legacy spellings are normalised independently;
      a commit list supplies only its last entry as head. When ``record`` was
      supplied and either usable half is absent, ``status`` is ``"incomplete"``
      even when every score parsed: one revision cannot identify the diff.
    - ``raw_text`` — the verbatim emitted text, so a later reader can
      re-derive the parse from the record alone.

    ``record`` is the merged record a call site already holds — the stored
    metadata carrying legacy spellings of the pair. Supplying it lets an
    existing stored record be parsed and canonicalised in one call.

    A score outside ``0..REVIEW_MAX_SCORE`` raises :class:`ReviewScoreError`
    naming the dimension and the value; it is never clamped into range.
    """
    source_record = record
    scores: dict[str, int] = {}
    justifications: dict[str, str] = {}
    item_verdicts: dict[str, str] = {}
    findings: list[dict[str, str]] = []
    call_sites: list[str] = []
    call_sites_seen = False
    call_sites_emission: str | None = None
    base_carried = False
    base_sha: str | None = None
    head_carried = False
    head_sha: str | None = None
    for line in _review_text_lines(text):
        match = _SCORE_RE.match(line)
        if match:
            dimension = match.group(1).lower()
            value = _as_int(match.group(2))
            if value is None:
                continue
            if not 0 <= value <= REVIEW_MAX_SCORE:
                raise ReviewScoreError(dimension, value)
            if dimension in REVIEW_DIMENSIONS:
                scores[dimension] = value
            continue
        match = _JUST_RE.match(line)
        if match:
            dimension = match.group(1).lower()
            if dimension in REVIEW_DIMENSIONS:
                justifications[dimension] = match.group(2).strip()
            continue
        match = _VERDICT_RE.match(line)
        if match:
            item = match.group(1).lower()
            verdict = match.group(2).strip()
            if item in REVIEW_ITEMS and verdict:
                item_verdicts[item] = verdict
            continue
        match = _CALL_SITES_RE.match(line)
        if match:
            call_sites_seen = True
            value = match.group(1).strip()
            if value.lower() == "none":
                call_sites_emission = "none"
                item_verdicts["call_sites"] = "no production call sites found"
            else:
                call_sites = [site.strip() for site in value.split(",") if site.strip()]
                if call_sites:
                    call_sites_emission = "sites"
                    item_verdicts["call_sites"] = (
                        f"verified {len(call_sites)} production call site(s)"
                    )
                else:
                    call_sites_seen = False
                    call_sites_emission = "empty"
            continue
        match = _REVISION_RE.match(line)
        if match and match.group(1).lower() in _REVISION_LABELS:
            label = match.group(1).lower()
            value = match.group(2).split(",")
            if label in BASE_REVISION_FIELDS:
                base_carried = True
                base_sha = _sha_from(value)
            else:
                # REVISION is retained as a head-only compatibility label.
                # It cannot make a record complete without an independently
                # carried base revision.
                head_carried = True
                head_sha = _sha_from(value)
            continue
        match = _FIND_RE.match(line)
        if match:
            ref, finding_text = match.group(1), match.group(2).strip()
            if ":" in ref:
                file_path, _, line_ref = ref.rpartition(":")
                finding = {"file": file_path, "line": line_ref, "text": finding_text}
                severity = _stated_severity(finding_text)
                if severity is not None:
                    finding["severity"] = severity
                findings.append(finding)
            continue
    absent = [dim for dim in REVIEW_DIMENSIONS if dim not in scores]
    absent_items = [item for item in REVIEW_ITEMS if item not in item_verdicts]
    if not scores:
        status = "unparsed"
        total = None
    else:
        status = "parsed"
        total = sum(scores.values()) if not absent else None
    item_aggregate = None if absent_items else len(item_verdicts)
    record = {
        "status": status,
        "scores": scores,
        "absent": absent,
        "justifications": justifications,
        "item_verdicts": item_verdicts,
        "absent_items": absent_items,
        "item_aggregate": item_aggregate,
        "findings": findings,
        "total": total,
        "raw_text": text,
    }
    if call_sites_seen:
        record["call_sites"] = call_sites
        record["call_site_count"] = len(call_sites)
    if call_sites_emission == "empty":
        record["call_sites_emission"] = call_sites_emission
    if source_record is not None:
        carried_base, stored_base, carried_head, stored_head = carried_revision_pair(
            source_record
        )
        if not base_carried and carried_base:
            base_carried, base_sha = True, stored_base
        if not head_carried and carried_head:
            head_carried, head_sha = True, stored_head
    if base_carried:
        record[REVIEWED_BASE_KEY] = base_sha
    if head_carried:
        record[REVIEWED_HEAD_KEY] = head_sha
    if source_record is not None and (not base_sha or not head_sha):
        record["status"] = "incomplete"
    return record


# ── The plan-report grammar ─────────────────────────────────────────────────
# A plan review emits its own line form beside the code-review one:
#     RUBRIC <item>: <pass, or the finding it produced — one sentence>
#     FINDING <item> <file>:<line> — <what is wrong and why> — WOULD_CHANGE_THE_PLAN: <yes|no> — REASON: <one line>
# The anchor is a ``<file>:<line>`` or a ``<plan>#<node>`` reference. The
# would-change verdict and its reason are the record: a finding without the
# verdict cannot be scored, so a finding line that carries no parseable verdict
# is still returned the id, type, anchor and text with ``would_change`` left
# ``None`` rather than defaulted. Lines in any other shape are ignored, so the
# reviewer may surround the emitted lines with prose without breaking the parse.
# The line reading is shared with :func:`parse_review` through
# :func:`_review_text_lines`.
#
# The rubric name selects the checklist items a report is judged against. Both
# item sets are owned by this module and mirrored into the two prompt files
# under prompts/, so they are read from there rather than copied. The dispatch
# flag spells the two rubrics ``content`` and ``design``; both aliases are
# accepted so a sidecar written from either reaches the same item set.
_PLAN_REPORT_RUBRIC_ITEMS: dict[str, tuple[str, ...]] = {
    "plan_review": PLAN_REVIEW_ITEMS,
    "plan_design_review": PLAN_DESIGN_REVIEW_ITEMS,
}
_PLAN_REPORT_RUBRIC_ALIASES: dict[str, str] = {
    "content": "plan_review",
    "design": "plan_design_review",
}
_PLAN_RUBRIC_LINE_RE = re.compile(
    r"^RUBRIC\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", re.IGNORECASE
)
_PLAN_FINDING_LINE_RE = re.compile(r"^FINDING\s+(\S+)\s+(.+)$", re.IGNORECASE)
_PLAN_FINDING_TAIL_RE = re.compile(
    r"^(?P<anchor>.*?)\s+(?:—|--)\s+(?P<text>.*?)\s+(?:—|--)\s+"
    r"WOULD_CHANGE_THE_PLAN\s*:\s*(?P<would_change>yes|no)\b"
    r"(?:\s+(?:—|--)\s+REASON\s*:\s*(?P<reason>.*))?$",
    re.IGNORECASE,
)


def _plan_report_rubric_items(rubric: str) -> tuple[str, ...]:
    """Return the checklist items a report under ``rubric`` is judged against."""
    name = _PLAN_REPORT_RUBRIC_ALIASES.get(str(rubric or "").strip().lower(), "")
    name = name or str(rubric or "").strip().lower()
    items = _PLAN_REPORT_RUBRIC_ITEMS.get(name)
    if items is None:
        raise ValueError(
            f"unknown review rubric {rubric!r}; known rubrics are "
            f"{', '.join(PLAN_REVIEW_RUBRICS)}"
        )
    return items


def _parse_plan_finding(item: str, remainder: str) -> dict[str, Any]:
    """Parse one FINDING line's tail into a finding, per the prompt grammar.

    The structured form carries the anchor, the text, the would-change verdict
    and its reason. A line that does not carry the verdict is accepted with
    ``would_change`` left ``None`` — an unstated verdict is a recorded absence,
    not a defaulted judgement — and the anchor is read as the leading token.
    """
    match = _PLAN_FINDING_TAIL_RE.match(remainder)
    if match:
        return {
            "anchor": match.group("anchor").strip(),
            "text": match.group("text").strip(),
            "would_change": match.group("would_change").lower() == "yes",
            "reason": (match.group("reason") or "").strip(),
        }
    anchor, separator, text = remainder.partition(" ")
    if not separator:
        anchor, text = remainder, ""
    return {
        "anchor": anchor.strip(),
        "text": text.strip(),
        "would_change": None,
        "reason": "",
    }


def parse_plan_review_report(text: str, *, rubric: str) -> dict[str, Any]:
    """Parse a delivered plan-review report into the store's record shape.

    The rubric selects the checklist items the report is judged against, and
    both item sets come from this module. Returns:

    - ``rubric`` — the rubric the report was parsed under.
    - ``rubric_items`` — item to the verdict sentence its ``RUBRIC`` line
      carried, for the items that were emitted with text.
    - ``absent_items`` — checklist items with no ``RUBRIC`` line, or with one
      carrying no text: a line that says nothing is not a verdict, so the item
      is reported absent rather than silently taken as checked.
    - ``findings`` — a list of ``{"id", "type", "anchor", "text",
      "would_change", "reason"}``, one per ``FINDING`` line whose item belongs
      to the rubric and which is a mapping. The id is ``<item>-<n>`` with ``n``
      counting the report's findings of that item in the order they appear, so
      an id is stable across a re-read of the same report and two findings of
      one type never share an id.
    """
    items = _plan_report_rubric_items(rubric)
    rubric_items: dict[str, str] = {}
    findings: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for line in _review_text_lines(text):
        match = _PLAN_RUBRIC_LINE_RE.match(line)
        if match:
            item = match.group(1).lower()
            verdict = match.group(2).strip()
            if item in items and verdict:
                rubric_items[item] = verdict
            continue
        match = _PLAN_FINDING_LINE_RE.match(line)
        if match:
            item = match.group(1).lower()
            if item not in items:
                continue
            parsed = _parse_plan_finding(item, match.group(2).strip())
            counters[item] = counters.get(item, 0) + 1
            parsed["id"] = f"{item}-{counters[item]}"
            parsed["type"] = item
            findings.append(parsed)
            continue
    absent_items = [item for item in items if item not in rubric_items]
    return {
        "rubric": rubric,
        "rubric_items": rubric_items,
        "absent_items": absent_items,
        "findings": findings,
    }


# ── Failures the reviewed run's own gate logs added ─────────────────────────
# A review can score a run highly while the run's own gate logs show tests that
# were green at the base revision and red at the head. That is the failures the
# run added, and a review that omits them reports a verdict a reader cannot
# reconcile with the evidence the run itself produced.
#
# The derivation lives on the read path, not the write path. A review worker
# writes its review JSON by hand inside its own sandbox; nothing in production
# calls store_review. Deriving at write time would therefore leave every
# delivered review unannotated, which is the same as deriving nothing. So
# read_review reconstructs the count from the reviewed run's own manifest —
# run_dir(run_id)/manifest.md — every time it returns a record, and the file on
# disk is never rewritten. A hand-written review and one built through
# store_review are treated alike, because both are read through this one path.
#
# The count is a set difference over pytest node ids, not a difference of two
# counts. A run that fixed one pre-existing failure while introducing another
# nets to zero by number and added a failing test by id; the two readings
# disagree exactly on the cases the count exists to catch.
#
# The gate logs are the files the reviewed run's manifest names under
# ``baseline_suite`` and ``after_suite`` — the same fields the promotion
# suite-delta check reads. A manifest naming no base log or no head log leaves
# the count unmeasured rather than zero: a zero asserts a measurement, and one
# that was never taken is a different claim.

# The total a review of a run with unretired added failures is capped at. The
# promotion path reads REVIEW_MAX_SCORE as the score a fully satisfied dimension
# reaches, so a capped total a quarter of one dimension cannot be read as the
# reviewer's own arithmetic on the six dimensions. The record carries the
# count, the ids and the reason beside it, so the cap is never mistaken for a
# measurement the reviewer made.
ADDED_FAILURES_TOTAL_CAP = REVIEW_MAX_SCORE // 4

_GATE_FAILURE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")

# A manifest line that opens a top-level field. The value is everything after
# the first colon, which for the two suite fields is a JSON object on one line.
_MANIFEST_FIELD_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*)$"
)

# The manifest fields naming the two gate logs. The head field is read alone
# when the base field is absent, because the pair is what a difference needs and
# a missing base is a state to report rather than a reason to skip the read.
_BASE_LOG_FIELD = "baseline_suite"
_HEAD_LOG_FIELD = "after_suite"

# Within a suite field, the key naming the log file. The path is read from the
# field's JSON object, or from a ``log_path`` line written beneath the field.
_LOG_PATH_KEY = "log_path"

# Within a suite field, the key naming the command that produced the log. The
# command is where an arm records the directory it ran in.
_COMMAND_KEY = "command"

# The manifest field naming the working directory a measurement resolved from,
# which a worker running a gate or a base arm in a scratch tree records.
_MEASUREMENT_CWD_FIELD = "measurement_cwd"

# The revision a gate log's first line names is what ties its recorded count to
# the commit it measured. The conventional first line spells it ``revision
# <sha> tree <path>``; a promotion replay spells it ``revision: <sha>`` and an
# arm log may spell it ``revision=<sha>``. Only the header line is read, so a
# revision-shaped token in a test's own output cannot be mistaken for the log's.
_LOGGED_REVISION_RE = re.compile(r"revision\s*[:=]?\s*(?P<sha>[0-9a-fA-F]{7,40})\b")

# The keys the annotated review carries for the revision each arm's gate log
# names. They sit beside the added-failure count so a reader can tell the
# revision the count is evidence about without re-running the gate — the fact a
# header naming the base withholds.
_BASE_LOG_REVISION_KEY = "base_log_revision"
_HEAD_LOG_REVISION_KEY = "head_log_revision"

# The token that pins a recorded command's working directory, in the two
# spellings ``env`` accepts. The match is anchored on ``env`` so a ``-C`` flag
# belonging to some other tool in the same command is not mistaken for it.
_ENV_CHDIR_RE = re.compile(
    r"\benv\b[^\n;|&]*?(?:-C|--chdir=)\s*(?P<directory>[^\s'\";|&]+)"
)

_MANIFEST_FILE_NAME = "manifest.md"

# The key a record carries when it was found away from the path its own run id
# names. The store keys a record by the run it reviews, so a record that names
# the run it reviews while sitting at another run's path is right in content and
# wrong in filename — the shape a hand-written review produces when its worker
# keys the file on its own run id. It also keeps :func:`stored_record`'s writer
# callers honest: the path returned beside the record is a path the record was
# actually read from, not a path it should have come from.
MISFILED_KEY = "misfiled"

# The filename a record carries when the store wrote it as a partial that must
# not enter current-review selection. The content scan skips these, because the
# exemption is deliberate and content matching must not undo it.
_INCOMPLETE_RECORD_MARK = ".incomplete-"

# The manifest fields that carry the observation rather than prose about it.
# They are dropped before retirement is read: the suite fields list the very
# ids being counted, so scanning them would find every added id named and never
# cap a total. Retirement is what a worker wrote about the ids.
_MEASURED_FIELDS = (_BASE_LOG_FIELD, _HEAD_LOG_FIELD)


# The path component a node id is reduced to. A gate log does not carry the
# repository root, so the reduction anchors on the directory the repository
# keeps its tests under: the component a base arm run at the repository root
# prints first, and the one a head arm run elsewhere prints after whatever
# prefix its own working directory contributed.
_REPO_PATH_ANCHOR = "tests"

# The opening components that mark a path as written from a working directory
# rather than from the repository root: an absolute path, whose first component
# is empty, and one beginning with the current or parent directory. A spelling
# that opens with a plain component is repository-relative already, and may
# legitimately name a package that keeps its own ``tests`` directory, so it is
# left whole rather than reduced onto the anchor.
_CWD_COMPONENTS = ("", ".", "..")

# The repository root this module lives in. A recorded working directory is a
# scratch tree — a copy of the repository — so a leading id component that names
# a real path under this root is part of a repository-relative id rather than
# the tree's own contribution. An arm run as ``env -C /tmp/pkg`` where the
# repository holds a ``pkg`` package prints ``pkg/tests/x.py`` for a test that
# is not the repository's ``tests/x.py``, and the tree's basename only coincides
# with the leading component; reducing the two onto one spelling would merge a
# package's own test into the repository's ``tests/`` and hide a failure the
# head really added. The root is read from this module's own location rather
# than written down, because a checkout sits under whatever directory it was
# created in — a worktree under the node's.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# The directory name the repository occupies. An arm invoked from a directory
# above the repository prints its summary lines with this component first
# (``reckon/tests/x.py``) where the arm run at the repository root printed
# ``tests/x.py``, and the two name one test.
_REPO_DIRECTORY_NAME = _REPOSITORY_ROOT.name


# The separator a recorded working directory is split on. Its components are
# what a leading id component is matched against: pytest prints a summary line's
# id relative to the root directory it resolved, which can sit above the arm's
# own working directory, so a plain relative spelling may open with that
# directory's trailing components. An arm run as ``env -C /tmp/rcq2-base`` whose
# root directory resolved to ``/tmp`` printed ``rcq2-base/tests/x.py`` where the
# arm at the repository root printed ``tests/x.py``. The tail is what tells that
# prefix apart from a plain repository-relative spelling — the repository
# directory name above is the one instance of it that needs no recording.
_DIRECTORY_SEPARATOR = "/"


def _recorded_directory_tail(arm_directory: str | Path | None) -> tuple[str, ...]:
    """Return the components of a recorded working directory, outermost first."""
    text = str(arm_directory or "").replace("\\", _DIRECTORY_SEPARATOR).strip()
    return tuple(
        part for part in text.split(_DIRECTORY_SEPARATOR) if part not in ("", ".")
    )


def _names_repository_path(component: str) -> bool:
    """Whether ``component`` names a real path at the repository root.

    A repository-relative id opens with the name of a real repository entry —
    ``pkg/tests/x.py`` names a package's own test. A scratch tree's own
    contribution opens with the tree's basename, which is arbitrary
    (``rcq2-base``). The two are indistinguishable from the id alone when the
    tree is named like a repository path, so the id is kept whole rather than
    reduced: reducing a genuine repository-relative id merges a package's own
    test into the repository's ``tests/`` spelling and hides a failure the head
    really added, the direction the count exists to prevent.
    """
    if not component:
        return False
    try:
        return (_REPOSITORY_ROOT / component).exists()
    except OSError:
        return False


def _strip_recorded_directory(path: str, arm_directory: str | Path | None) -> str:
    """Return ``path`` without the leading components the arm's own directory contributed.

    The longest run of leading components that reproduces the tail of the
    recorded directory is removed, and the rest of the path is left for the
    caller to reduce as it would any other spelling. Removing the whole run
    rather than one component keeps a scratch tree nested below the root
    directory the log resolved against — ``tmp/rcq2-base/tests/x.py`` — reading
    as one test with the id an arm at the repository root printed, while a path
    whose leading components share no suffix with the record
    (``pkg/tests/x.py``) is untouched and stays a different test. A path the
    strip would empty keeps its own spelling, so an id naming the directory
    itself is still compared rather than reduced to nothing.

    A leading component that names a real path at the repository root is left
    whole even when it matches the directory's basename: a scratch tree named
    like a repository path (``env -C /tmp/pkg`` with a ``pkg`` package in the
    repository) would otherwise reduce a genuine ``pkg/tests/x.py`` onto the
    repository's ``tests/`` spelling.
    """
    tail = _recorded_directory_tail(arm_directory)
    if not tail:
        return path
    components = path.split(_DIRECTORY_SEPARATOR)
    if _names_repository_path(components[0]):
        return path
    for length in range(len(tail), 0, -1):
        if length < len(components) and components[:length] == list(tail[-length:]):
            return _DIRECTORY_SEPARATOR.join(components[length:])
    return path


def canonical_node_id(node_id: str, arm_directory: str | Path | None = None) -> str:
    """Return a pytest node id in the repository-relative form the count compares.

    A gate log carries whatever spelling the command that wrote it printed, and
    that command may run from anywhere: a head arm invoked from another working
    directory reports its summary lines with the path relative to that
    directory (``../../home/…/tests/x.py``) or with an absolute one, where the
    base arm run at the repository root reported ``tests/x.py``. Compared
    verbatim, one test reads as two, and a run that added nothing reads as
    having added failures — which caps its total below the promotion floor.

    Three prefixes are the working directory's contribution rather than part of
    the path, and all are removed. A path opening with an absolute or dot
    component keeps the sub-path from its last ``tests`` component onward,
    because everything above the anchor there came from where the command ran.
    A path opening with the repository's own directory name has that one
    component removed, which is what an arm invoked from the repository's
    parent printed. A path opening with the trailing components of
    ``arm_directory`` — the working directory the arm's own record names — has
    those removed, which is what an arm run inside a scratch tree printed when
    the log's root directory resolved above it. Any other plain relative
    spelling is kept whole: reducing it would merge a package's own
    ``pkg/tests/x.py`` into ``tests/x.py``, and an addition under one would read
    as one the other already had. Separators are normalised to ``/`` in every
    case. The ``::`` segments and parametrisation brackets are returned
    untouched, because they are what tells apart two tests sharing a file. An id
    carrying no ``tests`` component cannot be resolved against the repository and
    is kept verbatim: it is still compared, under the spelling its own log used,
    rather than dropped from the difference.
    """
    path, separator, segments = node_id.partition("::")
    path = path.replace("\\", "/")
    path = _strip_recorded_directory(path, arm_directory)
    components = path.split("/")
    if len(components) > 1 and components[0] == _REPO_DIRECTORY_NAME:
        components = components[1:]
        path = "/".join(components)
    if components[0] in _CWD_COMPONENTS:
        for index in range(len(components) - 1, -1, -1):
            if components[index] == _REPO_PATH_ANCHOR:
                path = "/".join(components[index:])
                break
    return path + separator + segments


def _pytest_failure_ids(
    log_text: str, arm_directory: str | Path | None = None
) -> set[str]:
    """Return the pytest node ids a gate log reports FAILED or ERROR.

    A pytest summary line is one node id per line prefixed ``FAILED`` (or
    ``ERROR`` for a collection or setup error). Node ids run to the end of the
    line, so the whole token after the prefix is taken and surrounding
    whitespace is stripped. Both logs of a difference are read through
    :func:`canonical_node_id`, so the two sides are compared as ids rather than
    as the working directory each arm happened to be invoked from, and the
    directory this arm's own record names is the one its ids are reduced
    against.
    """
    ids: set[str] = set()
    for raw in log_text.splitlines():
        match = _GATE_FAILURE_RE.match(raw.strip())
        if match:
            ids.add(canonical_node_id(match.group(1), arm_directory))
    return ids


def _manifest_field_lines(manifest_text: str, field: str) -> list[str]:
    """Return a manifest field's value text and any block written beneath it.

    The field's own line contributes the text after the colon; a field written
    with an empty value contributes the indented lines that follow, which is how
    a worker writes a nested ``log_path``. Both shapes a real manifest uses are
    therefore read without a full parse, and this reader stays local on purpose:
    the read path must not fail a review because the reviewed run's manifest
    carries a field this module does not know.
    """
    lines = manifest_text.splitlines()
    for index, raw in enumerate(lines):
        match = _MANIFEST_FIELD_RE.match(raw.strip())
        if not match or match.group("key") != field:
            continue
        collected = [match.group("value").strip()]
        indent = len(raw) - len(raw.lstrip(" \t"))
        for following in lines[index + 1 :]:
            if not following.strip():
                continue
            if len(following) - len(following.lstrip(" \t")) <= indent:
                break
            collected.append(following.strip())
        return [part for part in collected if part]
    return []


def _gate_log_path(manifest_text: str, field: str) -> str | None:
    """Return the gate-log path a manifest names for ``field``, or ``None``.

    The value is read in the two shapes a manifest produces for a suite
    observation: a JSON object carrying ``log_path``, which is what the
    dispatch contract's manifest writes, and a bare path on the field line or
    on the first line indented beneath it.
    """
    parts = _manifest_field_lines(manifest_text, field)
    if not parts:
        return None
    first = parts[0]
    if first.startswith("{"):
        try:
            loaded = json.loads(first)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, Mapping):
            named = str(loaded.get(_LOG_PATH_KEY) or "").strip()
            if named:
                return named
    if first and not first.startswith(("{", "[")):
        return first.strip("'\"")
    for part in parts[1:]:
        match = _MANIFEST_FIELD_RE.match(part)
        if match and match.group("key") == _LOG_PATH_KEY:
            named = match.group("value").strip().strip("'\"")
            if named:
                return named
    return None


def _read_gate_log(log_path: str | None, run_directory: Path | None) -> str | None:
    """Read a named gate log, resolving a relative path against ``run_directory``."""
    if not log_path:
        return None
    path = Path(log_path)
    if not path.is_absolute() and run_directory is not None:
        path = Path(run_directory) / path
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def recorded_log_revision(log_text: str | None) -> str | None:
    """Return the revision a gate log's first line names, or ``None``.

    The first line is the log's own record of the revision it ran at, and it is
    what ties the failures it lists to a commit. A log whose first line names no
    revision says nothing about what it measured, so ``None`` is returned rather
    than a guess at the arm's revision. Only the header line is read: a node id
    or a traceback path in the body can carry a revision-shaped token, and those
    belong to the output rather than to the measurement.
    """
    if not log_text:
        return None
    lines = log_text.splitlines()
    if not lines:
        return None
    match = _LOGGED_REVISION_RE.search(lines[0])
    return match.group("sha") if match else None


def _manifest_prose(manifest_text: str) -> str:
    """Return a manifest's text with the two suite observations removed.

    Retirement is read from the manifest's prose, and the suite fields are not
    prose: their ``failure_ids`` lists name every added id, so a scan that kept
    them would find each id already named and no total would ever be capped.
    The field line and the block indented beneath it are dropped together,
    because a suite observation may carry its log path on a nested line.
    """
    kept: list[str] = []
    lines = manifest_text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        match = _MANIFEST_FIELD_RE.match(raw.strip())
        if match is None or match.group("key") not in _MEASURED_FIELDS:
            kept.append(raw)
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        index += 1
        while index < len(lines):
            following = lines[index]
            deeper = len(following) - len(following.lstrip(" \t"))
            if following.strip() and deeper <= indent:
                break
            index += 1
    return "\n".join(kept)


# The punctuation a sentence may wrap a node id in, and which is therefore not
# part of the id: a manifest naming an id in backticks and following it with a
# comma writes it with both. Only the token's edges are trimmed, so the ``::``
# segments and parametrisation brackets inside an id survive untouched.
_PROSE_EDGE_CHARS = "`'\"()<>{};,:.!?*|"

# The separators a retirement sentence writes between ids. A manifest retiring
# several ids groups them as the manifest's own list fields do: comma
# separated, with or without a space. Splitting on whitespace alone leaves such
# a group as one token that names no id and retires neither of them.
_PROSE_TOKEN_SPLIT_RE = re.compile(r"[,\s]+")


def _prose_id_token(raw: str) -> str | None:
    """Return the node id a prose token carries, or ``None`` when it carries none.

    A manifest retires an added id by naming the test, and names it as the
    manifest knows it. The wrapping punctuation of the sentence around it,
    trimmed here, is not part of the name. Unbalanced square brackets are
    trimmed too — a bracketed mention is prose, not a parametrised id — while a
    parametrised id keeps its own closing bracket, balanced against the one
    that opens it.
    """
    token = raw.strip(_PROSE_EDGE_CHARS).removeprefix("[")
    if token.endswith("]") and token.count("[") < token.count("]"):
        token = token.removesuffix("]")
    return token if "::" in token else None


def _retires_id(
    text: str,
    test_id: str,
    arm_directories: tuple[str | Path | None, ...] = (),
) -> bool:
    """Whether retirement prose names a test id in full.

    The prose spelling and the counted id are compared as canonical forms
    rather than as spellings, because they are written by different hands: the
    count's id comes from a gate log, which may have been printed from another
    working directory, while the manifest names the test as the repository
    knows it. A manifest that spells the id with the working-directory prefix the
    gate log printed must still retire it, and both sides therefore reduce
    through :func:`canonical_node_id`. Each token is compared as a whole, so a
    shorter id is not retired by a longer one that contains it. The sentence is
    split on the separators a manifest's own list fields use, so a single
    sentence retiring several ids retires each of them. Each recorded working
    directory is applied to the token as well, so a manifest that spells the id
    with the prefix the arm's own tree contributed still retires the counted id.
    """
    if not text or not test_id:
        return False
    target = canonical_node_id(test_id)
    directories = tuple(directory for directory in arm_directories if directory)
    for raw in _PROSE_TOKEN_SPLIT_RE.split(text):
        token = _prose_id_token(raw)
        if token is None:
            continue
        if canonical_node_id(token) == target:
            return True
        if any(
            canonical_node_id(token, directory) == target for directory in directories
        ):
            return True
    return False


# ── The durable store ───────────────────────────────────────────────────────
# Records are keyed by project, reviewed run id and reviewed head revision,
# under the crew configuration directory but outside any run directory and
# any worktree. A recheck therefore accumulates beside its predecessor instead
# of replacing the evidence that motivated it.


def review_store_root(base_dir: str | Path | None = None) -> Path:
    """Resolve the durable review store root.

    ``base_dir`` overrides the configured crew home for a caller (or a test)
    that wants the store elsewhere; when omitted the root resolves under the
    configuration directory through :func:`reckon._store._config_home`, so a
    ``RECKON_HOME`` override moves it too.
    """
    if base_dir is not None:
        return Path(base_dir).expanduser().resolve()
    return _store._config_home() / "crew" / "reviews"


# ── The committed store ─────────────────────────────────────────────────────
# The host staging store above holds a live review where the gate and
# acceptance read it, but it is not versioned and not shared with any other
# clone: its record timestamp is when the record was stored rather than when
# the reviewed work was dispatched and completed, and answering a finding
# rewrites it in place. A review is evidence about a plan or a run, so it also
# belongs beside the ledger, in the project's repository, where it travels with
# the plan, the ledger and the evidence and keeps its history.
#
# The committed tree is ``docs/state/<project>/reviews/`` — the same
# ``docs/state/<project>`` directory the ledger lives in — with a ``plan/<slug>``
# or ``run/<reviewed-run-id>`` directory naming the subject and one file per
# review named for the review run that produced it. The path functions below
# gain the committed root beside the unchanged staging root rather than a
# second path rule, so a caller selects the tree it writes to and the staging
# path is byte-identical to what it always was.

COMMITTED_REVIEWS_DIRNAME = "reviews"
COMMITTED_PLAN_DIRNAME = "plan"
COMMITTED_RUN_DIRNAME = "run"

# The two stamps a committed record carries from the run that produced it,
# never the moment the file was stored.
DISPATCH_TIME_KEY = "dispatched_at"
COMPLETION_TIME_KEY = "completed_at"


def committed_review_root(
    project: str, *, root: str | Path | None = None
) -> Path | None:
    """Return the project's committed reviews tree, or ``None`` when unmounted.

    The tree lives under the project's own ``docs/state/<project>/`` — the
    directory its ledger is committed in — so it is shared with every clone.
    ``root`` names the checkout to resolve against (a worker's worktree);
    omitted, the project's mounted docs directory resolves it, so no caller
    supplies a second path rule. A project with no resolvable docs directory
    has no committed tree and ``None`` is returned rather than an invented
    path under the configuration home.
    """
    docs_dir = _store._docs_dir_for_project(project, root)
    if docs_dir is None:
        return None
    return docs_dir / "state" / project / COMMITTED_REVIEWS_DIRNAME


def _blob_suffix(value: str, *, length: int | None = None) -> str:
    """Validate a revision and return its ``.at-<sha>`` sibling suffix.

    ``length`` truncates the digest to that many characters; omitted, the whole
    digest is kept. A value that is not a hex revision of at least seven
    characters is refused rather than silently normalised into a path. Callers
    that key a stored record by a content blob — a code review by the reviewed
    head, a plan review by the reviewed plan blob — build the suffix through
    here, so the store's keying rule is spelled once.
    """
    sha = str(value).strip()
    if not re.fullmatch(r"[0-9A-Fa-f]{7,64}", sha):
        raise ValueError(f"invalid reviewed_head_sha {value!r}")
    sha = sha.lower()
    if length is not None:
        sha = sha[:length]
    return f".at-{sha}"


def review_path(
    project: str,
    reviewed_run_id: str,
    base_dir: str | Path | None = None,
    *,
    reviewed_head_sha: str | None = None,
    committed_root: str | Path | None = None,
    review_run_id: str | None = None,
) -> Path:
    """Return the store path of one run review.

    The staging path — ``<store>/<project>/<reviewed-run-id>.json``, with a
    ``.at-<sha>`` sibling when the reviewed head is named — is unchanged and
    is what is returned when ``committed_root`` is omitted.

    ``committed_root`` selects the committed tree instead:
    ``<committed_root>/run/<reviewed-run-id>/<review-id>.json``, the run
    directory named for the reviewed run and the file named for the review run
    that produced it. ``review_run_id`` defaults to the reviewed run id so a
    caller that writes the record of the run itself names one file; the
    committed file is keyed by the review run rather than by a head revision,
    because a re-review is a new review run rather than a second file of one
    run.
    """
    if committed_root is not None:
        name = str(review_run_id or "").strip() or reviewed_run_id
        return (
            Path(committed_root)
            / COMMITTED_RUN_DIRNAME
            / reviewed_run_id
            / f"{name}.json"
        )
    suffix = "" if reviewed_head_sha is None else _blob_suffix(reviewed_head_sha)
    return review_store_root(base_dir) / project / f"{reviewed_run_id}{suffix}.json"


def _complete_review_exists(
    project: str,
    reviewed_run_id: str,
    base_dir: str | Path | None,
) -> bool:
    """Return whether any stored record for the run carries a usable pair."""
    directory = review_store_root(base_dir) / project
    candidates = [review_path(project, reviewed_run_id, base_dir)]
    if directory.is_dir():
        candidates.extend(directory.glob(f"{reviewed_run_id}.at-*.json"))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        _, base_sha, _, head_sha = carried_revision_pair(stored)
        if base_sha and head_sha:
            return True
    return False


def _partial_identity(record: Mapping[str, Any]) -> str:
    """Return the identity that keeps two partial records of one run apart.

    A record that cannot supply both halves of the revision pair is stored
    outside current-review selection, keyed by the run that wrote it. Two such
    records from one reviewing run would otherwise land on one path and the
    second would destroy the first, so the head the partial did carry — the one
    fact that distinguishes two partial answers about the same run — is folded
    in ahead of the reviewing-run id. That id stays last so the suffix a caller
    was already told about is unchanged, and a record carrying no run id falls
    back to its own timestamp.
    """
    head_sha = carried_revision_pair(record)[3]
    reviewing = str(record.get("review_run_id") or "").strip()
    if reviewing:
        return "-".join(part for part in (head_sha or "", reviewing) if part)
    return str(record.get("timestamp") or "").strip() or "unknown"


def _incomplete_review_path(
    project: str,
    reviewed_run_id: str,
    record: Mapping[str, Any],
    base_dir: str | Path | None,
) -> Path:
    """Return a durable path excluded from current-review selection."""
    identity = re.sub(r"[^A-Za-z0-9._-]+", "-", _partial_identity(record))
    identity = identity.strip("-.") or "unknown"
    return (
        review_store_root(base_dir)
        / project
        / f"{reviewed_run_id}.incomplete-{identity}.json"
    )


def _stored_partial_identity(path: Path) -> str:
    """Return the partial identity a stored file holds, or ``""`` if unreadable."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return _partial_identity(stored) if isinstance(stored, Mapping) else ""


def _partial_review_path(
    project: str,
    reviewed_run_id: str,
    record: Mapping[str, Any],
    base_dir: str | Path | None,
) -> Path:
    """Return where a record lacking the revision pair is stored.

    A complete record stored beside this run keeps the partial out of
    current-review selection whatever it carries. With no complete record, the
    first partial keeps the legacy path so a reader that has not learned to name
    a revision still finds it; rewriting that same partial targets the same
    path, while a second partial carrying different revision evidence must not
    overwrite it and moves to the identity-keyed path beside it.
    """
    if _complete_review_exists(project, reviewed_run_id, base_dir):
        return _incomplete_review_path(project, reviewed_run_id, record, base_dir)
    legacy = review_path(project, reviewed_run_id, base_dir)
    if not legacy.is_file():
        return legacy
    if _stored_partial_identity(legacy) == _partial_identity(record):
        return legacy
    return _incomplete_review_path(project, reviewed_run_id, record, base_dir)


def added_failures_from_gate_logs(
    base_text: str | None,
    head_text: str | None,
    *,
    base_directory: str | Path | None = None,
    head_directory: str | Path | None = None,
) -> tuple[int | None, list[str]]:
    """Return the added-failure count and ids the two gate logs support.

    The count is the pytest node ids reported FAILED or ERROR in the head log
    that do not appear in the base log. ``(None, [])`` means unmeasured: one or
    both logs are absent, and a count that was never taken must not be stored as
    a measured zero. A count of ``0`` with no ids means both logs were read and
    the run added no failing test.

    Each log is read against its own arm's recorded working directory, so ids an
    arm printed from inside a scratch tree are compared in the
    repository-relative form rather than under the prefix that tree
    contributed.
    """
    if base_text is None or head_text is None:
        return None, []
    added = sorted(
        _pytest_failure_ids(head_text, head_directory)
        - _pytest_failure_ids(base_text, base_directory)
    )
    return len(added), added


def annotate_added_failures(
    record: dict[str, Any],
    *,
    base_text: str | None,
    head_text: str | None,
    retirement_text: str = "",
    base_directory: str | Path | None = None,
    head_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Store the reviewed run's added failures on ``record`` and cap its total.

    Given the two gate logs' text and the retirement prose to read, records the
    added node ids and their count, then caps a nonzero count's stored total at
    :data:`ADDED_FAILURES_TOTAL_CAP` — unless ``retirement_text`` names every
    added id in full, the reviewed node's ``done_when`` or its manifest having
    retired those tests, in which case the total is left as the reviewer scored
    it and the record says the ids were retired.

    The count is recorded as unmeasured, never as zero, when either log is
    absent; the total is left alone in that case. The note naming the cap is
    written only when the cap lowered the stored total, so a total already at or
    below the cap is left unannotated and a record whose own total is null reads
    as unscored with its added failures counted. The returned record is a copy;
    the caller's mapping is not mutated.

    ``base_directory`` and ``head_directory`` are the working directories the
    two arms' own records name, and every id on the count's two sides is reduced
    against the arm that printed it.
    """
    result = dict(record)
    count, added_ids = added_failures_from_gate_logs(
        base_text,
        head_text,
        base_directory=base_directory,
        head_directory=head_directory,
    )
    result["added_failure_count"] = count
    result["added_failure_ids"] = added_ids
    if count is None:
        missing = "base" if base_text is None else "head"
        result["added_failures_note"] = (
            f"unmeasured: the reviewed manifest names no {missing} gate log, so "
            "the added-failure count was not taken"
        )
        return result
    if count == 0:
        return result
    unretired = [
        test_id
        for test_id in added_ids
        if not _retires_id(retirement_text, test_id, (base_directory, head_directory))
    ]
    if not unretired:
        result["added_failures_note"] = (
            "added failures retired by name in the reviewed node's done-when or "
            "manifest; total not capped"
        )
        return result
    stored_total = result.get("total")
    if stored_total is None:
        # A record the reviewer left unscored has no total to lower, so the note
        # records the added failures without naming a cap that was never applied.
        result["added_failures_note"] = f"unscored; {count} added failures"
        return result
    capped = min(int(stored_total), ADDED_FAILURES_TOTAL_CAP)
    result["total"] = capped
    if capped < int(stored_total):
        result["added_failures_note"] = (
            f"total capped at {ADDED_FAILURES_TOTAL_CAP}: the reviewed run added "
            f"{count} failing test(s) not retired by name ({', '.join(unretired)})"
        )
    return result


def _recorded_working_directory(manifest_text: str) -> str | None:
    """Return the working directory the manifest records for a measurement, if any.

    The field carries a bare path on its own line, which is the shape the
    dispatch contract's manifest writes. A value that opens as a JSON object or
    list is a structured record rather than a path and is not read as one.
    """
    for part in _manifest_field_lines(manifest_text, _MEASUREMENT_CWD_FIELD):
        named = part.strip().strip("'\"")
        if named and not named.startswith(("{", "[")):
            return named
    return None


def _suite_value(manifest_text: str, field: str, key: str) -> str | None:
    """Return one named value from a suite field, or ``None`` when it names none.

    The suite fields are written in the two shapes a manifest produces: a JSON
    object on the field's own line, which is what the dispatch contract's
    manifest writes, and a ``key: value`` line indented beneath the field. Both
    are read, so a worker writing either shape has its record understood.
    """
    parts = _manifest_field_lines(manifest_text, field)
    if not parts:
        return None
    first = parts[0]
    if first.startswith("{"):
        try:
            loaded = json.loads(first)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, Mapping):
            named = str(loaded.get(key) or "").strip()
            if named:
                return named
    for part in parts[1:]:
        match = _MANIFEST_FIELD_RE.match(part)
        if match and match.group("key") == key:
            named = match.group("value").strip().strip("'\"")
            if named:
                return named
    return None


def _arm_directory(manifest_text: str, field: str) -> str | None:
    """Return the working directory one arm's own record names, or ``None``.

    Two recorded facts can name it. A gate command pinning the run with ``env
    -C <directory>`` decides the directory the arm ran in. The manifest's own
    ``measurement_cwd`` names the tree a measurement in a scratch tree resolved
    from, and it is attributed to the arm whose command mentions it — the arm
    that ran elsewhere is left without one rather than given its sibling's tree,
    because the reduction is applied per arm and a directory attributed to the
    wrong arm reduces that arm's ids. An arm whose suite record names no command
    at all names no directory either: the manifest's single ``measurement_cwd``
    is not evidence that this arm ran there, so it is withheld rather than
    attributed to whichever arm is read first.
    """
    recorded = _recorded_working_directory(manifest_text)
    command = _suite_value(manifest_text, field, _COMMAND_KEY) or ""
    if not command:
        return None
    match = _ENV_CHDIR_RE.search(command)
    if match:
        return match.group("directory")
    if recorded and recorded in command:
        return recorded
    return None


def _run_directory(reviewed_run_id: str) -> Path | None:
    """Return the reviewed run's own directory, or ``None`` when it has none.

    The import is local because the run registry reads this module's store root
    to enumerate delivery locations, so a module-level edge between the two
    would be a cycle.
    """
    try:
        from reckon.crew.runs import run_dir

        directory = run_dir(reviewed_run_id)
    except (OSError, ValueError):
        return None
    return directory if directory.is_dir() else None


def annotate_review_of_run(
    record: dict[str, Any], reviewed_run_id: str
) -> dict[str, Any]:
    """Annotate ``record`` with the failures the reviewed run's own evidence added.

    Reads ``run_dir(reviewed_run_id)/manifest.md`` and the two gate logs it
    names, then applies :func:`annotate_added_failures`. A reviewed run with no
    manifest on disk yields the record unchanged: there is no evidence to derive
    from, and a review read before its run delivered a manifest must not gain a
    count that was never taken. A manifest that is present but names no base log
    or no head log yields an explicit unmeasured count, which is a state the
    record can carry.

    Each log is read against the working directory its own arm's record names,
    so an arm run inside a scratch tree is compared with the arm at the
    repository root rather than under the tree's prefix.
    """
    run_directory = _run_directory(reviewed_run_id)
    if run_directory is None:
        return record
    try:
        manifest_text = (run_directory / _MANIFEST_FILE_NAME).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return record
    base_text = _read_gate_log(
        _gate_log_path(manifest_text, _BASE_LOG_FIELD), run_directory
    )
    head_text = _read_gate_log(
        _gate_log_path(manifest_text, _HEAD_LOG_FIELD), run_directory
    )
    annotated = annotate_added_failures(
        record,
        base_text=base_text,
        head_text=head_text,
        retirement_text=_manifest_prose(manifest_text),
        base_directory=_arm_directory(manifest_text, _BASE_LOG_FIELD),
        head_directory=_arm_directory(manifest_text, _HEAD_LOG_FIELD),
    )
    # Each arm log's own header says which revision it measured, so recording it
    # lets a reader tie the added-failure count to that commit without re-running
    # the gate. A header naming the base while the change is at another commit
    # then reads as the mismatch it is rather than as evidence about the change.
    annotated[_BASE_LOG_REVISION_KEY] = recorded_log_revision(base_text)
    annotated[_HEAD_LOG_REVISION_KEY] = recorded_log_revision(head_text)
    return annotated


def store_review(
    record: dict[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """Persist one review record and return the path it was written to.

    The record must name ``project`` and ``reviewed_run_id``, which key the
    file. A missing ``timestamp`` is stamped with the current UTC moment so
    every stored record carries one; an existing timestamp is preserved. The
    five legacy revision spellings are normalised onto the canonical base/head
    pair before writing. A carried revision that names no commit in the
    reviewed run's own repository is refused before anything is written: a
    record keyed to it matches no revision any reader compares against, so
    refusing it here is what keeps the reviewer from storing a record whose
    subject cannot be found. A complete pair selects a revision-keyed path. A
    record lacking the pair is preserved outside current-review selection under
    its reviewing-run identity, so it can neither displace a complete review
    nor overwrite another partial record of the same run. The write is atomic.

    The added-failure derivation is not applied here. Records are annotated
    when they are read, from the reviewed run's own manifest, so a record may be
    written by a caller with no access to that manifest — a review worker
    writing its JSON by hand is the production case — and still be annotated
    for every reader. See :func:`annotate_review_of_run`.
    """
    project = record.get("project")
    reviewed_run_id = record.get("reviewed_run_id")
    if not project:
        raise ValueError("review record is missing project")
    if not reviewed_run_id:
        raise ValueError("review record is missing reviewed_run_id")
    base_carried, base_sha, head_carried, head_sha = carried_revision_pair(record)
    if base_carried or head_carried:
        record = dict(record)
        if base_carried:
            record[REVIEWED_BASE_KEY] = base_sha
        if head_carried:
            record[REVIEWED_HEAD_KEY] = head_sha
    refusal = unresolved_reviewed_revision(record)
    if refusal:
        raise ValueError(refusal)
    if not record.get("timestamp"):
        record = dict(record)
        record["timestamp"] = datetime.now(UTC).isoformat()
    if base_sha and head_sha:
        path = review_path(
            project,
            reviewed_run_id,
            base_dir,
            reviewed_head_sha=head_sha,
        )
    else:
        path = _partial_review_path(project, reviewed_run_id, record, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_record(path, record)
    return path


def run_record_times(
    project: str,
    run_id: str,
    *,
    root: str | Path | None = None,
) -> tuple[str, str]:
    """Return the ``(dispatched_at, completed_at)`` a run's own record carries.

    The run's own record — its committed per-run file beside the ledger — is
    what carries both stamps, so a committed review's times are the run's own
    rather than the moment the review file was stored. The live pointer is read
    only as a fallback for a run not yet promoted, and contributes its single
    ``created_at`` dispatch stamp; a run carrying no stamp yields an empty
    string for it rather than the store's own clock, because a defaulted time
    is a time nobody recorded.
    """
    if not run_id:
        return "", ""
    from reckon import ledger

    try:
        path = ledger.run_path(project, run_id, root)
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ledger.LedgerError):
        record = None
    if not isinstance(record, Mapping):
        pointer = _reviewed_run_pointer(run_id)
        if pointer is None:
            return "", ""
        return str(pointer.get("created_at") or ""), str(
            pointer.get("completed_at") or ""
        )
    return (
        str(record.get("dispatched_at") or ""),
        str(record.get("completed_at") or ""),
    )


def store_committed_review(
    record: dict[str, Any],
    *,
    root: str | Path | None = None,
    committed_root: str | Path | None = None,
) -> Path:
    """Persist one review record into the project's committed tree.

    The record must name ``project`` and ``reviewed_run_id``. ``review_run_id``
    — the run that produced the review — keys the committed file and supplies
    the dispatch and completion times, so the record carries when the review
    ran rather than when its file was stored; when it is absent the reviewed
    run supplies both. A stamp the run's own record does not carry is left off
    the committed record entirely: the store clock is never substituted for it,
    which is the defect the committed store exists to remove.

    ``committed_root`` names the tree directly (a caller that already resolved
    it, or a test); omitted, it resolves through :func:`committed_review_root`
    against ``root``. A plan review (one naming ``plan_slug``) is written under
    ``plan/<slug>/`` and a run review under ``run/<reviewed-run-id>/``; the write
    is atomic and every other body field is preserved.
    """
    project = str(record.get("project") or "").strip()
    if not project:
        raise ValueError("review record is missing project")
    reviewed_run_id = str(record.get("reviewed_run_id") or "").strip()
    if not reviewed_run_id:
        raise ValueError("review record is missing reviewed_run_id")
    review_run_id = str(record.get("review_run_id") or "").strip() or reviewed_run_id
    if committed_root is None:
        committed_root = committed_review_root(project, root=root)
    if committed_root is None:
        raise ValueError(f"no committed reviews tree resolves for project {project!r}")

    dispatched, completed = run_record_times(project, review_run_id, root=root)
    if not dispatched or not completed:
        review_dispatched, review_completed = run_record_times(
            project, reviewed_run_id, root=root
        )
        dispatched = dispatched or review_dispatched
        completed = completed or review_completed

    stored = dict(record)
    if dispatched:
        stored[DISPATCH_TIME_KEY] = dispatched
    if completed:
        stored[COMPLETION_TIME_KEY] = completed
    if not stored.get("timestamp"):
        stored["timestamp"] = datetime.now(UTC).isoformat()

    plan_slug = str(record.get("plan_slug") or "").strip()
    if plan_slug:
        from reckon.crew.plan_review import plan_review_path

        path = plan_review_path(
            project,
            plan_slug,
            int(record.get("plan_version") or 0),
            committed_root=committed_root,
            review_run_id=review_run_id,
        )
    else:
        path = review_path(
            project,
            reviewed_run_id,
            committed_root=committed_root,
            review_run_id=review_run_id,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_record(path, stored)
    return path


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


def stored_record(
    project: str,
    reviewed_run_id: str,
    *,
    base_dir: str | Path | None = None,
    reviewed_head_sha: str | None = None,
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

    The record returned is the file's own content, never the read-time
    annotation :func:`read_review` adds, so a caller that writes the record
    back does not persist a derived view. One exception is a record found away
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

    A record sitting at one of the run's own paths — the bare path or one keyed
    by a revision — settles where this run's review is filed, so the search for
    a record another writer filed under a different run id does not run: a
    named head that matched none of them is a fact about the revision, not
    about the filing, and the caller that wants the newest record asks again
    without a head. Only a run with none of its own files reaches the
    store-wide search.

    A record file that cannot be read or does not parse — a review worker
    composing its record by hand can be met mid-write — is skipped rather than
    raised: the reader answers from the records it could read, and every file
    it could not is appended to the caller's ``skipped`` list, or to the
    collection region a :func:`collect_read_failures` caller opened around the
    read, so a publisher can name it instead of the run reading as one that has
    no review at all. A read that names neither is nobody's finding and is
    dropped.
    """
    directory = review_store_root(base_dir) / project
    candidates = [review_path(project, reviewed_run_id, base_dir)]
    if directory.is_dir():
        candidates.extend(directory.glob(f"{reviewed_run_id}.at-*.json"))
    existing = {path.resolve(): path for path in candidates if path.is_file()}
    if existing:
        records: list[tuple[Path, Any, int]] = []
        for path in existing.values():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                mtime_ns = path.stat().st_mtime_ns
            except (OSError, ValueError) as error:
                # A record caught mid-write is skipped, not raised: the reader
                # still answers from the records it could read, and the file it
                # could not is named to the caller that asked.
                _note_skipped_record(path, error, into=skipped)
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
    # No file of this run's own exists, so the store's records are searched for
    # one whose content names the run it reviews — the shape a hand-written
    # record takes when its worker keys the file on its own run id instead of
    # the reviewed one.
    return _record_filed_elsewhere(
        directory, reviewed_run_id, reviewed_head_sha, skipped=skipped
    )


def _record_filed_elsewhere(
    directory: Path,
    reviewed_run_id: str,
    reviewed_head_sha: str | None,
    *,
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

_STORE_INDEXES: dict[Path, tuple[tuple[int, ...], dict[str, list[dict[str, Any]]]]] = {}


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


def _build_store_index(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Read each record file once, keyed by the run its content reviews."""
    index: dict[str, list[dict[str, Any]]] = {}
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
        index.setdefault(str(record.get("reviewed_run_id") or ""), []).append(
            {
                "path": path,
                "mtime_ns": mtime_ns,
                "head_carried": carried_head,
                "head_sha": head_sha,
            }
        )
    return index


def _store_index(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the store's record index, rebuilt only when the directory moves."""
    identity = _store_directory_identity(directory)
    if identity is None:
        _STORE_INDEXES.pop(directory, None)
        return {}
    cached = _STORE_INDEXES.get(directory)
    if cached is not None and cached[0] == identity:
        return cached[1]
    index = _build_store_index(directory)
    _STORE_INDEXES[directory] = (identity, index)
    return index


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


def read_review(
    project: str,
    reviewed_run_id: str,
    *,
    base_dir: str | Path | None = None,
    reviewed_head_sha: str | None = None,
) -> dict[str, Any] | None:
    """Return a stored review, optionally selecting the head it reviewed.

    A named head is matched against the record's normalised
    ``reviewed_head_sha`` rather than trusted from its filename, which keeps
    older short-sha preservation copies readable. Without a named head, the
    newest record is returned for compatibility with callers that have not yet
    learned to state the revision they need.

    A record filed under another run id is still returned when its content
    names this run, carrying :data:`MISFILED_KEY`; see :func:`stored_record`.
    The returned record is annotated with the failures the reviewed run's own
    gate logs added, and its total is capped when those failures were not
    retired by name; see :func:`annotate_review_of_run`. The stored file is not
    rewritten — the annotation is a read-time view — so a review written by hand
    and one written through :func:`store_review` are read alike, and a reader
    that never calls this function sees the record exactly as it was stored.
    """
    _, record = stored_record(
        project,
        reviewed_run_id,
        base_dir=base_dir,
        reviewed_head_sha=reviewed_head_sha,
    )
    if record is None:
        return None
    return annotate_review_of_run(record, reviewed_run_id)


def ledger_block(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the compact shape a promoted ledger row stores for a review.

    ``None`` means no review was stored — the run was promoted unreviewed,
    which is not a score of zero. A stored record reduces to its status,
    per-dimension scores, absent dimensions and total, dropping the verbatim
    text and findings that belong in the review store. A record that never
    produced scores keeps a status other than ``"parsed"`` with empty scores,
    so it reads unlike an absent review and unlike a parsed review whose
    dimensions genuinely measure zero.

    A record that names the revision it read carries the canonical
    ``reviewed_base_sha``/``reviewed_head_sha`` pair onto the block, resolved
    through :func:`carried_revision_pair` so any of the store's legacy
    spellings reaches the row under the canonical names. A record naming none
    keeps the bare block: the absence is a fact about the record, and a
    defaulted pair would claim the review read a revision nobody recorded.

    The checklist item verdicts are deliberately not carried here. This block
    is what a promotion and a lane comparison read, and both are defined on
    the six dimensions alone; the item verdicts are recorded in the stored
    record instead. A ledger block that gained a second, non-comparable set of
    fields would change what those readers mean without changing their code.
    """
    if record is None:
        return None
    score_values = record.get("scores")
    scores: dict[str, int] = {}
    if isinstance(score_values, Mapping):
        scores = {
            str(dimension): int(value) for dimension, value in score_values.items()
        }
    total = record.get("total")
    block: dict[str, Any] = {
        "status": str(record.get("status") or "unparsed"),
        "scores": scores,
        "absent": [str(dimension) for dimension in (record.get("absent") or [])],
        "total": None if total is None else int(total),
    }
    base_carried, base_sha, head_carried, head_sha = carried_revision_pair(record)
    if base_carried:
        block[REVIEWED_BASE_KEY] = base_sha
    if head_carried:
        block[REVIEWED_HEAD_KEY] = head_sha
    return block


# ── Floors on the dimensions, and the dispositions that answer them ─────────
# A total is a sum, so a single dimension far below the others is invisible in
# it: a review whose durability is 5 of 20 promotes on the same 78 as one whose
# six dimensions are even. The floor closes that, and it lives in flight
# configuration under ``gates.dimension_floors``, keyed by dimension name, so
# the standard a stored score is read against travels with the other gate
# settings and is readable by whoever is deciding what to do next.
#
# A dimension the map does not name carries no floor. A floor of zero would
# report nothing, since every stored score is at or above zero — so reading an
# undeclared floor as zero would claim a standard the configuration never
# declared while changing no verdict. The absence of a declared floor is read
# as the absence of a standard rather than as a standard of nothing.
#
# A sub-floor dimension is a finding with its own disposition, never a lower
# total: the total remains the reviewer's arithmetic over what it parsed, and
# the dimension keeps its own recorded score. The finding stands until a
# disposition in the closed set below is recorded against that dimension on
# the same stored record, by the code path that does the deciding.

DIMENSION_FLOORS_KEY = "dimension_floors"

# Where a disposition lives on the stored record. Keyed by dimension name, so
# one review may carry a folded durability and an exempted fit at once, and
# each row's own answer is readable without re-deriving it.
DIMENSION_DISPOSITIONS_KEY = "dimension_dispositions"

# The closed set a disposition may come from. ``folded`` names the dispatched
# node the finding was folded into; ``exempted`` records the reason it is not
# being acted on. Nothing else clears a finding: an unrecognised or incomplete
# entry leaves the row standing rather than silently retiring it, so a session
# cannot report a clean close over a dimension nobody answered.
DIMENSION_DISPOSITION_KINDS: tuple[str, ...] = ("folded", "exempted")


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
    configuration. Only the dimensions this module defines are returned: a key
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
    field, including the reviewer's verbatim text.
    """
    dimension_name = str(dimension or "").strip().lower()
    if dimension_name not in REVIEW_DIMENSIONS:
        raise ValueError(
            f"unknown review dimension {dimension!r}; "
            f"known dimensions are {', '.join(REVIEW_DIMENSIONS)}"
        )
    disposition_kind = str(kind or "").strip().lower()
    named_node = str(node or "").strip()
    recorded_reason = str(reason or "").strip()
    # The gate here is the reader's own predicate, so a disposition this writer
    # accepts is one :func:`sub_floor_dimensions` will honour. A second copy of
    # the closed set in the writer could accept a kind the reader then ignores,
    # which stores a row that reads as answered and stands.
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
    # The field a caller supplies for the other kind is refused rather than
    # dropped: a stored entry that carries a reason no fold can name, or a node
    # no exemption can stand behind, reads as recorded on both sides of a
    # question the caller only half answered.
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
    disposition: dict[str, Any] = {
        "kind": disposition_kind,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    if disposition_kind == "folded":
        disposition["node"] = named_node
    else:
        disposition["reason"] = recorded_reason
    dispositions = record.get(DIMENSION_DISPOSITIONS_KEY)
    merged = dict(dispositions) if isinstance(dispositions, Mapping) else {}
    merged[dimension_name] = disposition
    record[DIMENSION_DISPOSITIONS_KEY] = merged

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_record(path, record)
    return path


def _write_record(path: Path, record: Mapping[str, Any]) -> None:
    """Write one record to ``path`` atomically through the shared writer.

    The caller has already materialised ``path``'s parent, so the writer is
    asked not to create it: a missing parent is a caller error here, not a
    directory to resurrect. The record is indented and key-sorted as before,
    and the parent keeps its previous mode rather than taking a private one.
    """
    write_json_atomically(
        path,
        dict(record),
        indent=2,
        sort_keys=True,
        mode=None,
        fsync=False,
        create_parents=False,
    )
