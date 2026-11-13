"""Read section readiness and classify declared dependency states."""

from __future__ import annotations

from . import holds as _holds
from . import release as _release
from . import service as _service
from . import wait_refs as _wait_refs
from .shared import (
    _BOUNDED_READ,
    _MOUNT_UNSET,
    _PASS_PLAN_STATES,
    _READINESS_PASS_ACTIVE,
    _SECTION_RUNS_CACHE_KEY,
    _UNLOADED_CACHE_KEY,
    ABANDONED,
    BLOCKED_UPSTREAM_PHASE,
    DEAD_RUN_PHASES,
    GATE_REF_PREFIX,
    PENDING,
    READINESS_REF_PREFIX,
    REQUEST_REF_PREFIX,
    REVIEW_ROLE,
    RUN_DELIVERED_STATUSES,
    RUN_FAILED_STATUSES,
    SATISFIED,
    SECTION_HOLD_KINDS,
    WAIT_UNRESOLVABLE_PREFIX,
    Any,
    Iterable,
    Iterator,
    Mapping,
    Path,
    Sequence,
    TaskNode,
    contextlib,
    contextmanager,
    datetime,
    json,
    normalize_section,
    runs,
    subprocess,
)


def _plan_tree_fingerprint(docs_dir: str | Path | None) -> tuple[int, int, str]:
    """What a readiness read saw of the plan tree it derives from.

    The newest modification time and the count of the checkout's plan
    documents, and the checkout's ``HEAD``: a section verdict is built from
    every one of those plans and the metadata index beside them, so the answer
    is only as current as the tree it was read from. A plan edited, or a
    landing merged, during an admission pass moves one of these, and the
    cached rows are then missed rather than read as current.
    """
    if not docs_dir:
        return (0, 0, "")
    newest = 0
    count = 0
    try:
        plans = list((Path(str(docs_dir)) / "plans").glob("*.html"))
    except OSError:
        plans = []
    for path in plans:
        count += 1
        with contextlib.suppress(OSError):
            newest = max(newest, path.stat().st_mtime_ns)
    head = ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(str(docs_dir)).parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        head = completed.stdout.strip()
    return (newest, count, head)


def _readiness_rows(
    project: str,
    docs_dir: str | Path | None,
    cache: dict[str, Any],
    plan: str = "",
    refs: Iterable[str] = (),
    *,
    inventory_only: bool = False,
) -> tuple[Mapping[str, Mapping[str, Any]], Mapping[str, Mapping[str, Any]]]:
    """Read only the plans a request and its declared waits name.

    A pass reuses each bounded result while its contributing files keep their
    stat identities. An edit between requests or passes invalidates that result
    without walking every plan in the project.
    """
    refs = tuple(refs)
    bounded_read = _BOUNDED_READ.get() or _READINESS_PASS_ACTIVE.get() or inventory_only
    key = (
        project,
        str(docs_dir or ""),
        plan if bounded_read else "",
        tuple(sorted(map(str, refs))) if bounded_read else (),
        inventory_only,
    )
    cached = cache.get(key)
    if cached is not None and docs_dir is not None:
        rows, inventory, identities = cached
        current = (
            _readiness_file_identities(Path(docs_dir), inventory)
            if bounded_read
            else _plan_tree_fingerprint(docs_dir)
        )
        if identities == current:
            return rows, inventory
    if inventory_only and plan and docs_dir is not None:
        from reckon.crew.plan_review import _readiness_inventory

        try:
            inventory = {
                str(item["slug"]): item
                for item in _readiness_inventory(
                    project, Path(docs_dir), plan, refs=refs
                )
            }
        except Exception:  # noqa: BLE001 - unreadable plan gives no ready wait
            return {}, {}
        cache[key] = (
            {},
            inventory,
            _readiness_file_identities(Path(docs_dir), inventory),
        )
        return {}, inventory
    if plan and docs_dir is not None and bounded_read:
        try:
            from reckon.crew.plan_review import section_readiness_rows

            bounded, bounded_inventory = section_readiness_rows(
                project, Path(docs_dir), plan, refs=refs
            )
        except Exception:  # noqa: BLE001 - the project read below carries it
            bounded, bounded_inventory = {}, {}
        if plan in bounded or plan in bounded_inventory or _READINESS_PASS_ACTIVE.get():
            cache[key] = (
                bounded,
                bounded_inventory,
                _readiness_file_identities(Path(docs_dir), bounded_inventory),
            )
            return bounded, bounded_inventory
    if _READINESS_PASS_ACTIVE.get():
        return {}, {}
    rows: dict[str, Mapping[str, Any] | Any] = {}
    inventory: dict[str, Mapping[str, Any] | Any] = {}
    try:
        from reckon.crew.plan_review import project_section_readiness
        from reckon.mcp_discovery import _discover_project, _inventory_row

        if not project:
            return {}, {}
        discovered = _discover_project(project, None)
        items = [_inventory_row(item) for item in discovered.get("inventory", [])]
        for item in items:
            if item.get("type") != "plan" or not item.get("slug"):
                continue
            item["section_declarations"] = _plan_state_and_text(
                docs_dir, str(item["slug"]), project
            )[0].get("section_declarations", {})
        inventory = {str(item.get("slug") or ""): item for item in items}
        rows = project_section_readiness(project, Path(docs_dir))
    except Exception:  # noqa: BLE001 - an unbuildable roadmap holds every wait
        rows, inventory = {}, {}
    if not bounded_read and docs_dir is not None:
        cache[key] = (rows, inventory, _plan_tree_fingerprint(docs_dir))
    return rows, inventory


def _readiness_file_identities(
    docs_dir: Path, inventory: Mapping[str, Any]
) -> tuple[tuple[str, tuple[int, int, int, int, int] | None], ...]:
    """Stat identities of precisely the plan files that supplied a verdict."""
    from reckon import file_memo

    identities = []
    for slug in sorted(inventory):
        try:
            stat = (docs_dir / "plans" / f"{slug}.html").stat()
            identity = file_memo.signature_from_stat(stat)
        except OSError:
            identity = None
        identities.append((slug, identity))
    return tuple(identities)


def _persisted_plan_states(project: str, docs_dir: Path) -> Mapping[str, Any]:
    """Each plan's persisted parse, keyed by its docs-relative path.

    An admission pass reads every plan in the project to take its section
    declarations, and each read seeds the shared parse from the state the file
    already recorded in the metadata index. A fresh process then answers that
    read from the store rather than parsing the soup again, and a file whose
    bytes moved is the only one parsed. A store that cannot answer seeds
    nothing, so the read pays a parse rather than a whole-tree build.
    """

    from reckon import metadata_index

    held = _PASS_PLAN_STATES.get()
    key = (str(docs_dir), project)
    if held is not None and key in held:
        return held[key]
    try:
        states = metadata_index.discovery_state(docs_dir, project, build=False) or {}
    except Exception:  # noqa: BLE001 - an unreadable store parses as before
        states = {}
    if held is not None:
        held[key] = states
    return states


@contextmanager
def plan_read_scopes() -> Iterator[None]:
    """Observe the docs tree once for the length of one read-only pass.

    An admission pass, a landing pass and a dispatch each resolve plan and
    resource identities for many requests, and each lookup outside a scope
    revalidates the covered tree and rescans the resources. Entering both scopes
    once per pass makes the pass pay for one observation, as an MCP request
    does through the same boundary. A nested entry joins the region already
    open, and the outermost exit closes it, so a tree change after the pass is
    seen by the next reader.

    The scope must not span a write to the tree it observed, so a caller that
    writes closes the scopes first.
    """

    from reckon.metadata_index import discovery_file_scope
    from reckon.resources import resource_scan_scope

    tokens = _PASS_PLAN_STATES.set({})
    try:
        with discovery_file_scope(), resource_scan_scope():
            yield
    finally:
        _PASS_PLAN_STATES.reset(tokens)


def _plan_state_and_text(
    docs_dir: str | Path, slug: str, project: str = ""
) -> tuple[dict, str]:
    """One plan's semantic state and text, parsed once per file per process.

    The read goes through the memoised pair rather than re-parsing the file's
    text on every ref a pass resolves, and seeds the shared parse with the state
    the metadata index already recorded for the file, so a fresh process answers
    without building the soup. A store that cannot answer seeds nothing.
    """

    from reckon import _plan_html as plan_html_reader

    html_file = Path(docs_dir) / "plans" / f"{slug}.html"
    stored = _persisted_plan_states(project, Path(docs_dir)).get(f"plans/{slug}.html")
    if isinstance(stored, Mapping) and stored.get("state") is not None:
        plan_html_reader.reuse_parsed_state(html_file, stored["state"])
    return plan_html_reader.read_state_and_text_file(html_file)


@contextmanager
def readiness_pass() -> Iterator[None]:
    """Hold one admission pass, whose reads are shared across its requests."""
    token = _READINESS_PASS_ACTIVE.set(True)
    try:
        yield
    finally:
        _READINESS_PASS_ACTIVE.reset(token)


@contextmanager
def project_read() -> Iterator[None]:
    """Judge every request in the batch against one project-wide read."""
    token = _BOUNDED_READ.set(False)
    try:
        yield
    finally:
        _BOUNDED_READ.reset(token)


def _project_of_docs_dir(docs_dir: str | Path | None) -> str:
    """The project a checkout's docs directory belongs to, from mounts.json."""
    if not docs_dir:
        return ""
    try:
        from reckon._store import _mounts_path

        registered = json.loads(_mounts_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unregistered docs dir names no project
        return ""
    target = Path(str(docs_dir)).expanduser().resolve()
    for project, directory in (registered or {}).items():
        with contextlib.suppress(OSError, RuntimeError):
            if Path(str(directory)).expanduser().resolve() == target:
                return str(project)
    return ""


def _section_ref(ref: str) -> tuple[str, str] | None:
    """The ``(slug, section)`` a section ref names, or ``None``.

    The spelling is the repository's own. :func:`reckon._schema.parse_plan_ref`
    already answers ``[project:]slug[#stage]`` for the roadmap, the document
    check and the ledger, and it is written the way it is because two spellings
    of one ref have to compare equal; a hand-rolled split answers differently
    for the forms it does not anticipate, and then two readers of the same plan
    disagree about which section a wait names.

    A section ref carries a stage and nothing else — ``plan#s1`` — so a ref
    without one names no section of any plan and is not a wait.
    """
    from reckon._schema import parse_plan_ref

    parsed = parse_plan_ref(ref)
    if parsed is None or not parsed.stage:
        return None
    return parsed.slug, normalize_section(parsed.stage)


def wait_ref_kind(ref: str) -> str:
    """Which of the three kinds of wait one ref names.

    A section waits on a section, a gate, or another queued request, and the
    three are spelled apart rather than guessed from a ref's shape: a gate id
    and a section id are both bare words, so a reader guessing between them
    reads a wait as its own opposite.
    """
    text = str(ref).strip()
    if text.startswith(GATE_REF_PREFIX):
        return "gate"
    if text.startswith(REQUEST_REF_PREFIX):
        return "request"
    return "section"


def _gate_state(
    ref: str,
    *,
    rows: Mapping[str, Mapping[str, Any]],
    inventory: Mapping[str, Mapping[str, Any]],
) -> str:
    """Resolve a wait on a gate through the roadmap's own gate edges.

    A gate that holds execution reaches the roadmap as a blocker row carrying
    the gate's id in the section readiness split, so its verdict is read there
    rather than re-derived from the plan's gate records. The roadmap is the
    reader that already knows which gate holds which section; a second reading
    is how a request comes to be held on a gate the roadmap would call passed.

    A gate the roadmap holds no blocker for has passed or gates nothing this
    request waits on, so the wait is satisfied.
    """
    from reckon._schema import parse_plan_ref
    from reckon.lifecycle import COMPLETED_STATUSES, TERMINAL_STATUSES

    parsed = parse_plan_ref(ref[len(GATE_REF_PREFIX) :].strip())
    if parsed is None or not parsed.stage:
        return PENDING
    slug, gate_id = parsed.slug, parsed.stage
    row = rows.get(slug)
    if row is None:
        status = str(
            (inventory.get(slug) or {}).get("workflow_status")
            or (inventory.get(slug) or {}).get("status")
            or ""
        )
        if status in COMPLETED_STATUSES:
            return SATISFIED
        if status in TERMINAL_STATUSES:
            return ABANDONED
        return PENDING
    for held in (row.get("sections") or {}).values():
        for blocker in held.get("blockers") or ():
            if str(blocker.get("gate") or "") != gate_id:
                continue
            status = str(blocker.get("status") or "")
            if status in TERMINAL_STATUSES and status not in COMPLETED_STATUSES:
                return ABANDONED
            return PENDING
    return SATISFIED


def _manifest_status(run_id: str, *, issues: list[str] | None = None) -> str:
    """One run's manifest status as that format's own reader parses it.

    The reader is :func:`reckon.crew.reports.parse_manifest`, which reads a JSON
    body as a JSON document. A status read as a ``status:`` line alone answers
    empty for a JSON manifest -- a run that delivered reads as never having
    delivered, and whatever waits on it is never released. A body no reader can
    parse, or no manifest at all, is not evidence of delivery, so it answers
    empty.
    """
    try:
        manifest = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    from reckon.crew.reports import ManifestParseError, parse_manifest

    try:
        return str(parse_manifest(text, path=str(manifest)).get("status") or "")
    except ManifestParseError as exc:
        if issues is not None:
            issues.append(f"{run_id}: refused manifest status or shape: {exc}")
        return ""


def _ledger_run_rows(
    project: str, cache: dict[str, Any] | None
) -> list[Mapping[str, Any]] | None:
    """One project's committed run rows, or ``None`` when they cannot be read.

    The index is keyed on the project's write generation, which every ledger
    write path bumps, so a promotion recorded before this read is in the index
    this read gets back rather than only in the documents behind it. Reading
    through it is therefore both current and cheaper than walking the project's
    run documents on every pass.
    """
    from reckon import ledger

    key = (_SECTION_RUNS_CACHE_KEY, project)
    if cache is not None and key in cache:
        return cache[key]
    try:
        data, _version = ledger.load(project)
        rows: list[Mapping[str, Any]] | None = [
            row for row in data.get("runs") or () if isinstance(row, Mapping)
        ]
    except Exception:  # noqa: BLE001 - an unreadable ledger proves no landing
        rows = None
    if cache is not None:
        cache[key] = rows
    return rows


def _section_run_unlanded(
    project: str,
    slug: str,
    section: str,
    *,
    live: Sequence[Mapping[str, Any]],
    cache: dict[str, Any] | None = None,
) -> bool:
    """Whether a run of one section has delivered or been promoted and not landed.

    Marking a section done is committed just before its run is landed, so the
    declaration alone does not say the section's code is in the registered
    checkout's HEAD: between the two the section reads done while its revision
    is still off HEAD, and anything released onto that base would run without
    its upstream. A run therefore holds its section's waiters from delivery
    until its promoted revision is an ancestor of HEAD -- the landed test the
    queue already applies to a run ref, extended to cover a run that has
    delivered and is not yet promoted.

    A delivered run keeps its live pointer until promotion removes it, and a
    promoted run reaches the project's ledger, so both records are read. A
    discarded run will never land, so it holds nothing. A section with no run
    at all -- one completed by hand -- has nothing to hold and releases as the
    declaration says.
    """
    from reckon._plan_html import section_record_id
    from reckon.crew import runs as runs_module
    from reckon.crew.dispatch_sections import project_mount_repository
    from reckon.crew.promotion_scope import _revision_is_ancestor

    wanted = section_record_id(section)

    def identifies(record: Mapping[str, Any]) -> bool:
        node = record.get("node")
        if not isinstance(node, Mapping):
            node = {}
        plan = str(node.get("plan") or record.get("plan") or "")
        held = section_record_id(node.get("section") or record.get("section") or "")
        return plan == slug and held == wanted

    rows = _ledger_run_rows(project, cache)
    if rows is None:
        # A ledger that cannot be read says nothing about what was promoted, so
        # a landing is unknown rather than absent. Holding is the answer
        # :func:`_request_state` gives a record it cannot find.
        return True

    promoted: dict[str, str] = {}
    for row in rows:
        run_id = str(row.get("run_id") or "")
        revision = str(row.get("promoted_revision") or "").strip()
        if run_id and revision and not _release._discarded(run_id) and identifies(row):
            promoted[run_id] = revision

    # The runs of this section that may still land, each with the revision it
    # records. A delivered run carries none until a promotion writes it.
    candidates: list[tuple[str, str]] = []
    for pointer in live:
        if not isinstance(pointer, Mapping):
            continue
        run_id = str(pointer.get("run_id") or "")
        if not run_id or not identifies(pointer):
            continue
        if str(pointer.get("phase") or "") in (
            BLOCKED_UPSTREAM_PHASE,
            *DEAD_RUN_PHASES,
        ):
            continue
        if _release._discarded(run_id):
            continue
        candidates.append((run_id, str(pointer.get("promoted_revision") or "").strip()))

    if not candidates and not promoted:
        return False

    if promoted:
        # A promotion removes the live pointer before it records the revision,
        # so in production this is the branch that answers. The landed state of
        # a promoted run is the queue's own: the drain names declared work with
        # no live request and no reachable promotion, so a section it does not
        # name has landed and one it does name is still waiting. The derivation
        # uses the pass's ledger snapshot, and its answer is kept rather than
        # recomputed for every request waiting on the same section.
        key = (_UNLOADED_CACHE_KEY, project, slug, wanted)
        unloaded = cache.get(key) if cache is not None else None
        if unloaded is None:
            unloaded = runs_module._unloaded_sections(
                project,
                live,
                [(slug, {"section_declarations": {section: "implementable"}})],
                ledger_rows=rows,
            )
            if cache is not None:
                cache[key] = unloaded
        if any(section_record_id(name) == wanted for name in unloaded.get(slug) or ()):
            return True

    if not candidates:
        return False

    checkout = project_mount_repository(project)
    if checkout is None:
        # A project with no registered checkout offers no HEAD to read, so a
        # run's landing is unknown rather than absent. Releasing would put a
        # waiter onto a base the queue cannot show carries its upstream, so the
        # wait is held -- the answer :func:`_request_state` gives a run ref whose
        # project no mount names.
        return True

    def landed(revision: str) -> bool:
        try:
            return _revision_is_ancestor(checkout, revision, "HEAD")
        except Exception:  # noqa: BLE001 - a broken probe proves no landing
            return False

    for run_id, candidate in candidates:
        revision = candidate or promoted.get(run_id, "")
        if revision:
            if not landed(revision):
                return True
        elif _manifest_status(run_id) in RUN_DELIVERED_STATUSES:
            return True
    return False


def _request_state(
    ref: str,
    *,
    live: Sequence[Mapping[str, Any]],
    project: str = "",
    issues: list[str] | None = None,
) -> str:
    """A request lands only when its complete run's revision reaches HEAD."""
    run_id = str(ref[len(REQUEST_REF_PREFIX) :]).strip()
    if not run_id:
        return PENDING
    for pointer in live:
        if str(pointer.get("run_id") or "") != run_id:
            continue
        phase = str(pointer.get("phase") or "")
        if phase in (BLOCKED_UPSTREAM_PHASE, *DEAD_RUN_PHASES) or _release._discarded(
            run_id
        ):
            return ABANDONED
    if _release._discarded(run_id):
        return ABANDONED
    status = _manifest_status(run_id, issues=issues)
    if status in RUN_FAILED_STATUSES:
        return ABANDONED
    if status not in RUN_DELIVERED_STATUSES:
        return PENDING
    if not project:
        # The run delivered, but the request recorded no project for this ref,
        # so the landing that would satisfy it can never be read. Left as a
        # bare PENDING this wait never resolves and never reports why: the
        # request reads awaiting-upstream indefinitely. The wait's own state is
        # unchanged — a ref with no project is not evidence of a landing — but
        # the reason is recorded on the request and raised as an obligation.
        if issues is not None:
            issues.append(
                f"{run_id}: the wait names no project, so its landing cannot be read"
            )
        return PENDING
    try:
        from reckon import ledger
        from reckon.crew.dispatch_sections import project_mount_repository
        from reckon.crew.promotion_scope import _revision_is_ancestor

        row = json.loads(ledger.run_path(project, run_id).read_text(encoding="utf-8"))
        revision = str(row.get("promoted_revision") or "").strip()
        if not revision:
            if issues is not None:
                issues.append(f"{run_id}: promoted_revision absent")
            return PENDING
        checkout = project_mount_repository(project)
        if checkout is None:
            if issues is not None:
                issues.append(f"{run_id}: project {project} has no registered checkout")
            return PENDING
        return (
            SATISFIED if _revision_is_ancestor(checkout, revision, "HEAD") else PENDING
        )
    except (OSError, ValueError, KeyError):
        return PENDING


def _wait_state(
    ref: str,
    *,
    waiting_section: str,
    rows: Mapping[str, Mapping[str, Any]],
    inventory: Mapping[str, Mapping[str, Any]],
    dead_sections: set[tuple[str, str]],
    live: Sequence[Mapping[str, Any]],
    project: str = "",
    docs_dir: Path | None = None,
    cache: dict[str, Any] | None = None,
    wait_projects: Mapping[str, str] | None = None,
    issues: list[str] | None = None,
    mount: object = _MOUNT_UNSET,
) -> str:
    """Resolve one wait ref of any of the three kinds."""
    kind = wait_ref_kind(ref)
    if ref.startswith("readiness:"):
        target = ref[len("readiness:") :]
        return _section_readiness_state(
            target,
            project=project,
            docs_dir=docs_dir,
            cache=cache if cache is not None else {},
            live=live,
        )
    if kind == "gate":
        return _gate_state(ref, rows=rows, inventory=inventory)
    if kind == "request":
        run_id = ref[len(REQUEST_REF_PREFIX) :]
        # A pointer written before this record carried ``wait_projects`` names
        # no project for the ref it waits on, and nothing rewrites it. Read
        # through the same derivation the writers use rather than as a wait
        # that names nothing: an empty project never resolves, so the request
        # would sit awaiting-upstream for as long as the pointer exists.
        recorded = str((wait_projects or {}).get(run_id) or "")
        project_for_ref = recorded
        unreadable = ""
        if not recorded:
            project_for_ref, unreadable = _wait_refs.wait_project_lookup(
                run_id, owner_project=project, live=live, mount=mount
            )
        if not project_for_ref:
            if issues is not None:
                if unreadable:
                    # An unreadable registry answers nothing about this run, so
                    # the wait is unresolved rather than unresolvable: telling a
                    # coordinator to discard a request here would throw away
                    # work a readable registry would have released.
                    issues.append(
                        f"{run_id}: the mount registry could not be read "
                        f"({unreadable}), so this wait cannot be resolved until "
                        "it can be read again"
                    )
                else:
                    issues.append(
                        f"{WAIT_UNRESOLVABLE_PREFIX} {ref} — the wait names no "
                        "project and no project holds this run, so the landing "
                        "that would release this request can never be read"
                    )
            return PENDING
        return _request_state(
            ref,
            live=live,
            project=project_for_ref,
            issues=issues,
        )
    return _ref_state(
        ref,
        waiting_section=waiting_section,
        rows=rows,
        inventory=inventory,
        dead_sections=dead_sections,
        live=live,
        project=project,
        docs_dir=docs_dir,
        cache=cache,
    )


def _section_readiness_state(
    ref: str,
    *,
    project: str,
    docs_dir: Path | None,
    cache: dict[str, Any],
    live: Sequence[Mapping[str, Any]] = (),
) -> str:
    from reckon._schema import parse_plan_ref

    parsed = parse_plan_ref(ref)
    if parsed is None or not parsed.stage or docs_dir is None:
        return PENDING
    target_project = parsed.project or project
    target_docs = _wait_refs._docs_for_project(
        target_project, current_project=project, current_docs=docs_dir
    )
    if target_docs is None:
        return PENDING
    rows, inventory = _readiness_rows(target_project, target_docs, cache, parsed.slug)
    item = inventory.get(parsed.slug)
    if item is None:
        return PENDING
    status = _wait_refs._section_status(item, parsed.stage)
    if status == "done":
        if _section_run_unlanded(
            target_project,
            parsed.slug,
            normalize_section(parsed.stage),
            live=live,
            cache=cache,
        ):
            return PENDING
        return SATISFIED
    if status in {"descoped", "deferred", "failed", "blocked"}:
        return ABANDONED
    verdict = next(
        (
            value
            for section, value in (rows.get(parsed.slug) or {})
            .get("sections", {})
            .items()
            if normalize_section(section) == normalize_section(parsed.stage)
        ),
        None,
    )
    return SATISFIED if verdict and verdict.get("ready") else PENDING


def _dispatch_nonrun_wait_state(
    ref: str,
    *,
    project: str,
    docs_dir: Path,
    live: Sequence[Mapping[str, Any]] = (),
    cache: dict[str, Any] | None = None,
) -> str:
    """Read authored declarations without a portfolio scope-claim scan."""
    from reckon import _plan_html as plan_html_reader
    from reckon._schema import parse_plan_ref
    from reckon.lifecycle import COMPLETED_STATUSES, TERMINAL_STATUSES

    gate_ref = ref.startswith(GATE_REF_PREFIX)
    parsed = parse_plan_ref(ref[len(GATE_REF_PREFIX) :] if gate_ref else ref)
    if parsed is None:
        return PENDING
    target_project = parsed.project or project
    target_docs = _wait_refs._docs_for_project(
        target_project, current_project=project, current_docs=docs_dir
    )
    if target_docs is None:
        return PENDING
    html = _release._plan_html(target_docs.parent, parsed.slug)
    if not html:
        return PENDING
    state = plan_html_reader.read_state(html)
    if gate_ref:
        gate = next(
            (
                item
                for item in state.get("gates") or ()
                if str(item.get("id") or "") == parsed.stage
            ),
            None,
        )
        if gate is None:
            return PENDING
        if gate.get("passed"):
            return SATISFIED
        source = parse_plan_ref(str(gate.get("gating_plan") or ""))
        if source is not None and source.stage:
            source_docs = _wait_refs._docs_for_project(
                source.project or target_project,
                current_project=project,
                current_docs=docs_dir,
            )
            if source_docs is not None:
                source_state = plan_html_reader.read_state(
                    _release._plan_html(source_docs.parent, source.slug)
                )
                if _wait_refs._section_status(source_state, source.stage) == "done":
                    if _section_run_unlanded(
                        source.project or target_project,
                        source.slug,
                        normalize_section(source.stage),
                        live=live,
                        cache=cache,
                    ):
                        return PENDING
                    return SATISFIED
        return PENDING
    if parsed.stage:
        declaration = _wait_refs._section_status(state, parsed.stage)
        if declaration == "done":
            if _section_run_unlanded(
                target_project,
                parsed.slug,
                normalize_section(parsed.stage),
                live=live,
                cache=cache,
            ):
                return PENDING
            return SATISFIED
        if declaration in {"descoped", "deferred", "failed", "blocked"}:
            return ABANDONED
        return PENDING
    status = str(state.get("status") or "")
    if status in COMPLETED_STATUSES:
        return SATISFIED
    return ABANDONED if status in TERMINAL_STATUSES else PENDING


def _needs_own_section_verdict(node: TaskNode) -> bool:
    """Say whether the roadmap's verdict on the node's own section is read.

    A gate or an open decision holds new work on the section, so the section's
    verdict is read for every role except a review's: a review judges code a
    run has already written, and nothing it does depends on how the hold is
    settled. A node carrying no section has no verdict to read.
    """
    if str(node.role or "").strip() == REVIEW_ROLE:
        return False
    return bool(node.plan and node.section)


def dispatch_waits(
    node: TaskNode,
    *,
    repo_root: str | Path,
    project: str = "",
    run_id: str = "",
    live: Sequence[Mapping[str, Any]] | None = None,
    readiness_cache: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, str], list[str]]:
    """Validate a dispatch's graph and return all refs and its unmet refs.

    The caller uses the same run identity for validation and storage. No queued
    pointer exists when this raises for a missing target or a graph cycle.
    """
    own_verdict = _needs_own_section_verdict(node) and not node.waits_only_after
    refs = _wait_refs.derive_wait_refs(node, repo_root=repo_root)
    if own_verdict:
        # A preflight reads the section's verdict eagerly only when the plan
        # authors a hold that can apply; otherwise the roadmap is left unread.
        from reckon import _plan_html as plan_html_reader

        state = plan_html_reader.read_state(_release._plan_html(repo_root, node.plan))
        own_verdict = bool(state.get("gates") or state.get("decisions"))
    if not refs and not own_verdict:
        return [], {}, []
    docs_dir = Path(str(repo_root)) / "docs"
    owner_project = project or _project_of_docs_dir(docs_dir)
    if live is None:
        try:
            live = runs.list_live()
        except Exception:  # noqa: BLE001 - an unreadable live set holds no wait
            live = []
    candidate = run_id or f"candidate:{node.id}"
    refs_by_run, projects_by_run = _wait_refs._validated_batch_waits(
        [node],
        {node.id: candidate},
        project=owner_project,
        repo_root=repo_root,
        live=live,
        read_own_verdict=own_verdict,
        readiness_cache=readiness_cache,
    )
    refs = refs_by_run[candidate]
    wait_projects = projects_by_run[candidate]
    if not refs:
        return [], wait_projects, []
    if not any(wait_ref_kind(ref) == "request" for ref in refs):
        unmet = [
            ref
            for ref in refs
            if ref.startswith("readiness:")
            or _dispatch_nonrun_wait_state(
                ref,
                project=owner_project,
                docs_dir=docs_dir,
                live=live,
                cache=readiness_cache,
            )
            != SATISFIED
        ]
        return refs, wait_projects, unmet
    cache = readiness_cache if readiness_cache is not None else {}
    rows, inventory = _readiness_rows(owner_project, docs_dir, cache, node.plan, refs)
    dead = _release._dead_sections(live)
    mount = _wait_refs._mount_view()
    unmet = [
        ref
        for ref in refs
        if _wait_state(
            ref,
            waiting_section=node.section,
            rows=rows,
            inventory=inventory,
            dead_sections=dead,
            live=live,
            project=owner_project,
            docs_dir=docs_dir,
            cache=cache,
            wait_projects=wait_projects,
            mount=mount,
        )
        != SATISFIED
    ]
    return refs, wait_projects, unmet


def dispatch_wait_blockers(
    node: TaskNode,
    *,
    repo_root: str | Path,
    project: str,
    refs: Sequence[str],
    readiness_cache: dict[str, Any],
) -> list[dict[str, Any]]:
    """The roadmap blockers behind a dispatch's own readiness wait.

    Consume the verdict that supplied the wait, including its decision keys;
    neither the dispatcher nor admission reinterprets a decision's scope.
    """
    if not any(ref.startswith("readiness:") for ref in refs):
        return []
    rows, _ = _readiness_rows(
        project,
        Path(repo_root) / "docs",
        readiness_cache,
        node.plan,
        [ref for ref in refs if not ref.startswith("readiness:")],
    )
    verdict = next(
        (
            verdict
            for section, verdict in (rows.get(node.plan) or {})
            .get("sections", {})
            .items()
            if normalize_section(section) == normalize_section(node.section)
        ),
        {},
    )
    return list(verdict.get("blockers") or ())


def section_hold(
    refs: Iterable[str],
    *,
    project: str = "",
    docs_dir: str | Path | None = None,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Name what holds a request on its section, by kind and id.

    Three different things put a request in a waiting phase, and only one of
    them is an upstream that has not landed. A declared section wait names a
    section, a gate or another request, and the ref is the thing to go and
    settle. A hold on the request's own section is not a wait at all: it is a
    gate or an open decision the roadmap reports as a blocker, and the readiness
    ref that carries it names no upstream. A reader told "this section's waits
    have not landed" goes looking for that upstream and finds nothing, so a
    hold of that kind is reported by the kind and id its plan gives it.

    The wording about waits is used only where every unmet ref is a declared
    wait, so the reason a coordinator reads always names something that exists.

    Returns ``names`` (one entry per blocker), ``detail`` (the phrase a hold
    carries) and ``reason`` (the one-line reason). The three are derived here
    rather than by each caller, so the dispatch answer, the recorded hold on the
    pointer and the admission verdict cannot describe one hold two ways.

    An unreadable plan falls back to the readiness ref itself: the hold is real
    and the ref is what was read, which is more than a blocker id guessed from
    nothing would be.
    """
    names: list[str] = []
    for ref in refs:
        text = str(ref).strip()
        if not text:
            continue
        if not text.startswith(READINESS_REF_PREFIX):
            names.append(text)
            continue
        resolved = _readiness_blocker_names(
            text[len(READINESS_REF_PREFIX) :],
            project=project,
            docs_dir=docs_dir,
            cache=cache if cache is not None else {},
        )
        names.extend(resolved or [text])
    names = list(dict.fromkeys(names))
    authored = [name for name in names if name.split(":", 1)[0] in SECTION_HOLD_KINDS]
    waited = [name for name in names if name.split(":", 1)[0] not in SECTION_HOLD_KINDS]
    if not names:
        reason = ""
    elif not waited:
        reason = "this section is held by " + ", ".join(names)
    elif not authored:
        reason = "this section's waits have not landed"
    else:
        reason = (
            "this section is held by "
            + ", ".join(authored)
            + ", and its waits have not landed"
        )
    return {"names": names, "detail": ", ".join(names), "reason": reason}


def section_hold_verdict(hold: Mapping[str, Any]) -> str:
    """The verdict a hold is recorded under.

    A hold naming an authored blocker on the section is a readiness hold: the
    work is waiting on something its plan has to settle. A hold whose every
    name is a wait the section declares is waiting on an upstream, which is a
    different thing for a reader to act on, so it is recorded as its own
    verdict rather than as readiness.
    """
    kinds = {
        str(name).split(":", 1)[0] for name in hold.get("names") or () if str(name)
    }
    if kinds & set(SECTION_HOLD_KINDS):
        return "held-on-readiness"
    return "held-on-waits"


def record_section_hold(
    run_id: str,
    hold: Mapping[str, Any],
    *,
    verdict: str,
    live: Sequence[Mapping[str, Any]] | None = None,
    moment: datetime | None = None,
) -> bool:
    """Store on a queued request what holds it, naming the blocker.

    The reason and the blocker names are written together with the verdict, so
    a coordinator reading the pointer is told the same thing the dispatch
    answer said. The write goes through the same writer an admission pass uses,
    so a later admission hold replaces this one instead of sitting beside it,
    and a pointer that cannot be written is reported rather than assumed.
    """
    if not run_id or not hold.get("names"):
        return False
    pointer = {"run_id": run_id}
    _holds._record_pass_hold(
        pointer,
        {
            "verdict": verdict,
            "reason": str(hold.get("reason") or ""),
            "detail": str(hold.get("detail") or ""),
            "hold": list(hold.get("names") or ()),
        },
        live=list(live if live is not None else ()),
        moment=moment if moment is not None else _service._now(),
    )
    return True


def _readiness_blocker_names(
    ref: str,
    *,
    project: str,
    docs_dir: str | Path | None,
    cache: dict[str, Any],
) -> list[str]:
    """The gates and open decisions the roadmap reports as a section's blockers."""
    from reckon._schema import parse_plan_ref

    parsed = parse_plan_ref(ref)
    if parsed is None or not parsed.stage or docs_dir is None:
        return []
    target_project = parsed.project or project
    target_docs = _wait_refs._docs_for_project(
        target_project, current_project=project, current_docs=Path(docs_dir)
    )
    if target_docs is None:
        return []
    rows, _inventory = _readiness_rows(target_project, target_docs, cache, parsed.slug)
    verdict = next(
        (
            value
            for section, value in (rows.get(parsed.slug) or {})
            .get("sections", {})
            .items()
            if normalize_section(section) == normalize_section(parsed.stage)
        ),
        None,
    )
    names: list[str] = []
    for blocker in (verdict or {}).get("blockers") or ():
        kind = str(blocker.get("kind") or "")
        if kind not in SECTION_HOLD_KINDS:
            continue
        identifier = str(blocker.get("id") or blocker.get("gate") or "").strip()
        if identifier:
            names.append(f"{kind}:{identifier}")
    return names


def unmet_waits(
    node: TaskNode,
    *,
    repo_root: str | Path,
    project: str = "",
    live: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Return a node's unmet refs after validating its dependency graph."""
    return dispatch_waits(node, repo_root=repo_root, project=project, live=live)[2]


def _ref_state(
    ref: str,
    *,
    waiting_section: str,
    rows: Mapping[str, Mapping[str, Any]],
    inventory: Mapping[str, Mapping[str, Any]],
    dead_sections: set[tuple[str, str]],
    live: Sequence[Mapping[str, Any]] = (),
    project: str = "",
    docs_dir: Path | None = None,
    cache: dict[str, Any] | None = None,
) -> str:
    """Resolve a plan or section ref from its project's declared state."""
    from reckon._schema import parse_plan_ref
    from reckon.lifecycle import COMPLETED_STATUSES, TERMINAL_STATUSES

    parsed = parse_plan_ref(ref)
    if parsed is None:
        return PENDING
    target_project = parsed.project or project
    if docs_dir is not None and cache is not None:
        target_docs = _wait_refs._docs_for_project(
            target_project, current_project=project, current_docs=docs_dir
        )
        if target_docs is None:
            return PENDING
        if target_project != project or parsed.slug not in inventory:
            rows, inventory = _readiness_rows(
                target_project, target_docs, cache, parsed.slug
            )
    slug, section = parsed.slug, normalize_section(parsed.stage or "")
    if section and (slug, section) in dead_sections:
        return ABANDONED
    item = inventory.get(slug)
    if item is None:
        return PENDING
    status = str(item.get("workflow_status") or item.get("status") or "")
    if not section:
        if status in COMPLETED_STATUSES:
            return SATISFIED
        if status in TERMINAL_STATUSES:
            return ABANDONED
        return PENDING
    declaration = _wait_refs._section_status(item, section)
    if declaration == "done":
        if _section_run_unlanded(target_project, slug, section, live=live, cache=cache):
            return PENDING
        return SATISFIED
    if declaration in {"descoped", "deferred", "failed", "blocked"}:
        return ABANDONED
    if status in TERMINAL_STATUSES:
        return ABANDONED
    verdicts = (rows.get(slug) or {}).get("sections", {})
    if not any(normalize_section(key) == section for key in verdicts):
        return PENDING
    return PENDING
