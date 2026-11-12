"""Release waiters and advance requests whose upstream work has landed."""

from __future__ import annotations

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
    normalize_section,
    runs,
    time,
)


def _dead_sections(live: Sequence[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """The ``(plan, section)`` pairs no live request will ever deliver.

    A section whose own request is blocked on its own upstream will not deliver
    whatever waits behind it, and neither will one whose request is recorded
    failed or deliberately discarded. The set is what makes blocking
    transitive: the tail of a chain is blocked because the middle is, without
    the tail's ref having to name the original failure.
    """
    dead: set[tuple[str, str]] = set()
    for pointer in live:
        node = dict(pointer.get("node") or {})
        plan = str(node.get("plan") or pointer.get("plan") or "")
        section = str(node.get("section") or pointer.get("section") or "")
        if not plan or not section:
            continue
        phase = str(pointer.get("phase") or "")
        if phase in (BLOCKED_UPSTREAM_PHASE, *DEAD_RUN_PHASES) or _discarded(
            str(pointer.get("run_id") or "")
        ):
            dead.add((plan, normalize_section(section)))
    return dead


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
        pointer["upstream_obligations"] = list(dict.fromkeys(issues))
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
    pointers = list(live if live is not None else runs.list_live())
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


def advance_waiting_requests(live: list[dict[str, Any]]) -> dict[str, Any]:
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
        return _advance_waiting_requests(live)


def _advance_waiting_requests(live: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [
        pointer
        for pointer in live
        if str(pointer.get("phase") or "") in (*WAITING_UPSTREAM_PHASES, QUEUED_PHASE)
    ]
    if not candidates:
        return {"promoted": [], "blocked": [], "obligations": []}
    cache: dict[str, Any] = {}
    # One plan read per pass, shared by every request it derives for.
    derived_cache: dict[tuple[str, str, str, tuple[str, ...], bool], list[str]] = {}
    # One mount-registry read per pass, shared by every ref it resolves.
    mount = _wait_refs._mount_view()
    for pointer in candidates:
        try:
            _rederive_waits(pointer, cache=derived_cache, live=live, mount=mount)
        except Exception:  # noqa: BLE001, S112 - one unreadable request cannot end the pass
            continue
    promoted: list[str] = []
    blocked: list[str] = []
    obligations: dict[str, str] = {}
    for _round in range(len(candidates) + 1):
        dead = _dead_sections(live)
        changed = False
        for pointer in candidates:
            phase = str(pointer.get("phase") or "")
            if phase not in (*WAITING_UPSTREAM_PHASES, QUEUED_PHASE):
                continue
            docs_dir = Path(str(pointer.get("repo") or "") or ".") / "docs"
            previous_issues = list(pointer.get("upstream_obligations") or ())
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
            if issues:
                obligations[run_id] = issues[0]
            if issues != previous_issues:

                def record_issues(
                    current: dict[str, Any], recorded: list[str] = issues
                ) -> dict[str, Any]:
                    current["upstream_obligations"] = recorded
                    return current

                runs._mutate_pointer(run_id, record_issues)
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
        "obligations": [
            {"run_id": run_id, "detail": detail}
            for run_id, detail in obligations.items()
        ],
    }


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
