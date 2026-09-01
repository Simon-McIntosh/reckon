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
*dense in the nested sections* (always all six sections; every sub-key of each
decision / followup / question / research / comment is present, except the
conditional resolved/outcome fields).

The only dump configuration that reproduces both shapes is::

    PlanState.model_validate(read_state(html)).model_dump(exclude_unset=True)

``exclude_unset`` honours the ``fields_set`` that ``model_validate`` populates
from the input dict at *every* nesting level — so a missing top-level scalar
stays absent, while a present-but-default-valued section (``decisions={}``) is
still emitted. Plain ``model_dump()`` would inject every default and balloon
``<head>``; ``exclude_defaults`` would drop
``decisions={}`` and leave stale sections. Use :meth:`PlanState.canonical_dump`.

Lenient read vs strict write (locked decision: reject-write-warn-doctor)
-----------------------------------------------------------------------
:func:`reckon._plan_html.from_html` is **lenient** — it coerces/normalises and
never raises, so every existing plan validates on read. The enum-valued scalars
are typed as ``str`` (not ``Literal``) precisely so an off-enum value from an
old plan does not raise; the canonical enums travel into the JSON Schema via
``json_schema_extra``. Validation of enum membership and required-on-write
fields runs **only** at the explicit write boundary, via
:meth:`PlanState.validate_for_write` at the explicit write boundary.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from reckon.capability import (
    AUTONOMY_LEVELS,
    CAPABILITY_CLASSES,
    CAPABILITY_SCHEMA_VERSION,
    CONTEXT_LEVELS,
    REASONING_LEVELS,
    RISK_LEVELS,
    VERIFICATION_LEVELS,
    from_legacy_tier,
    validate_capability,
)
from reckon.tags import normalise_tag

# ── Schema version ──────────────────────────────────────────────────────────
#: Bump on any breaking change to the plan/index shape. Embedded in the derived
#: JSON Schema ($id + schemaVersion). Plans need NOT store this yet (additive).
SCHEMA_VERSION = "3.0"

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
PERSISTABLE_STATUS_ENUM = [status for status in STATUS_ENUM if status != "blocked"]
ROI_ENUM = ["high", "mid", "low"]
EFFORT_ENUM = ["S", "M", "L", "XL"]
LEGACY_EFFORT_HOURS = {"S": 1.0, "M": 2.0, "L": 4.0, "XL": 8.0}
TYPE_ENUM = ["plan", "research", "evidence"]
RESOURCE_TYPE_ENUM = [
    *TYPE_ENUM,
    "sprint",
    "milestone",
    "blocker",
    "timeline",
    "project",
]
SPRINT_STATUS_ENUM = ["planned", "active", "done", "shipped"]
_RESOURCE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_OPTIONAL_IDENTIFIER_PATTERN = r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*)?$"
_OPTIONAL_IDENTIFIER_RE = re.compile(_OPTIONAL_IDENTIFIER_PATTERN)
GRAPH_HANDLE_GRAMMAR = r"[A-Za-z0-9][A-Za-z0-9._-]*"
GRAPH_HANDLE_PATTERN = rf"^(?:{GRAPH_HANDLE_GRAMMAR})?$"
_GRAPH_HANDLE_RE = re.compile(rf"^{GRAPH_HANDLE_GRAMMAR}$")


def is_graph_handle(value: Any) -> bool:
    """Return whether *value* is a non-empty graph-handle identifier."""

    return isinstance(value, str) and _GRAPH_HANDLE_RE.fullmatch(value) is not None


def _enum(values: list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    """json_schema_extra payload advertising the canonical enum for a str field
    (without making the Python field reject off-enum values on read)."""
    return {"enum": list(values)}


def _normalise_tags(value: Any, *, reject_invalid: bool = False) -> Any:
    """Canonicalise tag identities while keeping legacy reads lenient.

    Invalid identities survive parsing so old resources remain readable.  The
    explicit write validator reports them, while valid spelling variants are
    normalised and duplicate canonical identities collapse in authored order.
    """

    if not isinstance(value, (list, tuple, set)):
        return value
    tags: list[Any] = []
    seen: set[str] = set()
    for authored in value:
        try:
            tag = normalise_tag(authored)
        except (TypeError, ValueError):
            if reject_invalid:
                raise
            tags.append(authored)
            continue
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


class ResourceIdentity(BaseModel):
    """Stable identity for one typed HTML resource."""

    project: str
    type: str = Field(json_schema_extra=_enum(RESOURCE_TYPE_ENUM))
    slug: str
    archived: bool = False

    @property
    def key(self) -> str:
        """Project-qualified identity independent of an on-disk path."""
        return f"{self.project}:{self.type}:{self.slug}"

    def validate_for_write(self) -> "ResourceIdentity":
        errors: list[str] = []
        if not _is_safe_resource_segment(self.project):
            errors.append("project: must be a single safe path segment")
        if self.type not in RESOURCE_TYPE_ENUM:
            errors.append(f"type: {self.type!r} not in {RESOURCE_TYPE_ENUM}")
        if not _is_safe_resource_segment(self.slug):
            errors.append("slug: must be a single safe path segment")
        if errors:
            raise ValueError(
                "ResourceIdentity.validate_for_write failed:\n  - "
                + "\n  - ".join(errors)
            )
        return self


def _is_safe_resource_segment(value: str) -> bool:
    """Return whether an identity is safe to interpolate into a path or URL."""
    return bool(
        value and value not in {".", ".."} and _RESOURCE_SEGMENT_RE.fullmatch(value)
    )


# ── Cross-project plan references ────────────────────────────────────────────
#
# Every link-list field (depends_on, blocks, informs, evidence_for, verifies,
# supersedes) holds PLAN REFS with one grammar:
#
#     ref     :=  [ project ":" ] slug [ "#" stage ]
#     project :=  a key in mounts.json          (e.g. "nova", "norma")
#     slug    :=  a plan slug in that project   (e.g. "nova-spine-refactor")
#     stage   :=  a section / stage anchor      (e.g. "s2", "parser")
#
# A BARE slug is a LOCAL reference — resolved inside the owning project, as it
# always has been. A "project:"-QUALIFIED ref is an EXTERNAL reference into
# another mounted project. The distinction is scope, not shape: both travel in
# the same comma-separated <meta> lists, and a qualified ref whose project
# equals the owning project reads as local. Resolution of external refs (does
# the target exist, what is its status/impl) is the server's job — the MCP
# read_plan single-plan response resolves depends_on into a ``deps`` list, and
# the audit reports dangling or unmounted external refs. Files stay portable:
# nothing in the HTML depends on another checkout being present.

_REF_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_PROJECT_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_PLAN_REF_RE = re.compile(
    rf"^(?:(?P<project>{_PROJECT_SEGMENT}):)?"
    rf"(?P<slug>{_REF_SEGMENT})"
    rf"(?:#(?P<stage>{_REF_SEGMENT}))?$"
)

#: The link-list fields whose entries are plan refs (local or external).
LINK_LIST_FIELDS = (
    "depends_on",
    "blocks",
    "informs",
    "evidence_for",
    "verifies",
    "supersedes",
)


class PlanRef:
    """A parsed plan reference. ``project is None`` ⇒ local (same-project)."""

    __slots__ = ("project", "slug", "stage")

    def __init__(self, project: str | None, slug: str, stage: str | None) -> None:
        self.project = project
        self.slug = slug
        self.stage = stage

    def is_external(self, owning_project: str = "") -> bool:
        """True when the ref points outside ``owning_project``. A qualifier
        naming the owning project itself reads as local."""
        return self.project is not None and self.project != owning_project

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"PlanRef(project={self.project!r}, slug={self.slug!r}, stage={self.stage!r})"


def parse_plan_ref(ref: str) -> PlanRef | None:
    """Parse ``[project:]slug[#stage]`` — returns ``None`` on a malformed ref
    (empty segments, more than one ``:``, illegal characters)."""
    if not isinstance(ref, str):
        return None
    m = _PLAN_REF_RE.match(ref.strip())
    if m is None:
        return None
    return PlanRef(m.group("project"), m.group("slug"), m.group("stage"))


def resolve_plan_ref(
    ref: str,
    owning_project: str,
    lookup: Callable[[str, str], Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Resolve one plan ref through a caller-provided project lookup.

    Parsing and scope classification stay here so dependency and sprint
    membership consumers cannot drift into subtly different ref grammars.
    Storage-specific callers supply only the lookup for ``(project, slug)``.
    """

    parsed = parse_plan_ref(ref)
    if parsed is None:
        return {"ref": ref, "scope": "invalid", "found": False}
    external = parsed.is_external(owning_project)
    target_project = str(parsed.project) if external else owning_project
    row: dict[str, Any] = {
        "ref": ref,
        "scope": "external" if external else "local",
        "project": target_project,
        "slug": parsed.slug,
        "found": False,
    }
    if parsed.stage:
        row["stage"] = parsed.stage
    target = lookup(target_project, parsed.slug)
    if not target:
        return row
    row.update(
        {
            "found": True,
            "status": target.get("status", ""),
            "impl": target.get("impl", 0),
            "title": target.get("title", ""),
        }
    )
    return row


def plan_section_anchors(plan: Mapping[str, Any]) -> frozenset[str]:
    """Return the section identities carried by a plan's semantic state.

    Gates bind evidence to authored sections and may name downstream sections
    they hold. Comment mapping keys are also authored section anchors. These
    derived identities let graph consumers validate staged refs without adding
    another persisted field to plan state.
    """

    anchors: set[str] = set()
    for gate in plan.get("gates") or []:
        if not isinstance(gate, Mapping):
            continue
        candidates = [gate.get("section"), *(gate.get("gated_sections") or [])]
        anchors.update(
            value
            for candidate in candidates
            if (value := str(candidate or "").strip())
            and _RESOURCE_SEGMENT_RE.fullmatch(value)
        )
    comments = plan.get("comments") or {}
    if isinstance(comments, Mapping):
        anchors.update(
            value
            for candidate in comments
            if (value := str(candidate or "").strip()) != "_top"
            and _RESOURCE_SEGMENT_RE.fullmatch(value)
        )
    return frozenset(anchors)


def split_refs(
    refs: list[str], owning_project: str = ""
) -> tuple[list[str], list[str]]:
    """Partition a link list into ``(local, external)`` refs, dropping
    malformed entries from both halves (the write boundary rejects those)."""
    local: list[str] = []
    external: list[str] = []
    for ref in refs or []:
        parsed = parse_plan_ref(ref)
        if parsed is None:
            continue
        (external if parsed.is_external(owning_project) else local).append(ref)
    return local, external


def _optional_enum(values: tuple[str, ...]) -> dict[str, Any]:
    """Advertise an optional string enum without contradicting its null arm."""
    return _enum((*values, None))


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


class CapabilityRequirements(BaseModel):
    """Structured hard floors that complement the broad capability class."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str | None = Field(
        None,
        json_schema_extra=_optional_enum(REASONING_LEVELS),
    )
    context: str | None = Field(
        None,
        json_schema_extra=_optional_enum(CONTEXT_LEVELS),
    )
    tool_autonomy: str | None = Field(
        None,
        json_schema_extra=_optional_enum(AUTONOMY_LEVELS),
    )
    verification: str | None = Field(
        None,
        json_schema_extra=_optional_enum(VERIFICATION_LEVELS),
    )
    risk: str | None = Field(
        None,
        json_schema_extra=_optional_enum(RISK_LEVELS),
    )


class CapabilityRequest(BaseModel):
    """Versioned, provider-neutral capability request persisted with work."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: str = Field(
        CAPABILITY_SCHEMA_VERSION,
        json_schema_extra={"const": CAPABILITY_SCHEMA_VERSION},
    )
    capability_class: str = Field(
        "general",
        alias="class",
        serialization_alias="class",
        json_schema_extra=_enum(CAPABILITY_CLASSES),
    )
    requirements: CapabilityRequirements = Field(default_factory=CapabilityRequirements)


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
    capability: CapabilityRequest | None = None
    tier: str = Field(
        "",
        description="Deprecated compatibility input; use capability",
        json_schema_extra={"deprecated": True},
    )
    written_by: str = ""
    written_at: str = ""
    recommends_skill: str = ""
    title: str = ""
    body: str = ""
    prompt: str = ""  # One-line invocation; mandatory and non-empty on write.
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


class Gate(BaseModel):
    """An evidence gate (``.r-gate`` element).

    ``passed`` is a read-only projection of ``verdict``. The renderer ignores
    it, so semantic HTML carries only the authoritative verdict.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    section: str = ""
    gated_sections: list[str] = Field(default_factory=list)
    status: str = ""
    measure: str
    required_evidence: str = ""
    verdict: str = ""
    evidence: str = ""
    passed: bool = Field(False, json_schema_extra={"readOnly": True})

    @model_validator(mode="after")
    def _derive_passed(self) -> "Gate":
        object.__setattr__(self, "passed", self.verdict == "passed")
        return self


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
    effort_hours: float | None = Field(
        None,
        gt=0,
        multiple_of=0.25,
        description="Neutral worker-hours from the plan-effort-hours meta",
    )
    wall_clock_hours: float | None = Field(
        None,
        gt=0,
        multiple_of=0.25,
        description=(
            "Elapsed hours from the plan-wall-clock-hours meta, assuming the "
            "maximum parallelism the plan itself supports. Never exceeds "
            "effort_hours; equals it for strictly sequential work."
        ),
    )
    effort_calibrated: bool | None = Field(
        None,
        description="Whether worker-hours were authored rather than mapped from a letter",
        json_schema_extra={"readOnly": True},
    )
    effort: str = Field(
        "M",
        description="Deprecated compatibility input; use effort_hours",
        json_schema_extra={**_enum(EFFORT_ENUM), "deprecated": True},
    )
    milestone: str = Field(
        "",
        description="Milestone identifier, or empty when unassigned",
        json_schema_extra={"pattern": _OPTIONAL_IDENTIFIER_PATTERN},
    )
    sprint: str | None = Field(
        None,
        description=(
            "Sprint ref this plan belongs to. Bare 'id' = same-project; "
            "'project:id' = external (cross-project)."
        ),
    )
    graph_handle: str | None = Field(
        None,
        description=(
            "Stable ship-target handle carried by this endpoint plan. Membership "
            "is always derived from depends_on and is never stored."
        ),
        json_schema_extra={"pattern": GRAPH_HANDLE_PATTERN},
    )
    north_star: str | None = None
    capability: CapabilityRequest | None = None
    tier: str = Field(
        "",
        description="Deprecated compatibility input; use capability",
        json_schema_extra={"deprecated": True},
    )
    owner: str = ""

    # ── Visibility flags ──
    archived: str | None = None  # "1" hides from default inventory
    read: str | None = None  # "1" marks a research/doc reviewed
    reviewed_at: str = ""
    recorded_at: str = ""
    verdict: str = ""
    environment: str = ""
    source: str = ""
    source_quality: str = ""

    # ── Link lists (comma-separated metas) ──
    # Entries are plan refs: a bare "slug" is LOCAL (same project); a
    # "project:slug" qualifier is EXTERNAL (another mounted project); an
    # optional "#stage" suffix names a section/stage. See parse_plan_ref.
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "Plan refs this plan depends on. Bare 'slug' = same-project; "
            "'project:slug' = external (cross-project); optional '#stage'."
        ),
    )
    blocks: list[str] = Field(
        default_factory=list,
        description=(
            "Plan refs this plan blocks. Same grammar as depends_on: bare "
            "'slug' = same-project; 'project:slug' = external; optional '#stage'."
        ),
    )
    informs: list[str] = Field(default_factory=list)  # research-only
    evidence_for: list[str] = Field(default_factory=list)  # evidence-only
    verifies: list[str] = Field(default_factory=list)  # evidence-only stage refs
    supersedes: list[str] = Field(default_factory=list)
    commits: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(
        default_factory=list,
        description="Canonical topical identities carried by this resource",
    )

    # ── Server-owned (never authored) ──
    modified: str = ""  # ISO date, server-written on each POST
    impl: float = 0.0  # progress fraction, server-written
    version: int = 0  # optimistic-concurrency counter, server-owned
    compatibility_warnings: list[str] = Field(
        default_factory=list,
        json_schema_extra={"readOnly": True},
    )
    validation_diagnostics: list[dict[str, str]] = Field(
        default_factory=list,
        json_schema_extra={"readOnly": True},
    )

    # ── Body sections ──
    gates: list[Gate] = Field(default_factory=list)
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

    @field_validator("tags", mode="before")
    @classmethod
    def _norm_tags(cls, v: Any) -> Any:
        return _normalise_tags(v)

    # ── Dependency scope views (derived, never stored) ──
    def local_depends_on(self) -> list[str]:
        """The depends_on refs that resolve inside this plan's own project."""
        return split_refs(self.depends_on, self.project)[0]

    def external_depends_on(self) -> list[str]:
        """The depends_on refs qualified into another project
        (``project:slug[#stage]``)."""
        return split_refs(self.depends_on, self.project)[1]

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
        data = self.model_dump(exclude_unset=True, by_alias=True)
        if self.type != "plan":
            # Plan-only defaults may be present on legacy documents or the
            # create template. Canonical non-plan writes remove them instead of
            # persisting meaningless progress/scheduling metadata.
            for field in (
                "status",
                "roi",
                "effort_hours",
                "effort_calibrated",
                "effort",
                "milestone",
                "sprint",
                "graph_handle",
                "north_star",
                "capability",
                "tier",
                "depends_on",
                "blocks",
                "impl",
            ):
                data.pop(field, None)
        return data

    # ── Strict write-boundary validation (reject path) ──
    def validate_for_write(self) -> "PlanState":
        """Enforce required-on-write fields + enum membership + non-empty
        followup prompts. Raises :class:`ValueError` listing every violation.

        This is the *reject* half of reject-write-warn-doctor. ``from_html`` is
        lenient; callers invoke this at the write boundary.
        """
        errors: list[str] = []
        required = (
            ("project", "slug", "title", "status")
            if self.type == "plan"
            else (
                "project",
                "slug",
                "title",
            )
        )
        for fld in required:
            if not (getattr(self, fld) or "").strip():
                errors.append(f"{fld}: required on write (empty)")
        if (
            self.type == "plan"
            and self.status
            and self.status not in PERSISTABLE_STATUS_ENUM
        ):
            errors.append(
                f"status: {self.status!r} not in persistable statuses "
                f"{PERSISTABLE_STATUS_ENUM}"
            )
        if self.type == "plan" and self.roi and self.roi not in ROI_ENUM:
            errors.append(f"roi: {self.roi!r} not in {ROI_ENUM}")
        if self.type == "plan" and self.effort and self.effort not in EFFORT_ENUM:
            errors.append(f"effort: {self.effort!r} not in {EFFORT_ENUM}")
        if self.type == "plan" and not _OPTIONAL_IDENTIFIER_RE.fullmatch(
            self.milestone
        ):
            errors.append(
                f"milestone: {self.milestone!r} must be an identifier or empty"
            )
        if self.type == "plan" and self.sprint:
            sprint_ref = parse_plan_ref(self.sprint)
            if sprint_ref is None or sprint_ref.stage is not None:
                errors.append(
                    f"sprint: malformed sprint ref {self.sprint!r} — expected "
                    "[project:]id"
                )
        if (
            self.type == "plan"
            and self.graph_handle
            and not is_graph_handle(self.graph_handle)
        ):
            errors.append(
                f"graph_handle: {self.graph_handle!r} must match "
                f"{GRAPH_HANDLE_GRAMMAR} or be empty"
            )
        if self.type == "plan" and self.capability:
            errors.extend(
                validate_capability(self.capability.model_dump(by_alias=True))
            )
        if self.type and self.type not in TYPE_ENUM:
            errors.append(f"type: {self.type!r} not in {TYPE_ENUM}")
        for tag in self.tags:
            try:
                normalise_tag(tag)
            except (TypeError, ValueError) as exc:
                errors.append(f"tags: {exc}")
        if self.type != "plan":
            neutral = {
                "status": ("", "draft", "reference"),
                "roi": ("", "mid"),
                "effort_hours": (None,),
                "effort_calibrated": (None,),
                "effort": ("", "M"),
                "milestone": ("", "—"),
                "sprint": ("", None),
                "graph_handle": ("", None),
                "north_star": ("", None),
                "capability": (None,),
                "impl": (0, 0.0, None),
                "depends_on": ([],),
                "blocks": ([],),
            }
            for field, allowed in neutral.items():
                value = getattr(self, field)
                if value not in allowed:
                    errors.append(
                        f"{field}: plan-only field cannot carry {value!r} "
                        f"on {self.type} artifacts"
                    )
            if self.tier:
                mapped, _ = from_legacy_tier(self.tier)
                if not mapped or mapped["class"] != "general":
                    errors.append(
                        f"tier: plan-only field cannot carry {self.tier!r} "
                        f"on {self.type} artifacts"
                    )
            for field in ("decisions", "followups", "questions"):
                if getattr(self, field):
                    errors.append(
                        f"{field}: plan-only workflow cannot be non-empty "
                        f"on {self.type} artifacts"
                    )
        for field in LINK_LIST_FIELDS:
            for ref in getattr(self, field, None) or []:
                if parse_plan_ref(ref) is None:
                    errors.append(
                        f"{field}: malformed plan ref {ref!r} — expected "
                        f"[project:]slug[#stage]"
                    )
        for fu in self.followups:
            if not (fu.prompt or "").strip():
                errors.append(
                    f"followup {fu.id or '<no-id>'}: one-line invocation prompt "
                    "is mandatory (empty)"
                )
            if fu.capability:
                errors.extend(
                    f"followup {fu.id or '<no-id>'}: {error}"
                    for error in validate_capability(
                        fu.capability.model_dump(by_alias=True)
                    )
                )
        if errors:
            raise ValueError(
                "PlanState.validate_for_write failed:\n  - " + "\n  - ".join(errors)
            )
        return self


# ── IndexState (index.json envelope — modelled, not written by this agent) ──


class _TolerantIndexModel(BaseModel):
    """Base for index.json models. ``extra='allow'`` keeps unknown fields
    (e.g. ``milestone.description``, projects-rollup extras) so writes never
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
    capability: CapabilityRequest | None = None
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
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def _norm_tags(cls, v: Any) -> Any:
        return _normalise_tags(v, reject_invalid=True)


class Milestone(_TolerantIndexModel):
    id: str = ""
    name: str = ""
    status: str = ""
    pct: int | None = None
    depends_on: list[str] | None = None
    evidence: list[str] | None = None


class NorthStar(_TolerantIndexModel):
    """One durable direction declared by a project."""

    id: str
    name: str
    statement: str
    href: str | None = None


class TimelineEntry(_TolerantIndexModel):
    id: str = ""
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
    north_stars: list[NorthStar] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    projects: list[ProjectRollup] | None = None
    #: Synthesised on GET; never persisted. Excluded from the write shape.
    inventory: list[InventoryItem] = Field(default_factory=list, exclude=True)

    def model_dump(self, **kwargs: Any) -> dict:
        # Default by_alias=True so the persisted key is "_version" (not the
        # python field name "version_"). _version is the only aliased field, so
        # this prevents a silent index-counter-zeroing trap.
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
    schema.setdefault("$defs", {})["ResourceIdentity"] = (
        ResourceIdentity.model_json_schema()
    )
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
