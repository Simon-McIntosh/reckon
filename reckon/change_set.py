"""What a project's state did between two reads.

A read that names no window answers with the whole of a project's state, so a
coordinator polling for work re-reads all of it on every poll. This module
answers the narrower question — what moved since the instant a previous read
returned — from the two timestamped sources reckon already keeps, so the answer
adds no store, no cursor and no refusal:

* a section's declaration comes from the plan files' git history, which
  :func:`reckon.velocity._plan_history` already reads, each change carrying its
  commit time;
* a run's classification change comes from the project producer's transition
  stream, whose rows carry ``observed_at``.

Wiring findings are absent by construction: they are recomputed on every read
and keep no history, so a change set that named them would be inventing one.

The window is half-open at its start and closed at its end, both at second
resolution: a change is reported when ``since < time <= observed_at``. The
caller passes the previous answer's ``observed_at`` as its next ``since``, so a
change landing in the same second a read observed state is reported by that
read and not again by the next.

A window that reaches further back than the transition stream retains cannot be
answered as an empty change set. Such a read widens its window to the oldest
row the stream still holds and says so with ``full``, because "nothing changed"
and "I cannot see that far back" are different answers and only one of them is
safe to act on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._timestamps import parse_utc

#: How many entries of each list one answer carries. A full window over a long
#: history is bounded here rather than refused, and ``truncated`` says the
#: reader is seeing the most recent part of it.
MAX_ENTRIES = 200

#: A dependency ref is ``[project:]plan[#section]``, the one grammar every row
#: and edge in reckon uses.
_REF_SECTION = "#"


def observed_now(now: datetime | None = None) -> str:
    """The instant a read observed state, in the ISO form a caller passes back."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _epoch(value: str | None) -> int | None:
    parsed = parse_utc(value) if value else None
    return int(parsed.timestamp()) if parsed is not None else None


@dataclass(frozen=True)
class _PlanView:
    """One plan document as one revision declares it."""

    status: str
    declarations: Mapping[str, str]
    depends_on: Mapping[str, Any]


def _plan_view(text: str, path: str) -> _PlanView:
    """Read a plan document's status, section declarations and section edges.

    The head-only reader supplies all three: the declarations are authored
    metadata, so the body's records are not needed to know what a section was
    declared at a past revision, and parsing a body for either end of the
    window would cost a full document parse per revision.
    """
    from reckon import _plan_html
    from reckon._schema import section_depends_on

    record, metas = _plan_html._meta_head_fields(Path(path), text, Path(path).stem)
    declarations = _plan_html._head_declarations(metas) or {}
    return _PlanView(
        status=str(record.get("status") or ""),
        declarations=declarations,
        depends_on=section_depends_on(text) or {},
    )


def _split_ref(ref: str) -> tuple[str | None, str, str | None]:
    """Split one dependency ref into ``(project, plan, section)``."""
    text = str(ref or "").strip()
    plan_part, _, section = text.partition(_REF_SECTION)
    project, _, plan = plan_part.rpartition(":")
    return (project or None), plan.strip(), (section.strip() or None)


class _Documents:
    """The plan documents of one repository at the two ends of a window.

    Both revisions are read in one batched object read, so a change set over a
    whole project is one pass over the window's plan blobs rather than a
    subprocess per plan.
    """

    def __init__(self, repo: Path, since_epoch: int | None, prefix: str):
        self.repo = repo
        self.prefix = prefix
        self._views: dict[tuple[str, str], _PlanView] = {}
        self.history = self._history()
        self.base_of = self._base_revision(since_epoch)
        self._load()

    def _history(self) -> dict[str, list[dict[str, Any]]]:
        from reckon import velocity

        return velocity._plan_history(self.repo, "HEAD", prefix=self.prefix)

    def _base_revision(self, since_epoch: int | None) -> dict[str, str | None]:
        """The last commit at or before ``since`` that touched each plan.

        That commit's tree is the state the plan carried entering the window,
        so comparing it with ``HEAD`` is exactly the changes the window holds.
        A plan that did not exist then has no base, and every section it
        declares now arrived inside the window.
        """
        base: dict[str, str | None] = {}
        for path, entries in self.history.items():
            if since_epoch is None:
                base[path] = None
                continue
            earlier = [entry for entry in entries if int(entry["epoch"]) <= since_epoch]
            base[path] = str(earlier[-1]["sha"]) if earlier else None
        return base

    def _load(self) -> None:
        from reckon import velocity

        paths = sorted(self.history) or list(self.base_of)
        specs: list[Any] = []
        for path in paths:
            specs.append(("HEAD", path))
            base = self.base_of.get(path)
            if base:
                specs.append((base, path))
        blobs = velocity.read_blobs(self.repo, specs) if specs else {}
        for path in paths:
            head = blobs.get(("HEAD", path))
            if head:
                self._views[("HEAD", path)] = _plan_view(
                    head.decode("utf-8", errors="replace"), path
                )
            base = self.base_of.get(path)
            text = blobs.get((base, path)) if base else None
            if text:
                self._views[(base, path)] = _plan_view(
                    text.decode("utf-8", errors="replace"), path
                )

    def at_head(self, path: str) -> _PlanView | None:
        return self._views.get(("HEAD", path))

    def at_base(self, path: str) -> _PlanView | None:
        base = self.base_of.get(path)
        return self._views.get((base, path)) if base else None

    def slug_of(self, path: str) -> str:
        return Path(path).stem


def _satisfied(slug: str, section: str | None, heads: Mapping[str, _PlanView]) -> bool:
    """Whether one dependency ref's target has landed in the given revision.

    A completed plan satisfies every ref into it. Otherwise a section ref is
    satisfied by that section's own ``done`` declaration, and a whole-plan ref
    is not: a plan that has not completed has work a dependent still needs.
    """
    from reckon.lifecycle import COMPLETED_STATUSES

    view = heads.get(slug)
    if view is None:
        return False
    if view.status in COMPLETED_STATUSES:
        return True
    if not section:
        return False
    return str(view.declarations.get(section) or "").strip() == "done"


def _ready(
    slug: str, section: str, views: Mapping[str, _PlanView], project: str
) -> bool:
    """Whether a section is dispatchable in one revision of the project.

    Readiness is the section's own declaration being implementable with nothing
    it waits on still open. The dependency walk reads the same declaration
    vocabulary the roadmap does, so a section this reports as newly ready is
    one the roadmap reports as ready at the same revision.
    """
    from reckon._schema import is_implementable_section

    view = views.get(slug)
    if view is None:
        return False
    if not is_implementable_section(view.declarations.get(section)):
        return False
    for ref in view.depends_on.get(section) or ():
        if not isinstance(ref, str):
            return False
        target_project, target_plan, target_section = _split_ref(ref)
        if target_project is not None and target_project != project:
            return False
        if not _satisfied(target_plan, target_section, views):
            return False
    return True


def _section_changes(
    documents: _Documents, since_epoch: int | None, heads: Mapping[str, _PlanView]
) -> list[dict[str, Any]]:
    """Every section whose declaration moved in a commit inside the window."""
    changes: list[dict[str, Any]] = []
    for path in sorted(documents.history):
        entries = documents.history[path]
        moved = [
            entry
            for entry in entries
            if since_epoch is not None and int(entry["epoch"]) > since_epoch
        ]
        if not moved:
            continue
        head = heads.get(documents.slug_of(path))
        base = documents.at_base(path)
        if head is None:
            continue
        before = base.declarations if base is not None else {}
        commit = moved[-1]
        for section in sorted(set(before) | set(head.declarations)):
            was = str(before.get(section) or "").strip()
            now = str(head.declarations.get(section) or "").strip()
            if was == now:
                continue
            changes.append(
                {
                    "ref": f"{documents.slug_of(path)}#{section}",
                    "plan": documents.slug_of(path),
                    "section": section,
                    "from": was or None,
                    "to": now or None,
                    "commit": str(commit["sha"]),
                    "observed_at": observed_now(
                        datetime.fromtimestamp(int(commit["epoch"]), tz=UTC)
                    ),
                }
            )
    return changes


def _newly_ready(
    documents: _Documents,
    changes: Sequence[Mapping[str, Any]],
    heads: Mapping[str, _PlanView],
    project: str,
) -> list[dict[str, Any]]:
    """Sections whose readiness moved because a section they wait on landed.

    Only a change that satisfies something can release a dependent, so only
    those are walked. Each dependent is compared across the same two revisions
    its dependency was read at, which is what makes "moved" a measurement of
    the window rather than of the present.
    """
    landed = {
        (str(change["plan"]), str(change["section"]))
        for change in changes
        if change.get("to") == "done"
    }
    if not landed:
        return []
    bases: dict[str, _PlanView] = {}
    for path in documents.history:
        base = documents.at_base(path)
        if base is not None:
            bases[documents.slug_of(path)] = base
    released: list[dict[str, Any]] = []
    for slug in sorted(heads):
        head = heads[slug]
        before = bases.get(slug)
        if before is None:
            continue
        for section, refs in sorted(head.depends_on.items()):
            if not isinstance(refs, (list, tuple)):
                continue
            waited_on = set()
            for ref in refs:
                if not isinstance(ref, str):
                    continue
                target_project, target_plan, target_section = _split_ref(ref)
                if target_project is not None and target_project != project:
                    continue
                if target_section and (target_plan, target_section) in landed:
                    waited_on.add(f"{target_plan}#{target_section}")
            if not waited_on:
                continue
            if _ready(slug, section, bases, project):
                continue
            if not _ready(slug, section, heads, project):
                continue
            released.append(
                {
                    "ref": f"{slug}#{section}",
                    "plan": slug,
                    "section": section,
                    "declaration": str(head.declarations.get(section) or ""),
                    "finished_waiting_on": sorted(waited_on),
                }
            )
    return released


def _run_changes(
    project: str, since_epoch: int | None, stream_path: Path
) -> tuple[list[dict[str, Any]], int | None]:
    """Every run whose classification moved in the transition stream.

    The rows carry their own instant, so the producer's transition is the
    timestamp source and nothing is inferred from a file's mtime. A rendered
    row whose zone could not be established carries no instant and is left out
    rather than placed at a guessed time — the change still appears in the run's
    next JSON row, which is stamped.
    """
    from reckon.crew.query import read_watch_rows

    if not stream_path.is_file():
        return [], None
    rows = [
        row
        for row in read_watch_rows([stream_path])
        if str(row.get("project") or "") == project
    ]
    stamped = [row for row in rows if row.get("observed_at_utc")]
    oldest = min((_epoch(row["observed_at_utc"]) for row in stamped), default=None)
    changes: list[dict[str, Any]] = []
    for row in stamped:
        moved_to = row.get("to_state")
        if row.get("event") != "transition" or not moved_to:
            continue
        if row.get("from_state") == moved_to:
            continue
        moment = _epoch(row["observed_at_utc"])
        if since_epoch is not None and moment is not None and moment <= since_epoch:
            continue
        identity = str(row.get("run_id") or row.get("node") or "")
        entries = [change for change in changes if change.get("run_id") == identity]
        if entries:
            entries[0]["to_state"] = moved_to
            entries[0]["observed_at"] = row["observed_at_utc"]
            entries[0]["transitions"] += 1
            continue
        changes.append(
            {
                "run_id": row.get("run_id"),
                "node": row.get("node"),
                "from_state": row.get("from_state"),
                "to_state": moved_to,
                "observed_at": row["observed_at_utc"],
                "transitions": 1,
            }
        )
    return changes, oldest


def change_set(
    project: str,
    since: str | None = None,
    *,
    repo: str | Path | None = None,
    stream_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Answer what a project's plans and runs did since ``since``.

    ``since`` is an instant a previous read returned as its ``observed_at``,
    or ``None`` for a read that establishes one. ``repo`` is the checkout whose
    history is read and defaults to the project's mounted root; ``stream_path``
    is the producer's transition stream and defaults to the project's own. Both
    are explicit so a test can drive this over a repository it built itself.
    """
    observed_at = observed_now(now)
    since_epoch = _epoch(since)
    since_usable = since is None or since_epoch is not None
    answer: dict[str, Any] = {
        "project": project,
        "since": since,
        "observed_at": observed_at,
        "full": since is None,
        "window_start": since if since_epoch is not None else None,
        "sections": [],
        "newly_ready": [],
        "runs": [],
        "truncated": False,
        "sources": {"plans": False, "runs": False},
        "error": None if since_usable else "unreadable_since",
    }
    if since_epoch is None:
        # A caller naming no window, or naming one that cannot be read, asks
        # for no change set. The instant is still returned so its next read has
        # one to pass, and an unreadable instant is named rather than silently
        # treated as "nothing changed".
        return answer

    root = _resolve_repo(project, repo)
    stream = Path(stream_path) if stream_path else _resolve_stream(project)

    runs, oldest_row = _run_changes(project, since_epoch, stream)
    answer["sources"]["runs"] = True
    window_start = since_epoch
    if oldest_row is not None and since_epoch < oldest_row:
        # The stream does not reach back this far, so the answer is everything
        # it still holds rather than an empty set that would read as no change.
        answer["full"] = True
        window_start = oldest_row
        answer["window_start"] = observed_now(
            datetime.fromtimestamp(oldest_row, tz=UTC)
        )
    answer["runs"] = runs

    if root is not None:
        from reckon.velocity import PLAN_DIRECTORY

        documents = _Documents(root, window_start, PLAN_DIRECTORY)
        heads = {
            documents.slug_of(path): documents.at_head(path)
            for path in documents.history
        }
        heads = {slug: view for slug, view in heads.items() if view is not None}
        sections = _section_changes(documents, window_start, heads)
        answer["sections"] = sections
        answer["newly_ready"] = _newly_ready(documents, sections, heads, project)
        answer["sources"]["plans"] = True

    if len(answer["sections"]) > MAX_ENTRIES:
        answer["sections"] = answer["sections"][-MAX_ENTRIES:]
        answer["truncated"] = True
    if len(answer["newly_ready"]) > MAX_ENTRIES:
        answer["newly_ready"] = answer["newly_ready"][-MAX_ENTRIES:]
        answer["truncated"] = True
    if len(answer["runs"]) > MAX_ENTRIES:
        answer["runs"] = answer["runs"][-MAX_ENTRIES:]
        answer["truncated"] = True
    answer["counts"] = {
        "sections": len(answer["sections"]),
        "newly_ready": len(answer["newly_ready"]),
        "runs": len(answer["runs"]),
    }
    return answer


def _resolve_repo(project: str, repo: str | Path | None) -> Path | None:
    """The checkout whose history a project's plan changes are read from."""
    if repo is not None:
        root = Path(repo).expanduser().resolve()
        return root if root.is_dir() else None
    from reckon._store import _docs_dir_for_project

    docs = _docs_dir_for_project(project)
    return docs.parent if docs is not None else None


def _resolve_stream(project: str) -> Path:
    from reckon.crew.run_paths import watch_stream_path

    return watch_stream_path(project)
