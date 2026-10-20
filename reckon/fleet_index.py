"""Compute one mounted project's fleet-index row from its own repository.

Every field is derived live from discovery, live run pointers, and Git
history — nothing here reads a persisted rollup file. The served-surface
contract defines the field-by-field rationale.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from reckon._plan_html import derive_fleet_impl_imputing_effort
from reckon._timestamps import parse_iso
from reckon.lifecycle import COMPLETED_STATUSES
from reckon.resources import nav_key

_ACTIVITY_WINDOW_DAYS = 30
_ACTIVITY_SUBPATHS = ("plans", "research", "evidence")
_LANDED_WINDOW_HOURS = 72
_LANDED_LIMIT = 7

# A full-history ``git log`` costs seconds per repository on a shared
# filesystem, and the fleet index asks for every mounted project on each page
# load. The dated history is a pure function of the commit graph, so it is
# memoised against HEAD and bucketed into the window per call.
_ACTIVITY_DAYS_CACHE: dict[tuple[str, tuple[str, ...]], tuple[str, list[str]]] = {}
_ACTIVITY_DAYS_LOCK = threading.Lock()


def _git_head(repo_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = result.stdout.strip()
    return head if result.returncode == 0 and head else None


def _commit_days(repo_dir: Path, paths: list[str]) -> list[str] | None:
    """Return one ``YYYY-MM-DD`` per commit touching ``paths``, or None."""

    key = (str(repo_dir.resolve()), tuple(paths))
    head = _git_head(repo_dir)
    if head is not None:
        with _ACTIVITY_DAYS_LOCK:
            cached = _ACTIVITY_DAYS_CACHE.get(key)
        if cached is not None and cached[0] == head:
            return cached[1]
    # No --since/--until: those prune traversal by assuming ancestor commits
    # are monotonically older, which a backdated or rebased commit violates
    # and silently truncates the walk. Read every date and bucket in Python
    # instead — still exactly one git invocation per project and HEAD.
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
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    days = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if head is not None:
        with _ACTIVITY_DAYS_LOCK:
            _ACTIVITY_DAYS_CACHE[key] = (head, days)
    return days


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


def _progress(inventory: list[dict]) -> float | None:
    """The project's progress, from the fleet roll-up over its plans."""

    return derive_fleet_impl_imputing_effort(
        item for item in inventory if _is_actionable_plan(item)
    )


def _last_edited(inventory: list[dict]) -> str:
    stamps = [str(item["edited"]) for item in inventory if item.get("edited")]
    return max(stamps) if stamps else ""


def _local_naive_stamp(value: object) -> datetime | None:
    """Read an edited stamp on the local clock used by the fleet row."""
    parsed = parse_iso(value)
    if parsed is not None and parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _recent_landed(
    inventory: list[dict], project: str, reference: datetime
) -> list[dict]:
    """Return compact delivered artifacts edited in the preceding 72 hours."""

    end = reference.timestamp()
    start = end - timedelta(hours=_LANDED_WINDOW_HOURS).total_seconds()
    candidates = []
    for item in inventory:
        kind = item.get("type") or "plan"
        status = item.get("effective_status") or item.get("status")
        if kind != "evidence" and not (
            kind == "plan" and status in {"done", "shipped"}
        ):
            continue
        slug = item.get("slug")
        edited = item.get("edited") or item.get("last")
        if not slug or not edited:
            continue
        parsed = _local_naive_stamp(edited)
        if parsed is None:
            continue
        try:
            stamp = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            continue
        if not start <= stamp <= end:
            continue
        archived = item.get("archived") in {True, "1", "true"}
        key = nav_key(kind, slug, archived=archived)
        candidates.append(
            (
                stamp,
                {
                    "type": kind,
                    "slug": slug,
                    "nav_key": key,
                    "title": item.get("title") or slug,
                    "edited": edited,
                    "project": project,
                },
            )
        )
    candidates.sort(key=lambda entry: entry[0], reverse=True)
    return [row for _, row in candidates[:_LANDED_LIMIT]]


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

    days = _commit_days(repo_dir, paths)
    if days is None:
        return []

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
    if reference.tzinfo is not None:
        reference = reference.astimezone().replace(tzinfo=None)
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
        "progress": _progress(inventory),
        "active": counts["active"],
        "blocked": counts["blocked"],
        "pending": counts["pending"],
        "shipped": counts["shipped"],
        "live": len(live_pointers),
        "last_edited": last_edited,
        "last_modified": last_edited,
        "recent_landed": _recent_landed(inventory, project, reference),
        "active_sprint": _active_sprint(discovered),
        "activity30": _activity30(docs_dir.parent, docs_dir, reference),
    }


def collect_project_rows(
    mounts: Mapping[str, Path],
    *,
    state_root: Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Return compact fleet rows for every mounted project in stable order."""

    return [
        compute_project_row(path, name, state_root=state_root, now=now)
        for name, path in sorted(mounts.items())
    ]
