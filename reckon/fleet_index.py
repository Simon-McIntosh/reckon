"""Compute one mounted project's fleet-index row from its own repository.

Every field is derived live from discovery, live run pointers, and Git
history — nothing here reads a persisted rollup file. See served-surface
data contracts §3 for the field-by-field rationale.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from reckon.lifecycle import COMPLETED_STATUSES

_ACTIVITY_WINDOW_DAYS = 30
_ACTIVITY_SUBPATHS = ("plans", "research", "evidence")


def _is_actionable_plan(item: dict) -> bool:
    return item.get("type") == "plan" and not item.get("archived")


def _status_bucket(item: dict) -> str:
    status = str(item.get("effective_status") or item.get("status") or "pending")
    if status in COMPLETED_STATUSES:
        return "shipped"
    if status == "blocked":
        return "blocked"
    if status == "active":
        return "active"
    return "pending"


def _status_counts(inventory: list[dict]) -> dict[str, int]:
    counts = {"active": 0, "blocked": 0, "pending": 0, "shipped": 0}
    for item in inventory:
        if not _is_actionable_plan(item):
            continue
        counts[_status_bucket(item)] += 1
    return counts


def _last_edited(inventory: list[dict]) -> str:
    stamps = [str(item["edited"]) for item in inventory if item.get("edited")]
    return max(stamps) if stamps else ""


def _active_sprint(discovered: dict) -> dict | None:
    active_id = discovered.get("active_sprint_id")
    if not active_id:
        return None
    for sprint in discovered.get("sprints", []):
        if isinstance(sprint, dict) and sprint.get("id") == active_id:
            return {"id": sprint.get("id"), "theme": sprint.get("theme", "")}
    return None


def _activity30(repo_dir: Path, docs_dir: Path, now: datetime) -> list[int]:
    """Return one commit count per calendar day in [now-29d, now], or []."""

    if not (repo_dir / ".git").exists():
        return []
    try:
        rel_docs = docs_dir.relative_to(repo_dir)
    except ValueError:
        return []

    today = datetime(now.year, now.month, now.day)  # noqa: DTZ001 — naive, paired with `now`
    start = today - timedelta(days=_ACTIVITY_WINDOW_DAYS - 1)
    paths = [str(rel_docs / sub) for sub in _ACTIVITY_SUBPATHS]

    # No --since/--until: those prune traversal by assuming ancestor commits
    # are monotonically older, which a backdated or rebased commit violates
    # and silently truncates the walk. Read every date and bucket in Python
    # instead — still exactly one git invocation per project.
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--format=%ad",
                "--date=format:%Y-%m-%d",
                "--",
                *paths,
            ],
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=10,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []

    days = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    window_end = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    window_start = start.strftime("%Y-%m-%d")
    days = [day for day in days if window_start <= day < window_end]
    if not days:
        return []

    counts: dict[str, int] = {}
    for day in days:
        counts[day] = counts.get(day, 0) + 1
    return [
        counts.get((start + timedelta(days=offset)).strftime("%Y-%m-%d"), 0)
        for offset in range(_ACTIVITY_WINDOW_DAYS)
    ]


def compute_project_row(
    docs_dir: Path,
    project: str,
    *,
    state_root: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Return one project's fleet-index row, computed live from its repository."""

    from reckon import crew
    from reckon.serve import discover_plans

    reference = now or datetime.now()  # noqa: DTZ005 — naive local, matches serve._row_times
    discovered = discover_plans(docs_dir, project, state_root)
    inventory = discovered.get("inventory", [])
    counts = _status_counts(inventory)
    plans_count = sum(counts.values())
    live_pointers = crew.list_live(project=project)
    last_edited = _last_edited(inventory)

    return {
        "project": project,
        "path": str(docs_dir),
        "plans_count": plans_count,
        "active": counts["active"],
        "blocked": counts["blocked"],
        "pending": counts["pending"],
        "shipped": counts["shipped"],
        "live": len(live_pointers),
        "last_edited": last_edited,
        "last_modified": last_edited,
        "active_sprint": _active_sprint(discovered),
        "activity30": _activity30(docs_dir.parent, docs_dir, reference),
    }
