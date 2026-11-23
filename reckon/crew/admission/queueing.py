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

A pass costs one plan's parse per plan that has moved, not a project's. Each
plan carries an identity — its file's modification time and whether a plan hold
covers it — and a plan whose identity is unchanged is passed over without being
decoded. A project's first pass still parses every plan it holds (measured at
15.75 s for 40 plans and 120 sections), and a pass after one plan is edited
costs that plan alone. It is also right on its own terms: a plan whose sections
have not moved has nothing new to fill, and a request a coordinator dropped was
dropped deliberately.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Any

#: The session requests filed by the service are recorded under when no live
#: coordinator owns the plan. It names the service rather than a person, so a
#: reader of the queue can tell work nobody asked for from work a session did.
SERVICE_SESSION = "admission-service"


def mounted_projects() -> dict[str, Path]:
    """Every project the operator has mounted, mapped to its docs directory."""
    from reckon.serve import load_mounts

    try:
        return load_mounts()
    except (OSError, ValueError):
        return {}


def _default_config(project: str, root: Path) -> Mapping[str, Any]:
    from reckon import flight

    return flight.resolve(project, checkout_path=str(root)).config


def _plan_identity(docs: Path, slug: str, held: bool) -> tuple[Any, ...]:
    """What one plan's sections and hold would have to move for a refill.

    A plan file's modification time is the cheapest identity that moves when a
    section is declared, delivered or edited, and whether the plan is held is
    what a hold or a release changes. Both are read per plan, so a pass after
    one plan moved refiles that plan rather than the project's whole set.
    """
    try:
        stamp = (docs / "plans" / f"{slug}.html").stat().st_mtime_ns
    except OSError:
        stamp = None
    return stamp, held


class PlanQueue:
    """File the requests a project's plans may have, remembering what it filed.

    One instance belongs to the service process: the record it keeps is what
    makes a pass cost a stat per plan rather than a parse per plan.
    """

    def __init__(self) -> None:
        self._filled: dict[tuple[str, str], dict[str, tuple[Any, ...]]] = {}

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
        from reckon.crew.plan_coordinator import (
            _plans_and_owners,
            resolve_plan_coordinators,
        )

        store = queue if queue is not None else queue_plan
        catalogue = dict(projects) if projects is not None else mounted_projects()
        report: dict[str, Any] = {"projects": [], "queued": 0, "filed": []}
        for project, docs in sorted(catalogue.items()):
            root = Path(docs).parent
            entry: dict[str, Any] = {"project": project, "plans": []}
            report["projects"].append(entry)
            # The service's own pass names no callable, so its projects are
            # resolved per pass from each project's checkout.
            config_of = (
                config_for
                if config_for is not None
                else partial(_default_config, root=root)
            )
            try:
                config = config_of(project)
            except Exception as error:  # noqa: BLE001 - one layer cannot end a pass
                entry["error"] = f"config: {error}"
                continue
            from reckon import flight

            if not flight.service_queues_plans(config):
                entry["reason"] = "service queueing is off for this project"
                continue
            key = (project, str(root))
            remembered = dict(self._filled.get(key) or {})
            held = queue_edit.plan_holds(project)
            moved: dict[str, tuple[Any, ...]] = {}
            slugs = _plans_and_owners(Path(docs))
            for slug in slugs:
                identity = _plan_identity(Path(docs), slug, slug in held)
                moved[slug] = identity
                if remembered.get(slug) == identity:
                    entry["plans"].append({"plan": slug, "reason": "unchanged"})
            pending = [slug for slug in slugs if remembered.get(slug) != moved[slug]]
            # A plan whose store refused is left out of the remembered set, so
            # the next pass tries it again rather than waiting for its file to
            # move a second time.
            failed: set[str] = set()
            if not pending:
                self._filled[key] = moved
                entry["reason"] = "no plan has moved since this project was filled"
                continue
            try:
                live = list(runs.list_live(project=project))
            except Exception:  # noqa: BLE001 - an unreadable live set names nobody
                live = []
            try:
                ownership = resolve_plan_coordinators(project, root=root, pointers=live)
            except Exception:  # noqa: BLE001 - no resolution files nothing for it
                ownership = {"plans": {}}
            plans = ownership.get("plans") or {}
            for slug in pending:
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
                        config_for=config_of,
                        repo_root=root,
                        queued_by=SERVICE_SESSION,
                        live=live,
                        ownership=ownership,
                    )
                except Exception as error:  # noqa: BLE001 - one plan cannot end a pass
                    failed.add(slug)
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
            self._filled[key] = {
                slug: identity for slug, identity in moved.items() if slug not in failed
            }
        return report


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
