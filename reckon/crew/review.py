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


def review_path(
    project: str,
    reviewed_run_id: str,
    base_dir: str | Path | None = None,
    *,
    reviewed_head_sha: str | None = None,
) -> Path:
    """Return the legacy path or a path keyed by the reviewed head revision."""
    suffix = ""
    if reviewed_head_sha is not None:
        head_sha = reviewed_head_sha.strip()
        if not re.fullmatch(r"[0-9A-Fa-f]{7,64}", head_sha):
            raise ValueError(f"invalid reviewed_head_sha {reviewed_head_sha!r}")
        suffix = f".at-{head_sha.lower()}"
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
    pair before writing. A complete pair selects a revision-keyed path. A record
    lacking the pair is preserved outside current-review selection under its
    reviewing-run identity, so it can neither displace a complete review nor
    overwrite another partial record of the same run. The write is atomic.
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
    reviewed_head_sha: str | None = None,
) -> dict[str, Any] | None:
    """Return a stored review, optionally selecting the head it reviewed.

    A named head is matched against the record's normalised
    ``reviewed_head_sha`` rather than trusted from its filename, which keeps
    older short-sha preservation copies readable. Without a named head, the
    newest record is returned for compatibility with callers that have not yet
    learned to state the revision they need.
    """
    directory = review_store_root(base_dir) / project
    candidates = [review_path(project, reviewed_run_id, base_dir)]
    if directory.is_dir():
        candidates.extend(directory.glob(f"{reviewed_run_id}.at-*.json"))
    existing = {path.resolve(): path for path in candidates if path.is_file()}
    if not existing:
        return None

    records = [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in existing.values()
    ]
    if reviewed_head_sha is not None:
        named = reviewed_head_sha.strip().lower()
        for _, record in records:
            _, _, carried_head, stored_head = carried_revision_pair(record)
            if not carried_head or not stored_head:
                continue
            actual = stored_head.lower()
            if actual.startswith(named) or named.startswith(actual):
                return record
        return None
    return max(records, key=lambda item: item[0].stat().st_mtime_ns)[1]


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
