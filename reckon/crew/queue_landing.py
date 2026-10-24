"""Land delivered, accepted implementation runs from the admission service."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reckon.crew import recovery, runs
from reckon.crew.recovery_review_acceptance import _review_accepts_promotion

_run_srun = subprocess.run


def _record_attempt(run_id: str, *, head: str, guard: str, status: str) -> None:
    """Keep a refusal on the live run so another pass cannot retry silently."""

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["admission_landing"] = {
            "head": head,
            "guard": guard,
            "status": status,
        }
        return pointer

    runs._mutate_pointer(run_id, mutation)


def _land_command(run_id: str) -> list[str]:
    """Run the coordinator's land command in one explicit fleet step."""
    from reckon.nested_launch import shim_directory

    return [
        str(shim_directory() / "srun"),
        "--overlap",
        "--ntasks=1",
        sys.executable,
        "-c",
        "from reckon.cli import main; main()",
        "crew",
        "complete",
        "--run",
        run_id,
        "--gate",
        "passed",
        "--land",
    ]


def _land(run_id: str) -> tuple[bool, str]:
    # The CLI calls the same promotion lease and merge index-lock retry a
    # coordinator uses. Its gate also runs inside this step, off the service.
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source, environment.get("PYTHONPATH", "")) if part
    )
    finished = _run_srun(
        _land_command(run_id),
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    output = (finished.stdout or "").strip()
    try:
        receipt = json.loads(output)
    except ValueError:
        receipt = {}
    if finished.returncode == 0 and receipt.get("ok") is True:
        return True, ""
    if isinstance(receipt, Mapping):
        steps = receipt.get("steps")
        if isinstance(steps, Mapping):
            for step in ("promote", "land", "release"):
                entry = steps.get(step)
                if isinstance(entry, Mapping) and entry.get("ok") is False:
                    return (
                        False,
                        f"{step}: {entry.get('error') or receipt.get('error') or 'refused'}",
                    )
        detail = receipt.get("error") or receipt.get("detail")
        if detail:
            return False, str(detail)
    return False, (
        finished.stderr or output or f"land exited {finished.returncode}"
    ).strip()


def land_once() -> dict[str, Any]:
    """Land each accepted delivery once and return obligations for refusals."""
    from reckon.crew.recovery_review_subject import select_review_for_head

    landed: list[str] = []
    obligations: list[dict[str, str]] = []
    for pointer in runs.list_live():
        if (
            str(pointer.get("role") or (pointer.get("node") or {}).get("role") or "")
            != "implement"
        ):
            continue
        run_id = str(pointer.get("run_id") or "")
        project = str(pointer.get("project") or "")
        if not run_id or not project:
            continue
        classified = recovery.classify_pointer(pointer)
        if classified.get("manifest_status") != "complete":
            continue
        head, tree = recovery._review_head_and_tree(pointer)
        delivered = list(classified.get("manifest_commits") or ())
        if (
            not head
            or not delivered
            or not recovery.same_revision(head, str(delivered[-1]))
        ):
            continue
        previous = pointer.get("admission_landing")
        if isinstance(previous, Mapping) and recovery.same_revision(
            str(previous.get("head") or ""), head
        ):
            if previous.get("status") == "attempting":
                guard = "automatic landing interrupted before a receipt"
                _record_attempt(run_id, head=head, guard=guard, status="refused")
                obligations.append(
                    {
                        "run_id": run_id,
                        "session": str(pointer.get("session") or ""),
                        "guard": guard,
                    }
                )
            continue
        try:
            review, stale = select_review_for_head(project, run_id, head, tree=tree)
        except (OSError, ValueError) as exc:
            review, stale = None, str(exc)
        if stale or review is None:
            continue
        if not _review_accepts_promotion(review):
            guard = "review acceptance floor or findings"
            _record_attempt(run_id, head=head, guard=guard, status="refused")
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": guard,
                }
            )
            continue
        _record_attempt(run_id, head=head, guard="crew land", status="attempting")
        try:
            accepted, guard = _land(run_id)
        except (OSError, ValueError) as exc:
            accepted, guard = False, f"{type(exc).__name__}: {exc}"
        if accepted:
            landed.append(run_id)
        else:
            guard = guard or "crew land refused without a guard"
            _record_attempt(run_id, head=head, guard=guard, status="refused")
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": guard,
                }
            )
    return {"landed": landed, "obligations": obligations}
