"""Hold and release requests against the review contract they read."""

from __future__ import annotations

from . import launching as _launching
from . import service as _service
from . import wait_refs as _wait_refs
from .shared import (
    AWAITING_UPSTREAM_PHASE,
    HELD_PHASE,
    PLAN_REVIEW_HELD_REASON,
    QUEUED_PHASE,
    REVIEW_CONTRACT_KEY,
    WAITING_UPSTREAM_PHASES,
    Any,
    Callable,
    ContextVar,
    Iterator,
    Mapping,
    Path,
    PlanReviewMissingError,
    Sequence,
    TaskNode,
    contextlib,
    contextmanager,
    datetime,
    hashlib,
    json,
    parse_utc,
    plan_review_units,
    runs,
)


class _PointerNotHoldableError(Exception):
    """A request that is a live run, not a waiting request, may not be held.

    Only a request still waiting for a slot — ``queued`` — or already waiting
    for its upstream — any of ``WAITING_UPSTREAM_PHASES`` — can be held. Every
    other phase names a run the fleet is working on: one being admitted holds a
    worktree, and one starting or working is a live worker. Writing a wait over
    such a pointer would take a running run off its lane and re-store it as a
    request nothing had launched, so the write is refused and the pointer is
    left exactly as it stands.
    """


def _hold_pointer(
    run_id: str,
    refs: Sequence[str],
    *,
    wait_projects: Mapping[str, str] | None = None,
    owner_project: str = "",
    live: Sequence[Mapping[str, Any]] = (),
) -> bool:
    """Record a request's refs and its waiting phase in one write.

    The refs and the phase are one fact about a request, so they are written
    together: a pointer left holding refs in the queued phase would be launched
    by the pass that never sees it waiting. A pointer that cannot be written
    stays queued and launches when the lane reaches it, which is the behaviour
    of a request whose plan declares no wait.

    A pointer carrying a run that is already admitted or working is not the
    record of a request at all, so nothing is written to it and the hold
    reports that it did not land. The phase is read inside the same mutation
    that writes, under the pointer's own lock, so a run admitted between the
    caller's decision and this write is refused too.

    Every ``run:`` ref written here carries the project its landing is read
    from, derived by the function the reader resolves a missing entry through,
    so the writer and the reader cannot disagree about where a wait lands.
    """
    projects = _wait_refs.wait_projects_for(
        refs, owner_project=owner_project, live=live
    )
    projects.update(dict(wait_projects or {}))

    def hold(current: dict[str, Any]) -> dict[str, Any]:
        if str(current.get("phase") or "") not in (
            QUEUED_PHASE,
            *WAITING_UPSTREAM_PHASES,
        ):
            raise _PointerNotHoldableError(run_id)
        current["waits"] = list(refs)
        current["wait_projects"] = dict(projects)
        current["phase"] = AWAITING_UPSTREAM_PHASE
        current["waits_at"] = _service._utc_now()
        return current

    try:
        runs._mutate_pointer(run_id, hold)
    except _PointerNotHoldableError:
        return False
    except Exception:  # noqa: BLE001 - an unwritable pointer stays where it is
        return False
    return True


def review_contract(node: TaskNode, *, repo_root: str | Path) -> dict[str, Any] | None:
    """The plan digests a queued request is reviewed against, taken as it queues.

    A request is gated on its own contract when it enters the queue: its
    section's text and the document text it reads as that contract — the
    summary, the decisions and the gates. Both digests are taken here, from the
    plan as the requesting session queued it, so a later edit is measured
    against what the request was dispatched for rather than against the whole
    plan's debt. ``None`` when the plan cannot be read: a request whose contract
    was never recorded is not held on a comparison it has no side of.
    """
    from reckon import _plan_html as plan_html
    from reckon.crew import plan_review, routing

    plan = str(node.plan or "").strip()
    if not plan:
        return None
    try:
        path = Path(str(repo_root)) / "docs" / "plans" / f"{plan}.html"
        state = plan_html.read_state_file(path)
        digests = plan_review._section_digests(path)
    except Exception:  # noqa: BLE001 - an unreadable plan records no contract
        return None
    unit = ""
    if str(node.section or "").strip():
        from reckon._plan_html import section_record_id

        unit = section_record_id(node.section)
    return {
        "section": unit,
        "section_digest": str(digests.get(unit) or ""),
        "document_digest": routing._contract_digest(state),
        "recorded_at": _service._utc_now(),
    }


def _contract_is_current(contract: Mapping[str, Any], node: TaskNode) -> bool:
    """Whether a recorded contract still describes the section now queued.

    ``crew queue edit`` changes a request's section, and the digests recorded
    for the old section say nothing about the new one, so such a request is
    stamped again from the plan rather than held on a comparison of two
    different sections.
    """
    from reckon._plan_html import section_record_id

    recorded = str(contract.get("section") or "")
    present = section_record_id(node.section) if str(node.section or "").strip() else ""
    return recorded == present


def record_review_contract(pointer: Mapping[str, Any]) -> bool:
    """Stamp one queued pointer with the contract it was queued against.

    The write happens under the pointer's own lock and only while the request
    still waits, so a pointer that has been launched is left alone. A pointer
    that cannot be written keeps no contract and is not held on one.
    """
    if not isinstance(pointer, dict):
        return False
    run_id = str(pointer.get("run_id") or "")
    if not run_id:
        return False
    node = _launching._node_from_pointer(pointer)
    if not node.plan.strip():
        return False
    contract = review_contract(node, repo_root=str(pointer.get("repo") or ""))
    if contract is None:
        return False

    def stamp(current: dict[str, Any]) -> dict[str, Any]:
        if str(current.get("phase") or "") not in (
            QUEUED_PHASE,
            *WAITING_UPSTREAM_PHASES,
        ):
            raise _PointerNotHoldableError(run_id)
        current[REVIEW_CONTRACT_KEY] = contract
        return current

    try:
        runs._mutate_pointer(run_id, stamp)
    except Exception:  # noqa: BLE001 - an unwritten contract holds nothing
        return False
    pointer[REVIEW_CONTRACT_KEY] = contract
    return True


def _move_pointer(run_id: str, phase: str) -> bool:
    """Write one request's new phase, reporting whether the write landed.

    A pointer that cannot be written stays where it is and is attempted again
    on the next pass; the pass never treats an unwritten move as done, so a
    request is not launched from a phase the store never recorded.
    """

    def move(current: dict[str, Any]) -> dict[str, Any]:
        current["phase"] = phase
        current["waits_at"] = _service._utc_now()
        return current

    try:
        runs._mutate_pointer(run_id, move)
    except Exception:  # noqa: BLE001 - an unwritable pointer stays where it is
        return False
    return True


#: The plan evidence one admission pass has read, keyed by the plan text and the
#: stored reviews that judgment reads. A pass judges the review gate for every
#: request it considers, and each judgment re-read the same plan four times. The
#: pass holds the answer while neither the plan nor its review store has moved.
_PLAN_REVIEW_PASS: ContextVar[dict[tuple[Any, ...], _PlanReviewRecord] | None] = (
    ContextVar("queue_admission_plan_review_pass", default=None)
)


@contextmanager
def plan_review_pass() -> Iterator[None]:
    """Read each plan's review evidence once for the length of one pass.

    Entered by an admission pass, which judges the review gate for every request
    it considers. Outside such a pass each judgment reads the plan itself, so a
    direct dispatch and a coordinator's own read always see the plan as it is
    now.
    """

    token = _PLAN_REVIEW_PASS.set({})
    try:
        yield
    finally:
        _PLAN_REVIEW_PASS.reset(token)


class _PlanReviewRecord:
    """What one pass read once about one plan's review evidence.

    The coverage, the section digests and the parsed plan state are read when
    the record is built. The document-contract verdict reads a review snapshot
    of its own, so it is taken only when a request actually needs it and held
    from then on: a plan whose document unit no request reads costs no snapshot
    read at all. The dispatch gate's answer is memoized here too, keyed by the
    section a request builds and by whether the request is exempt from the gate,
    because those are the whole of what the gate's verdict turns on for one
    plan.
    """

    def __init__(
        self,
        *,
        project: str,
        plan_slug: str,
        path: Path,
        records: Sequence[Mapping[str, Any]],
        uncovered: set[str],
        changes: Mapping[str, float | None],
        digests: Mapping[str, str],
        state: Mapping[str, Any],
    ) -> None:
        self.project = project
        self.plan_slug = plan_slug
        self.path = path
        self.records = records
        self.uncovered = uncovered
        self.changes = changes
        self.digests = digests
        self.state = state
        self.gate: dict[tuple[str, bool], str | None] = {}
        self._document_is_contract: bool | None = None

    def documents_change_is_contract(self) -> bool:
        """Whether the plan's document unit changed in the text a node reads."""
        if self._document_is_contract is None:
            from reckon.crew import routing

            self._document_is_contract = routing._document_change_is_contract(
                self.project, self.plan_slug, plan=self.path, records=self.records
            )
        return self._document_is_contract

    def gate_owed(
        self,
        node: TaskNode,
        *,
        repo: str,
        options: Mapping[str, Any],
    ) -> str | None:
        """The dispatch gate's verdict, read once per section and exemption.

        The gate's verdict turns on the plan and on the units the request reads,
        and the queue holds one request per node, so a key carrying the
        request's own identity would never be reused. Keyed on the section it
        builds and on whether the request is exempt from the gate at all, two
        implement requests on one section share one read while a review
        request's exempt verdict is never handed to a building one.
        """
        from reckon.crew import routing

        key = (str(node.section or ""), routing._plan_review_exempt(node))
        if key not in self.gate:
            self.gate[key] = _dispatch_gate_owed(
                node, project=self.project, repo=repo, options=options
            )
        return self.gate[key]


def _plan_text_digest(path: Path) -> str:
    """The content identity of one plan file, or ``""`` when it is unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _stored_review_key(project: str, plan_slug: str) -> tuple[tuple[str, ...], ...]:
    """The identities and digests of the stored reviews a plan's judgment reads."""
    from reckon.crew import plan_review

    entries: list[tuple[str, ...]] = []
    for record in plan_review.list_plan_reviews(project, plan_slug):
        digests = record.get("section_digests") or {}
        entries.append(
            (
                plan_review._plan_review_identity(record)
                or str(record.get("review_path") or ""),
                str(record.get("plan_version") or ""),
                str(record.get("plan_fingerprint") or ""),
                str(record.get("reviewed_blob_sha") or ""),
                str(record.get("status") or ""),
                hashlib.sha256(
                    json.dumps(digests, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
            )
        )
    return tuple(sorted(entries))


def _build_plan_review_record(
    path: Path, *, project: str, plan_slug: str
) -> _PlanReviewRecord:
    """Read one plan's review coverage, digests and state for a pass."""
    from reckon import _plan_html as plan_html
    from reckon.crew import plan_review

    document = path.read_text(encoding="utf-8", errors="replace")
    records, uncovered, changes = plan_review.review_coverage(
        project, plan_slug, plan=document
    )
    return _PlanReviewRecord(
        project=project,
        plan_slug=plan_slug,
        path=path,
        records=records,
        uncovered=uncovered,
        changes=changes,
        digests=plan_review._section_digests(document),
        state=plan_html.read_state_file(path),
    )


def _plan_review_record(
    path: Path, *, project: str, plan_slug: str
) -> _PlanReviewRecord:
    """The plan's review evidence for this pass, read once and reused.

    Outside a pass nothing is held, so the record is built for this caller
    alone. Inside one it is keyed by the plan's content and by the stored
    reviews it reads: a plan edited, a review stored or a review rewritten
    during the pass changes the key, so the next decision on that plan is taken
    against what the plan and its store say now rather than against a reading
    the pass has outlived.
    """
    cache = _PLAN_REVIEW_PASS.get()
    if cache is None:
        return _build_plan_review_record(path, project=project, plan_slug=plan_slug)
    key = (str(path), _plan_text_digest(path), _stored_review_key(project, plan_slug))
    record = cache.get(key)
    if record is None:
        record = _build_plan_review_record(path, project=project, plan_slug=plan_slug)
        cache[key] = record
    return record


def _dispatch_gate_owed(
    node: TaskNode,
    *,
    project: str,
    repo: str,
    options: Mapping[str, Any],
) -> str | None:
    """The dispatch gate's verdict for a request that recorded no contract."""
    from reckon.crew import routing

    try:
        authority = routing.resolve_dispatch_authority(project, repo)
        routing.require_plan_reviewed(
            node=node,
            project=project,
            repo=repo,
            authority=authority,
            allow_unreviewed=bool(options.get("allow_unreviewed_plan")),
            enforce=True,
            units=plan_review_units(node.section),
        )
    except PlanReviewMissingError as exc:
        return str(exc)
    except Exception:  # noqa: BLE001 - an unreadable authority holds nothing
        return None
    return None


def _plan_review_owed(
    pointer: Mapping[str, Any],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> str | None:
    """Why this request's own contract owes a stored review, or ``None``.

    A request is checked for review when it enters the queue, and the dispatch
    gate has already judged it there. What admission owes is the narrower
    question the queue raises: has the plan text this request was queued
    against moved since, and does no stored review cover the text it moved to?
    Only the request's own section and the document text it reads as its
    contract — the summary, the decisions and the gates — are compared, so an
    edit to another section, an ordering or bookkeeping edit, and a contract
    change a review already covers all leave the request admissible. A fresh
    dispatch of the same section and this request therefore reach the same
    verdict on the same plan.

    Two states carry no comparison of their own, and both are answered by the
    dispatch gate rather than by holding nothing. A pointer with no contract
    current for the section it now names — every request written by the queue
    writer that records none, and every request whose section ``crew queue
    edit`` changed — has no second side of a digest comparison. A plan whose
    stored reviews cover no unit the request reads — an unreviewed plan, or one
    whose every review is incomplete — has no reviewed text to compare against
    and is owed the review the gate asks for. Each is held exactly when the
    gate holds it, so the two never disagree.

    Only the enforced mode holds, exactly as the dispatch gate does; under the
    shipped report-only default the launch proceeds.
    """
    from reckon import flight
    from reckon.crew import plan_review, routing

    node = _launching._node_from_pointer(pointer)
    project = str(pointer.get("project") or "")
    repo = str(pointer.get("repo") or "")
    if not node.plan.strip() or not project or not repo:
        return None
    config = (
        config_for(pointer)
        if config_for is not None
        else _launching._resolve_lane_config(pointer)
    )
    if not flight.plan_review_gate_enforces(config):
        return None
    options = dict(pointer.get("dispatch_options") or {})
    if options.get("allow_unreviewed_plan"):
        # A request dispatched with the waiver carries it into the queue: its
        # coordinator has answered the review question for that dispatch.
        return None
    contract = pointer.get(REVIEW_CONTRACT_KEY)
    path = Path(repo) / "docs" / "plans" / f"{node.plan}.html"
    if not isinstance(contract, Mapping) or not _contract_is_current(contract, node):
        # A request queued by a writer that records no contract is judged by the
        # dispatch gate itself, which is what admission asked before requests
        # carried their own: it holds on every unit the request reads. Narrowing
        # that to the request's own contract needs the digests taken at queue
        # time, so a pointer without them keeps the wider hold rather than
        # holding nothing. The gate's answer is read through the same per-plan
        # record, so a pass that holds several such requests over one plan asks
        # the gate once.
        try:
            evidence = _plan_review_record(path, project=project, plan_slug=node.plan)
        except Exception:  # noqa: BLE001 - an unreadable plan answers through the gate
            return _dispatch_gate_owed(
                node, project=project, repo=repo, options=options
            )
        return evidence.gate_owed(node, repo=repo, options=options)
    try:
        evidence = _plan_review_record(path, project=project, plan_slug=node.plan)
        units = plan_review_units(node.section)
        if not any(unit not in evidence.uncovered for unit in units):
            # No stored review covers a single unit this request reads — the
            # plan is unreviewed, or every review is incomplete — so there is no
            # reviewed text to compare against and the request is owed the
            # review the dispatch gate asks for. Answering here with the gate's
            # own verdict is what keeps a queued request and a fresh dispatch
            # from disagreeing.
            return evidence.gate_owed(node, repo=repo, options=options)
        digests = evidence.digests
        moved: list[str] = []
        unit = str(contract.get("section") or "")
        if (
            unit
            and unit in evidence.uncovered
            and str(digests.get(unit) or "") != str(contract.get("section_digest"))
        ):
            moved.append(unit)
        state = evidence.state
        if (
            plan_review.DOCUMENT_UNIT in evidence.uncovered
            and routing._contract_digest(state)
            != str(contract.get("document_digest") or "")
            and evidence.documents_change_is_contract()
        ):
            moved.append(plan_review.DOCUMENT_UNIT)
    except Exception:  # noqa: BLE001 - an unreadable plan holds nothing
        return None
    if not moved:
        return None
    return (
        f"plan {node.plan!r} in project {project!r} changed after this request "
        "was queued and no stored review covers the new text; "
        f"uncovered units: {', '.join(sorted(moved))}; "
        f"{routing._uncovered_change_detail(moved, evidence.changes)}"
        "the request waits until a review of that text is stored; "
        f"compose one with `{plan_review.review_invocation(project, node.plan)}`"
    )


def _hold_for_plan_review(pointer: Mapping[str, Any], detail: str) -> bool:
    """Move a request the gate holds to ``held``, recording why and its remedy.

    The hold is marked automatic so that the pass which finds the review stored
    releases it, while a hold a coordinator wrote is left alone whatever the
    plan's review and this appointment's review have in common.
    """
    run_id = str(pointer.get("run_id") or "")
    started = _service._utc_now()

    def hold(current: dict[str, Any]) -> dict[str, Any]:
        if str(current.get("phase") or "") not in (
            QUEUED_PHASE,
            *WAITING_UPSTREAM_PHASES,
        ):
            raise _PointerNotHoldableError(run_id)
        current["queue_hold"] = {
            "prior_phase": str(current.get("phase") or QUEUED_PHASE),
            "started_at": started,
            "reason": PLAN_REVIEW_HELD_REASON,
            "detail": detail,
            "automatic": True,
        }
        current["phase"] = HELD_PHASE
        return current

    try:
        runs._mutate_pointer(run_id, hold)
    except _PointerNotHoldableError:
        return False
    except Exception:  # noqa: BLE001 - an unwritable pointer stays where it is
        return False
    if isinstance(pointer, dict):
        pointer["phase"] = HELD_PHASE
    return True


def _section_rederivation(
    pointer: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Each section-derived field as the section states it now, with both digests.

    A request keeps a field the dispatcher supplied and a field ``crew queue
    edit`` set; only the fields its section supplied are re-derived, and the
    mark the queue writer left on the stored node is what names them. The plan
    is read from the checkout the release launches against, which is the text
    the request will run under, so a request released onto a changed section
    carries the contract of the text it launches with rather than the one it
    was queued against.

    A field the section no longer supplies keeps its stored value: releasing a
    request the plan has moved past is what the review it awaited was for, and
    dropping a required field here would refuse a launch the queue has already
    admitted.

    A plan or section that cannot be read raises. The stored fields are not a
    fallback: releasing on them would put the request back on the text the plan
    has left behind, so the caller refuses the release and records why.
    """
    from reckon.crew import dispatch_batch

    marks = dict((pointer.get("node") or {}).get("section_derived") or {})
    if not marks:
        # A request whose fields the dispatcher supplied has nothing to
        # re-derive, and the rebuild is skipped so an unmarked request cannot
        # raise here.
        return {}, {}
    node = _launching._node_from_pointer(pointer)
    if not node.plan.strip() or not node.section.strip():
        return {}, {}
    return dispatch_batch.rederived_section_fields(
        project=str(pointer.get("project") or ""),
        plan=node.plan,
        section=node.section,
        repo_root=str(pointer.get("repo") or ""),
        marked=marks,
        stored=dict(pointer.get("node") or {}),
    )


def _apply_section_rederivation(
    pointer: dict[str, Any],
    values: Mapping[str, Any],
    digests: Mapping[str, Mapping[str, str]],
) -> None:
    """Write the re-derived fields onto a pointer, marking them current.

    The fields stay marked as the section's: they were re-derived from it, so a
    later release re-derives them again from text it moved to since.

    A field the pointer no longer marks is skipped. ``crew queue edit`` clears
    the mark on every field it changes, and that clearing is what makes the
    field the coordinator's: writing a re-derived value onto it would restore
    the section's text over the edit and re-mark the field, so every later
    release would do it again.
    """
    node_block = dict(pointer.get("node") or {})
    marks = dict(node_block.get("section_derived") or {})
    applied: set[str] = set()
    for name, value in values.items():
        if name not in marks:
            continue
        node_block[name] = value
        marks[name] = digests[name]["new"]
        applied.add(name)
    node_block["section_derived"] = marks
    pointer["node"] = node_block
    changed = {
        name: dict(pair)
        for name, pair in digests.items()
        if pair["old"] != pair["new"] and name in applied
    }
    if changed:
        pointer["section_rederivation"] = changed


def _record_rederivation_refusal(
    pointer: Mapping[str, Any], run_id: str, detail: str
) -> None:
    """Keep a request held when its section cannot be read, and say why.

    The stored fields are not a fallback for a re-derivation that failed: they
    describe the text the plan has moved past, which is the disagreement the
    re-derivation exists to remove. So the release is refused, the request
    stays held, and the reason goes into the hold the request is already under
    — the record the plan-review hold owns — where the next reader of the queue
    finds it. Releasing removes that hold, so the reason goes with it.
    """
    refused_at = _service._now().isoformat()
    refused = (
        "the section this request was queued under cannot be read, so its "
        f"section-supplied fields cannot be brought to the current text: {detail}"
    )

    def refuse(current: dict[str, Any]) -> dict[str, Any]:
        hold = dict(current.get("queue_hold") or {})
        hold["detail"] = refused
        hold["rederivation_refused_at"] = refused_at
        current["queue_hold"] = hold
        return current

    with contextlib.suppress(Exception):
        runs._mutate_pointer(run_id, refuse)
    if isinstance(pointer, dict):
        hold = dict(pointer.get("queue_hold") or {})
        hold["detail"] = refused
        hold["rederivation_refused_at"] = refused_at
        pointer["queue_hold"] = hold


class _RederivationRefusedError(Exception):
    """A release whose section cannot be read, carrying the refusal's reason."""


def _release_plan_review_holds(
    live: Sequence[Mapping[str, Any]],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> list[str]:
    """Release every request this path held whose units are now reviewed.

    The pass asks the gate again rather than trusting a record of the hold, so
    a review stored since the last pass releases its requests with no
    coordinator call. A hold a coordinator wrote carries no ``automatic`` mark
    and is never released here, and a request so held stays held.
    """
    released: list[str] = []
    for pointer in live:
        if str(pointer.get("phase") or "") != HELD_PHASE:
            continue
        hold = pointer.get("queue_hold")
        if not isinstance(hold, Mapping) or not hold.get("automatic"):
            continue
        if str(hold.get("reason") or "") != PLAN_REVIEW_HELD_REASON:
            continue
        if _plan_review_owed(pointer, config_for=config_for) is not None:
            continue
        prior = str(hold.get("prior_phase") or "")
        if prior not in (QUEUED_PHASE, *WAITING_UPSTREAM_PHASES):
            continue
        run_id = str(pointer.get("run_id") or "")
        ended = _service._now()

        def release(
            current: dict[str, Any],
            prior_phase: str = prior,
            released_at: datetime = ended,
        ) -> dict[str, Any]:
            # The hold was taken because the section moved; the release launches
            # against the text it moved to, so the fields it supplied are
            # re-derived here and the launched node is the current contract
            # rather than the one the request was queued under. It is derived
            # from the pointer as it stands now, under the pointer's own lock:
            # the pass's snapshot was taken before a coordinator's ``crew queue
            # edit`` may have changed a field, and deriving from it writes the
            # section's text back over that edit.
            try:
                rederived, digests = _section_rederivation(current)
            except Exception as exc:  # one request's section cannot
                # stop the pass for every other request and project, so a
                # failure here holds this request and records why.
                raise _RederivationRefusedError(f"{type(exc).__name__}: {exc}") from exc
            started = parse_utc((current.get("queue_hold") or {}).get("started_at"))
            queued_at = parse_utc(current.get("queued_at"))
            if started is not None and queued_at is not None:
                current["queued_at"] = (queued_at + (released_at - started)).isoformat()
            if rederived:
                _apply_section_rederivation(current, rederived, digests)
            current["phase"] = prior_phase
            current.pop("queue_hold", None)
            current["queue_released_at"] = released_at.isoformat()
            return current

        try:
            written = runs._mutate_pointer(run_id, release)
        except _RederivationRefusedError as exc:
            _record_rederivation_refusal(pointer, run_id, str(exc))
            continue
        except Exception:  # noqa: BLE001, S112 - an unwritable pointer stays held
            continue
        if isinstance(pointer, dict):
            # The pass launches from this pass's in-memory records, so it takes
            # the node the release actually wrote rather than the one its
            # snapshot carried.
            pointer["node"] = dict(written.get("node") or pointer.get("node") or {})
            pointer["phase"] = prior
            pointer.pop("queue_hold", None)
            recorded = written.get("section_rederivation")
            if isinstance(recorded, Mapping) and recorded:
                pointer["section_rederivation"] = dict(recorded)
        released.append(run_id)
    return released
