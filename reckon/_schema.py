#!/usr/bin/env python3
"""Versioned PlanState schema — the contract for reckon plan state.

The data IS semantic HTML (see ``_plan_html.py`` and ``PLAN-FORMAT.md``).
``read_state`` parses that HTML into a canonical dict; ``write_state``
regenerates the reckon-owned elements from a dict. This module adds a typed
contract *on top of* those dicts:

  - :class:`PlanState` is the single source of truth for the plan schema.
  - :class:`IndexState` models the ``index.json`` envelope (project config).
  - The published JSON Schema at ``docs/_shared/plan.schema.json`` is **derived**
    from these models via :func:`gen_json_schema` — it is never hand-edited.

Maintenance contract
--------------------
**Change the model → regenerate ``docs/_shared/plan.schema.json``.**
Never hand-edit the derived JSON, and never hand-edit any prose copy of the
schema. ``tests/test_schema.py`` asserts the committed file equals the freshly
generated schema, so drift is caught in CI. Run::

    python -c "from reckon._schema import write_json_schema; write_json_schema()"

The shape contract (read this before touching the models)
--------------------------------------------------------
``read_state`` produces a dict that is *sparse at the top level* (a scalar key
exists only when its ``<meta>`` is present — no defaults are injected) but
*dense in the nested sections* (always all five sections; every sub-key of each
decision / followup / question / research / comment is present, except the
conditional resolved/outcome fields).

The only dump configuration that reproduces both shapes is::

    PlanState.model_validate(read_state(html)).model_dump(exclude_unset=True)

``exclude_unset`` honours the ``fields_set`` that ``model_validate`` populates
from the input dict at *every* nesting level — so a missing top-level scalar
stays absent, while a present-but-default-valued section (``decisions={}``) is
still emitted. Plain ``model_dump()`` would inject every default (effort=M,
tier=sonnet, …) and balloon ``<head>``; ``exclude_defaults`` would drop
``decisions={}`` and leave stale sections. Use :meth:`PlanState.canonical_dump`.

Lenient read vs strict write (locked decision: reject-write-warn-doctor)
-----------------------------------------------------------------------
:func:`reckon._plan_html.from_html` is **lenient** — it coerces/normalises and
never raises, so every existing plan validates on read. The enum-valued scalars
are typed as ``str`` (not ``Literal``) precisely so an off-enum value from an
old plan does not raise; the canonical enums travel into the JSON Schema via
``json_schema_extra``. Validation of enum membership and required-on-write
fields runs **only** at the explicit write boundary, via
:meth:`PlanState.validate_for_write` (wired into edit_plan/doctor by F3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# ── Schema version ──────────────────────────────────────────────────────────
#: Bump on any breaking change to the plan/index shape. Embedded in the derived
#: JSON Schema ($id + schemaVersion). Plans need NOT store this yet (additive).
SCHEMA_VERSION = "1.0"

#: Stable identifier for the published JSON Schema.
SCHEMA_ID = f"https://reckon/schema/plan/{SCHEMA_VERSION}/plan.schema.json"


# ── Canonical enums (advisory — fields stay `str` for lenient reads) ────────
STATUS_ENUM = [
    "draft",
    "pending",
    "active",
    "in-progress",
    "blocked",
    "shipped",
    "done",
    "superseded",
    "abandoned",
    "archived",
    "historical",
    "reference",
]
ROI_ENUM = ["high", "mid", "low"]
EFFORT_ENUM = ["S", "M", "L", "XL"]
TIER_ENUM = ["haiku", "sonnet", "opus"]
TYPE_ENUM = ["plan", "research"]
SPRINT_STATUS_ENUM = ["planned", "active", "done", "shipped"]


def _enum(values: list[str]) -> dict[str, Any]:
    """json_schema_extra payload advertising the canonical enum for a str field
    (without making the Python field reject off-enum values on read)."""
    return {"enum": list(values)}


# ── Plan section models ──────────────────────────────────────────────────────


class Decision(BaseModel):
    """A single decision (``.r-dec`` element). Keyed by ``data-key`` in the
    plan's ``decisions`` dict — the key lives in the mapping, not here.

    ``choice == ""`` means open. ``chosen`` is intentionally absent: the SPA's
    list view derives it from ``choice``; storing it would be redundant.
    """

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    context: str = ""
    choices: list[str] = Field(default_factory=list)
    option_labels: dict[str, str] = Field(default_factory=dict)
    choice: str = ""  # "" == open; an option value OR free text
    rationale: str = ""
    when: str = ""
    by: str = ""


class Followup(BaseModel):
    """A followup (``.r-fu`` element). ``status`` mirrors read_state's value;
    write_state re-derives ``data-status`` from ``resolved_at`` on render.

    ``resolved_at`` / ``resolved_by`` / ``outcome`` are present in the dict only
    when the followup is resolved — they MUST stay unset otherwise so
    ``exclude_unset`` reproduces read_state's conditional shape.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    status: str = "open"
    tier: str = ""
    written_by: str = ""
    written_at: str = ""
    recommends_skill: str = ""
    title: str = ""
    body: str = ""
    prompt: str = ""  # §05 template — mandatory-non-empty on write
    resolved_at: str | None = None
    resolved_by: str | None = None
    outcome: str | None = None

    @model_validator(mode="after")
    def _derive_status(self) -> "Followup":
        # A resolved_at implies resolved, regardless of a stale literal status.
        if self.resolved_at:
            object.__setattr__(self, "status", "resolved")
        return self


class Question(BaseModel):
    """A question (``.r-q`` element). No ``status`` *field* — read_state omits it
    from question dicts. Use the :attr:`status` property for views; a Pydantic
    field (or computed_field) would force-include it and break the round-trip.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    section: str = ""
    opened_by: str = ""
    opened_at: str = ""
    body: str = ""
    resolution: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None

    @property
    def status(self) -> str:
        return "resolved" if self.resolved_at else "open"


class ResearchItem(BaseModel):
    """A research entry (``.r-research`` element)."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    type: str = ""
    title: str = ""
    source: str = ""
    added_by: str = ""
    when: str = ""
    url: str | None = None


class Comment(BaseModel):
    """A comment (``.r-comment`` element). Anchored to a section via the dict
    key (default ``_top``) — the anchor is the mapping key, not a field here."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    who: str = ""
    when: str = ""
    quote: str | None = None
    body: str = ""


# ── PlanState (the contract) ─────────────────────────────────────────────────


class PlanState(BaseModel):
    """Typed contract for a single plan's state.

    ``extra='ignore'`` on read so unknown attrs/metas drop cleanly. The
    canonical dump (:meth:`canonical_dump`) reproduces the read_state dict
    shape byte-for-byte through write_state.
    """

    model_config = ConfigDict(extra="ignore")

    # ── Identity / required-on-write (lenient read defaults) ──
    project: str = Field("", description="docs-project meta; required-on-write")
    type: str = Field("plan", json_schema_extra=_enum(TYPE_ENUM))
    slug: str = ""  # required-on-write; lenient default = filename stem
    title: str = ""  # required-on-write; lenient default = <title> -> slug
    summary: str = ""

    # ── Authored scalars ──
    status: str = Field("draft", json_schema_extra=_enum(STATUS_ENUM))
    roi: str = Field("mid", json_schema_extra=_enum(ROI_ENUM))
    effort: str = Field("M", json_schema_extra=_enum(EFFORT_ENUM))
    milestone: str = "—"
    sprint: str | None = None
    tier: str = Field("sonnet", json_schema_extra=_enum(TIER_ENUM))
    owner: str = ""

    # ── Visibility flags ──
    archived: str | None = None  # "1" hides from default inventory
    read: str | None = None  # "1" marks a research/doc reviewed

    # ── Link lists (comma-separated metas) ──
    depends_on: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)
    informs: list[str] = Field(default_factory=list)  # research-only

    # ── Server-owned (never authored) ──
    modified: str = ""  # ISO date, server-written on each POST
    impl: float = 0.0  # progress fraction, server-written
    version: int = 0  # optimistic-concurrency counter, server-owned

    # ── Body sections ──
    decisions: dict[str, Decision] = Field(default_factory=dict)
    followups: list[Followup] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    research: list[ResearchItem] = Field(default_factory=list)
    comments: dict[str, list[Comment]] = Field(default_factory=dict)

    # ── Lenient normalisation (mode='before' — never raises) ──
    @field_validator("roi", mode="before")
    @classmethod
    def _norm_roi(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() == "med":
            return "mid"
        return v

    @field_validator("type", mode="before")
    @classmethod
    def _norm_type(cls, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip().lower()
            if s == "doc":
                return "research"
            return s or "plan"
        return v

    # ── Read-only SPA views (derived, never stored) ──
    def decisions_list(self) -> list[dict]:
        """The list-shaped decisions view parse_plan exposes for the SPA.

        Each entry is ``{"key": k, **decision_fields, "chosen": choice}``.
        ``chosen`` is derived from ``choice`` — redundant with it, never stored.
        """
        out: list[dict] = []
        for key, d in self.decisions.items():
            row = {"key": key, **d.model_dump()}
            row["chosen"] = d.choice
            out.append(row)
        return out

    # Alias used by some callers / docs.
    as_list = decisions_list

    # ── Canonical dump (matches read_state/write_state dict shape EXACTLY) ──
    def canonical_dump(self) -> dict:
        """Return the dict that ``write_state`` consumes.

        Uses ``exclude_unset=True`` so the result mirrors read_state's sparse
        top level (only present metas) while keeping its dense nested sections.
        Feed via ``PlanState.model_validate(read_state(html)).canonical_dump()``.
        """
        return self.model_dump(exclude_unset=True)

    # ── Strict write-boundary validation (reject path) ──
    def validate_for_write(self) -> "PlanState":
        """Enforce required-on-write fields + enum membership + non-empty
        followup prompts. Raises :class:`ValueError` listing every violation.

        This is the *reject* half of reject-write-warn-doctor. ``from_html`` is
        lenient; callers (edit_plan / doctor, wired by F3) invoke this at the
        write boundary.
        """
        errors: list[str] = []
        for fld in ("project", "slug", "title", "status"):
            if not (getattr(self, fld) or "").strip():
                errors.append(f"{fld}: required on write (empty)")
        if self.status and self.status not in STATUS_ENUM:
            errors.append(f"status: {self.status!r} not in {STATUS_ENUM}")
        if self.roi and self.roi not in ROI_ENUM:
            errors.append(f"roi: {self.roi!r} not in {ROI_ENUM}")
        if self.effort and self.effort not in EFFORT_ENUM:
            errors.append(f"effort: {self.effort!r} not in {EFFORT_ENUM}")
        if self.tier and self.tier not in TIER_ENUM:
            errors.append(f"tier: {self.tier!r} not in {TIER_ENUM}")
        if self.type and self.type not in TYPE_ENUM:
            errors.append(f"type: {self.type!r} not in {TYPE_ENUM}")
        for fu in self.followups:
            if not (fu.prompt or "").strip():
                errors.append(
                    f"followup {fu.id or '<no-id>'}: §05 prompt is mandatory (empty)"
                )
        if errors:
            raise ValueError(
                "PlanState.validate_for_write failed:\n  - " + "\n  - ".join(errors)
            )
        return self


# ── IndexState (index.json envelope — modelled, not written by this agent) ──


class _TolerantIndexModel(BaseModel):
    """Base for index.json models. ``extra='allow'`` keeps unknown fields
    (e.g. ``milestone.description``, projects-rollup extras) so F3/F5 never
    silently lose data. Real index.json files carry stray ``null`` scalars
    (ambix ``sprint.summary``); the before-validator coerces ``None`` to the
    declared default for plain ``str`` fields so validation never hard-fails.
    """

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _none_to_default(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        out = dict(v)
        for name, field in cls.model_fields.items():
            key = field.alias if field.alias else name
            if out.get(key) is None and field.annotation is str:
                # str field given null → use its default ('' unless overridden)
                out[key] = field.get_default(call_default_factory=True)
        return out


class SprintItem(_TolerantIndexModel):
    """One sprint item. Coerces a bare-string item (``"slug"``) and an
    efit-style ``path``-keyed item into a ``slug`` on read."""

    slug: str = ""
    title: str | None = None
    roi: str | None = None
    effort: str | None = None
    milestone: str | None = None
    why_now: str | None = None
    done_when: str | None = None
    status: str | None = None
    tier: str | None = None
    blocked_by: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"slug": v}
        if isinstance(v, dict) and not v.get("slug") and v.get("path"):
            v = {**v, "slug": v["path"]}
        return v


class Sprint(_TolerantIndexModel):
    id: str = ""
    theme: str = ""
    description: str = ""
    status: str = Field("planned", json_schema_extra=_enum(SPRINT_STATUS_ENUM))
    starts: str = ""
    ends: str = ""
    items: list[SprintItem] = Field(default_factory=list)
    summary: str = ""


class Milestone(_TolerantIndexModel):
    id: str = ""
    name: str = ""
    status: str = ""
    pct: int | None = None
    depends_on: list[str] | None = None
    evidence: list[str] | None = None


class TimelineEntry(_TolerantIndexModel):
    when: str = ""
    who: str = ""
    what: str = ""


class Blocker(_TolerantIndexModel):
    summary: str = ""
    id: str | None = None
    origin: str = ""
    n: int = 0
    owner: str = ""
    next: str = ""


class ProjectRollup(_TolerantIndexModel):
    """Optional computed cross-project rollup (reckon/ambix only)."""

    project: str = ""


class InventoryItem(_TolerantIndexModel):
    """READ-ONLY / synthesised by discover_plans on GET — NEVER persisted.
    Excluded from the serialised write shape (see :class:`IndexData`)."""

    slug: str = ""


class IndexData(_TolerantIndexModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Server-owned counter — distinct from the per-plan version. The on-disk
    # key is "_version" (read by _store.py). model_dump() is overridden below to
    # default by_alias=True so a plain dump emits "_version" — without that the
    # dump would carry "version_" and _store.py would silently read 0, breaking
    # the index optimistic-concurrency check.
    version_: int = Field(0, alias="_version")
    active_sprint_id: str | None = None
    sprints: list[Sprint] = Field(default_factory=list)
    milestones: list[Milestone] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    projects: list[ProjectRollup] | None = None
    #: Synthesised on GET; never persisted. Excluded from the write shape.
    inventory: list[InventoryItem] = Field(default_factory=list, exclude=True)

    def model_dump(self, **kwargs: Any) -> dict:
        # Default by_alias=True so the persisted key is "_version" (not the
        # python field name "version_"). _version is the only aliased field, so
        # this is safe and saves F3 from a silent index-counter-zeroing trap.
        kwargs.setdefault("by_alias", True)
        return super().model_dump(**kwargs)


class IndexState(_TolerantIndexModel):
    """The ``index.json`` envelope: project-level config only."""

    updated: str = ""
    project: str = ""
    doc: str = "index"
    data: IndexData = Field(default_factory=IndexData)

    def model_dump(self, **kwargs: Any) -> dict:
        # Default by_alias=True so the nested data carries "_version" (the
        # on-disk key) when dumping the whole envelope — nested models are
        # serialised internally, so IndexData's own override does not apply
        # during recursion. _version is the only aliased field anywhere.
        kwargs.setdefault("by_alias", True)
        return super().model_dump(**kwargs)


# ── JSON Schema generation (Pydantic derives it) ────────────────────────────


def gen_json_schema() -> dict:
    """Return the derived JSON Schema for :class:`PlanState`, with the reckon
    schema id + version embedded. This is THE published contract."""
    schema = PlanState.model_json_schema()
    schema["$id"] = SCHEMA_ID
    schema["schemaVersion"] = SCHEMA_VERSION
    schema["title"] = "reckon PlanState"
    return schema


def schema_path() -> Path:
    """Path to the published JSON Schema (``docs/_shared/plan.schema.json``)."""
    return (
        Path(__file__).resolve().parent.parent / "docs" / "_shared" / "plan.schema.json"
    )


def write_json_schema(path: Path | None = None) -> Path:
    """Write the derived JSON Schema to disk. Run after any model change."""
    import json

    target = Path(path) if path is not None else schema_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(gen_json_schema(), indent=2) + "\n", encoding="utf-8")
    return target
