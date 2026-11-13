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
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

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
        "waits_only_after",
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
        "allow_unreviewed_plan",
    }
)

#: One path token as a section's write-paths sentence names it. A token is a
#: path when it names a directory (`src/payload.py`) or carries a source-file
#: extension (`.py`, `.md`). Prose abbreviations such as ``e.g`` and version
#: tags such as ``v1.2`` carry neither, so a sentence that names a file only by
#: description ("the new test") yields no path for it. A leading ``./`` or
#: ``../`` is taken as the prefix it is — never as a bare slash — and a leading
#: ``/`` not preceded by a dot keeps an absolute path absolute: a sentence
#: naming a file outside the repository must derive that file's whole path,
#: since a slash dropped turns it into a relative path that resolves nowhere.
#: The prefix is matched whole so a parent segment stays visible in the token,
#: where ``write_paths`` can refuse it rather than derive a shortened form.
_PATH_TOKEN = re.compile(r"(?:\.{1,2}/)*/?[A-Za-z0-9_][A-Za-z0-9_./-]*")
_SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".json",
        ".html",
        ".css",
        ".md",
        ".rst",
        ".toml",
        ".ini",
        ".cfg",
        ".yaml",
        ".yml",
        ".sh",
        ".sql",
        ".txt",
        ".lock",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".f90",
        ".cmake",
        ".j2",
        ".svg",
    }
)

#: The write-paths sentence ends at its first full stop. A page's tags are
#: stripped to text with a space in their place, so a sentence closing on a
#: ``<code>`` span reads ``one.py .`` — the stop is found whether or not a
#: space precedes it, while the stops inside a path (``one.py``) never end it.
_SENTENCE_END = re.compile(r"\.(?=\s|$)")

#: Phases a request reports as waiting for its upstream rather than queued.
WAITING_UPSTREAM_PHASES = frozenset({"awaiting-upstream", "blocked-upstream"})


class BatchEntryRefusedError(CrewError):
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

    The authored section headings are memoised on the same boundary and on the
    document text itself, so the roadmap's heading walk and every wait edge
    that names a section of the same plan share one parse rather than paying
    for two independent ones.
    """
    from reckon import _plan_html

    original = _plan_html.section_prose
    cache: dict[tuple[str, tuple[tuple[str, Any], ...]], list[Any]] = {}

    def cached(document, **kwargs):
        key = (document, tuple(sorted(kwargs.items())))
        if key not in cache:
            cache[key] = list(original(document, **kwargs))
        return iter(cache[key])

    original_headings = _plan_html.authored_section_headings
    headings: dict[str, list[Any]] = {}

    def counted_headings(html_text: str):
        if html_text not in headings:
            headings[html_text] = original_headings(html_text)
        return headings[html_text]

    _plan_html.section_prose = cached
    _plan_html.authored_section_headings = counted_headings
    try:
        yield
    finally:
        _plan_html.section_prose = original
        _plan_html.authored_section_headings = original_headings


@contextlib.contextmanager
def _built_once(
    cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> Iterator[None]:
    """Derive each project's roadmap at most once for the whole batch.

    Two readers inside a batch ask for a project's roadmap: queue ordering
    ranks each stored request against it, and the wait check reads a section's
    own verdict from it. Left alone, a six-entry batch over one project derives
    that roadmap six times over — once per stored request and again per section
    whose waits are checked — which is the cost the batch exists to remove.

    The batch derives each project's roadmap once, over the project's whole
    inventory, and serves every reader inside it from that one derivation. The
    whole-inventory build is the one ordering needs, and it answers the
    bounded per-section question too: a section verdict is computed the same
    way from either inventory, so the single build carries the subset's answer.
    """
    from reckon import roadmap as roadmap_module
    from reckon.roadmap import roadmap_build_scope

    original = roadmap_module.build_roadmap
    built: dict[tuple[str, str], dict[str, Any]] = cache if cache is not None else {}

    def once(project, inventory, sprints, **kwargs):
        from reckon._store import _docs_dir_for_project
        from reckon.mcp_discovery import _discover_project, _inventory_row

        # Ordering names no docs dir and resolves through the project's mount,
        # while the wait check names the checkout's own; both answer with the
        # same tree, so the key is the resolved dir rather than the arguments.
        docs_dir = kwargs.get("docs_dir")
        resolved = Path(docs_dir) if docs_dir else _docs_dir_for_project(project, None)
        key = (str(project), str(resolved or ""))
        if key not in built:
            # Discovery derives the project's roadmap to attach its ready set,
            # and the scope hands that derivation back, so the batch takes it
            # rather than paying for a second one. The build inside discovery
            # runs unwrapped: leaving the wrapper in place would re-enter here
            # for the inventory it is being called to produce.
            roadmap_module.build_roadmap = original
            try:
                # Neither reader in a batch, queue ordering or the wait check,
                # reads who coordinates a critical-path link, so the build skips
                # that resolution: it reads every run's ledger document, which
                # was most of each entry's queue time.
                with roadmap_build_scope(resolve_coordinators=False) as scope:
                    discovered = _discover_project(
                        str(project), Path(resolved).parent if resolved else None
                    )
            except Exception:  # noqa: BLE001 - the caller's inventory carries it
                discovered = {}
                scope = None
            finally:
                roadmap_module.build_roadmap = once
            rows = [
                _inventory_row(item) for item in discovered.get("inventory", ()) or ()
            ]
            if scope is not None and scope.built is not None:
                built[key] = scope.built
            elif rows:
                built[key] = original(
                    project,
                    rows,
                    list(discovered.get("sprints") or ()) or list(sprints),
                    active_sprint_id=discovered.get("active_sprint_id"),
                    docs_dir=resolved,
                )
            else:
                built[key] = original(project, inventory, sprints, **kwargs)
        return built[key]

    roadmap_module.build_roadmap = once
    try:
        yield
    finally:
        roadmap_module.build_roadmap = original


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
            raise BatchEntryRefusedError(
                f"{project}:{plan}: plan document {self.path} cannot be read: {exc}"
            ) from exc
        self.prose: dict[str, str] = dict(_plan_html.section_prose(self.document))
        self.state = _plan_html.read_state(self.document)

    def section_prose(self, section: str) -> str:
        """The named section's prose, or a refusal naming the section."""
        from reckon.crew.node import normalize_section

        wanted = normalize_section(section)
        for identity, prose in self.prose.items():
            if normalize_section(identity) == wanted:
                return prose
        raise BatchEntryRefusedError(
            f"{self.project}:{self.plan}#{section}: the plan declares no such section"
        )

    def done_when(self, section: str) -> str:
        from reckon.crew.plan_review import _done_when_paragraph

        paragraph = _done_when_paragraph(self.section_prose(section))
        if not paragraph:
            raise BatchEntryRefusedError(
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
        sentence = prose[match.end() :]
        stop = _SENTENCE_END.search(sentence)
        if stop is not None:
            sentence = sentence[: stop.start()]
        found: list[str] = []
        for token in _PATH_TOKEN.findall(sentence):
            if ".." in token.split("/") and (
                "/" in token or Path(token).suffix.lower() in _SOURCE_EXTENSIONS
            ):
                raise BatchEntryRefusedError(
                    f"{self.project}:{self.plan}#{section}: write path {token} "
                    f"carries a parent-directory segment, so it is refused rather "
                    f"than derived in a shortened form, from the sentence: "
                    f"{sentence.strip()}"
                )
            # A leading ``./`` is a relative prefix and goes; a leading ``/`` is
            # the path's own root and stays.
            trimmed = token
            while trimmed.startswith("./"):
                trimmed = trimmed[2:]
            candidate = trimmed.lstrip(".-").rstrip("./-")
            if not candidate:
                continue
            if "/" not in candidate and (
                Path(candidate).suffix.lower() not in _SOURCE_EXTENSIONS
            ):
                continue
            if candidate not in found:
                found.append(candidate)
        for candidate in found:
            if Path(candidate).is_absolute():
                continue
            resolved = self.repo_root / candidate
            if resolved.exists() or resolved.parent.is_dir():
                continue
            raise BatchEntryRefusedError(
                f"{self.project}:{self.plan}#{section}: derived write path "
                f"{candidate} is neither present under {self.repo_root} nor in a "
                f"present directory, from the sentence: {sentence.strip()}"
            )
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
    raise BatchEntryRefusedError(f"{value!r} is not a list of strings")


def section_derivation(read: _PlanRead, section: str) -> dict[str, tuple[Any, str]]:
    """Each node field this section supplies, with the digest of its value.

    A batch entry that omits a field takes the section's, and a released
    request re-derives those same fields, so both sides of that contract are
    answered here rather than twice. A field the section does not supply is
    absent, which is what keeps a supplied or edited field the request's own.
    The digest is the plan reader's own canonical-JSON digest, so a value
    recorded at queue time and the same value re-derived at release compare
    equal.
    """
    from reckon.crew.plan_review import _digest

    values: dict[str, Any] = {}
    with contextlib.suppress(BatchEntryRefusedError):
        values["done_when"] = read.done_when(section)
    paths = read.write_paths(section)
    if paths:
        values["write_paths"] = paths
    estimate = read.section_record(section).get("effort_hours")
    if isinstance(estimate, (int, float)):
        values["estimated_hours"] = float(estimate)
    return {name: (value, _digest(value)) for name, value in values.items()}


def rederived_section_fields(
    *,
    project: str,
    plan: str,
    section: str,
    repo_root: str | Path,
    marked: Mapping[str, str],
    stored: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Re-derive a stored request's section-supplied fields from the plan now.

    ``marked`` is the field-to-digest record the queue writer left on the
    request, naming which of its node fields the section supplied, and
    ``stored`` is the node those fields sit on. Both digests are taken here, the
    old one by digesting the stored field rather than by trusting its mark: a
    mark written under an earlier spelling of the same value is not a change,
    and comparing text the two sides both mean is what says so. A field the
    section no longer states is absent from the values, which is what keeps the
    request's stored value.

    A plan or section that cannot be read raises ``BatchEntryRefusedError``.
    The caller must not read that as "nothing was derived": releasing on the
    stored fields would put the request back on the text the plan has left
    behind, which is exactly what the release re-derivation exists to stop.
    """
    from reckon.crew.plan_review import _digest

    read = _PlanRead(project, plan, Path(repo_root))
    derivation = section_derivation(read, section)
    values: dict[str, Any] = {}
    digests: dict[str, dict[str, str]] = {}
    for name, stored_digest in marked.items():
        fresh = derivation.get(name)
        if fresh is None:
            continue
        value, digest = fresh
        values[name] = value
        previous = _digest(stored[name]) if name in stored else str(stored_digest)
        digests[name] = {"old": previous, "new": digest}
    return values, digests


def _entry_node(
    index: int,
    entry: Mapping[str, Any],
    *,
    plans: _Plans,
) -> tuple[str, TaskNode, dict[str, str]]:
    """Build one entry's node, filling every omitted field from its section.

    The third value names every field whose value the section supplied, with
    the digest of the value taken. The queue records it on the stored request:
    a field the dispatcher supplied or an editor set carries no mark, so a
    later release can re-derive the section's fields and keep the rest.
    """
    unknown = sorted(set(entry) - BATCH_ENTRY_FIELDS)
    if unknown:
        raise BatchEntryRefusedError(
            f"entry {index + 1} carries unknown field(s): {', '.join(unknown)}"
        )
    # The waiver is read as a boolean, so a batch file that spells it "false"
    # cannot waive the review by being a non-empty string.
    if not isinstance(entry.get("allow_unreviewed_plan", False), bool):
        raise BatchEntryRefusedError(
            f"entry {index + 1} allow_unreviewed_plan must be true or false"
        )
    project = str(entry.get("project") or "").strip()
    plan = str(entry.get("plan") or "").strip()
    section = str(entry.get("section") or "").strip()
    node_id = str(entry.get("node") or "").strip()
    goal = str(entry.get("goal") or "").strip()
    if not project or not plan or not section:
        raise BatchEntryRefusedError(
            f"entry {index + 1} must name its project, plan and section"
        )
    if not node_id:
        raise BatchEntryRefusedError(f"entry {index + 1} names no node id")
    if not goal:
        raise BatchEntryRefusedError(
            f"{project}:{plan}#{section}: the entry gives no goal"
        )

    read = plans.read(project, plan)
    derivation = section_derivation(read, section)
    derived: dict[str, str] = {}

    done_when = str(entry.get("done_when") or "").strip()
    if not done_when:
        if "done_when" not in derivation:
            raise BatchEntryRefusedError(
                f"{project}:{plan}#{section}: the section states no "
                "done-when, so the entry must give one"
            )
        done_when, derived["done_when"] = derivation["done_when"]
    write_paths = _as_list(entry.get("write_paths"))
    if not write_paths:
        if "write_paths" not in derivation:
            raise BatchEntryRefusedError(
                f"{project}:{plan}#{section}: the section states no write-paths "
                "sentence and the entry gives none"
            )
        write_paths, derived["write_paths"] = derivation["write_paths"]

    estimated_hours = entry.get("estimated_hours")
    if estimated_hours is None and "estimated_hours" in derivation:
        estimated_hours, derived["estimated_hours"] = derivation["estimated_hours"]
    spec_level = str(entry.get("spec_level") or "")

    node = TaskNode(
        id=node_id,
        goal=goal,
        plan=plan,
        section=section,
        after=_as_list(entry.get("after")),
        waits_only_after=bool(entry.get("waits_only_after")),
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
    return project, node, derived


def _peer_scopes(values: Any) -> dict[str, list[str]]:
    scopes: dict[str, list[str]] = {}
    for item in _as_list(values):
        name, _, paths = str(item).partition("=")
        scopes[str(name).strip()] = [
            path.strip() for path in paths.split(",") if path.strip()
        ]
    return scopes


def _stamp_section_derived(run_id: str, derived: Mapping[str, str]) -> None:
    """Record on a stored request which fields its section supplied.

    The mark lives on the stored node beside the values it describes, so an
    editor that changes one of those fields clears it in the same write, and a
    release reads it without re-reading the plan the request was queued from.
    """
    from reckon.crew import runs as runs_module

    def stamp(current: dict[str, Any]) -> dict[str, Any]:
        node_block = dict(current.get("node") or {})
        node_block["section_derived"] = dict(derived)
        current["node"] = node_block
        return current

    runs_module._mutate_pointer(run_id, stamp)


def _require_stored(stored: Mapping[str, Any], project: str, node: TaskNode) -> None:
    """Refuse an entry whose request the queue did not store."""
    if not stored:
        raise BatchEntryRefusedError(
            f"{project}:{node.plan}#{node.section}: the request was not stored"
        )


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
    """Queue each entry on its own — see :func:`_queue_batch`.

    Inside one batch each named plan is parsed once, each named project's
    roadmap is derived once, and each plan's state, text and section headings
    are read once, so the work of queuing does not grow with the number of
    sections chosen.
    """
    from reckon.crew.queue_admission import plan_read_scopes

    with _parsed_once(), _built_once(), plan_read_scopes():
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
    from reckon.crew.dispatch_admission import node_context_fit_finding
    from reckon.crew.runs import new_run_id, read_pointer, run_dir

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
            project, node, derived = _entry_node(index, entry, plans=plans)
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
            # A queued request names the manifest path its worker delivers to,
            # exactly as a single dispatch stores it, so a coordinator can edit
            # the request while it waits: the queue edit judges every request on
            # that contract, and holds its own run directory as the durable
            # place for it.
            node.manifest_path = node.manifest_path or str(
                run_dir(run_id) / "manifest.md"
            )
            # A request larger than its lane fits in the queue and is refused
            # at the launch that first tries it, where the refusal costs the
            # queue position and reaches nobody. It is measured here instead,
            # before `queue_one` writes a pointer, so the row that comes back
            # names the estimate and the window the entry could not meet.
            work_repository = plans.read(project, node.plan).repo_root
            fit = node_context_fit_finding(
                node,
                project=project,
                repository=work_repository,
                config=config,
                backend=backend,
            )
            if fit is not None:
                refuse(index, entry, str(fit["detail"]))
                continue
            # The batch stays inside the per-section readiness read: queuing
            # each entry through the project-wide view would build every plan
            # in the project once per entry, which is what the batch exists to
            # avoid. The review contract is stamped per stored request exactly
            # as the single-dispatch queue path stamps it.
            stored = queue_one(
                [node],
                project=project,
                session=str(entry.get("session") or session),
                repo_root=work_repository,
                config=config,
                backend_name=backend,
                launch_kind=launch_kind,
                run_ids={node.id: run_id},
                allow_unreviewed_plan=bool(entry.get("allow_unreviewed_plan")),
            )
            for stored_id in stored:
                pointer = read_pointer(stored_id)
                if isinstance(pointer, dict):
                    queue_admission.record_review_contract(pointer)
                if derived and stored_id == run_id:
                    # A request already queued keeps the node it was stored
                    # with, so its marks stay the ones that node was built under.
                    _stamp_section_derived(stored_id, derived)
            _require_stored(stored, project, node)
            actual_id, row = next(iter(stored.items()))
            phase = str(row.get("phase") or "")
            stored_by_node[node.id] = actual_id
            already = actual_id != run_id
            status = (
                "already-queued"
                if already
                else (
                    "awaiting-upstream" if phase in WAITING_UPSTREAM_PHASES else phase
                )
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
        except (BatchEntryRefusedError, CrewError) as exc:
            refuse(index, entry, str(exc))

    results.sort(key=lambda row: int(row["index"]))
    return {
        "ok": refused == 0,
        "queued": len(results) - refused,
        "refused": refused,
        "results": results,
    }
