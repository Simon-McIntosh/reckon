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

Nothing here is wired into dispatch, promotion, recovery or the ticker. This
node only exposes the functions those call sites will use.
"""

from __future__ import annotations

import json
import re
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

# The prompt is a versioned, diffable file rather than a string inside this
# module, so editing it is a text change rather than a code change. It is read
# from disk on every call: the module holds the path, not the text.
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "review.md"


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
_FIND_RE = re.compile(r"^FINDING\s+(\S+)\s*(.*)$", re.IGNORECASE)


def _as_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def parse_review(text: str) -> dict[str, Any]:
    """Parse emitted review text into the five-dimension schema.

    Returns a record shaped like the stored one, without the run metadata a
    call site adds before persisting:

    - ``status`` — ``"parsed"`` when at least one SCORE line was recognised,
      ``"unparsed"`` when none of the emitted text parsed as a review.
    - ``scores`` — dimension to score, for the dimensions that were emitted.
    - ``absent`` — schema dimensions with no recognised SCORE line.
    - ``justifications`` — dimension to its one-sentence justification.
    - ``findings`` — a list of ``{"file", "line", "text"}``.
    - ``total`` — the arithmetic sum of the parsed scores when every dimension
      is present, otherwise ``None``. The total is never computed over a
      subset: a total taken over fewer dimensions is a lower score
      indistinguishable from a worse one.
    - ``raw_text`` — the verbatim emitted text, so a later reader can
      re-derive the parse from the record alone.

    A score outside ``0..REVIEW_MAX_SCORE`` raises :class:`ReviewScoreError`
    naming the dimension and the value; it is never clamped into range.
    """
    scores: dict[str, int] = {}
    justifications: dict[str, str] = {}
    findings: list[dict[str, str]] = []
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
    if not scores:
        status = "unparsed"
        total = None
    else:
        status = "parsed"
        total = sum(scores.values()) if not absent else None
    return {
        "status": status,
        "scores": scores,
        "absent": absent,
        "justifications": justifications,
        "findings": findings,
        "total": total,
        "raw_text": text,
    }


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
    every stored record carries one; an existing timestamp is preserved. The
    write is atomic: the record lands in a temporary sibling and is renamed
    into place.
    """
    project = record.get("project")
    reviewed_run_id = record.get("reviewed_run_id")
    if not project:
        raise ValueError("review record is missing project")
    if not reviewed_run_id:
        raise ValueError("review record is missing reviewed_run_id")
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
