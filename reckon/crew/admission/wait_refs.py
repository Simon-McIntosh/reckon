"""Resolve wait identities and validate batch dependency declarations."""

from __future__ import annotations

from . import readiness as _readiness
from .shared import (
    _MOUNT_UNSET,
    GATE_REF_PREFIX,
    READINESS_REF_PREFIX,
    REQUEST_REF_PREFIX,
    WAIT_UNRESOLVABLE_PREFIX,
    Any,
    CrewError,
    Iterable,
    Mapping,
    MountView,
    Path,
    Sequence,
    TaskNode,
    json,
    normalize_section,
)


def section_wait_refs(plan_html: str, section: str) -> list[str]:
    """The refs one section declares it waits on, read from the plan itself.

    The mapping is the plan's own ``plan-section-depends-on`` declaration, so a
    request queued for a waited section carries the refs the plan already
    states rather than a second copy a coordinator would have to keep in step.
    A plan that declares no mapping, and a section the mapping does not name,
    both yield no refs, and the request is queueable at once.
    """
    from reckon._schema import section_depends_on

    requested_section = normalize_section(section)
    refs = next(
        (
            value
            for key, value in (section_depends_on(plan_html) or {}).items()
            if normalize_section(key) == requested_section
        ),
        None,
    )
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    return [str(ref).strip() for ref in refs if str(ref).strip()]


def section_waits_for(pointer: Mapping[str, Any]) -> list[str]:
    """The refs a queued request waits on, as its own record states them."""
    refs = pointer.get("waits")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    return [str(ref).strip() for ref in refs if str(ref).strip()]


def _run_record_held(project: str, run_id: str) -> bool:
    """Whether one project's ledger holds the row for a run."""
    if not project:
        return False
    try:
        from reckon import ledger

        return ledger.run_path(project, run_id).is_file()
    except Exception:  # noqa: BLE001 - an unreadable ledger holds nothing
        return False


def _mount_view() -> MountView:
    """Read the mount registry once: each project's tree root, or the failure.

    The roots are the same ones a promotion resolves a run's record through, and
    they are resolved by that owner rather than re-derived here: a caller hands
    this mapping to :func:`reckon.crew.runs._promoted_record_project` rather than letting it read
    the registry again per run, and the two readers of one registry cannot then
    disagree about what an unreadable one means.
    """
    from reckon.crew import runs as runs_module

    try:
        roots, failure = runs_module._mounted_project_roots_and_failure(None)
    except Exception as exc:  # noqa: BLE001 - the failure is the value reported
        return None, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if failure:
        return None, failure
    return roots, ""


def wait_project_lookup(
    run_id: str,
    *,
    owner_project: str,
    live: Sequence[Mapping[str, Any]] = (),
    mount: object = _MOUNT_UNSET,
) -> tuple[str, str]:
    """The project a ``run:`` wait lands in, and why it could not be found.

    Returns ``(project, "")`` when a project holds the run, ``("", reason)``
    when the mount registry could not be read so the question is unanswered,
    and ``("", "")`` when every source was read and none holds the run. A
    caller resolving several refs passes one :data:`MountView` for all of them,
    so the registry is parsed once and an unreadable one is reported, not
    re-read.
    """
    if not run_id:
        return "", ""
    for pointer in live:
        if str(pointer.get("run_id") or "") == run_id:
            return str(pointer.get("project") or owner_project), ""
    if _run_record_held(owner_project, run_id):
        return owner_project, ""
    roots, failure = _mount_view() if mount is _MOUNT_UNSET else mount
    if roots is None:
        return "", failure
    from reckon.crew import runs as runs_module

    # The owner of the mounted-ledger scan: the same search a promotion uses to
    # name the project holding a run's record, given the registry read above
    # rather than reading it again.
    name = runs_module._promoted_record_project(run_id, None, roots=roots)
    if name:
        return str(name), ""
    return "", ""


def wait_project_for_run(
    run_id: str,
    *,
    owner_project: str,
    live: Sequence[Mapping[str, Any]] = (),
    mount: object = _MOUNT_UNSET,
) -> str:
    """The project a ``run:`` wait lands in, or "" when no project holds it.

    One derivation for every writer of a request's waits and for the reader
    that resolves them, so the record and the reading cannot disagree. The
    order is the one a dispatch resolves a run ref in: the live pointer's own
    project first, then the owner project the dispatch falls back to, then any
    other mounted project whose ledger holds the run. A ref none of those holds
    is unresolvable, and says so rather than reading as a wait naming no
    project.

    ``mount`` is the mount registry already read by the caller, so a derivation
    over many refs parses it once rather than once per ref.
    """
    return wait_project_lookup(
        run_id, owner_project=owner_project, live=live, mount=mount
    )[0]


def wait_projects_for(
    refs: Iterable[str],
    *,
    owner_project: str,
    live: Sequence[Mapping[str, Any]] = (),
    mount: object = _MOUNT_UNSET,
) -> dict[str, str]:
    """The project of every resolvable ``run:`` ref in ``refs``.

    Written beside a request's waits in the same pointer mutation, so every
    ``run:`` ref a request waits on carries the project its landing is read
    from. A ref no project holds is left out: nothing can land for it, and the
    reads of the pointer report that rather than inventing a project.

    The mount registry is read once for the whole list, and an unreadable one
    resolves every ref to nothing from that single failure rather than being
    re-read for each.
    """
    projects: dict[str, str] = {}
    refs = [str(ref) for ref in refs]
    if mount is _MOUNT_UNSET and any(
        ref.startswith(REQUEST_REF_PREFIX) for ref in refs
    ):
        mount = _mount_view()
    for ref in refs:
        if not ref.startswith(REQUEST_REF_PREFIX):
            continue
        run_id = ref[len(REQUEST_REF_PREFIX) :].strip()
        if not run_id:
            continue
        project = wait_project_for_run(
            run_id, owner_project=owner_project, live=live, mount=mount
        )
        if project:
            projects[run_id] = project
    return projects


def unresolvable_wait_obligations(pointer: Mapping[str, Any]) -> list[str]:
    """The obligations a pointer carries for waits no project can resolve."""
    return [
        str(detail)
        for detail in pointer.get("upstream_obligations") or ()
        if str(detail).startswith(WAIT_UNRESOLVABLE_PREFIX)
    ]


def _docs_for_project(
    project: str, *, current_project: str, current_docs: Path
) -> Path | None:
    if project == current_project:
        return current_docs
    try:
        from reckon._store import _mounts_path

        mounts = json.loads(_mounts_path().read_text(encoding="utf-8"))
        mounted = mounts.get(project)
        return Path(mounted).expanduser().resolve() if mounted else None
    except (OSError, ValueError, TypeError):
        return None


def _section_status(row: Mapping[str, Any], section: str) -> str:
    declarations = row.get("section_declarations") or {}
    return next(
        (
            str(status).lower()
            for key, status in declarations.items()
            if normalize_section(key) == normalize_section(section)
        ),
        "",
    )


def derive_wait_refs(node: TaskNode, *, repo_root: str | Path) -> list[str]:
    """Read section, plan and explicit request waits from the authored node.

    A node that waits only on its own ``--after`` refs derives those alone: the
    section's declared waits, the plan's ``depends_on`` and the section's gates
    are the section's own deliverable and are not inherited by this request.
    """
    explicit = [f"{REQUEST_REF_PREFIX}{run_id}" for run_id in node.after]
    if node.waits_only_after:
        return list(dict.fromkeys(explicit))
    refs = section_wait_refs(
        _readiness.plan_document_text(repo_root, node.plan), node.section
    )
    if node.plan:
        state = _readiness._plan_state_and_text(
            Path(str(repo_root)) / "docs", node.plan
        )[0]
        refs.extend(str(ref) for ref in state.get("depends_on") or ())
    refs.extend(explicit)
    return list(dict.fromkeys(refs))


def _own_readiness_ref(
    node: TaskNode,
    *,
    project: str,
    docs_dir: str | Path,
    cache: dict[str, Any],
    refs: Sequence[str],
    read_own_verdict: bool = True,
) -> str:
    """The readiness ref a node's own section carries, or ``""`` for none.

    A gate or an open decision on the section holds new work on it, and the
    roadmap reports that hold as a blocker on the section. The ref is derived
    here rather than by each caller, because a reader and a writer that derive
    it differently disagree the moment one of them asks again: the dispatch and
    the pass that re-reads the request both compose their waits through this,
    so a held request stays held rather than being released by the next pass.
    """
    from reckon._schema import parse_plan_ref

    if not read_own_verdict or node.waits_only_after:
        return ""
    if not _readiness._needs_own_section_verdict(node):
        return ""
    rows, _ = _readiness._readiness_rows(project, docs_dir, cache, node.plan, refs)
    verdicts = (rows.get(node.plan) or {}).get("sections", {})
    own = next(
        (
            verdict
            for section, verdict in verdicts.items()
            if normalize_section(section) == normalize_section(node.section)
        ),
        None,
    )
    holding_kinds = {
        blocker.get("kind") for blocker in (own or {}).get("blockers") or ()
    }
    explicit_gates = {
        parsed.stage
        for ref in refs
        if ref.startswith(GATE_REF_PREFIX)
        if (parsed := parse_plan_ref(ref[len(GATE_REF_PREFIX) :])) is not None
        and parsed.stage
    }
    held_gates = {
        str(blocker.get("id") or blocker.get("gate") or "")
        for blocker in (own or {}).get("blockers") or ()
        if blocker.get("kind") == "gate"
    }
    if not holding_kinds & {"gate", "decision"}:
        return ""
    if held_gates and held_gates <= explicit_gates and "decision" not in holding_kinds:
        return ""
    identity = normalize_section(node.section)
    if identity.startswith("§"):
        identity = f"s{identity[1:]}"
    return f"{READINESS_REF_PREFIX}{project}:{node.plan}#{identity}"


def _validated_batch_waits(
    nodes: Sequence[TaskNode],
    run_ids: Mapping[str, str],
    *,
    project: str,
    repo_root: str | Path,
    live: Sequence[Mapping[str, Any]],
    read_own_verdict: bool = True,
    readiness_cache: dict[str, Any] | None = None,
) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    """Resolve all waits and reject absent targets or a combined graph cycle."""
    from reckon import _plan_html as plan_html_reader
    from reckon import ledger
    from reckon._schema import parse_plan_ref

    current_docs = Path(repo_root) / "docs"
    # A pass hands every launch the same readiness cache, so the roadmap one
    # project is derived from is built once for the whole pass rather than once
    # per launch. A direct dispatch supplies none and builds its own.
    cache = readiness_cache if readiness_cache is not None else {}
    request_nodes = {str(run_ids[node.id]): node for node in nodes}
    live_requests = {
        str(pointer.get("run_id") or ""): pointer
        for pointer in live
        if pointer.get("run_id")
    }
    refs_by_run: dict[str, list[str]] = {}
    projects_by_run: dict[str, dict[str, str]] = {}
    edges: dict[str, set[str]] = {}

    def section_key(target_project: str, slug: str, section: str) -> str:
        identity = normalize_section(section)
        if identity.startswith("§"):
            identity = f"s{identity[1:]}"
        return f"{target_project}:{slug}#{identity}"

    def plan_key(target_project: str, slug: str) -> str:
        return f"{target_project}:{slug}"

    def target(ref: str, owner_project: str) -> tuple[str, str]:
        if ref.startswith(REQUEST_REF_PREFIX):
            run_id = ref[len(REQUEST_REF_PREFIX) :]
            if run_id not in request_nodes and run_id not in live_requests:
                known = any(
                    ledger.run_path(candidate, run_id).is_file()
                    for candidate in {owner_project, project}
                    if candidate
                )
                if not known:
                    raise CrewError(f"wait names missing request {run_id}")
            upstream_project = str(
                (live_requests.get(run_id) or {}).get("project") or owner_project
            )
            return f"run:{run_id}", upstream_project
        if ref.startswith((GATE_REF_PREFIX, "readiness:")):
            return ref, owner_project
        parsed = parse_plan_ref(ref)
        if parsed is None:
            raise CrewError(f"wait names malformed plan ref {ref!r}")
        target_project = parsed.project or owner_project
        docs = _docs_for_project(
            target_project, current_project=project, current_docs=current_docs
        )
        if docs is None:
            raise CrewError(f"wait names missing project {target_project!r}: {ref}")
        html = _readiness.plan_document_text(docs.parent, parsed.slug)
        if not html:
            raise CrewError(f"wait names missing plan {target_project}:{parsed.slug}")
        row = _readiness._plan_state_and_text(docs, parsed.slug, target_project)[0]
        if parsed.stage:
            authored = {
                normalize_section(identity)
                for identity, _heading in plan_html_reader.authored_section_headings(
                    html
                )
            }
            if (
                not _section_status(row, parsed.stage)
                and normalize_section(parsed.stage) not in authored
            ):
                raise CrewError(f"wait names missing section {ref}")
            return section_key(
                target_project, parsed.slug, parsed.stage
            ), target_project
        return plan_key(target_project, parsed.slug), target_project

    def section_edges(key: str) -> None:
        if key in edges:
            return
        edges[key] = set()
        parsed = parse_plan_ref(key)
        if parsed is None or not parsed.stage:
            return
        docs = _docs_for_project(
            parsed.project or project,
            current_project=project,
            current_docs=current_docs,
        )
        if docs is None:
            return
        html = _readiness.plan_document_text(docs.parent, parsed.slug)
        for ref in section_wait_refs(html, parsed.stage):
            child, _ = target(ref, parsed.project or project)
            edges[key].add(child)
            if not child.startswith("run:"):
                section_edges(child)
        state = _readiness._plan_state_and_text(
            docs, parsed.slug, parsed.project or project
        )[0]
        for ref in state.get("depends_on") or ():
            child, _ = target(str(ref), parsed.project or project)
            edges[key].add(child)

    for run_id, node in request_nodes.items():
        refs = derive_wait_refs(node, repo_root=repo_root)
        wait_projects: dict[str, str] = {}
        edge_set: set[str] = set()
        for ref in refs:
            child, upstream_project = target(ref, project)
            edge_set.add(child)
            if ref.startswith(REQUEST_REF_PREFIX):
                wait_projects[ref[len(REQUEST_REF_PREFIX) :]] = upstream_project
            elif not ref.startswith((GATE_REF_PREFIX, "readiness:")):
                section_edges(child)
        # A node waiting only on what it names reads no verdict on its own
        # section, so a gate there appends no readiness ref. The flag would
        # otherwise leave exactly the gate waits in place that it exists to
        # drop.
        readiness = _own_readiness_ref(
            node,
            project=project,
            docs_dir=current_docs,
            cache=cache,
            refs=refs,
            read_own_verdict=read_own_verdict,
        )
        if readiness:
            refs.append(readiness)
        refs_by_run[run_id] = list(dict.fromkeys(refs))
        projects_by_run[run_id] = wait_projects
        edges[f"run:{run_id}"] = edge_set

    for run_id, pointer in live_requests.items():
        if run_id in request_nodes:
            continue
        if str(pointer.get("phase") or "") in {
            "failed",
            "complete",
            "blocked-upstream",
            "discarded",
        }:
            continue
        owner = str(pointer.get("project") or project)
        edges[f"run:{run_id}"] = set()
        for ref in section_waits_for(pointer):
            try:
                child, _ = target(ref, owner)
            except CrewError:
                # Existing requests only supply cycle edges. An unresolved
                # edge cannot complete a cycle through this dispatch's batch.
                edges[f"run:{run_id}"].add(ref)
                continue
            edges[f"run:{run_id}"].add(child)
            if not child.startswith("run:"):
                section_edges(child)

    for run_id, node in request_nodes.items():
        if node.plan and node.section:
            key = section_key(project, node.plan, node.section)
            section_edges(key)
            edges[key].add(f"run:{run_id}")
    for run_id, pointer in live_requests.items():
        node = dict(pointer.get("node") or {})
        if node.get("plan") and node.get("section") and f"run:{run_id}" in edges:
            key = section_key(
                str(pointer.get("project") or project),
                str(node["plan"]),
                str(node["section"]),
            )
            section_edges(key)
            edges[key].add(f"run:{run_id}")

    visited: set[str] = set()
    active: list[str] = []

    def visit(key: str) -> list[str] | None:
        if key in active:
            return [*active[active.index(key) :], key]
        if key in visited:
            return None
        active.append(key)
        for child in sorted(edges.get(key, ())):
            cycle = visit(child)
            if cycle:
                return cycle
        active.pop()
        visited.add(key)
        return None

    for run_id in request_nodes:
        cycle = visit(f"run:{run_id}")
        if cycle:
            raise CrewError("wait cycle: " + " -> ".join(cycle))
    return refs_by_run, projects_by_run
