"""Queue a coordinator's chosen sections as one batch.

A coordinator decides which work needs doing, often a few sections from each of
several plans, and writes one entry per chosen section. This module turns that
list into queued requests: it reads each named plan once, fills every field an
entry omits from the section itself, and stores each entry on its own so a
refused entry never stops the rest.

The batch never chooses work and never loads a plan wholesale. What it adds to
a single dispatch is that the per-plan read — the section's done-when, its
write paths, its declared waits — is done once for every section the batch
takes from that plan, and that an entry may wait on another entry of the same
batch by its node id.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from reckon.crew.node import CrewError, TaskNode

#: The fields a batch entry may carry. Anything else is refused by name rather
#: than silently dropped, because a misspelt field would otherwise store a
#: request that does not say what its author asked for.
BATCH_ENTRY_FIELDS = frozenset(
    {
        "project",
        "plan",
        "section",
        "node",
        "goal",
        "after",
        "role",
        "spec_level",
        "done_when",
        "write_paths",
        "regenerate_paths",
        "anchor_paths",
        "negative_control",
        "time_budget",
        "estimated_hours",
        "manifest",
        "brief",
        "peers",
        "requires_decisions",
        "images",
        "inputs",
        "compares_with",
        "session",
        "backend",
    }
)

#: One path token as a section's write-paths sentence names it. A token is a
#: path when it carries a file extension, or when it names at least three
#: segments of a directory tree; a bare word, and a pair such as ``and/or``,
#: are neither, so a sentence that names a file only by description ("the new
#: test") yields no path for it.
_PATH_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")

#: Phases a request reports as waiting for its upstream rather than queued.
WAITING_UPSTREAM_PHASES = frozenset({"awaiting-upstream", "blocked-upstream"})


class BatchEntryRefused(CrewError):
    """One entry cannot be queued, and the rest of the batch still can."""


def read_batch_entries(path: str | Path) -> list[dict[str, Any]]:
    """Read a batch file: a JSON list of entries, or an object carrying one."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise CrewError(f"batch file {path} is not JSON: {exc}") from exc
    if isinstance(payload, Mapping):
        payload = payload.get("entries")
    if not isinstance(payload, list) or not payload:
        raise CrewError(f"batch file {path} carries no entries list")
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, Mapping):
            raise CrewError(f"batch entry {index + 1} is not an object")
        entries.append(dict(entry))
    return entries


@contextlib.contextmanager
def _parsed_once() -> Iterator[None]:
    """Parse each distinct plan text once for the whole batch.

    Queuing a request records the plan's review contract, which reads every
    section's prose; a derivation reads it too. Left alone, a six-entry batch
    over two plans parses each plan once per entry. The batch holds one cache
    for its duration, so every reader inside it — the batch's derivation and
    the queue-time contract alike — buys each parse once, however many sections
    of that plan are chosen.
    """
    from reckon import _plan_html

    original = _plan_html.section_prose
    cache: dict[tuple[str, tuple[tuple[str, Any], ...]], list[Any]] = {}

    def cached(document, **kwargs):
        key = (document, tuple(sorted(kwargs.items())))
        if key not in cache:
            cache[key] = list(original(document, **kwargs))
        return iter(cache[key])

    _plan_html.section_prose = cached
    try:
        yield
    finally:
        _plan_html.section_prose = original


class _PlanRead:
    """One plan document, parsed once for every section the batch takes from it.

    ``section_prose`` is called once per plan, so a batch naming five sections
    of one plan parses that plan once. The prose is kept by section identity so
    each entry's derived fields are bought from that single read.
    """

    def __init__(self, project: str, plan: str, repo_root: Path) -> None:
        from reckon import _plan_html

        self.project = project
        self.plan = plan
        self.repo_root = repo_root
        self.path = repo_root / "docs" / "plans" / f"{plan}.html"
        self.package = repo_root.name
        try:
            self.document = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BatchEntryRefused(
                f"{project}:{plan}: plan document {self.path} cannot be read: {exc}"
            ) from exc
        self.prose: dict[str, str] = {
            identity: text for identity, text in _plan_html.section_prose(self.document)
        }
        self.state = _plan_html.read_state(self.document)

    def section_prose(self, section: str) -> str:
        """The named section's prose, or a refusal naming the section."""
        from reckon.crew.node import normalize_section

        wanted = normalize_section(section)
        for identity, prose in self.prose.items():
            if normalize_section(identity) == wanted:
                return prose
        raise BatchEntryRefused(
            f"{self.project}:{self.plan}#{section}: the plan declares no such section"
        )

    def done_when(self, section: str) -> str:
        from reckon.crew.plan_review import _done_when_paragraph

        paragraph = _done_when_paragraph(self.section_prose(section))
        if not paragraph:
            raise BatchEntryRefused(
                f"{self.project}:{self.plan}#{section}: the section states no "
                "done-when, so the entry must give one"
            )
        return paragraph

    def write_paths(self, section: str) -> list[str]:
        from reckon.crew.plan_review import _WRITE_PATHS_SENTENCE

        prose = self.section_prose(section)
        match = _WRITE_PATHS_SENTENCE.search(prose)
        if match is None:
            return []
        found: list[str] = []
        for token in _PATH_TOKEN.findall(prose[match.end() :]):
            token = token.strip("./-")
            if not token:
                continue
            if not (_EXTENSION.search(token) or token.count("/") >= 2):
                continue
            if token not in found:
                found.append(token)
        return found

    def section_record(self, section: str) -> Mapping[str, Any]:
        from reckon.crew.node import normalize_section

        wanted = normalize_section(section)
        for record in self.state.get("sections") or ():
            if not isinstance(record, Mapping):
                continue
            if normalize_section(str(record.get("id") or "")) == wanted:
                return record
        return {}


class _Plans:
    """Every plan a batch names, each read at most once."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._reads: dict[tuple[str, str], _PlanRead] = {}

    def read(self, project: str, plan: str) -> _PlanRead:
        key = (project, plan)
        if key not in self._reads:
            root = self.repo_root
            if not (root / "docs" / "plans" / f"{plan}.html").is_file():
                # A batch spanning projects reads each plan from the checkout
                # its project is mounted in, never from the caller's directory.
                from reckon.crew.dispatch_sections import project_mount_repository

                mount = project_mount_repository(project)
                if mount is not None:
                    root = mount
            self._reads[key] = _PlanRead(project, plan, root)
        return self._reads[key]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    raise BatchEntryRefused(f"{value!r} is not a list of strings")


def _entry_node(
    index: int,
    entry: Mapping[str, Any],
    *,
    plans: _Plans,
) -> tuple[str, TaskNode]:
    """Build one entry's node, filling every omitted field from its section."""
    unknown = sorted(set(entry) - BATCH_ENTRY_FIELDS)
    if unknown:
        raise BatchEntryRefused(
            f"entry {index + 1} carries unknown field(s): {', '.join(unknown)}"
        )
    project = str(entry.get("project") or "").strip()
    plan = str(entry.get("plan") or "").strip()
    section = str(entry.get("section") or "").strip()
    node_id = str(entry.get("node") or "").strip()
    goal = str(entry.get("goal") or "").strip()
    if not project or not plan or not section:
        raise BatchEntryRefused(
            f"entry {index + 1} must name its project, plan and section"
        )
    if not node_id:
        raise BatchEntryRefused(f"entry {index + 1} names no node id")
    if not goal:
        raise BatchEntryRefused(f"{project}:{plan}#{section}: the entry gives no goal")

    read = plans.read(project, plan)
    record = read.section_record(section)

    done_when = str(entry.get("done_when") or "").strip() or read.done_when(section)
    write_paths = _as_list(entry.get("write_paths"))
    if not write_paths:
        write_paths = read.write_paths(section)
    if not write_paths:
        raise BatchEntryRefused(
            f"{project}:{plan}#{section}: the section states no write-paths "
            "sentence and the entry gives none"
        )

    estimated_hours = entry.get("estimated_hours")
    if estimated_hours is None:
        estimate = record.get("effort_hours")
        estimated_hours = float(estimate) if isinstance(estimate, (int, float)) else None
    spec_level = str(entry.get("spec_level") or "")

    node = TaskNode(
        id=node_id,
        goal=goal,
        plan=plan,
        section=section,
        after=_as_list(entry.get("after")),
        brief=str(entry.get("brief") or ""),
        role=str(entry.get("role") or "implement"),
        spec_level=spec_level,
        negative_control=str(entry.get("negative_control") or ""),
        done_when=done_when,
        write_paths=list(write_paths),
        regenerate_paths=_as_list(entry.get("regenerate_paths")),
        anchor_paths=_as_list(entry.get("anchor_paths")),
        time_budget=str(entry.get("time_budget") or ""),
        estimated_hours=estimated_hours,
        manifest_path=str(entry.get("manifest") or ""),
        requires_decisions=_as_list(entry.get("requires_decisions")),
        peer_scopes=_peer_scopes(entry.get("peers")),
        images=_as_list(entry.get("images")),
        inputs=_as_list(entry.get("inputs")),
        compares_with=str(entry.get("compares_with") or ""),
    )
    return project, node


def _peer_scopes(values: Any) -> dict[str, list[str]]:
    scopes: dict[str, list[str]] = {}
    for item in _as_list(values):
        name, _, paths = str(item).partition("=")
        scopes[str(name).strip()] = [
            path.strip() for path in paths.split(",") if path.strip()
        ]
    return scopes


def _batch_order(entries: Sequence[Mapping[str, Any]]) -> Iterator[int]:
    """Yield entry indexes so an entry's batch-siblings are stored before it."""
    by_node = {
        str(entry.get("node") or "").strip(): index
        for index, entry in enumerate(entries)
        if str(entry.get("node") or "").strip()
    }
    pending = list(range(len(entries)))
    emitted: set[int] = set()
    while pending:
        progressed = False
        for index in list(pending):
            entry = entries[index]
            siblings = {
                by_node[ref]
                for ref in _as_list(entry.get("after"))
                if ref in by_node and by_node[ref] != index
            }
            if siblings <= emitted:
                emitted.add(index)
                pending.remove(index)
                progressed = True
                yield index
        if not progressed:
            raise CrewError(
                "batch waits form a cycle among entries: "
                + ", ".join(str(entries[index].get("node")) for index in pending)
            )


def queue_batch(
    entries: Sequence[Mapping[str, Any]],
    *,
    session: str,
    config_for,
    repo_root: str | Path,
    launch_kind: str = "cli",
) -> dict[str, Any]:
    """Queue each entry on its own — see :func:`_queue_batch` — parsing once."""
    with _parsed_once():
        return _queue_batch(
            entries,
            session=session,
            config_for=config_for,
            repo_root=repo_root,
            launch_kind=launch_kind,
        )


def _queue_batch(
    entries: Sequence[Mapping[str, Any]],
    *,
    session: str,
    config_for,
    repo_root: str | Path,
    launch_kind: str = "cli",
) -> dict[str, Any]:
    """Queue each entry on its own and report one row per entry.

    ``config_for`` answers the flight configuration for one entry's project, so
    a batch spanning projects is queued under each project's own lanes. The
    ``ok`` field is true only when every entry was stored, while a refused
    entry leaves the rest of the batch stored either way.
    """
    from reckon.crew import queue_admission
    from reckon.crew.dispatch import queue_requests as queue_one
    from reckon.crew.runs import new_run_id, read_pointer

    root = Path(repo_root).expanduser().resolve()
    plans = _Plans(root)
    results: list[dict[str, Any]] = []
    refused = 0

    def refuse(index: int, entry: Mapping[str, Any], detail: str) -> None:
        nonlocal refused
        refused += 1
        results.append(
            {
                "index": index,
                "node": entry.get("node"),
                "project": entry.get("project"),
                "plan": entry.get("plan"),
                "section": entry.get("section"),
                "status": "refused",
                "reason": detail,
            }
        )

    stored_by_node: dict[str, str] = {}
    ordered: list[int]
    try:
        ordered = list(_batch_order(entries))
    except CrewError as exc:
        # A cycle is refused whole: no entry of it can be ordered, so none is
        # stored and every member is reported by name.
        for index, entry in enumerate(entries):
            refuse(index, entry, str(exc))
        return {"ok": False, "queued": 0, "refused": refused, "results": results}

    for index in ordered:
        entry = entries[index]
        try:
            project, node = _entry_node(index, entry, plans=plans)
            # An entry waits on another entry of this batch by that entry's node
            # id; the stored request is named by its run id, which the earlier
            # entry's row carries.
            after: list[str] = []
            for ref in node.after:
                if ref in stored_by_node:
                    after.append(stored_by_node[ref])
                else:
                    after.append(ref)
            node.after = after

            config = config_for(project)
            backend = str(entry.get("backend") or "")
            run_id = new_run_id(node.id)
            # The batch stays inside the per-section readiness read: queuing
            # each entry through the project-wide view would build every plan
            # in the project once per entry, which is what the batch exists to
            # avoid. The review contract is stamped per stored request exactly
            # as the single-dispatch queue path stamps it.
            stored = queue_one(
                [node],
                project=project,
                session=str(entry.get("session") or session),
                repo_root=plans.read(project, node.plan).repo_root,
                config=config,
                backend_name=backend,
                launch_kind=launch_kind,
                run_ids={node.id: run_id},
            )
            for stored_id in stored:
                pointer = read_pointer(stored_id)
                if isinstance(pointer, dict):
                    queue_admission.record_review_contract(pointer)
            if not stored:
                raise BatchEntryRefused(
                    f"{project}:{node.plan}#{node.section}: the request was not stored"
                )
            actual_id, row = next(iter(stored.items()))
            phase = str(row.get("phase") or "")
            stored_by_node[node.id] = actual_id
            already = actual_id != run_id
            status = "already-queued" if already else (
                "awaiting-upstream" if phase in WAITING_UPSTREAM_PHASES else phase
            )
            results.append(
                {
                    "index": index,
                    "node": node.id,
                    "project": project,
                    "plan": node.plan,
                    "section": node.section,
                    "status": status or "queued",
                    "run_id": actual_id,
                    "position": row.get("position"),
                    "waits": list(row.get("waits") or ()),
                }
            )
        except (BatchEntryRefused, CrewError) as exc:
            refuse(index, entry, str(exc))

    results.sort(key=lambda row: int(row["index"]))
    return {
        "ok": refused == 0,
        "queued": len(results) - refused,
        "refused": refused,
        "results": results,
    }