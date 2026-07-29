"""Pydantic type definitions for all reckon MCP tool arguments and responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ── Request models ─────────────────────────────────────────────────────────


class ReadPlanArgs(BaseModel):
    project: str = Field(..., description="Project name (key in mounts.json, e.g. 'imas-ambix')")
    slug: str = Field(..., description="Plan slug (HTML filename stem, e.g. 'tokenizers'); use 'index' for project config")


class ListPlansArgs(BaseModel):
    project: str = Field(..., description="Project name")
    status: str | None = Field(
        None,
        description="Optional status filter: active | pending | blocked | shipped | draft",
    )


class PatchPlanArgs(BaseModel):
    project: str = Field(..., description="Project name")
    slug: str = Field(..., description="Plan slug")
    patch: dict[str, Any] = Field(..., description="JSON merge-patch to apply to data")
    expected_version: int = Field(
        ...,
        description="Current data._version — write is rejected with VersionConflict if it doesn't match",
    )


class AppendCommentArgs(BaseModel):
    project: str
    slug: str
    section_id: str = Field(..., description="Section id the comment belongs to")
    body: str = Field(..., description="Comment text (markdown ok)")
    author: str = Field(..., description="Human or agent author identifier")
    quote: str | None = Field(None, description="Optional quoted passage from the plan body")
    expected_version: int


class LockDecisionArgs(BaseModel):
    project: str
    slug: str
    key: str = Field(..., description="Decision key, e.g. 'transport'")
    choice: str = Field(..., description="The chosen option")
    rationale: str = Field(..., description="Why this choice was made")
    by: str = Field(..., description="Who locked the decision")
    expected_version: int


class AppendFollowupArgs(BaseModel):
    project: str
    slug: str
    followup: dict[str, Any] = Field(
        ...,
        description=(
            "Full followup record. Must include: id, written_by, written_at, title, body, prompt. "
            "Optional: recommends_skill, touches, blocked_by, capability, est_turn."
        ),
    )
    expected_version: int


class ResolveFollowupArgs(BaseModel):
    project: str
    slug: str
    followup_id: str = Field(..., description="The 'id' field of the followup to resolve")
    outcome: str = Field(..., description="Short description of what was done / decided")
    by: str = Field(..., description="Who resolved the followup")
    expected_version: int


class SetStatusArgs(BaseModel):
    project: str
    slug: str
    status: str = Field(
        ...,
        description="New status: active | pending | blocked | shipped | draft | archived",
    )
    expected_version: int


class SetImplArgs(BaseModel):
    project: str
    slug: str
    impl: float = Field(..., ge=0.0, le=1.0, description="Implementation fraction 0..1")
    expected_version: int


# ── Response models ────────────────────────────────────────────────────────


class ReadPlanResult(BaseModel):
    project: str
    slug: str
    version: int
    data: dict[str, Any]


class ListPlansResult(BaseModel):
    project: str
    plans: list[dict[str, Any]]


class WriteResult(BaseModel):
    ok: bool = True
    project: str
    slug: str
    new_version: int


class VersionConflictResult(BaseModel):
    ok: bool = False
    error: str = "version_conflict"
    expected_version: int
    current_version: int
    hint: str = "Re-read the plan with reckon.read_plan to get the current version, then retry."
