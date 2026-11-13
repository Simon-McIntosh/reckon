"""Rebuild, launch and record queued requests without losing their identity."""

from __future__ import annotations

from . import holds as _holds
from . import service as _service
from . import verdicts as _verdicts
from .shared import (
    _CARRIED_OPTION_KEYS,
    ADMISSION_HISTORY_KEY,
    HELD_PHASE,
    LAUNCH_FAILED_PHASE,
    MISSING,
    QUEUED_LIFE_KEYS,
    RECORDED_HOLD_FIELD,
    Any,
    Callable,
    CrewError,
    Mapping,
    Path,
    ScopeConflict,
    Sequence,
    TaskNode,
    contextlib,
    copy,
    datetime,
    fields,
    json,
    plan_review_units,
    runs,
    time,
)


def _inside_run_directory(value: str, directory: Path) -> bool:
    """Whether a declared path lies inside one run's own directory.

    The queued run's id is minted at dispatch, so no coordinator could have
    named a path inside its directory: a path there is a default the plan
    derived for the queued run, and it must not follow the request onto the
    launched run's id. Only an absolute path can be inside the run directory —
    a relative path resolves against the repository, never the run directory.
    """
    if not value:
        return False
    path = Path(value).expanduser()
    if not path.is_absolute():
        return False
    return path.resolve().is_relative_to(directory.resolve())


def _stored_field_value(spec: Any, value: Any) -> Any:
    """Return one stored field value in the shape ``TaskNode`` declares for it.

    The stored node is JSON, so a declared collection arrives as a list or a
    dict and a declared text field as a string. The copy is deep, so neither the
    rebuilt node nor anything nested in it reaches the pointer record it was
    read out of.
    """
    if spec.default_factory is not MISSING:
        empty = spec.default_factory()
        if isinstance(empty, list):
            return copy.deepcopy(list(value or ()))
        if isinstance(empty, dict):
            return copy.deepcopy(dict(value or {}))
    if spec.type == "str":
        return str(value)
    return value


def _node_from_pointer(pointer: Mapping[str, Any]) -> TaskNode:
    """Rebuild a queued request's node from the pointer it was stored as.

    Every field ``TaskNode`` declares is carried, read from the dataclass's own
    field list. The queued launch never carries the built node, so a field this
    rebuild does not name is a field the launched worker never sees, and a list
    written by hand drifts from the dataclass the first time a field is added.
    A field the stored node lacks takes the dataclass default.

    Two deliberate rewrites remain: a manifest path or a write path a previous
    dispatch defaulted into the queued run's own directory is dropped, so the
    launched run's plan derives those defaults afresh from its own id. Every
    other path — a ``--manifest`` the coordinator named elsewhere, a declared
    write path, the brief the run reads — is carried over unchanged.
    """
    stored = dict(pointer.get("node") or {})
    queued_dir = runs.run_dir(str(pointer["run_id"]))
    manifest_path = str(stored.get("manifest_path") or "")
    if _inside_run_directory(manifest_path, queued_dir):
        manifest_path = ""
    rewritten: dict[str, Any] = {
        "manifest_path": manifest_path,
        "write_paths": [
            str(path)
            for path in (stored.get("write_paths") or ())
            if not _inside_run_directory(str(path), queued_dir)
        ],
    }

    values: dict[str, Any] = {}
    for spec in fields(TaskNode):
        if spec.name in rewritten:
            values[spec.name] = rewritten[spec.name]
            continue
        stored_value = stored.get(spec.name)
        if stored_value is None:
            continue
        value = _stored_field_value(spec, stored_value)
        # An empty string is a stored field that was never filled, so a field
        # whose dataclass default is a non-empty string keeps that default
        # rather than being launched with nothing where the default belongs.
        # Only a real non-empty string default qualifies: ``MISSING``, the
        # default of a field that has none, is truthy, and a stored empty
        # string there is the value the run was dispatched with. A brief-carried
        # review stores ``plan=""`` and must rebuild with it.
        if (
            value == ""
            and spec.default_factory is MISSING
            and isinstance(spec.default, str)
            and spec.default
        ):
            continue
        values[spec.name] = value
    return TaskNode(**values)


def _resolve_lane_config(
    pointer: Mapping[str, Any], *, select_local: bool = True
) -> Mapping[str, Any]:
    """Resolve the request's project config, selecting its local lane when needed."""
    from reckon import flight as flight_module
    from reckon.crew_dispatch_commands import _resolved_flight

    config = _resolved_flight(
        flight_module,
        str(pointer["project"]),
        pointer.get("repo"),
        runs.recorded_dispatch_overrides(pointer),
    )
    if select_local and pointer.get("local"):
        config = flight_module.select_local_backend(config)
    return config


def _steering_brief(pointer: Mapping[str, Any]) -> Path | None:
    """The launch-time authority text a queued request carries, if any.

    ``steering.md`` in the request's own run directory is what the launch
    reads, not the brief text the queued pointer carries: the file is where a
    coordinator replaces the node's authority while the request waits. It is
    contract-checked here, inside the admission path and before the launch
    composes anything, so a file that no longer reads as authority text is
    refused and the request returns to the queue with the failure named.
    """
    path = runs.run_dir(str(pointer["run_id"])) / runs.STEERING_FILE
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CrewError(f"the steering file {path} is not readable: {exc}") from exc
    if not text.strip():
        raise CrewError(f"the steering file {path} carries no authority text")
    return path


def _attempt_with_a_result(run_id: str) -> str | None:
    """Name an attempt of this run that ended having produced something.

    A run directory keeps one immutable exit record per attempt. A launch that
    never reached a model writes one too, and that attempt has no result: the
    queue's own retry is what follows it, so it does not refuse a relaunch. Any
    other exit record says a worker ran and its attempt ended, so launching the
    request again under the same run id would start a second attempt over the
    account of the first and overwrite whatever that attempt delivered.
    """
    from reckon.crew.recovery_liveness import _exit_record_is_launch_failure

    try:
        paths = sorted(runs.run_dir(run_id).glob("attempt-*-exit.json"))
    except OSError:
        return None
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, Mapping):
            continue
        recorded = str(payload.get("run_id") or "")
        if recorded and recorded != run_id:
            continue
        if _exit_record_is_launch_failure(payload):
            continue
        return (
            f"an attempt of the run already exited with a recorded result ({path.name})"
        )
    return None


def _launch_reread_refusal(pointer: Mapping[str, Any]) -> str | None:
    """Why this request must not be launched now, or ``None`` when it may be.

    A pass ranks the queued pointers it read at its start and then works
    through the queue, so a request can move while the pass is busy with the
    others: a coordinator may hold, promote, discard or stop it, and a
    supervisor may end an attempt of it that already ran. Launching from the
    pass's opening read would then start a second attempt on a request that no
    longer waits, overwriting an attempt's manifest, so the pointer and the
    run's attempt records are re-read together here, under the same per-run
    lock every pointer writer takes.
    """
    run_id = str(pointer.get("run_id") or "")
    if not run_id:
        return None
    with runs._pointer_lock(run_id):
        try:
            text = runs.pointer_path(run_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return (
                "the request's live pointer was removed while the pass waited on "
                "it (promoted, discarded or stopped)"
            )
        except OSError as exc:
            return f"the request's live pointer cannot be read: {exc}"
        try:
            current = json.loads(text)
        except ValueError:
            current = None
        phase = str(current.get("phase") or "") if isinstance(current, Mapping) else ""
        if phase == HELD_PHASE:
            hold = current.get("queue_hold")
            reason = (
                str(hold.get("reason") or "").strip()
                if isinstance(hold, Mapping)
                else ""
            )
            return "the request was held while the pass waited on it" + (
                f": {reason}" if reason else ""
            )
        return _attempt_with_a_result(run_id)


def _launch_queued(
    pointer: Mapping[str, Any],
    *,
    launcher: Callable[..., int] | None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    backend_override: str = "",
    readiness_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Launch one queued run through the shared dispatch path a coordinator uses.

    ``backend_override`` names the backend this request launches on instead of
    the one it was queued for: a rebind of a request that never launched, so the
    request is re-composed from the recorded node and its recorded dispatch
    options (its waivers among them) rather than the backend it originally named.

    ``readiness_cache`` is the pass's own, handed to dispatch so this launch
    reads the readiness an earlier launch in the same pass already derived.

    Every launch of a queued request goes through here, whatever chose it — a
    pass over the queue, a dispatch yielding its lane slot to an older request,
    or a request moved onto another backend — so the join of a launched plan
    review to its composed directory is taken here rather than after any one of
    them. A launch path that reached the dispatch another way would leave the
    sidecar naming no run, and the delivered review would never be stored.
    """
    from reckon.crew.dispatch import dispatch

    options = dict(pointer.get("dispatch_options") or {})
    node = _node_from_pointer(pointer)
    # The request's steering file is the authority the worker reads, so the
    # node is pointed at it in place of the brief the queue-time pointer
    # carried. A request whose authority is a plan section alone has no such
    # file and launches from its section as it always has.
    steering = _steering_brief(pointer)
    if steering is not None:
        node.brief = str(steering)
        node.brief_path = str(steering)
        node.brief_sha256 = ""
    # Keep the request visible while dispatch checks its own identity and
    # other claims. A refused launch leaves the same pointer for dependents.
    # An operator may discard the request this pass is holding. A later launch
    # failure can remove its run directory, including a discard marker, so copy
    # that marker to the shared spool before handing the request to dispatch.
    from reckon.crew.promotion import preserve_discard_record

    preserve_discard_record(str(pointer["run_id"]))
    config = (
        config_for(pointer) if config_for is not None else _resolve_lane_config(pointer)
    )
    destination = str(backend_override).strip()
    selection = runs.dispatch_field(pointer, "picker_selection")
    replay_pick = (
        isinstance(selection, Mapping)
        and selection.get("action") == "route"
        and not destination
        and not pointer.get("named_route")
        and not pointer.get("route_fallback")
    )
    # The launch continues the request it was admitted from, so it keeps that
    # request's run id: the id a dispatch printed is the one a coordinator
    # records and later acts on, and a fresh id here would leave the printed one
    # naming nothing the moment the lane frees.
    from reckon.crew import routing

    # The pass has already judged the gate over the units this request reads;
    # the launch publishes them so the same gate inside dispatch asks the same
    # question instead of the whole plan's. A direct dispatch publishes nothing
    # and keeps the whole-plan check.
    units_token = routing.ADMISSION_REVIEW_UNITS.set(plan_review_units(node.section))
    try:
        launched = dispatch(
            node=node,
            run_id=str(pointer["run_id"]),
            project=str(pointer["project"]),
            repo=pointer.get("repo"),
            config=config,
            session=str(pointer["session"]),
            wave=str(options.get("wave") or ""),
            base=str(options.get("base") or "HEAD"),
            locked_decisions=tuple(options.get("locked_decisions") or ()),
            peer_scopes=node.peer_scopes,
            member=str(options.get("member") or ""),
            launcher=launcher,
            execution_override=bool(options.get("allow_execution_mismatch")),
            orchestrator_lane_reason=options.get("orchestrator_lane_reason") or None,
            # The queue exists so that work runs when a lane frees, whether or
            # not its coordinator is awake: an admission launch is the request
            # continuing, so it never re-applies its session's terminal backlog
            # at launch. Dispatch skips computing and recording that backlog.
            unreconciled_override=True,
            unreviewed_plan_override=bool(options.get("allow_unreviewed_plan")),
            watch_required=True,
            watch_override=bool(options.get("no_watch")),
            local=bool(pointer.get("local")) and not destination,
            backend_override=destination or str(pointer.get("backend") or ""),
            repairs=str(options.get("repairs") or ""),
            accept_directory_claim=bool(options.get("accept_directory_claim")),
            no_fence_reason=str(options.get("no_fence_reason") or ""),
            # A stored picker answer is supplied to dispatch, so the model is
            # resolved from that choice without asking the picker again. An
            # edited route or a recorded fallback stays on the backend it
            # already names.
            route="picker" if replay_pick else "deterministic",
            picker_selection=selection,
            comment=str(options.get("comment") or ""),
            dispatch_options=options,
            admission_launch=True,
            admitted_from=str(pointer["run_id"]),
            readiness_cache=readiness_cache,
        )
    finally:
        routing.ADMISSION_REVIEW_UNITS.reset(units_token)
    # The join resolves only for a record the launch check accepts: a dispatch
    # that returned a request still queued names no run that ran the review.
    # A sidecar that cannot be written must not report the launch as refused
    # either, because every caller writes the request back when this raises,
    # which would queue a second copy of a request that did launch.
    try:
        _require_launched_record(launched)
    except CrewError:
        pass
    else:
        with contextlib.suppress(OSError):
            _join_admitted_plan_review(pointer, str(launched["run_id"]))
        _carry_section_rederivation(pointer, str(launched["run_id"]))
        _announce_submission(pointer, launched, steering=steering, options=options)
    return launched


def _announce_submission(
    pointer: Mapping[str, Any],
    launched: Mapping[str, Any],
    *,
    steering: Path | None,
    options: Mapping[str, Any],
) -> None:
    """Tell the coordinating session its request was submitted, in this pass.

    The coordinator queued the request possibly hours earlier and cannot see
    the moment it starts, so the row is written here rather than left to the
    next tick's announcement. The advice is the text the worker was launched
    with: its steering file where one exists, otherwise the comment the
    request was queued with. A row that cannot be written costs the
    announcement, never the launch that already happened.
    """
    from reckon.crew import watch_unit

    advice = ""
    if steering is not None:
        with contextlib.suppress(OSError, ValueError):
            advice = steering.read_text(encoding="utf-8")
    if not advice:
        advice = str(options.get("comment") or "")
    watch_unit.announce_submission(pointer, launched, advice=advice)


def _carry_section_rederivation(pointer: Mapping[str, Any], run_id: str) -> None:
    """Keep a release's re-derivation record on the run it launched.

    A launch publishes a fresh record built from the node it launched, so the
    record the release wrote on the request is not part of it. The digests are
    written beside the launched node here: the node carries the re-derived
    values, and this says which fields the section supplied and what they were
    before the release.
    """
    recorded = pointer.get("section_rederivation")
    if not isinstance(recorded, Mapping) or not recorded:
        return

    def carry(current: dict[str, Any]) -> dict[str, Any]:
        current["section_rederivation"] = dict(recorded)
        return current

    with contextlib.suppress(Exception):
        runs._mutate_pointer(run_id, carry)


def carried_options(pointer: Mapping[str, Any]) -> dict[str, Any]:
    """The recorded dispatch options a lane change copies onto the new launch."""
    options = dict(pointer.get("dispatch_options") or {})
    return {key: options[key] for key in _CARRIED_OPTION_KEYS if key in options}


def move_queued_run(
    run_id: str,
    backend_name: str,
    *,
    reason: str = "",
    config: Mapping[str, Any] | None = None,
    launch: bool = True,
    launcher: Callable[..., int] | None = None,
) -> dict[str, Any]:
    """Move a queued request that never launched onto another backend.

    A queued request has no prompt and no worktree — its first attempt has not
    started — so the move re-composes its launch from the recorded node through
    the admission pass's own path rather than composing a fresh prompt. That
    recomposition forwards the request's recorded dispatch options, so the
    waivers the original dispatch answered are carried into the new launch, and
    the request is released only inside it, so the move never races the
    admission pass with a discard of a request that is still waiting.
    """
    record = runs.read_pointer(run_id)
    if str(record.get("phase") or "") != "queued":
        raise CrewError(f"run {run_id!r} is not a queued request to move")
    destination = str(backend_name).strip()
    if not destination:
        raise CrewError("changing a run's backend requires a destination backend")
    config_for = None if config is None else (lambda _pointer: config)
    if not launch:
        return {
            "run_id": run_id,
            "node": record.get("node"),
            "backend": destination,
            "launch": destination,
            "reason": str(reason),
            "carried": carried_options(record),
        }
    moved = _await_moved_launch(
        record,
        snapshot=copy.deepcopy(dict(record)),
        destination=destination,
        launcher=launcher,
        config_for=config_for,
    )
    carried = carried_options(record)
    moved_run_id = str(moved["run_id"])

    def stamp(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["carried_options"] = carried
        pointer["lane_move"] = {
            "from_backend": str(record.get("backend") or ""),
            "to_backend": destination,
            "reason": str(reason),
        }
        return pointer

    return runs._mutate_pointer(moved_run_id, stamp)


def _await_moved_launch(
    record: Mapping[str, Any],
    *,
    snapshot: dict[str, Any],
    destination: str,
    launcher: Callable[..., int] | None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> Mapping[str, Any]:
    """Launch a moved queued request, restoring it when the launch does not land.

    The launch releases the request as it launches, so a launch that is refused
    would otherwise take a request that is still waiting for a lane down with
    it. The snapshot is written back when the launch leaves no live pointer for
    it — neither the request's own id nor one naming it as the request it was
    admitted from. It is not written back when the request came back queued on
    the destination, so no second copy of one waiting request is created.
    """
    run_id = str(record["run_id"])
    try:
        return _require_launched_record(
            _launch_queued(
                record,
                launcher=launcher,
                config_for=config_for,
                backend_override=destination,
            )
        )
    except Exception:
        project = str(record.get("project") or "")
        survives = any(
            str(row.get("run_id") or "") == run_id
            or str(row.get("admitted_from") or "") == run_id
            for row in runs.list_live(project=project or None)
        )
        if not survives:
            with contextlib.suppress(Exception):
                runs.queue_dispatch(snapshot)
        raise


def _release_stranded_launches(
    live: list[dict[str, Any]],
    *,
    verdicts: list[dict[str, Any]] | None = None,
    now_seconds: float | None = None,
) -> list[str]:
    """Release launches that held their span past the bound, and record each.

    Nothing inside the blocked span can observe its own bound, so the pass,
    which is a separate process, does. The release writes the refusal naming
    the step the span reached before it gives the claim back: the launch that
    resumes reads the record rather than the claim, and removing the run
    directory first would destroy the only local account of where the span
    stopped. So the order is record, pointer, directory.
    """
    from reckon.crew import dispatch_claims, dispatch_refusals, recovery_liveness
    from reckon.crew.recovery_review_delivery import _resolved_reviewed_run_id

    moment = time.time() if now_seconds is None else now_seconds
    released: list[str] = []
    for pointer in live:
        if not recovery_liveness._stranded_launch(pointer, now_seconds=moment):
            continue
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            continue
        claim = dispatch_claims.read_launch_claim(run_id)
        step = str(claim.get("step") or "")
        holder = {
            key: claim.get(key)
            for key in ("holder_pid", "holder_host", "launched_at", "step", "step_at")
        }
        reviewed_run = _resolved_reviewed_run_id(
            pointer, str(pointer.get("project") or ""), pointers=live
        )
        if reviewed_run:
            holder["reviewed_run_id"] = reviewed_run
        dispatch_refusals.record_launch_refusal(run_id=run_id, step=step, holder=holder)
        dispatch_claims._release_launch_claim(run_id)
        released.append(run_id)
        if verdicts is not None:
            verdicts.append(
                _verdicts._verdict_row(
                    pointer,
                    allowance=None,
                    allowance_source="",
                    verdict="released-stranded-launch",
                    detail=(
                        "the launch span passed its bound at step "
                        f"{step or 'an unrecorded step'}; the claim is released "
                        "and the release is recorded"
                    ),
                )
            )
    return released


def _requeued_request(request: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    """The request written back as a queued pointer under the id it already has.

    A launch the reaper marked failed is an attempt, not a different request:
    the id a dispatch printed is the one a coordinator records and later acts
    on, so the re-queue keeps it. Anything reading one request's own history
    across its attempts — the pane's once-only refusal row is one such reader —
    therefore keys on the run id, and this is the line that makes that hold. A
    fresh id here would make every re-queue look like a new request to those
    readers, and would leave the printed id naming nothing.
    """
    restored = copy.deepcopy(dict(request))
    restored["run_id"] = str(run_id)
    restored["phase"] = "queued"
    return restored


def _requeue_admitted_failures(
    live: list[dict[str, Any]], *, verdicts: list[dict[str, Any]] | None = None
) -> list[str]:
    """Re-queue runs this drainer launched that the reaper marked launch-failed.

    The admission pass stamps every run it launches with the request it
    launched from. A run the reaper later marks ``launch-failed`` is a request
    still waiting behind a launch that never reached a model, so the next pass
    writes it back as one fresh queued pointer carrying the failure.
    """
    requeued = []
    for pointer in live:
        if str(pointer.get("phase") or "") != LAUNCH_FAILED_PHASE:
            continue
        request = runs.dispatch_field(pointer, "admit_request")
        if not isinstance(request, Mapping):
            continue
        failures = list(pointer.get("launch_failures") or ())
        failure = dict(failures[-1]) if failures else {}
        restored = _requeued_request(request, str(pointer["run_id"]))
        restored["queued_at"] = _service._utc_now()
        restored.setdefault("created_at", restored["queued_at"])
        # Stamp the re-queue with the same back-off a failed attempt records, so
        # a request that keeps failing on launch is attempted once per back-off
        # rather than released, failed and restored on every pass.
        _holds._stamp_failure(
            restored,
            failure or {"kind": LAUNCH_FAILED_PHASE},
            moment=_service._now(),
            guard=_holds._dependency_guard(restored, None),
        )
        try:
            _writeback_pointer(str(pointer["run_id"]), restored)
        except _PointerWritebackError as exc:
            if verdicts is not None:
                verdicts.append(
                    _verdicts._verdict_row(
                        pointer,
                        allowance=None,
                        allowance_source="",
                        verdict="refused",
                        detail=str(exc),
                    )
                )
            continue
        requeued.append(str(pointer["run_id"]))
    return requeued


class _PointerWritebackError(CrewError):
    """An admission snapshot no longer names a live, unpromoted request."""


def _require_live_writeback(snapshot: Mapping[str, Any]) -> None:
    """Refuse a departed request while the caller holds its pointer lock.

    Promotion records the run before removing its pointer, so its ledger record
    takes precedence over pointer presence. Both readings and the write share
    the same lock as promotion and discard.
    """
    run_id = str(snapshot["run_id"])
    project = str(snapshot.get("project") or "")
    repo = str(snapshot.get("repo") or "")
    promoted = runs._promoted_record_project(
        run_id,
        project or None,
        roots={project: Path(repo)} if project and repo else None,
    )
    if promoted:
        raise _PointerWritebackError(
            f"queue writeback refused: promotion of {run_id!r} is recorded in {promoted!r}"
        )
    if not runs.pointer_path(run_id).is_file():
        raise _PointerWritebackError(
            f"queue writeback refused: live pointer for {run_id!r} was removed"
        )


def _write_pointer(run_id: str, record: dict[str, Any]) -> None:
    runs._write_json(runs.pointer_path(run_id), record)


def _writeback_pointer(run_id: str, record: dict[str, Any]) -> None:
    with runs._pointer_lock(run_id):
        _require_live_writeback(record)
        _write_pointer(run_id, record)


def _discarded_since(snapshot: Mapping[str, Any]) -> bool:
    """Whether a deliberate discard has been recorded for this request.

    The pass holds the queued pointer in memory while it attempts a launch and
    writes it back when the launch does not survive. A ``crew discard`` issued in
    that window removes the pointer and records the departure — in the run
    directory, or in the shared discards spool for a request that never launched
    and so has no run directory. Without this reading the restore would write the
    request back and undo the discard. The record's presence is the fact the
    restore reads.
    """
    from reckon.crew.promotion import discard_recorded

    return discard_recorded(str(snapshot["run_id"]))


def _restore_queued(
    snapshot: Mapping[str, Any],
    failure: str,
    *,
    moment: datetime | None = None,
    guard: Mapping[str, Any] | None = None,
    hold: Mapping[str, Any] | None = None,
) -> None:
    """Write a launched request back as one queued pointer carrying the failure.

    The restored pointer carries the same bounded back-off as a failure
    annotated in place. A discarded, promoted or removed request is never
    restored; the write checks departure under the pointer lock.

    A ``hold`` names a refusal that is not a failed attempt — a claim taken in
    the window between the pre-launch check and the launch. The request is then
    restored carrying that hold and no failure, so the next pass launches the
    request as soon as the claim it waits on is released rather than waiting out
    a retry back-off.
    """
    if _discarded_since(snapshot):
        return
    restored = copy.deepcopy(dict(snapshot))
    restored["phase"] = "queued"
    if hold is None:
        _holds._stamp_failure(
            restored, failure, moment=moment or _service._now(), guard=guard
        )
    else:
        restored[RECORDED_HOLD_FIELD] = dict(hold)
        restored["reason"] = str(hold.get("reason") or "")
    for key in (
        "pid",
        "pid_start_time",
        "worktree",
        "log_path",
        "stderr_path",
        "prompt_path",
        "admitted_at",
    ):
        restored.pop(key, None)
    run_id = str(snapshot["run_id"])
    with runs._pointer_lock(run_id):
        _require_live_writeback(snapshot)
        current = runs.read_pointer(run_id)
        if _retain_launched_record(current, restored):
            restored = current
        _write_pointer(run_id, restored)


def _return_claim_refusal_to_queue(
    snapshot: Mapping[str, Any],
    caught: ScopeConflict,
    *,
    live: Sequence[Mapping[str, Any]],
    moment: datetime,
) -> tuple[str, str | None]:
    """Return a launch refused on a claim to the queue, held on that claim.

    A claim taken between the pre-launch check and the launch refuses the
    launch once its pointer has been published in phase ``starting``. The
    refusal is a hold, not a failed attempt: the request goes back to
    ``queued`` carrying the claim that must settle, and the next pass launches
    it once that claim is released. Left as a failure the starting pointer
    would stay behind with no worker, which the reaper does not read as a
    launch to clean up.
    """
    run_id = str(snapshot["run_id"])
    try:
        _restore_queued(
            snapshot,
            f"{type(caught).__name__}: {caught}",
            moment=moment,
            hold=_holds._refusal_hold(caught, live, moment=moment),
        )
    except _PointerWritebackError as exc:
        return run_id, str(exc)
    return run_id, None


def _record_queued_failure(
    snapshot: Mapping[str, Any],
    failure: str,
    *,
    moment: datetime | None = None,
    guard: Mapping[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Return the retained request's id and any unrecoverable write error."""
    original_run_id = str(snapshot["run_id"])
    failed_at = moment or _service._now()

    def annotate(current: dict[str, Any]) -> dict[str, Any]:
        _require_live_writeback(snapshot)
        refused = _holds._stamp_failure(
            copy.deepcopy(dict(snapshot)), failure, moment=failed_at, guard=guard
        )
        if _retain_launched_record(current, refused):
            return current
        return _holds._stamp_failure(current, failure, moment=failed_at, guard=guard)

    try:
        runs._mutate_pointer(original_run_id, annotate)
    except _PointerWritebackError as exc:
        return original_run_id, str(exc)
    except Exception as annotate_error:  # noqa: BLE001 - a failed write needs restoration
        try:
            _restore_queued(snapshot, failure, moment=failed_at, guard=guard)
        except _PointerWritebackError as exc:
            return original_run_id, str(exc)
        except Exception as restore_error:  # noqa: BLE001 - report storage failure to the pass
            annotation = f"annotation {type(annotate_error).__name__}: {annotate_error}"
            restoration = f"restore {type(restore_error).__name__}: {restore_error}"
            return original_run_id, f"{annotation}; {restoration}"
        return original_run_id, None
    return original_run_id, None


def _require_launched_record(candidate: Any) -> Mapping[str, Any]:
    if (
        not isinstance(candidate, Mapping)
        or not candidate.get("run_id")
        or not candidate.get("phase")
        or candidate.get("phase") == "queued"
    ):
        raise CrewError("dispatch did not return a launched record")
    return candidate


def _admission_history_entry(
    request: Mapping[str, Any], *, admitted_at: str
) -> dict[str, Any] | None:
    """The queued-life facts of one admitted request, or ``None`` if it has none.

    ``None`` for a request that launched on its first attempt: a history entry
    recording nothing is a record a reader has to open to learn nothing.
    """
    entry = {
        key: copy.deepcopy(request[key])
        for key in QUEUED_LIFE_KEYS
        if request.get(key) is not None
    }
    if not entry:
        return None
    entry["queued_at"] = request.get("queued_at")
    entry["admitted_at"] = admitted_at
    return entry


def _archive_queued_life(
    pointer: dict[str, Any], request: Mapping[str, Any], *, admitted_at: str
) -> None:
    """Keep a refusal once, away from the launched record's current state."""
    entry = _admission_history_entry(request, admitted_at=admitted_at)
    if entry is not None:
        history = list(pointer.get(ADMISSION_HISTORY_KEY) or ())
        facts = {key: value for key, value in entry.items() if key != "admitted_at"}
        if not any(
            all(item.get(key) == value for key, value in facts.items())
            for item in history
        ):
            history.append(entry)
        pointer[ADMISSION_HISTORY_KEY] = history
    for key in QUEUED_LIFE_KEYS:
        pointer.pop(key, None)


def _retain_launched_record(
    pointer: dict[str, Any], refused: Mapping[str, Any]
) -> bool:
    """A refusal arriving after a successful spawn changes only its history."""
    launched_at = pointer.get("launch_recorded_at") or pointer.get("launched_at")
    if not launched_at or pointer.get("phase") == LAUNCH_FAILED_PHASE:
        return False
    _archive_queued_life(pointer, refused, admitted_at=str(launched_at))
    return True


def _stamp_admission(
    run_id: str, request: Mapping[str, Any], *, admitted_at: str
) -> None:
    """Record on a launched run the queued request it was admitted from.

    The stamp is what lets the next pass recognise a run this drainer launched
    and re-queue it when the reaper marks it ``launch-failed``.

    The request's queued-life fields are moved under
    :data:`ADMISSION_HISTORY_KEY` in the same write, and removed from the top
    level, so the pointer is the launched run's record and nothing else: a
    reader of a live run never sees the refusal its queued life recorded, and
    the suppression of that history is visible where it was moved to.
    """

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["admitted_at"] = admitted_at
        pointer["admit_request"] = copy.deepcopy(dict(request))
        _archive_queued_life(pointer, request, admitted_at=admitted_at)
        return pointer

    runs._mutate_pointer(run_id, mutation)


def _join_admitted_plan_review(pointer: Mapping[str, Any], launched_run_id: str) -> str:
    """Write a launched plan review's run id into its composed dispatch sidecar.

    A queued plan review's report directory names the crew run that ran it only
    through ``dispatch.json``, and the compose left that sidecar unlaunched. The
    join resolves promotion's lookup, so it is taken by the launch itself — not
    a later sweep, and not one call site among the several that launch a queued
    request. A pointer that names no plan review joins nothing. Imported here
    rather than at module load so the admission surface does not depend on the
    review surface's own imports.
    """
    from reckon.crew import recovery_review_dispatch

    return recovery_review_dispatch.join_admitted_plan_review(pointer, launched_run_id)
