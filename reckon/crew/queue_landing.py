"""Land delivered, accepted implementation runs from the admission service."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reckon.crew import landing_measure, recovery, runs
from reckon.crew.promotion_records import _node_test_files
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


def _record_suite_evidence(run_id: str, measured: landing_measure.Measurement) -> None:
    """Keep the measurement where a later reader can open it.

    The live pointer loses this at promotion, so the run's own directory holds
    the pair as well.
    """

    record = measured.as_dict()
    directory = runs.run_dir(run_id) / "landing-measure"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "measurement.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["suite_evidence"] = record
        return pointer

    runs._mutate_pointer(run_id, mutation)


def _measure_delivery(
    pointer: Mapping[str, Any], run_id: str, head: str, tree: str
) -> Any:
    """Run the three suite readings for one delivered run.

    A run that armed no command has nothing to measure; the landing path keeps
    its earlier behaviour for such a run rather than inventing a command.
    """
    command = str(pointer.get("suite_command") or "").strip()
    if not command:
        return None
    base_sha = str(pointer.get("base_sha") or "").strip()
    repo = Path(str(pointer.get("repo") or tree or ""))
    if not base_sha or not repo.is_dir():
        return landing_measure.Measurement(
            command=command,
            suite={},
            population={},
            unreadable="the run records no base revision to compare against",
        )
    logs = runs.run_dir(run_id) / "landing-measure"
    with landing_measure.base_checkout(repo, base_sha) as base_tree:
        measured = landing_measure.measure_run(
            command=command,
            tree=Path(tree),
            base_sha=base_sha,
            head_sha=head,
            base_tree=base_tree,
            logs_dir=logs,
            test_files=_node_test_files(pointer),
        )
    _record_suite_evidence(run_id, measured)
    return measured


def _measure_guard(measured: Any) -> str:
    """The refusal a measurement states, or "" when the run may land."""
    if measured is None:
        return ""
    if measured.unreadable:
        return f"landing measure unreadable: {measured.unreadable}"
    if not measured.real:
        return ""
    details = []
    for test_id in measured.real:
        results = measured.isolated.get(test_id) or {"base": [], "head": []}
        details.append(
            f"{test_id} failed {sum(results['head'])}/{len(results['head'])} at head "
            f"and {sum(results['base'])}/{len(results['base'])} at base"
        )
    return "added suite regression: " + "; ".join(details)


def _landing_beat(pointer: Mapping[str, Any], run_id: str) -> str:
    """Tick the landed node's section through the ops a coordinator uses.

    Returns "" when the beat was recorded or the run names no plan section, and
    the refusal otherwise. The evidence anchor is the node's own fragment path,
    so a reader follows the closing comment to the record the worker wrote.
    """
    node = pointer.get("node") if isinstance(pointer.get("node"), Mapping) else {}
    plan = str(node.get("plan") or "").strip()
    section = str(node.get("section") or "").strip()
    project = str(pointer.get("project") or "").strip()
    repo = str(pointer.get("repo") or "").strip()
    if not (plan and section and project and repo):
        return ""
    from datetime import UTC, datetime

    from reckon import _store
    from reckon.mcp_edit_plan import _edit_plan_tool

    anchor = str(node.get("id") or run_id)
    record_href = f"/{project}/evidence/archive/{plan}-landed.html#{anchor}"
    body = (
        f"<p>Landed {run_id} on the measured suite pair; "
        f'<a href="{record_href}">evidence</a></p>'
    )
    ops = [
        {
            "op": "append_evidence",
            "plan": plan,
            "anchor": anchor,
            "title": f"{section} landed",
            "body": f"<p>Run <code>{run_id}</code> landed after the landing pass "
            "measured its suite at base and head.</p>",
        },
        {"op": "set", "path": f"section_declarations.{section}", "value": "done"},
        {
            "op": "append",
            "target": "comments",
            "section": section,
            "item": {
                "id": f"c-close-{section}",
                "who": "reckon-build",
                "when": datetime.now(UTC).isoformat(),
                "body": body,
            },
        },
    ]
    _data, version = _store.read_plan(project, plan, repo)
    try:
        result = _edit_plan_tool(
            project,
            plan,
            expected_version=version,
            ops=ops,
            checkout_path=repo,
        )
    except (OSError, ValueError) as exc:
        return f"landing beat refused: {type(exc).__name__}: {exc}"
    if not result.get("ok"):
        return f"landing beat refused: {result.get('error') or 'unknown refusal'}"
    return ""


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
        measured = _measure_delivery(pointer, run_id, head, tree)
        measure_guard = _measure_guard(measured)
        if measure_guard:
            _record_attempt(run_id, head=head, guard=measure_guard, status="refused")
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": measure_guard,
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
            beat_refusal = _landing_beat(pointer, run_id)
            if beat_refusal:
                obligations.append(
                    {
                        "run_id": run_id,
                        "session": str(pointer.get("session") or ""),
                        "guard": beat_refusal,
                    }
                )
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
