"""Published argument and common response models for Reckon's MCP surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

STORAGE_SLOW = "storage-slow"


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
        "summary",
        "flight",
        "live",
        "scopes",
        "drain",
        "records",
        "ledger",
        "budget",
        "lanes",
    ] = "summary"
    checkout_path: str | None = None
    plan: str | None = None
    since: str | None = None
    limit: int | None = Field(None, ge=1)
    candidates: list[dict[str, Any]] | None = Field(
        None,
        description="Ordered node manifests with id and write_paths for scopes planning",
    )


class CrewRecoverArgs(BaseModel):
    action: Literal["resume", "session", "sweep"]
    project: str | None = Field(None, description="Project whose held runs to sweep")
    run_id: str | None = Field(None, description="Run id for resume or session")
    advice: str | None = Field(
        None, description="The orchestrator's answer, for resume"
    )
    dry_run: bool = Field(
        False, description="For sweep: report what would be resumed, resume nothing"
    )


class StorageSlowResult(BaseModel):
    """Typed result for a tool call whose storage work outlived its deadline.

    ``path`` is the storage the worker was on, when it had resolved one; it is
    ``None`` when the deadline passed before the path resolved, and ``label``
    then names the tool that held it. ``landed`` is present only for a write:
    it records whether the abandoned body reached its file, so a timed-out
    write that did land is not retried blindly.
    """

    ok: bool = False
    error: Literal["storage-slow"] = STORAGE_SLOW
    kind: Literal["read", "write"]
    label: str
    path: str | None = None
    waited_seconds: float
    deadline_seconds: float
    landed: bool | None = None
    message: str
    hint: str = (
        "Storage is slow or unresponsive, so the result is unknown, not empty. "
        "Retry once the storage recovers; a write that reports landed=True must "
        "not be resubmitted."
    )

    @classmethod
    def for_call(
        cls,
        *,
        kind: Literal["read", "write"],
        label: str,
        path: str | None,
        waited: float,
        deadline: float,
        landed: bool | None = None,
    ) -> StorageSlowResult:
        where = path or f"{label} (path unresolved)"
        return cls(
            kind=kind,
            label=label,
            path=path,
            waited_seconds=round(waited, 3),
            deadline_seconds=round(deadline, 3),
            landed=landed,
            message=(
                f"{kind.capitalize()} of {where} did not finish within "
                f"{deadline:g}s (waited {waited:.1f}s)."
            ),
        )


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
