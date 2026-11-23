"""Release waiters and advance requests whose upstream work has landed."""

from __future__ import annotations

from .. import stranded_wait as _stranded_wait
from . import launching as _launching
from . import readiness as _readiness
from . import reviews as _reviews
from . import service as _service
from . import wait_refs as _wait_refs
from .shared import (
    _MOUNT_UNSET,
    _RELEASE_TICK_LAST,
    ABANDONED,
    AWAITING_UPSTREAM_PHASE,
    BLOCKED_UPSTREAM_PHASE,
    DEAD_RUN_PHASES,
    GATE_REF_PREFIX,
    LANDED_RUN_PHASES,
    QUEUED_PHASE,
    READINESS_REF_PREFIX,
    RELEASE_FAILURE_KEY,
    RELEASE_TICK_SECONDS,
    REQUEST_REF_PREFIX,
    SATISFIED,
    WAITING_UPSTREAM_PHASES,
    Any,
    Callable,
    Iterable,
    Mapping,
    Path,
    Sequence,
    TaskNode,
    datetime,
    normalize_section,
    parse_utc,
    runs,
    time,
)


def _dead_sections(live: Sequence[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """The ``(plan, section)`` pairs no live request will ever deliver.

    A section ends without delivering when one of its runs is recorded failed,
    blocked on its own upstream, or deliberately discarded. That one run does
    not close the section: a section's refs are released by the section
    landing, so a section's death is a question about every run that names it.
    The section is dead only when no run it owns may still land — a sibling
    that is queued, held or working, a pointer whose phase no pass has moved
    on, and a failed run with a resume already in flight all keep the section
    open and the refs waiting on it pending rather than abandoned.

    A section with no run at all is not dead either: nothing has ended. The set
    is what makes blocking transitive: the tail of a chain is blocked because
    the middle is, without the tail's ref having to name the original failure.
    """
    ended: set[tuple[str, str]] = set()
    standing: set[tuple[str, str]] = set()
    for pointer in live:
        key = _section_key(pointer)
        if key is None:
            continue
        phase = str(pointer.get("phase") or "")
        discarded = _discarded(str(pointer.get("run_id") or ""))
        if phase in (BLOCKED_UPSTREAM_PHASE, *DEAD_RUN_PHASES) or discarded:
            if _resume_in_flight(pointer):
                standing.add(key)
            else:
                ended.add(key)
        elif phase not in LANDED_RUN_PHASES:
            standing.add(key)
    return ended - standing


def _section_key(pointer: Mapping[str, Any]) -> tuple[str, str] | None:
    """The ``(plan, section)`` one live pointer names, or ``None`` for neither."""
    node = dict(pointer.get("node") or {})
    plan = str(node.get("plan") or pointer.get("plan") or "")
    section = str(node.get("section") or pointer.get("section") or "")
    if not plan or not section:
        return None
    return (plan, normalize_section(section))


def _resume_in_flight(pointer: Mapping[str, Any]) -> bool:
    """Whether a resume of this run began and has not yet recorded its attempt.

    A resume writes its mark on the pointer before the attempt it launches
    records one of its own, so the pointer still reads ``failed`` throughout
    that window. Reading the phase alone there would close the section under a
    request that is on its way back, so the mark is read as the fact the later
    phase records.
    """
    from reckon.crew.resumption import RESUME_LAUNCH_IN_FLIGHT

    if not isinstance(pointer.get(RESUME_LAUNCH_IN_FLIGHT), Mapping):
        return False
    from reckon.crew.resumption import launch_in_flight

    try:
        return bool(launch_in_flight(pointer))
    except Exception:  # noqa: BLE001 - an unreadable liveness probe is no resume
        return False


def _live_runs_on(
    refs: Iterable[str],
    live: Sequence[Mapping[str, Any]],
    *,
    project: str = "",
    exclude: str = "",
) -> list[str]:
    """The runs still on their way to landing these section refs, by run id.

    The judgement :func:`_dead_sections` makes about a section's death, read
    for the one request waiting on it: a request that stays waiting names the
    run whose work it waits for, so a reader sees a live sibling rather than
    only the section's name.
    """
    wanted: set[tuple[str, str]] = set()
    wanted_runs: set[str] = set()
    for ref in refs:
        if str(ref).startswith(REQUEST_REF_PREFIX):
            named = str(ref)[len(REQUEST_REF_PREFIX) :].strip()
            if named:
                wanted_runs.add(named)
            continue
        key = _release_section_key(str(ref), project)
        if key is not None:
            wanted.add((key[1], key[2]))
    if not wanted and not wanted_runs:
        return []
    working: list[str] = []
    for pointer in live:
        run_id = str(pointer.get("run_id") or "")
        if not run_id or run_id == exclude:
            continue
        named = run_id in wanted_runs
        section = _section_key(pointer)
        if not named and (section is None or (section[0], section[1]) not in wanted):
            continue
        phase = str(pointer.get("phase") or "")
        if phase in LANDED_RUN_PHASES or _discarded(run_id):
            continue
        if phase in (
            BLOCKED_UPSTREAM_PHASE,
            *DEAD_RUN_PHASES,
        ) and not _resume_in_flight(pointer):
            continue
        working.append(run_id)
    return sorted(set(working))


def _discarded(run_id: str) -> bool:
    """Whether a deliberate discard is recorded for one run.

    A discard is the operator closing a request as abandoned, so it reads as
    abandoned to whatever waits behind it. An unreadable discard store is no
    evidence of a discard, so it reads as none.
    """
    from reckon.crew.promotion import discard_recorded

    try:
        return bool(discard_recorded(run_id))
    except Exception:  # noqa: BLE001 - an unreadable discards store is no evidence
        return False


def resolve_waits(
    pointer: Mapping[str, Any],
    *,
    live: Sequence[Mapping[str, Any]],
    docs_dir: str | Path | None,
    cache: dict[str, Any],
    dead_sections: set[tuple[str, str]] = frozenset(),  # type: ignore[assignment]
    mount: object = _MOUNT_UNSET,
) -> str:
    """The phase one waiting request takes, from the roadmap's own readiness.

    ``awaiting-upstream`` while every ref is pending, ``blocked-upstream`` the
    moment any ref is closed as abandoned or failed, and ``queued`` once the
    last ref lands. A request carrying a ref that is neither of those stays
    exactly where it is: an unobserved ref is not evidence that its dependency
    landed.
    """
    refs = _wait_refs.section_waits_for(pointer)
    if not refs:
        return QUEUED_PHASE
    inventory_only = not any(
        ref.startswith((GATE_REF_PREFIX, "readiness:")) for ref in refs
    )
    rows, inventory = _readiness._readiness_rows(
        str(pointer.get("project") or "") or _readiness._project_of_docs_dir(docs_dir),
        docs_dir,
        cache,
        str(dict(pointer.get("node") or {}).get("plan") or pointer.get("plan") or ""),
        refs,
        inventory_only=inventory_only,
    )
    issues: list[str] = []
    owner_project = str(
        pointer.get("project") or ""
    ) or _readiness._project_of_docs_dir(docs_dir)
    if mount is _MOUNT_UNSET and any(
        _readiness.wait_ref_kind(ref) == "request" for ref in refs
    ):
        mount = _wait_refs._mount_view()
    states = [
        _readiness._wait_state(
            ref,
            waiting_section=str(
                dict(pointer.get("node") or {}).get("section")
                or pointer.get("section")
                or ""
            ),
            rows=rows,
            inventory=inventory,
            dead_sections=dead_sections,
            live=live,
            project=owner_project,
            docs_dir=Path(docs_dir) if docs_dir is not None else None,
            cache=cache,
            wait_projects=dict(pointer.get("wait_projects") or {}),
            issues=issues,
            mount=mount,
        )
        for ref in refs
    ]
    if isinstance(pointer, dict):
        obligations = list(dict.fromkeys(issues))
        # A request that stays waiting names the run whose work it waits for.
        # The section it waits on can own several runs, and one of them having
        # failed is not the section's end while another is still on its way: a
        # reader that saw only the section's name could not tell the two apart.
        working = _live_runs_on(
            refs,
            live,
            project=owner_project,
            exclude=str(pointer.get("run_id") or ""),
        )
        # The pointer is this pass's copy of the row, so the obligations land
        # where the loop that persists them reads them, and a shared view
        # reaching this writer raises rather than being quietly dropped.
        pointer["upstream_obligations"] = obligations
        pointer["upstream_live"] = working
        # What could release this request, read from the same per-ref states the
        # phase is resolved from: an upstream that can never land, work still on
        # its way, or a wait nobody is building.
        stranded_reasons: list[str] = []
        unlanded: list[str] = []
        for ref, state in zip(refs, states, strict=False):
            if state == SATISFIED:
                continue
            reason = _stranded_wait.section_cannot_land(
                ref,
                project=owner_project,
                docs_dir=Path(docs_dir) if docs_dir is not None else None,
                inventory=inventory,
            )
            if reason:
                stranded_reasons.append(reason)
                continue
            key = _release_section_key(ref, owner_project)
            if key is not None:
                unlanded.append(f"{key[1]}#{key[2]}")
        wait_class, wait_detail = _stranded_wait.classify(
            stranded=stranded_reasons, working=working, unlanded=unlanded
        )
        pointer["wait_class"] = wait_class
        pointer["wait_detail"] = wait_detail
    if ABANDONED in states:
        return BLOCKED_UPSTREAM_PHASE
    if all(state == SATISFIED for state in states):
        return QUEUED_PHASE
    return AWAITING_UPSTREAM_PHASE


def _resolve_and_move(
    pointer: Mapping[str, Any],
    *,
    live: Sequence[Mapping[str, Any]],
    docs_dir: str | Path | None,
    cache: dict[str, Any],
    dead_sections: set[tuple[str, str]] = frozenset(),  # type: ignore[assignment]
    run_id: str = "",
    mount: object = _MOUNT_UNSET,
) -> str:
    """Resolve one request's waits and move its pointer to the phase they justify.

    The one owner of the resolve-and-move idiom. The queue-time check in a
    dispatch, the release step of an admission pass, a promotion that lands a
    section, a discard that closes a request and the service's release tick all
    reach a request through here, so every route that can make a wait's upstream
    land releases its waiters by the same derivation.

    The phase the request already holds is the one a caller reports when the
    move cannot be written: a request is never launched from a phase the store
    did not record.
    """
    phase = str(pointer.get("phase") or AWAITING_UPSTREAM_PHASE)
    target = resolve_waits(
        pointer,
        live=live,
        docs_dir=docs_dir,
        cache=cache,
        dead_sections=dead_sections,
        mount=mount,
    )
    if target == phase:
        return phase
    if not _reviews._move_pointer(run_id or str(pointer.get("run_id") or ""), target):
        return phase
    if isinstance(pointer, dict):
        pointer["phase"] = target
    return target


def _record_release_failure(run_id: str, detail: str) -> bool:
    """Write a failed release against one request, reporting whether it landed."""

    def write(current: dict[str, Any]) -> dict[str, Any]:
        current[RELEASE_FAILURE_KEY] = detail
        return current

    try:
        runs._mutate_pointer(run_id, write)
    except Exception:  # noqa: BLE001 - an unwritable pointer is no evidence
        return False
    return True


def _release_section_key(ref: str, project: str) -> tuple[str, str, str] | None:
    """Identify a section wait by project, plan, and normalized section."""
    from reckon._schema import parse_plan_ref

    if ref.startswith((GATE_REF_PREFIX, REQUEST_REF_PREFIX, "readiness:")):
        return None
    plan_ref, separator, section = ref.partition("#")
    if not separator or not section or "#" in section or "@" in section:
        return None
    parsed = parse_plan_ref(plan_ref)
    if parsed is None or parsed.stage or parsed.followup:
        return None
    return parsed.project or project, parsed.slug, normalize_section(section)


def _release_waiters(
    refs: Iterable[str],
    *,
    project: str = "",
    live: Sequence[Mapping[str, Any]] | None = None,
    cache: dict[str, Any] | None = None,
    docs_dir_for: Callable[[Mapping[str, Any]], Path] | None = None,
) -> dict[str, Any]:
    """Release every waiting request whose waits name one of ``refs``.

    Called by the route that made the refs land — a promotion, for the section
    its run delivered, and a discard, for the requests waiting on the run it
    removed — so the release does not wait for the next admission pass. A ref is
    a trigger and not a verdict: each candidate's waits are re-derived against
    the roadmap, so a request whose other refs are still pending stays waiting.

    A release that raises for one request is recorded on that request and does
    not end the release for the others. The landing that triggered it has
    already happened, so a refused release is a fact about one request rather
    than a condition on the landing.
    """
    landed = {str(ref).strip() for ref in refs if str(ref).strip()}
    if not landed:
        return {
            "candidates": [],
            "promoted": [],
            "blocked": [],
            "failed": {},
        }
    section_keys = (
        {key for ref in landed if (key := _release_section_key(ref, project))}
        if project
        else set()
    )
    exact_refs = {
        ref
        for ref in landed
        if not project or _release_section_key(ref, project) is None
    }
    # A listing hands out the memo's read-only view, and this pass derives a
    # phase and two obligation fields onto each candidate it releases, so the
    # candidates are copied first.
    pointers = [
        dict(pointer) for pointer in (live if live is not None else runs.list_live())
    ]
    shared = cache if cache is not None else {}
    promoted: list[str] = []
    blocked: list[str] = []
    failed: dict[str, str] = {}
    candidates: list[str] = []
    for pointer in pointers:
        if not isinstance(pointer, dict):
            continue
        if str(pointer.get("phase") or "") not in WAITING_UPSTREAM_PHASES:
            continue
        waits = _wait_refs.section_waits_for(pointer)
        waiter_project = str(pointer.get("project") or "").strip()
        if not any(
            ref in exact_refs
            or _release_section_key(ref, waiter_project) in section_keys
            for ref in waits
        ):
            continue
        run_id = str(pointer.get("run_id") or "")
        candidates.append(run_id)
        docs_dir = (
            docs_dir_for(pointer)
            if docs_dir_for is not None
            else Path(str(pointer.get("repo") or "") or ".") / "docs"
        )
        try:
            target = _resolve_and_move(
                pointer,
                live=pointers,
                docs_dir=docs_dir,
                cache=shared,
            )
        except Exception as exc:  # noqa: BLE001 - one request cannot end the release
            detail = f"release for {run_id!r} raised: {exc}"
            failed[run_id] = detail
            _record_release_failure(run_id, detail)
            continue
        if target == QUEUED_PHASE:
            promoted.append(run_id)
        elif target == BLOCKED_UPSTREAM_PHASE:
            blocked.append(run_id)
    return {
        "candidates": candidates,
        "promoted": promoted,
        "blocked": blocked,
        "failed": failed,
    }


def _release_tick(
    *,
    now: float | None = None,
    interval: float = RELEASE_TICK_SECONDS,
) -> dict[str, Any]:
    """Run the release step alone, at most once per ``interval``.

    The admission service calls this as a stage of its own, beside rather than
    inside a launch pass or a landing, so a request whose upstream landed by any
    route — a promotion, a discard, a hand merge of the plan — moves to
    ``queued`` within one tick however long those stages take.
    """
    moment = time.monotonic() if now is None else now
    last = float(_RELEASE_TICK_LAST.get("at") or 0.0)
    if last and moment - last < interval:
        return {
            "ran": False,
            "waited_seconds": round(moment - last, 3),
            "interval_seconds": interval,
        }
    _RELEASE_TICK_LAST["at"] = moment
    released = advance_waiting_requests(runs.list_live())
    return {"ran": True, **released}


def _rederive_waits(
    pointer: dict[str, Any],
    *,
    cache: dict[tuple[str, str, str, tuple[str, ...], bool], list[str]] | None = None,
    live: Sequence[Mapping[str, Any]] | None = None,
    mount: object = _MOUNT_UNSET,
) -> bool:
    """Re-read a stored request's waits from the plan and rewrite them.

    The plan is what a coordinator steers a queued request through, so an edit
    that narrows a section's waits can release the request and an edit that
    widens them can send it back to ``awaiting-upstream``. The derivation is
    the one that composed the waits at dispatch, and it keeps the request's own
    ``--after`` refs because no plan edit carries them. A pointer with no
    repository, or one whose plan cannot be read, keeps the waits it has: an
    unreadable authority is not evidence that the waits changed.

    The pass's cache is keyed on the request's own ``--after`` refs and the
    flag beside the plan and section, because two requests of one section
    derive different waits: a sibling's refs handed to a request that names
    none of its own is a wait on work it never asked for.

    The projects the rewritten ``run:`` refs land in are written in the same
    mutation, because a ref added here without one is a wait that names no
    project and no pass can resolve it.
    """
    node = _launching._node_from_pointer(pointer)
    repo_root = str(pointer.get("repo") or "")
    if not repo_root:
        return False
    if not node.plan.strip() and not node.after:
        return False
    key = (
        repo_root,
        node.plan,
        node.section,
        tuple(node.after),
        node.waits_only_after,
    )
    stored = _wait_refs.section_waits_for(pointer)
    derived = None if cache is None else cache.get(key)
    if derived is None:
        derived = _wait_refs.derive_wait_refs(node, repo_root=repo_root)
        # The readiness ref is re-derived only for a request that carries one
        # already. It is composed at queue time from the plan as it stood then,
        # and a record that carries none was queued against a section under no
        # authored hold; composing one here would add a wait the request never
        # declared.
        if any(ref.startswith(READINESS_REF_PREFIX) for ref in stored):
            own = _wait_refs._own_readiness_ref(
                node,
                project=str(pointer.get("project") or "")
                or _readiness._project_of_docs_dir(Path(repo_root) / "docs"),
                docs_dir=Path(repo_root) / "docs",
                cache=cache if cache is not None else {},
                refs=derived,
            )
            if own and own not in derived:
                derived = [*derived, own]
        if cache is not None:
            cache[key] = list(derived)
    # ``derived`` is the whole of what the request waits on: derive_wait_refs
    # builds the request's own ``run:`` refs from the authored ``--after``, so
    # a stored ref the request never named is absent from it and is dropped
    # by the rewrite below. Keeping such a ref instead would make it
    # permanent, and discarding the run it names would then strand this
    # request blocked-upstream with no route back.
    if derived == stored:
        return False
    run_id = str(pointer.get("run_id") or "")
    owner_project = str(
        pointer.get("project") or ""
    ) or _readiness._project_of_docs_dir(Path(repo_root) / "docs")
    if live is None:
        try:
            live = runs.list_live()
        except Exception:  # noqa: BLE001 - an unreadable live set derives no project
            live = ()
    projects = _wait_refs.wait_projects_for(
        derived, owner_project=owner_project, live=live, mount=mount
    )

    def rewrite(current: dict[str, Any]) -> dict[str, Any]:
        current["waits"] = list(derived)
        current["wait_projects"] = dict(projects)
        current["waits_at"] = _service._utc_now()
        return current

    try:
        runs._mutate_pointer(run_id, rewrite)
    except Exception:  # noqa: BLE001 - an unwritable pointer keeps its waits
        return False
    pointer["waits"] = list(derived)
    pointer["wait_projects"] = dict(pointer.get("wait_projects") or {}) | projects
    return True


#: The statuses under which a whole-plan dispatch has stored a request for a
#: section. A row carrying one of these is work now in the queue.
_FILED_STATUSES = frozenset({"queued", "awaiting-upstream", "held"})


def _file_idle_upstreams(
    pointer: Mapping[str, Any],
    *,
    config_for: Callable[[str, Path], Mapping[str, Any]] | None = None,
    filing: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """File a request for each unqueued section one idle wait names.

    The read is the one the whole-plan dispatch already answers with the same
    per-section reasons, so a wait is never judged a second way here. Each plan
    named is filed once per pass through the call the service's own queue stage
    uses, and the requests filed are returned so the pass can read the wait
    (and whatever that request itself waits on) against the store it just
    extended.

    A section the read refuses is returned with that answer. An event that
    stops a plan from being filed is not the read's answer and is not recorded
    as one: the request keeps waiting and the next pass tries again.

    ``filing`` is the pass's record of what it has already filed and read. The
    coordinator resolution answers for a whole project and is read once per
    project, and a plan is filed once per pass however many waits name it, so
    a later wait on the same plan reads the answers the first filing gave
    rather than filing it again.
    """
    refs = _wait_refs.section_waits_for(pointer)
    project = str(pointer.get("project") or "")
    repo_root = str(pointer.get("repo") or "")
    session = str(pointer.get("session") or "")
    if not refs or not project or not repo_root:
        return [], []
    docs = Path(repo_root) / "docs"
    wanted: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for ref in refs:
        key = _release_section_key(ref, project)
        if key is None:
            continue
        wanted.setdefault((key[0], key[1]), []).append((ref, key[2]))
    if not wanted:
        return [], []
    from reckon.crew.dispatch_batch import QUEUE_ANSWER_MAY, queue_plan

    from . import queueing as _queueing

    resolve_config = config_for if config_for is not None else _queueing._default_config
    filed: list[dict[str, Any]] = []
    unqueued: list[dict[str, str]] = []

    # The waiter's own session chose the work that is now blocked, so the queue
    # records that identity; a request carrying none names the service.
    queued_by = session or _queueing.SERVICE_SESSION

    record = filing if filing is not None else {}
    owners: dict[str, Mapping[str, Any]] = record.setdefault("ownership", {})
    filed_plans: dict[tuple[str, str], Mapping[str, Any]] = record.setdefault(
        "plans", {}
    )
    for (waited_project, plan), sections in wanted.items():
        target_docs = _wait_refs._docs_for_project(
            waited_project, current_project=project, current_docs=docs
        )
        if target_docs is None:
            continue
        target_root = Path(target_docs).parent
        first_filing = (waited_project, plan) not in filed_plans
        if first_filing:
            if waited_project not in owners:
                from reckon.crew.plan_coordinator import resolve_plan_coordinators

                try:
                    owners[waited_project] = resolve_plan_coordinators(
                        waited_project, root=target_root
                    )
                except Exception:  # noqa: BLE001 - the plan's own read resolves it
                    owners[waited_project] = None
            try:
                filed_plans[(waited_project, plan)] = queue_plan(
                    waited_project,
                    plan,
                    session=session,
                    config_for=lambda name, _root=target_root: resolve_config(
                        name, _root
                    ),
                    repo_root=target_root,
                    queued_by=queued_by,
                    ownership=owners[waited_project],
                )
            except Exception:  # noqa: BLE001, S112 - one unreadable plan ends nothing
                continue
        result = filed_plans[(waited_project, plan)]
        rows = {
            normalize_section(str(row.get("section") or "")): row
            for row in result.get("results") or ()
            if isinstance(row, Mapping)
        }
        for ref, section in sections:
            row = rows.get(normalize_section(section))
            if row is None:
                continue
            status = str(row.get("status") or "")
            answer = str(row.get("queue_answer") or "")
            if status in _FILED_STATUSES:
                if not first_filing:
                    # The first filing returned this request to the pass.
                    continue
                run_id = str(row.get("run_id") or "")
                new_pointer = runs.read_pointer(run_id) if run_id else None
                if isinstance(new_pointer, dict):
                    filed.append(new_pointer)
            elif answer and answer != QUEUE_ANSWER_MAY:
                unqueued.append({"ref": ref, "reason": answer})
    return filed, unqueued


def advance_waiting_requests(
    live: list[dict[str, Any]],
    *,
    config_for: Callable[[str, Path], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Move each waiting request to the phase its refs now justify.

    The pass re-reads readiness for every request in ``WAITING_UPSTREAM_PHASES``
    and moves it to ``queued`` or ``blocked-upstream``. Barrier blocking is
    transitive, so the resolution repeats to a fixpoint: a request blocked by a
    ref the same pass has just blocked becomes blocked too, whatever order the
    pointers are listed in. A queued request is read by the same pass, because
    a plan edit that widens its waits moves it back to ``awaiting-upstream``
    and one that narrows them can release it.

    The whole pass reads the docs tree once: one covered-file observation and
    one resource scan serve every request it judges, rather than each request's
    readiness read walking the tree again. Both close before the pass returns,
    so a landing that merges while the pass runs is seen by the next one.
    """
    with _readiness.plan_read_scopes():
        return _advance_waiting_requests(live, config_for=config_for)


def _advance_waiting_requests(
    live: list[dict[str, Any]],
    *,
    config_for: Callable[[str, Path], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    candidates = [
        (row, dict(row))
        for row in live
        if str(row.get("phase") or "") in (*WAITING_UPSTREAM_PHASES, QUEUED_PHASE)
    ]
    if not candidates:
        return {"promoted": [], "blocked": [], "obligations": [], "retired": []}
    cache: dict[str, Any] = {}
    # One plan read per pass, shared by every request it derives for.
    derived_cache: dict[tuple[str, str, str, tuple[str, ...], bool], list[str]] = {}
    # One mount-registry read per pass, shared by every ref it resolves.
    mount = _wait_refs._mount_view()
    for _row, pointer in candidates:
        try:
            _rederive_waits(pointer, cache=derived_cache, live=live, mount=mount)
        except Exception:  # noqa: BLE001, S112 - one unreadable request cannot end the pass
            continue
    promoted: list[str] = []
    blocked: list[str] = []
    retired: list[dict[str, Any]] = []
    obligations: dict[str, str] = {}
    moment = _service._now()
    report_after = _stranded_wait.report_after_hours()
    # Each candidate is (the caller's row, the copy this pass derives and
    # stamps); a resolved copy is mirrored onto its row, so a caller reading
    # its own rows sees the phase the pass recorded, exactly as it did when
    # the pass wrote the row itself. The copy is what keeps the record a
    # listing handed out — the memo's view — out of every write here.
    # One record per pass of what the idle-wait filing has read and filed.
    filing: dict[str, Any] = {}
    for _round in range(len(candidates) + 1):
        dead = _dead_sections(live)
        changed = False
        for row, pointer in candidates:
            phase = str(pointer.get("phase") or "")
            if phase not in (*WAITING_UPSTREAM_PHASES, QUEUED_PHASE):
                continue
            waiting = phase in WAITING_UPSTREAM_PHASES
            docs_dir = Path(str(pointer.get("repo") or "") or ".") / "docs"
            previous_issues = list(pointer.get("upstream_obligations") or ())
            previous_live = list(pointer.get("upstream_live") or ())
            previous_class = str(pointer.get("wait_class") or "")
            run_id = str(pointer.get("run_id") or "")
            target = _resolve_and_move(
                pointer,
                live=live,
                docs_dir=docs_dir,
                cache=cache,
                dead_sections=dead,
                run_id=run_id,
                mount=mount,
            )
            issues = list(pointer.get("upstream_obligations") or ())
            working = list(pointer.get("upstream_live") or ())
            wait_class = str(pointer.get("wait_class") or "")
            wait_detail = str(pointer.get("wait_detail") or "")
            previous_unqueued = list(pointer.get("upstream_unqueued") or ())
            unqueued: list[dict[str, Any]] = []
            if waiting and wait_class == _stranded_wait.IDLE:
                # A wait on a section nobody is building is work a coordinator
                # already chose to queue, one step further upstream. The pass
                # files that section's request through the whole-plan dispatch,
                # which is the same call the service's own queue stage uses, so
                # a wait does not stand on a section the read says may be
                # queued. A section the read refuses leaves its answer here for
                # the owner's duty to name.
                filed, unqueued = _file_idle_upstreams(
                    pointer, config_for=config_for, filing=filing
                )
                if filed:
                    live.extend(filed)
                    candidates.extend(
                        (new_pointer, dict(new_pointer)) for new_pointer in filed
                    )
                    changed = True
                    # Re-read the wait against the requests just filed: the
                    # upstream is now somebody's work, so the class and any
                    # duty follow the store rather than the pre-queue reading.
                    _resolve_and_move(
                        pointer,
                        live=live,
                        docs_dir=docs_dir,
                        cache=cache,
                        dead_sections=dead,
                        run_id=run_id,
                        mount=mount,
                    )
                    issues = list(pointer.get("upstream_obligations") or ())
                    working = list(pointer.get("upstream_live") or ())
                    wait_class = str(pointer.get("wait_class") or "")
                    wait_detail = str(pointer.get("wait_detail") or "")
            pointer["upstream_unqueued"] = unqueued
            if (
                waiting
                and wait_class == _stranded_wait.STRANDED
                and _retirable(pointer)
            ):
                # A wait whose upstream can never land is removed on the pass
                # that finds it. Its departure names the upstream and the
                # reason, and the owner's obligations carry it once.
                record = _retire_stranded(pointer, detail=wait_detail)
                if record is not None:
                    retired.append(record)
                    obligations[run_id] = wait_detail or record["reason"]
                    # The copy this pass derives keeps the retired phase so a
                    # later round skips a request that no longer has a pointer.
                    pointer["phase"] = "retired"
                    row["phase"] = "retired"
                    row["wait_class"] = wait_class
                    row["wait_detail"] = wait_detail
                    continue
            if issues:
                obligations[run_id] = issues[0]
            if (
                waiting
                and wait_class == _stranded_wait.IDLE
                and _idle_report_due(pointer, moment=moment, after_hours=report_after)
            ):
                # An idle wait is nobody's work, which is the owner's to know --
                # once. The stamp is the report, so a later pass is silent.
                _stamp_idle_report(pointer, detail=wait_detail, moment=moment)
                obligations[run_id] = wait_detail
            if (
                issues != previous_issues
                or working != previous_live
                or wait_class != previous_class
                or unqueued != previous_unqueued
            ):

                def record_issues(
                    current: dict[str, Any],
                    recorded: list[str] = issues,
                    live_runs: list[str] = working,
                    class_value: str = wait_class,
                    detail_value: str = wait_detail,
                    unqueued_value: list[dict[str, Any]] = unqueued,
                ) -> dict[str, Any]:
                    current["upstream_obligations"] = recorded
                    current["upstream_live"] = live_runs
                    current["wait_class"] = class_value
                    current["wait_detail"] = detail_value
                    current["upstream_unqueued"] = unqueued_value
                    return current

                runs._mutate_pointer(run_id, record_issues)
            row["phase"] = pointer["phase"]
            row["upstream_obligations"] = issues
            row["upstream_live"] = working
            row["wait_class"] = wait_class
            row["wait_detail"] = wait_detail
            row["upstream_unqueued"] = unqueued
            if target == phase:
                # The request holds the phase its waits justify: either they
                # still wait, or the move could not be written and is retried
                # on the next tick.
                continue
            changed = True
            if target == QUEUED_PHASE:
                promoted.append(run_id)
            elif target == BLOCKED_UPSTREAM_PHASE:
                blocked.append(run_id)
        if not changed:
            break
    return {
        "promoted": promoted,
        "blocked": blocked,
        "retired": retired,
        "obligations": [
            {"run_id": run_id, "detail": detail}
            for run_id, detail in obligations.items()
        ],
    }


def _retirable(pointer: Mapping[str, Any]) -> bool:
    """Whether one stranded request may be retired by the pass.

    A request a coordinator holds keeps its hold whatever its class: the hold is
    a decision about the work, not a reading of the upstream. A request that has
    launched is a run rather than a wait, so its failure belongs to the run's own
    paths. Everything else in a waiting phase is a stored request nobody can
    release.
    """
    if str(pointer.get("phase") or "") not in WAITING_UPSTREAM_PHASES:
        return False
    hold = pointer.get("queue_hold")
    if isinstance(hold, Mapping) and str(hold.get("reason") or ""):
        return False
    return not (pointer.get("pid") or pointer.get("launched_at"))


def _retire_stranded(
    pointer: Mapping[str, Any], *, detail: str
) -> dict[str, Any] | None:
    """Retire one request whose upstream can never land, recording why.

    The reason is written on the request before the departure, so the discard
    record carries it: a reader later finds the upstream it named and why it
    could not land, rather than only that a request went away.
    """
    from reckon.crew import promotion

    run_id = str(pointer.get("run_id") or "")
    if not run_id:
        return None
    reason = detail or "the upstream this request waits on cannot land"

    def stamp(current: dict[str, Any]) -> dict[str, Any]:
        current["reason"] = reason
        current["wait_class"] = _stranded_wait.STRANDED
        current["wait_detail"] = reason
        return current

    try:
        runs._mutate_pointer(run_id, stamp)
    except Exception:  # noqa: BLE001 - an unwritable request keeps waiting
        return None
    try:
        promotion.discard(run_id, allowed_phases=tuple(WAITING_UPSTREAM_PHASES))
    except Exception:  # noqa: BLE001 - a refused discard leaves the request live
        return None
    node = dict(pointer.get("node") or {})
    return {
        "run_id": run_id,
        "session": str(pointer.get("session") or ""),
        "project": str(pointer.get("project") or ""),
        "plan": str(node.get("plan") or pointer.get("plan") or ""),
        "section": str(node.get("section") or pointer.get("section") or ""),
        "node": str(node.get("id") or ""),
        "reason": reason,
    }


def _idle_report_due(
    pointer: Mapping[str, Any], *, moment: datetime, after_hours: float
) -> bool:
    """Whether an idle wait has stood past the threshold and never been told."""
    if pointer.get("idle_reported_at"):
        return False
    started = parse_utc(
        pointer.get("waits_at") or pointer.get("created_at") or pointer.get("queued_at")
    )
    if started is None:
        return False
    return (moment - started).total_seconds() >= after_hours * 3600.0


def _stamp_idle_report(
    pointer: dict[str, Any], *, detail: str, moment: datetime
) -> None:
    """Record the once-only idle report on the request, then mirror it locally."""
    run_id = str(pointer.get("run_id") or "")
    at = moment.isoformat()

    def stamp(current: dict[str, Any]) -> dict[str, Any]:
        current["idle_reported_at"] = at
        current["wait_class"] = _stranded_wait.IDLE
        current["wait_detail"] = detail
        return current

    try:
        runs._mutate_pointer(run_id, stamp)
    except Exception:  # noqa: BLE001 - an unwritable stamp reports again next pass
        return
    pointer["idle_reported_at"] = at


def _plan_html(repo_root: str | Path, plan: str) -> str:
    """One copy of a plan document, or an empty string when absent."""
    if not plan:
        return ""
    try:
        return (Path(str(repo_root)) / "docs" / "plans" / f"{plan}.html").read_text(
            encoding="utf-8"
        )
    except OSError:
        return ""


def queue_requests(
    nodes: Sequence[TaskNode],
    *,
    project: str,
    session: str,
    repo_root: str | Path,
    config: Mapping[str, Any],
    backend_name: str = "",
    launch_kind: str = "cli",
    run_ids: Mapping[str, str] | None = None,
    cap: int | None = None,
) -> dict[str, Any]:
    """Keep the batch entry while dispatch owns validation and queue writes.

    Each request is stamped with the plan digests it was queued against before
    this call returns, so the plan as the queuing session left it is the text
    admission holds the request to rather than whatever the plan says at its
    first pass.
    """
    from reckon.crew import runs as runs_module
    from reckon.crew.dispatch import queue_requests as dispatch_queue_requests

    with _readiness.project_read():
        stored = dispatch_queue_requests(
            nodes,
            project=project,
            session=session,
            repo_root=repo_root,
            config=config,
            backend_name=backend_name,
            launch_kind=launch_kind,
            run_ids=run_ids,
            cap=cap,
        )
    for run_id in stored:
        pointer = runs_module.read_pointer(run_id)
        if isinstance(pointer, dict):
            _reviews.record_review_contract(pointer)
    return stored
