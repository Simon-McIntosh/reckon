"""Fill the queue from the plans whose work may be queued.

A coordinator stops and its plans' sections stay unqueued, so the lane drains
while the plans still declare work. The admission service is the one process
that is always running, so on its own clock it files, through the whole-plan
dispatch, a request for every implementable section the queue read says may be
queued and that has none.

Filing is filed twice over rather than re-derived: the section-by-section
answer is :func:`reckon.crew.dispatch_batch.plan_queue_answers`, and the requests
are stored by :func:`reckon.crew.dispatch_batch.queue_plan`, which is what a
coordinator's own whole-plan dispatch calls. A section the read holds, a plan
another live session coordinates and a plan a coordinator has held are all left
alone by those two, in one vocabulary, so the service can never queue something
a coordinator's dispatch would have refused.

A pass costs a plan's parse, not a project's. Each project's plans carry an
identity — the file names and their modification times, plus whether a plan
hold is recorded — and a pass that finds an unchanged identity files nothing for
it. That is what keeps a service pass every fifteen seconds from decoding every
plan of every mounted project, and it is also right on its own terms: a plan
whose sections have not moved has nothing new to fill, and a request a
coordinator dropped was dropped deliberately.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

#: The session requests filed by the service are recorded under when no live
#: coordinator owns the plan. It names the service rather than a person, so a
#: reader of the queue can tell work nobody asked for from work a session did.
SERVICE_SESSION = "admission-service"


def mounted_projects() -> dict[str, Path]:
    """Every project the operator has mounted, mapped to its docs directory."""
    try:
        from reckon._store import _mounts_path

        registered = json.loads(_mounts_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(registered, dict):
        return {}
    return {
        str(project): Path(str(directory)).expanduser()
        for project, directory in registered.items()
        if project and directory
    }


def _default_config(project: str, root: Path) -> Mapping[str, Any]:
    from reckon import flight

    return flight.resolve(project, checkout_path=str(root)).config


def _plans_identity(root: Path, hold_path: Path) -> tuple[Any, ...]:
    """What a project's sections and holds would have to move for a refill.

    A plan file's name and modification time is the cheapest identity that
    moves when a section is declared, delivered or edited, and the hold file's
    own timestamp is what a hold or a release changes.
    """
    plans_dir = root / "docs" / "plans"
    try:
        entries = tuple(
            sorted(
                (path.name, path.stat().st_mtime_ns)
                for path in plans_dir.glob("*.html")
            )
        )
    except OSError:
        entries = ()
    try:
        held = hold_path.stat().st_mtime_ns
    except OSError:
        held = None
    return entries, held


class PlanQueue:
    """File the requests a project's plans may have, remembering what it filed.

    One instance belongs to the service process: the record it keeps is what
    makes a pass cost a stat per project rather than a parse per plan.
    """

    def __init__(self) -> None:
        self._filled: dict[tuple[str, str], tuple[Any, ...]] = {}

    def forget(self, project: str = "") -> None:
        """Drop the remembered identities, of one project or of every one."""
        if not project:
            self._filled.clear()
            return
        for key in [key for key in self._filled if key[0] == project]:
            del self._filled[key]

    def fill(
        self,
        *,
        config_for: Callable[[str], Mapping[str, Any]] | None = None,
        queue: Callable[..., Mapping[str, Any]] | None = None,
        projects: Mapping[str, Path] | None = None,
    ) -> dict[str, Any]:
        """File every request the mounted projects' plans may have.

        A project whose configuration turns the service's queueing off is
        reported and left alone. One plan's refusal is reported on that plan
        and does not end the pass: a plan whose sections cannot be stored —
        a project with no backend to queue for, a plan file that will not
        parse — must not keep every other plan out of the queue.
        """
        from reckon.crew import queue_edit, runs
        from reckon.crew.dispatch_batch import queue_plan
        from reckon.crew.plan_coordinator import resolve_plan_coordinators

        store = queue if queue is not None else queue_plan
        resolve_config = config_for or _default_config
        catalogue = dict(projects) if projects is not None else mounted_projects()
        report: dict[str, Any] = {"projects": [], "queued": 0, "filed": []}
        for project, docs in sorted(catalogue.items()):
            root = Path(docs).parent
            entry: dict[str, Any] = {"project": project, "plans": []}
            report["projects"].append(entry)
            try:
                config = resolve_config(project)
            except Exception as error:  # noqa: BLE001 - one layer cannot end a pass
                entry["error"] = f"config: {error}"
                continue
            from reckon import flight

            if not flight.service_queues_plans(config):
                entry["reason"] = "service queueing is off for this project"
                continue
            identity = _plans_identity(root, queue_edit.plan_hold_path(project))
            key = (project, str(root))
            if self._filled.get(key) == identity:
                entry["reason"] = "no plan has moved since this project was filled"
                continue
            try:
                live = list(runs.list_live(project=project))
            except Exception:  # noqa: BLE001 - an unreadable live set names nobody
                live = []
            try:
                ownership = resolve_plan_coordinators(
                    project, root=root, pointers=live
                )
            except Exception:  # noqa: BLE001 - no resolution files nothing for it
                ownership = {"plans": {}}
            plans = ownership.get("plans") or {}
            for slug in _plan_slugs(root):
                row = plans.get(slug) or {}
                session = (
                    str(row.get("coordinator") or "")
                    if row.get("live")
                    else SERVICE_SESSION
                )
                try:
                    result = store(
                        project,
                        slug,
                        session=session,
                        config_for=resolve_config,
                        repo_root=root,
                        queued_by=SERVICE_SESSION,
                    )
                except Exception as error:  # noqa: BLE001 - one plan cannot end a pass
                    entry["plans"].append(
                        {"plan": slug, "error": str(error), "session": session}
                    )
                    continue
                filed = int(result.get("queued") or 0)
                entry["plans"].append(
                    {
                        "plan": slug,
                        "session": session,
                        "queued": filed,
                        "skipped": int(result.get("skipped") or 0),
                        "refused": int(result.get("refused") or 0),
                    }
                )
                if filed:
                    report["queued"] += filed
                    for queued in result.get("results") or ():
                        if queued.get("status") in ("queued", "awaiting-upstream"):
                            report["filed"].append(
                                {
                                    "run_id": queued.get("run_id"),
                                    "project": project,
                                    "plan": slug,
                                    "section": queued.get("section"),
                                }
                            )
            self._filled[key] = identity
        return report


def _plan_slugs(root: Path) -> list[str]:
    """Every plan slug a checkout's plans directory holds, in a stable order."""
    plans_dir = Path(root) / "docs" / "plans"
    try:
        return sorted(path.stem for path in plans_dir.glob("*.html"))
    except OSError:
        return []


#: The record one service process keeps of what it has already filled. It is
#: process-lifetime state rather than a file: a fresh image starts with none.
_PLAN_QUEUE = PlanQueue()


def fill_queue(
    *,
    config_for: Callable[[str], Mapping[str, Any]] | None = None,
    projects: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """The pass's queue stage: file what may be filed, once per plan change."""
    return _PLAN_QUEUE.fill(config_for=config_for, projects=projects)


def forget_filled(project: str = "") -> None:
    """Forget which projects were filled, so the next pass reads them again."""
    _PLAN_QUEUE.forget(project)