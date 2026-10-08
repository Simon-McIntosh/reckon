"""Published argument and common response models for Reckon's MCP surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

STORAGE_SLOW = "storage-slow"

# The hint a waiting body gets: the storage really is the suspect, so the
# advice is to retry. A computing body gets its own hint, because the storage
# is not the suspect and a blind retry is the wrong instruction.

_STORAGE_SLOW_HINT = (
    "Storage is slow or unresponsive, so the result is unknown, not empty. "
    "Retry once the storage recovers; a write that reports landed=True must "
    "not be resubmitted."
)


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


def _timing_fragment(cpu_seconds: float | None, run_seconds: float | None) -> str:
    """Name the thread CPU and run-queue seconds a timed-out body spent.

    Both figures are reported because together they are the computing measure:
    a body busy on the CPU shows in ``cpu_seconds``, one starved runnable on an
    oversubscribed node shows in ``run_seconds``. Either omitted when the
    counter was unreadable.
    """

    parts = []
    if cpu_seconds is not None:
        parts.append(f"{cpu_seconds:.1f}s CPU")
    if run_seconds is not None:
        parts.append(f"{run_seconds:.1f}s runnable")
    return f"; {', '.join(parts)}" if parts else ""


class StorageSlowResult(BaseModel):
    """Typed result for a tool call whose storage work outlived its deadline.

    ``path`` is the storage the worker was on, when it had resolved one; it is
    ``None`` when the deadline passed before the path resolved, and ``label``
    then names the tool that held it. ``landed`` is present only for a write:
    it records whether the abandoned body reached its file, so a timed-out
    write that did land is not retried blindly. ``landed`` is ``None`` when the
    filesystem would not answer within the landing check's own deadline, which
    is an unknown state rather than a negative one.

    What the abandoned body was doing when the deadline expired is ``cause``. A
    body that wanted the CPU reads ``computing``: it either burned thread CPU
    time (``cpu_seconds``) or sat runnable on the runqueue while an
    oversubscribed node ran other work first (``run_seconds``). A body blocked
    on storage reads ``waiting``, burning neither. The ``error`` value stays
    ``storage-slow`` so no existing caller's branch changes, while the message
    and hint name the cause and, for a computing body whose tool has one, the
    CLI command that answers without a deadline.
    """

    ok: bool = False
    error: Literal["storage-slow"] = STORAGE_SLOW
    kind: Literal["read", "write"]
    label: str
    path: str | None = None
    waited_seconds: float
    cpu_seconds: float | None = None
    run_seconds: float | None = None
    cause: Literal["computing", "waiting"] = "waiting"
    deadline_seconds: float
    landed: bool | None = None
    cli_command: str | None = None
    message: str
    hint: str = _STORAGE_SLOW_HINT

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
        cpu_seconds: float | None = None,
        run_seconds: float | None = None,
        cause: Literal["computing", "waiting"] = "waiting",
        cli_command: str | None = None,
    ) -> StorageSlowResult:
        where = path or f"{label} (path unresolved)"
        outcome = ""
        if kind == "write":
            outcome = {
                True: " The write did reach its file; do not resubmit it.",
                False: " The write did not reach its file.",
                None: (
                    " Whether the write reached its file could not be determined, "
                    "so its landed state is unknown rather than false."
                ),
            }[landed]
        cpu_fragment = _timing_fragment(cpu_seconds, run_seconds)
        if cause == "computing":
            activity = "It was still computing when the deadline expired"
            hint = (
                "The body was abandoned while it was still computing, not while "
                "it waited on storage, so this is a reckon work deadline rather "
                "than a slow filesystem."
            )
            if cli_command:
                hint += f" The same work answers without a deadline from the CLI: {cli_command}."
        else:
            activity = "It was waiting on storage when the deadline expired"
            hint = _STORAGE_SLOW_HINT
        return cls(
            kind=kind,
            label=label,
            path=path,
            waited_seconds=round(waited, 3),
            cpu_seconds=None if cpu_seconds is None else round(cpu_seconds, 3),
            run_seconds=None if run_seconds is None else round(run_seconds, 3),
            cause=cause,
            deadline_seconds=round(deadline, 3),
            landed=landed,
            cli_command=cli_command,
            message=(
                f"{kind.capitalize()} of {where} did not finish within "
                f"{deadline:g}s (waited {waited:.1f}s{cpu_fragment}). "
                f"{activity}.{outcome}"
            ),
            hint=hint,
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
