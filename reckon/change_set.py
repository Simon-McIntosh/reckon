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
read and not again by the next. A change landing after the instant this read
observed state is outside that window: it is reported once, by the next answer,
which is why the end is bounded here rather than left open to the wall clock.

A window that reaches further back than the transition stream retains cannot be
answered as an empty change set. Such a read widens its window to the oldest
row the stream still holds and says so with ``full``, because "nothing changed"
and "I cannot see that far back" are different answers and only one of them is
safe to act on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._schema import parse_plan_ref
from reckon._timestamps import parse_utc

#: How many entries of each list one answer carries. A full window over a long
#: history is bounded here rather than refused, and ``truncated`` says the
#: reader is seeing the most recent part of it.
MAX_ENTRIES = 200


def observed_now(now: datetime | None = None) -> str:
    """The instant a read observed state, in the ISO form a caller passes back."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _epoch(value: str | None) -> int | None:
    parsed = parse_utc(value) if value else None
    return int(parsed.timestamp()) if parsed is not None else None


@dataclass
class _PlanView:
    """One plan document as one revision declares it.

    ``gates`` is parsed on first use and cached: a section carrying no
    declaration is satisfied by its gates alone, so the gate list is needed
    exactly where the change set asks whether a dependency has landed, and
    parsing every plan's body at every revision for a list most of them never
    consult would cost a document parse per revision of the whole project.
    """

    status: str
    declarations: Mapping[str, str]
    depends_on: Mapping[str, Any]
    text: str
    _gates: list[dict[str, Any]] | None = field(default=None, repr=False)

    def gates(self) -> list[dict[str, Any]]:
        if self._gates is None:
            from reckon import _plan_html

            try:
                self._gates = list(_plan_html.read_state(self.text).get("gates") or [])
            except (OSError, ValueError):
                self._gates = []
        return self._gates

    def as_plan(self) -> dict[str, Any]:
        """The shape :func:`reckon.roadmap._section_satisfied` reads."""
        return {
            "status": self.status,
            "section_declarations": dict(self.declarations),
            "gates": self.gates(),
        }


def _plan_view(text: str, path: str) -> _PlanView:
    """Read a plan document's status, section declarations and section edges.

    The head-only reader supplies the declarations and the edges: both are
    authored metadata, so the body's records are not needed to know what a
    section was declared at a past revision.
    """
    from reckon import _plan_html
    from reckon._schema import section_depends_on

    record, metas = _plan_html._meta_head_fields(Path(path), text, Path(path).stem)
    declarations = _plan_html._head_declarations(metas) or {}
    return _PlanView(
        status=str(record.get("status") or ""),
        declarations=declarations,
        depends_on=section_depends_on(text) or {},
        text=text,
    )


def _ref_parts(ref: str) -> tuple[str | None, str, str | None] | None:
    """One dependency ref as ``(project, plan, section)``, or ``None``.

    The grammar is owned by :func:`reckon._schema.parse_plan_ref`, the same
    reader every other surface splits a ref with, so a ref this module cannot
    place is one no other surface can place either.
    """
    parsed = parse_plan_ref(ref)
    if parsed is None:
        return None
    return parsed.project or None, parsed.slug, parsed.stage or None


class _Documents:
    """The plan documents of one repository across one window.

    The revision entering the window and the head are read in one batched
    object read, and each commit inside the window adds its own revision of the
    plans it touched, so a section's change is attributed to the commit that
    made it rather than to the last commit the window happens to hold.
    """

    def __init__(self, repo: Path, since_epoch: int, observed_epoch: int, prefix: str):
        self.repo = repo
        self.prefix = prefix
        self.since_epoch = since_epoch
        self.observed_epoch = observed_epoch
        self._views: dict[tuple[str, str], _PlanView | None] = {}
        self.history = self._history()
        self.base_of = self._base_revision()
        self.window = {
            path: [
                entry
                for entry in entries
                if since_epoch < int(entry["epoch"]) <= observed_epoch
            ]
            for path, entries in self.history.items()
        }
        self._load()

    def _history(self) -> dict[str, list[dict[str, Any]]]:
        from reckon import velocity

        return velocity._plan_history(self.repo, "HEAD", prefix=self.prefix)

    def _base_revision(self) -> dict[str, str | None]:
        """The last commit at or before the window's start that touched each plan.

        That commit's tree is the state the plan carried entering the window,
        so comparing it with ``HEAD`` is exactly the changes the window holds.
        A plan that did not exist then has no base, and every section it
        declares now arrived inside the window.
        """
        base: dict[str, str | None] = {}
        for path, entries in self.history.items():
            earlier = [
                entry for entry in entries if int(entry["epoch"]) <= self.since_epoch
            ]
            base[path] = str(earlier[-1]["sha"]) if earlier else None
        return base

    def _load(self) -> None:
        from reckon import velocity

        specs: list[Any] = []
        seen: set[tuple[str, str]] = set()
        for path in sorted(self.history):
            revisions = ["HEAD"]
            base = self.base_of.get(path)
            if base:
                revisions.append(base)
            revisions.extend(str(entry["sha"]) for entry in self.window.get(path, ()))
            for revision in revisions:
                if (revision, path) in seen:
                    continue
                seen.add((revision, path))
                specs.append((revision, path))
        blobs = velocity.read_blobs(self.repo, specs) if specs else {}
        for revision, path in seen:
            text = blobs.get((revision, path))
            self._views[(revision, path)] = (
                _plan_view(text.decode("utf-8", errors="replace"), path)
                if text
                else None
            )

    def at_head(self, path: str) -> _PlanView | None:
        return self._views.get(("HEAD", path))

    def at_base(self, path: str) -> _PlanView | None:
        base = self.base_of.get(path)
        return self._views.get((base, path)) if base else None

    def at_commit(self, path: str, sha: str) -> _PlanView | None:
        return self._views.get((sha, path))

    def slug_of(self, path: str) -> str:
        return Path(path).stem


def _satisfied(slug: str, section: str | None, views: Mapping[str, _PlanView]) -> bool:
    """Whether one dependency ref's target has landed in the given revision.

    The verdict is the roadmap's own — :func:`reckon.roadmap._section_satisfied`
    — so a section released here is one the roadmap releases too, including the
    arm where a section carries no declaration and only its passed gates
    satisfy it.
    """
    from reckon.roadmap import _section_satisfied

    view = views.get(slug)
    if view is None:
        return False
    return _section_satisfied(view.as_plan(), section or "")


def _ready(
    slug: str, section: str, views: Mapping[str, _PlanView], project: str
) -> bool:
    """Whether a section is dispatchable in one revision of the project.

    Readiness is the section's own declaration being implementable with nothing
    it waits on still open.
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
        parts = _ref_parts(ref)
        if parts is None:
            return False
        target_project, target_plan, target_section = parts
        if target_project is not None and target_project != project:
            return False
        if not _satisfied(target_plan, target_section, views):
            return False
    return True


def _section_changes(
    documents: _Documents, heads: Mapping[str, _PlanView]
) -> list[dict[str, Any]]:
    """Every section whose declaration moved, credited to the commit that moved it.

    Each plan's revisions are walked in order — the state entering the window,
    then every commit inside it — and a section's change is credited to the
    last commit in that walk at which its declaration differs from the one
    before it. A window holding two commits therefore names two commits for two
    moved sections, instead of crediting both to the newest.
    """
    changes: list[dict[str, Any]] = []
    for path in sorted(documents.history):
        window = documents.window.get(path) or []
        if not window:
            continue
        slug = documents.slug_of(path)
        head = heads.get(slug)
        if head is None:
            continue
        chain: list[tuple[dict[str, Any] | None, _PlanView | None]] = [
            (None, documents.at_base(path))
        ]
        chain.extend(
            (entry, documents.at_commit(path, str(entry["sha"]))) for entry in window
        )
        before = chain[0][1].declarations if chain[0][1] is not None else {}
        for section in sorted(set(before) | set(head.declarations)):
            previous = str(before.get(section) or "").strip()
            moved_at: dict[str, Any] | None = None
            for entry, view in chain[1:]:
                current = (
                    str(view.declarations.get(section) or "").strip()
                    if view is not None
                    else ""
                )
                if current != previous:
                    moved_at = entry
                    previous = current
            if moved_at is None:
                continue
            now = str(head.declarations.get(section) or "").strip()
            was = str(before.get(section) or "").strip()
            if was == now:
                continue
            changes.append(
                {
                    "ref": f"{slug}#{section}",
                    "plan": slug,
                    "section": section,
                    "from": was or None,
                    "to": now or None,
                    "commit": str(moved_at["sha"]),
                    "observed_at": observed_now(
                        datetime.fromtimestamp(int(moved_at["epoch"]), tz=UTC)
                    ),
                }
            )
    return changes


def _landed(
    documents: _Documents,
    heads: Mapping[str, _PlanView],
    project: str,
) -> set[tuple[str, str]]:
    """The ``(plan, section)`` targets whose satisfaction moved in the window.

    A target has landed when it did not satisfy its dependents entering the
    window and does at its end. That is broader than a declaration reaching
    ``done``: a section carrying no declaration is satisfied by its passed
    gates, and a gate closing inside the window releases its dependents exactly
    as a declaration does.
    """
    bases: dict[str, _PlanView] = {}
    for path in documents.history:
        view = documents.at_base(path)
        if view is not None:
            bases[documents.slug_of(path)] = view
    landed: set[tuple[str, str]] = set()
    for slug in sorted(heads):
        head = heads[slug]
        before = bases.get(slug)
        if before is None:
            continue
        sections = set(head.declarations) | set(before.declarations)
        for gate in head.gates():
            sections.add(str(gate.get("section") or ""))
        sections.discard("")
        for section in sorted(sections):
            if _satisfied(slug, section, bases):
                continue
            if _satisfied(slug, section, heads):
                landed.add((slug, section))
    return landed


def _newly_ready(
    documents: _Documents,
    landed: set[tuple[str, str]],
    heads: Mapping[str, _PlanView],
    project: str,
) -> list[dict[str, Any]]:
    """Sections whose readiness moved because a section they wait on landed.

    Only a landed target can release a dependent, so only those are walked.
    Each dependent is compared across the same two revisions its dependency was
    read at, which is what makes "moved" a measurement of the window rather
    than of the present.
    """
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
                parts = _ref_parts(ref)
                if parts is None:
                    continue
                target_project, target_plan, target_section = parts
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
    project: str, since_epoch: int, observed_epoch: int, stream_path: Path
) -> tuple[list[dict[str, Any]], int | None, bool]:
    """Every run whose classification moved in the transition stream.

    The rows carry their own instant, so the producer's transition is the
    timestamp source and nothing is inferred from a file's mtime. A rendered
    row whose zone could not be established carries no instant and is left out
    rather than placed at a guessed time — the change still appears in the run's
    next JSON row, which is stamped.

    The third value says whether the stream was there to read: a project with
    no producer has no run history, which is a different fact from a stream
    that was read and held nothing in the window.
    """
    from reckon.crew.query import read_watch_rows

    if not stream_path.is_file():
        return [], None, False
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
        if moment is None or not since_epoch < moment <= observed_epoch:
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
    return changes, oldest, True


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
    observed_epoch = _epoch(observed_at)
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
    if since_epoch is None or observed_epoch is None:
        # A caller naming no window, or naming one that cannot be read, asks
        # for no change set. The instant is still returned so its next read has
        # one to pass, and an unreadable instant is named rather than silently
        # treated as "nothing changed".
        return answer

    root = _resolve_repo(project, repo)
    stream = Path(stream_path) if stream_path else _resolve_stream(project)

    runs, oldest_row, read_stream = _run_changes(
        project, since_epoch, observed_epoch, stream
    )
    answer["sources"]["runs"] = read_stream
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

        documents = _Documents(root, window_start, observed_epoch, PLAN_DIRECTORY)
        heads = {
            documents.slug_of(path): documents.at_head(path)
            for path in documents.history
        }
        heads = {slug: view for slug, view in heads.items() if view is not None}
        sections = _section_changes(documents, heads)
        answer["sections"] = sections
        answer["newly_ready"] = _newly_ready(
            documents, _landed(documents, heads, project), heads, project
        )
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
