"""Published argument and common response models for Reckon's MCP surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ReadPlanArgs(BaseModel):
    project: str | None = Field(None, description="Project key, or * for mounts")
    slug: str | None = Field(None, description="Resource slug or compatibility index")
    resource: dict[str, Any] | None = Field(
        None, description="Typed selector with project, type, id, and optional archived"
    )
    view: str | None = Field(
        None, description="summary, detail, history, version, raw, or schema"
    )
    with_schema: bool = False
    checkout_path: str | None = None
    status: str | None = None
    doc_type: str | None = None
    sprint: str | None = None
    milestone: str | None = None
    owner: str | None = None
    search: str | None = None
    limit: int | None = Field(None, ge=1)
    cursor: str | None = None
    include_followups: bool = True
    include_questions: bool = True
    include_prompts: bool = False


class EditPlanArgs(BaseModel):
    project: str
    slug: str
    ops: list[dict[str, Any]] | None = None
    expected_version: int = Field(..., ge=0)
    mode: Literal["state", "text"] = "state"
    old_html: str | None = Field(None, description="Exact fragment to replace")
    new_html: str | None = Field(None, description="Replacement authored HTML")
    create: bool = False
    checkout_path: str | None = None
    doc_type: str | None = None


class RoadmapArgs(BaseModel):
    project: str = Field(..., description="Project key, or * for all mounts")
    checkout_path: str | None = None
    sprint: str | None = None
    max_paths: int = Field(5, ge=1, le=50)


class AuditArgs(BaseModel):
    project: str
    checkout_path: str | None = None
    view: str | None = None
    cursor: str | None = None
    limit: int | None = Field(None, ge=1)


class CrewArgs(BaseModel):
    project: str
    view: Literal[
        "summary", "flight", "live", "drain", "records", "ledger", "budget"
    ] = "summary"
    checkout_path: str | None = None
    plan: str | None = None
    since: str | None = None
    limit: int | None = Field(None, ge=1)


class WriteResult(BaseModel):
    ok: bool = True
    project: str
    slug: str
    new_version: int
    path: str | None = None


class VersionConflictResult(BaseModel):
    ok: bool = False
    error: str = "version_conflict"
    expected_version: int
    current_version: int
    hint: str = (
        "Re-read the resource with reckon.read_plan using the same checkout_path, "
        "then retry."
    )
