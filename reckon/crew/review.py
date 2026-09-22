"""Schema, parser and durable store for independent node reviews.

A promoted run today carries a gate the worker wrote itself, which is why
gate pass sits at 1.00 on every lane in the committed ledger and cannot
separate one lane from another. This module supplies the second opinion: the
artefacts an independent review worker emits, how they are parsed into one
five-dimension record, and where those records live so they survive the run
directory and the worktree that produced them.

The five dimensions and their meanings are authored once in two mirrored
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
:func:`reckon.crew.recovery._review_is_complete`), which stays the five
dimensions: reviews stored before this line existed carry no item verdicts,
and folding them into that predicate would mark every one of them incomplete.

It also records the production call sites the reviewer verified against the
change. A literal ``CALL_SITES: none`` is an explicit zero; an omitted line is
left absent so a missing measurement cannot be mistaken for a measured zero.
A label with no usable value is also left absent, but carries a machine-readable
emission state so a partial answer is not confused with an omitted question.

It records the revision the review read under one canonical key,
``reviewed_revision``. A review is evidence about a revision: once a repair
lands, a stored verdict describes code that no longer exists, so a guard that
reads the presence of a review as evidence about the code being promoted is
reading a true statement that stopped being the one required. Staleness is
detectable today and nothing looks, because the store spells that one fact
five ways — ``reviewed_head_sha``, ``reviewed_commit``, ``commits_read``,
``reviewed_base_sha`` and ``reviewed_base`` — and no reader knows any of them.
The canonical key is populated from whichever spelling a record carries, by
key presence rather than truthiness, so a record holding the field with an
empty value is a *recorded absence* and a record holding none of them is an
*unread field*; those are different claims and stay distinguishable.

The parser is on the live path, not a library awaiting a caller: dispatch and
runs resolve the review store through :func:`review_store_root`,
``reckon/crew/promotion.py`` reduces a stored record to the ledger block it
writes, and ``reckon/crew/recovery.py`` decides ``promotable`` from the
parsed dimensions. A change here changes what all of them store or decide.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _store

# ── The schema: five dimensions, one maximum ────────────────────────────────
# A dimension added later is added in exactly one place. The mirror lives in
# prompts/review.md, whose text the falsifier checks against these names.

REVIEW_DIMENSIONS: tuple[str, ...] = (
    "goal_fidelity",
    "evidence",
    "scope_discipline",
    "durability",
    "fit",
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

# ── The revision a review read, under one canonical key ─────────────────────
# The fact is recorded in the store under five different names. Reading any one
# of them makes the comparison a guess about which field a given reviewer set,
# so the record is normalised onto REVIEW_REVISION_KEY from whichever spelling
# it carries. Preserving the legacy spellings is deliberate: they are what the
# stored records already hold, and rewriting them would churn the store.
REVIEW_REVISION_KEY = "reviewed_revision"

# Precedence, and why. The staleness comparison is against the reviewed run's
# landed head, so head spellings outrank base spellings: a record naming both
# is compared on the revision the review looked at rather than the one it
# diffed against. Within a class the explicit sha field outranks the commit
# list, whose last entry is the head it read.
REVISION_FIELDS: tuple[str, ...] = (
    "reviewed_head_sha",
    "reviewed_commit",
    "commits_read",
    "reviewed_base_sha",
    "reviewed_base",
)

# The emitted form gains a slot for the revision, so a review written from now
# on states it rather than having it inferred from a call site's metadata. The
# legacy spellings are accepted as labels too, because the store's own records
# were emitted by prompts that asked for them by name.
_REVISION_LABELS = frozenset({"revision", REVIEW_REVISION_KEY, *REVISION_FIELDS})

# The prompt is a versioned, diffable file rather than a string inside this
# module, so editing it is a text change rather than a code change. It is read
# from disk on every call: the module holds the path, not the text.
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "review.md"


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


def carried_revision(record: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Resolve the revision a review read from whichever spelling ``record`` carries.

    Returns ``(carried, revision)``. ``carried`` is **key presence, never
    truthiness**: a record holding ``reviewed_commit: null`` carries the field,
    and its ``None`` is a recorded absence — a different claim from a record
    carrying none of the spellings, whose age is unknown rather than absent.
    Falling through to a later spelling on an empty value would collapse those
    two claims back together, which is the defect this key exists to remove.
    """
    for field in REVISION_FIELDS:
        if field in record:
            return True, _sha_from(record[field])
    return False, None


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


# ── The parser ──────────────────────────────────────────────────────────────
# The emitted form the prompt asks for is one line per element:
#     VERDICT <item>: <one sentence saying what was read and what was found>
#     SCORE <dimension>: <integer 0..20>
#     JUSTIFICATION <dimension>: <one sentence citing a path or a line>
#     FINDING <file>:<line> <what is wrong and why it matters>
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


def parse_review(
    text: str, *, record: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Parse emitted review text into the five-dimension schema.

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
    - ``findings`` — a list of ``{"file", "line", "text"}``.
    - ``total`` — the arithmetic sum of the parsed scores when every dimension
      is present, otherwise ``None``. The total is never computed over a
      subset: a total taken over fewer dimensions is a lower score
      indistinguishable from a worse one.
    - ``reviewed_revision`` — the revision the review read, under the one
      canonical key. It is taken from an emitted revision line when the text
      carries one, otherwise from ``record`` if a spelling of it was supplied,
      otherwise the key is left **absent** rather than empty. ``None`` as the
      value is a recorded absence: a spelling was carried and held no sha,
      which is a different claim from a key that is not there at all.
    - ``raw_text`` — the verbatim emitted text, so a later reader can
      re-derive the parse from the record alone.

    ``record`` is the merged record a call site already holds — the stored
    metadata carrying one of the legacy spellings of the revision. Supplying
    it lets an existing stored record be parsed *and* canonicalised in one
    call, which is the retrofit path: the emitted slot (``REVISION:`` and the
    legacy labels) serves reviews written from now on, and ``record`` serves
    the ones already in the store.

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
    revision_carried = False
    revision: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
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
            # The revision line is the one line whose label is not a fixed
            # prefix: a review may state it canonically or under any of the
            # five spellings the store already uses, so the label is matched
            # against the accepted set rather than the shape of the line.
            revision_carried = True
            revision = _sha_from(match.group(2).split(","))
            continue
        match = _FIND_RE.match(line)
        if match:
            ref, finding_text = match.group(1), match.group(2).strip()
            if ":" in ref:
                file_path, _, line_ref = ref.rpartition(":")
                findings.append(
                    {"file": file_path, "line": line_ref, "text": finding_text}
                )
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
    if not revision_carried and source_record is not None:
        # The emitted text states the revision when it can; otherwise a record
        # the caller already holds is canonicalised, so an existing stored
        # entry carrying a legacy spelling gains the canonical key too.
        revision_carried, revision = carried_revision(source_record)
    if revision_carried:
        record["reviewed_revision"] = revision
    return record


# ── The durable store ───────────────────────────────────────────────────────
# One record per reviewed run, keyed by project and reviewed run id, under the
# crew configuration directory but outside any run directory and any worktree.
# The cleaner may delete the run directory and the worktree; a measure that
# reads a file the cleaner removes decays to nothing, so the review lives in
# the configuration home instead.


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


def review_path(
    project: str,
    reviewed_run_id: str,
    base_dir: str | Path | None = None,
) -> Path:
    """Return the record path for a reviewed run of a project."""
    return review_store_root(base_dir) / project / f"{reviewed_run_id}.json"


def store_review(
    record: dict[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """Persist one review record and return the path it was written to.

    The record must name ``project`` and ``reviewed_run_id``, which key the
    file. A missing ``timestamp`` is stamped with the current UTC moment so
    every stored record carries one; an existing timestamp is preserved. A
    record carrying one of the legacy spellings of the revision it read gains
    the canonical key before it is written, so every review this machinery
    stores answers the staleness question under one name whatever the call
    site supplied; a record already stating the canonical key is left as it
    states it. The write is atomic: the record lands in a temporary sibling
    and is renamed into place.
    """
    project = record.get("project")
    reviewed_run_id = record.get("reviewed_run_id")
    if not project:
        raise ValueError("review record is missing project")
    if not reviewed_run_id:
        raise ValueError("review record is missing reviewed_run_id")
    carried, revision = carried_revision(record)
    if carried and REVIEW_REVISION_KEY not in record:
        record = dict(record)
        record[REVIEW_REVISION_KEY] = revision
    if not record.get("timestamp"):
        record = dict(record)
        record["timestamp"] = datetime.now(UTC).isoformat()
    path = review_path(project, reviewed_run_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def read_review(
    project: str,
    reviewed_run_id: str,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return the stored review record for a reviewed run, or ``None``.

    The record is what was written and carries the verbatim emitted text, so a
    later reader can re-derive the parse and see what the reviewer actually
    said.
    """
    path = review_path(project, reviewed_run_id, base_dir)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def ledger_block(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the compact shape a promoted ledger row stores for a review.

    ``None`` means no review was stored — the run was promoted unreviewed,
    which is not a score of zero. A stored record reduces to its status,
    per-dimension scores, absent dimensions and total, dropping the verbatim
    text and findings that belong in the review store. A record that never
    produced scores keeps a status other than ``"parsed"`` with empty scores,
    so it reads unlike an absent review and unlike a parsed review whose
    dimensions genuinely measure zero.

    The checklist item verdicts are deliberately not carried here. This block
    is what a promotion and a lane comparison read, and both are defined on
    the five dimensions alone; the item verdicts are recorded in the stored
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
    return {
        "status": str(record.get("status") or "unparsed"),
        "scores": scores,
        "absent": [str(dimension) for dimension in (record.get("absent") or [])],
        "total": None if total is None else int(total),
    }
