"""Decide whether a report item is persisted, cited, or working material.

A report item under a project's reports store is working material unless
something deliberately keeps it. Two things do: a persisted record, and a
citation from one of the project's own plan, research or evidence documents.
Everything else expires — at the close of the plan it is attributed to, or at
the 30-day ceiling when no plan is attributed at all.

One function, :func:`classify_item`, owns that judgement. The retention sweep
and any listing read it, so a listing never shows an item as retained while
the sweep removes it.

Attribution is the subtler half. An item belongs to a plan when its own name is
that plan's slug, or when a ledger row of the same project names a plan and
owns a run report directory inside the item. Nothing else attributes it: an
item nobody can name a plan for must not be ended by any plan's close.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

RETENTION_DAYS = 30

PERSISTED = "persisted"
CITED = "cited"
WORKING = "working"
CLASSES = (PERSISTED, CITED, WORKING)

# Statuses a plan never leaves. An item attributed to one of these has reached
# its plan's close, so nothing but a persist or a citation keeps it.
TERMINAL_STATUSES = frozenset(
    {"shipped", "done", "archived", "superseded", "abandoned"}
)

# Folders reckon writes into a project's reports store. None of them is ever an
# item itself: ``plan-review`` keeps its own retention rule, while the other
# three hold items as their children so the folder is never removed whole.
PLAN_REVIEW_FOLDER = "plan-review"
CONTAINER_FOLDERS = ("briefs", "code-review", "portfolio-review")
RECKON_WRITTEN_FOLDERS = (PLAN_REVIEW_FOLDER, *CONTAINER_FOLDERS)

_LINK_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w/.-])(/[^\s\"'<>()]+)")


@dataclass(frozen=True)
class Verdict:
    cls: str
    expires: datetime | None
    reason: str
    plan: str | None = None

    @property
    def kept(self) -> bool:
        return self.expires is None


@dataclass
class Context:
    """Everything one project's items are judged against, read once."""

    project: str
    now: datetime
    persisted: tuple[Path, ...] = ()
    citations: tuple[Path, ...] = ()
    live: tuple[Path, ...] = ()
    plan_statuses: dict[str, str] = field(default_factory=dict)
    # (plan slug, run report directory) for every ledger row of this project
    # that names a plan.
    ledger_report_dirs: tuple[tuple[str, Path], ...] = ()
    staging_root: Path | None = None


def _forms(path: Path) -> tuple[Path, ...]:
    forms: list[Path] = [path]
    try:
        resolved = path.resolve()
    except OSError:
        return tuple(forms)
    if resolved not in forms:
        forms.append(resolved)
    return tuple(forms)


def _names_path(item_forms: tuple[Path, ...], candidate: Path) -> bool:
    """True when ``candidate`` is the item or lies inside it."""
    for other in _forms(candidate):
        for item in item_forms:
            if other == item or other.is_relative_to(item):
                return True
    return False


def attribution(context: Context, item: Path) -> str | None:
    """Return the plan slug this item is attributed to, or ``None``."""
    if not item.is_symlink() and item.name in context.plan_statuses:
        return item.name
    for plan_slug, report_dir in context.ledger_report_dirs:
        if _names_path(_forms(item), report_dir):
            return plan_slug
    return None


def classify_item(
    context: Context, item: Path, *, newest_write: datetime | None
) -> Verdict:
    """Return the class, expiry and reason for one report item.

    A kept item's expiry is ``None``. A working item expires at the earlier of
    its plan's close — reported as the item's own newest write once that plan is
    terminal — and ``RETENTION_DAYS`` after its newest write. ``newest_write``
    is supplied by the caller so one walk of the item serves both the verdict
    and the sweep's report.
    """
    item_forms = _forms(item)

    if any(_names_path(item_forms, kept) for kept in context.persisted):
        return Verdict(PERSISTED, None, "a persisted record keeps a copy of it")

    if any(_names_path(item_forms, cited) for cited in context.citations):
        return Verdict(CITED, None, "a project document cites it")

    if any(_names_path(item_forms, live) for live in context.live):
        return Verdict(WORKING, None, "a live run pointer names it")

    modified = newest_write
    if modified is None:
        return Verdict(WORKING, None, "no file inside it carries a write time")
    ceiling = modified + timedelta(days=RETENTION_DAYS)

    plan = attribution(context, item)
    if plan is not None and context.plan_statuses.get(plan) in TERMINAL_STATUSES:
        status = context.plan_statuses[plan]
        return Verdict(
            WORKING,
            modified,
            f"plan {plan} is {status}, so the item expires at its close",
            plan=plan,
        )
    if plan is not None:
        return Verdict(
            WORKING,
            ceiling,
            f"working material for plan {plan}; expires {RETENTION_DAYS} days "
            "after its newest write",
            plan=plan,
        )
    return Verdict(
        WORKING,
        ceiling,
        f"working material attributed to no plan; expires {RETENTION_DAYS} "
        "days after its newest write",
    )


def citation_paths(document: Path) -> set[Path]:
    """Absolute filesystem paths a document cites.

    An href or src attribute, and any absolute path written as text. A
    project-absolute URL such as ``/project/figures/x.svg`` resolves under the
    served page root rather than the filesystem, so it never matches a report
    item and costs nothing.
    """
    try:
        text = document.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    raw: list[str] = list(_LINK_RE.findall(text))
    raw.extend(_ABSOLUTE_PATH_RE.findall(text))
    found: set[Path] = set()
    for value in raw:
        candidate = value.strip()
        if not candidate.startswith("/"):
            continue
        candidate = candidate.split("#", 1)[0].split("?", 1)[0]
        if candidate:
            found.add(Path(candidate))
    return found


def report_items(project_dir: Path) -> list[Path]:
    """Return the report items of one project's reports directory.

    Each child is an item except ``plan-review``, which keeps its own rule.
    The three other reckon-written folders hold items as their children, so
    the folder itself is never offered for removal whole.
    """
    if not project_dir.is_dir():
        return []
    items: list[Path] = []
    for child in sorted(project_dir.iterdir()):
        if child.name == PLAN_REVIEW_FOLDER:
            continue
        if child.name in CONTAINER_FOLDERS and child.is_dir():
            items.extend(sorted(child.iterdir()))
            continue
        items.append(child)
    return items


def build_context(
    *, project: str, docs_dir: Path, reports_root: Path, now: datetime
) -> Context:
    """Read everything one project's items are judged against, once."""
    from reckon import _plan_html, flight, ledger
    from reckon.crew import persist
    from reckon.crew.runs import list_live
    from reckon.resources import iter_resources

    persisted: list[Path] = []
    try:
        for record in persist.read_persisted(project, docs_dir=docs_dir):
            source = str(record.get("source") or "")
            if source:
                persisted.append(Path(source))
    except (OSError, ValueError):
        persisted = []

    citations: list[Path] = []
    plan_statuses: dict[str, str] = {}
    for resource in iter_resources(docs_dir, project, ignore_invalid=True):
        if resource.type not in ("plan", "research", "evidence"):
            continue
        citations.extend(sorted(citation_paths(resource.path)))
        if resource.type == "plan":
            plan_statuses[resource.slug] = str(
                _plan_html.parse_plan(resource.path).get("status") or ""
            )

    project_reports = reports_root / project
    live: list[Path] = [
        project_reports / str(record.get("run_id") or "")
        for record in list_live(project=project)
    ]

    ledger_dirs: list[tuple[str, Path]] = []
    try:
        for row in ledger.runs(project, root=docs_dir.parent):
            plan_slug = str(row.get("plan") or "")
            run_id = str(row.get("run_id") or "")
            if plan_slug and run_id:
                run_dir = project_reports / run_id
                if not run_dir.exists():
                    for parent in sorted(project_reports.iterdir()):
                        if parent.is_dir() and (parent / run_id).exists():
                            run_dir = parent / run_id
                            break
                ledger_dirs.append((plan_slug, run_dir))
    except (OSError, ValueError):
        ledger_dirs = []

    staging: Path | None = None
    try:
        declared = flight.reports_setting(
            flight.resolve(project=project).config, flight.STAGING_ROOT_KEY
        )
        if declared:
            staging = Path(declared).expanduser()
    except (OSError, ValueError):
        staging = None

    return Context(
        project=project,
        now=now,
        persisted=tuple(persisted),
        citations=tuple(citations),
        live=tuple(live),
        plan_statuses=plan_statuses,
        ledger_report_dirs=tuple(ledger_dirs),
        staging_root=staging,
    )
