# ruff: noqa: I001, UP035
from __future__ import annotations
import json
import subprocess
from pathlib import (
    Path,
)
from typing import (
    Any,
    Iterable,
    Mapping,
)
from reckon import (
    _backends,
    flight,
)
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    CompetenceLimit,
    CrewError,
    TaskNode,
    member_in_flight_verdict,
)
from reckon.crew.prompts import (
    time_fence_statement,
)
from reckon.crew.recovery import (
    REVIEW_NODE_PREFIX,
    _resolve_commit,
    resume_window_refusal,
)
from reckon.crew.routing import (
    _agent_configuration,
    _budget_verdict,
    _competence_verdict,
    _signal_process_group,
    resolve_dispatch_authority,
    resolve_dispatch_ledger_root,
)
from reckon.crew.runs import (
    _manifest_freshness,
    _manifest_mtime_ns,
    _mutate_pointer,
    _process_start_time,
    _utc_now,
    capture_run_session,
    process_alive,
    read_pointer,
    record_process_alive,
    run_dir,
)


# Workers launch inside the fence. Every worker launch — a fresh dispatch, an
# in-place resume and a lane-change redispatch alike — sits behind a read-only
# overlay of the operator's dot directories, so a worker cannot write the
# coordinator's home, project state or plan store; the run's own write roots are
# re-bound writable over that overlay, including a linked worktree's git
# directory and its repository's shared object store, so a fenced worker can
# still commit the work it was dispatched to do; each run then adopts its own
# harness home, seeded from the operator's settings and instruction files so the
# run's hooks and guidance are still read.
#
# One launch is deliberately outside the overlay: the lane-availability probe in
# reckon/crew/resumption.py (_request_lane_availability) composes its launch with
# fence=False. It issues the smallest supported model request to classify whether
# a lane serves, so it runs no worker and writes nowhere the overlay protects; it
# exists to decide routing rather than to do a run's work, and the fence binds a
# run's worktree and run directory, neither of which a probe has.
FENCE_WORKERS = True


def attach(run_id: str, task: str) -> dict[str, Any]:
    """Bind an in-harness dispatch to its live pointer.

    Reckon cannot spawn the calling harness's delegation primitive, so the
    harness dispatches its own task and reports the identity back here. That
    binding is what makes an in-harness run observable on the same surface as a
    spawned one.
    """

    def bind(record: dict[str, Any]) -> dict[str, Any]:
        if record.get("launch") != "in-harness":
            raise CrewError(
                f"run {run_id!r} is a {record.get('launch')!r} launch; attach binds "
                "an in-harness task, and a spawned run already has its pid"
            )
        if record.get("task"):
            raise CrewError(
                f"run {run_id!r} is already attached to task {record['task']!r}; "
                "a second binding would hide which worker holds the write scope"
            )
        if not str(task).strip():
            raise CrewError("attach requires a non-empty task identifier")
        record["task"] = str(task).strip()
        record["attached_at"] = _utc_now()
        record["phase"] = "working"
        return record

    return _mutate_pointer(run_id, bind)


def _stored_phase_survives(stored_phase: str, observed_phase: str) -> bool:
    """Whether a fold keeps a phase the run already reached, unopened.

    Two stored phases are final for their pointer and a fold may not reopen
    them: a terminal phase, which the supervisor wrote once the worker exited,
    and ``launch-failed``, which records that the worker exited before its
    first event and stops a further lift until a person acts. An observation
    that derives a different phase from a stream, a manifest or a dead pid
    folds over a run whose own evidence already settled it.
    """
    return _terminal_phase_survives(stored_phase, observed_phase) or (
        stored_phase == LAUNCH_FAILED_PHASE and observed_phase != LAUNCH_FAILED_PHASE
    )


def _terminal_phase_survives(stored_phase: str, observed_phase: str) -> bool:
    """Whether a fold keeps a terminal phase the supervisor already stored.

    The supervisor is the one writer of a terminal stored phase: it sets the
    delivered manifest's status once the worker exits, and a run that has
    finished stays finished. A late observation reads the run's own stream, and
    a stream that was rewritten or truncated, or a stream whose terminal event
    the observer did not reach, reports a live phase for a run that has already
    ended. Folding that over the stored terminal phase would show a finished
    run as still running, so a terminal stored phase is kept against a
    non-terminal observation.
    """
    return (
        stored_phase in _TERMINAL_RUN_PHASES
        and observed_phase not in _TERMINAL_RUN_PHASES
    )


def observe(run_id: str, *, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from reckon.crew.query import _resumability
    from reckon.crew.recovery import _apply_budget_watchdog
    from reckon.crew.reports import parse_manifest

    """Fold a run's on-disk evidence back into its pointer and return it.

    Reads the event stream, the manifest path and process liveness, then writes
    the derived phase, session id and budget block into the record. Everything
    it reports is recoverable from disk, so a fresh session can observe a run it
    did not dispatch.
    """

    def fold(record: dict[str, Any]) -> dict[str, Any]:
        # Resolve before folding the stream into the pointer so the answer says
        # where the session was recovered rather than always reporting pointer.
        session = _current_harness_session(record, config=config)
        backend_name = str(record.get("backend") or "")
        manifest = Path(record.get("manifest_path") or "")
        manifest_file_present, manifest_fresh = _manifest_freshness(record)
        record["manifest_file_present"] = manifest_file_present
        record["manifest_fresh"] = manifest_fresh
        record["manifest_present"] = manifest_fresh
        record["process_alive"] = record_process_alive(record, process_alive)
        record["observed_at"] = _utc_now()
        stored_phase = str(record.get("phase") or "")
        stopped = stored_phase == "stopped"

        if record.get("launch") == "cli":
            backend = _backend_settings(record, config)
            observation = _backends.observe_log(
                backend_name=backend_name,
                backend=backend,
                log_path=record.get("log_path", ""),
            )
            data = observation.as_dict()
            record["budget"] = data["budget"]
            record["events"] = data["events"]
            record["exit_status"] = data["exit_status"]
            record["final_message"] = data["final_message"]
            record["throughput"] = data["throughput"]
            observed_phase = "stopped" if stopped else data["phase"]
            if _stored_phase_survives(stored_phase, observed_phase):
                # The run finished under the supervisor's terminal phase; a
                # stream that reports it as live does not reopen it.
                observed_phase = stored_phase
            record["phase"] = observed_phase
            if (
                not stopped
                and record.get("attempt_kind") == "resume"
                and record["process_alive"] is True
                and record["phase"] in _TERMINAL_RUN_PHASES
            ):
                record["phase"] = "working"
            record["session_id"] = data["session_id"] or session.get("session_id")
            if data["detail"]:
                record["detail"] = data["detail"]
            final_file = Path(record.get("final_message_path") or "")
            if not record["final_message"] and final_file.is_file():
                record["final_message"] = final_file.read_text().strip() or None
            if (
                not stopped
                and data["phase"] in ("starting", "working")
                and record["process_alive"] is False
                and not _stored_phase_survives(stored_phase, "orphaned")
            ):
                # A dead process with no terminal event is a recoverable orphan,
                # not a finished run. An empty log counts because argument
                # failures can exit before the first event is written. A run the
                # supervisor already finished is neither: its terminal stored
                # phase stands and a spent pid does not reopen it. A zero-length
                # stream whose stderr carries a message is the third case: the
                # worker exited before its first turn and wrote the reason where
                # the launcher's reap could quote it but nothing read it, so the
                # reason is read here and the run is classified as the launch
                # failure it was instead of as an orphan with no cause.
                failure = _empty_stream_launch_failure(record, data)
                if failure is not None:
                    record["phase"] = LAUNCH_FAILED_PHASE
                    cause = str(failure["stderr_tail"]).strip().splitlines()[-1]
                    record["detail"] = (
                        "the worker exited before writing any stream record; "
                        f"stderr: {cause}"
                    )
                    failures = list(record.get("launch_failures") or ())
                    failures.append(failure)
                    record["launch_failures"] = failures
                else:
                    record["phase"] = "orphaned"
                    record["detail"] = (
                        "process exited without a terminal event in its log; "
                        f"check {record.get('stderr_path')}"
                    )
        elif record.get("task") and record["manifest_present"] and not stopped:
            manifest_status = str(
                parse_manifest(manifest.read_text()).get("status") or ""
            ).strip()
            if manifest_status and not _stored_phase_survives(
                stored_phase, manifest_status
            ):
                record["phase"] = manifest_status

        _apply_budget_watchdog(record, config)
        _apply_orientation_check(record, manifest if manifest_fresh else None)

        worktree = str(record.get("worktree") or "").strip()
        resumable, reason = _resumability(
            session,
            worktree_exists=bool(worktree) and Path(worktree).is_dir(),
            process_alive=record["process_alive"],
        )
        record["session_source"] = session["source"]
        record["session_resolution"] = session
        record["resumable"] = resumable
        record["resumable_reason"] = reason
        record["resume_session_id"] = session["session_id"] if resumable else None
        # The pointer records the session id the run carried, or names why it
        # carried none. A bare null left a reader unable to tell a run whose
        # stream had not been read from one whose stream had no id to give, and
        # that ambiguity is what promoted five resumable runs.
        absence = _capture_session_absence(record, session)
        if absence is None:
            record.pop("session_id_absent", None)
        else:
            record["session_id_absent"] = absence

        capture = _capture_member_session(record)
        if capture is not None:
            record["session_capture"] = capture
        return record

    return _mutate_pointer(run_id, fold)


def _apply_orientation_check(record: dict[str, Any], manifest: Path | None) -> None:
    """Block a run whose first reported orientation differs from its pointer."""
    from reckon.crew.reports import parse_manifest

    prior = record.get("orientation_check")
    if isinstance(prior, Mapping):
        if prior.get("matched") is False:
            record["phase"] = "blocked"
            record["detail"] = str(prior.get("detail") or "orientation mismatch")
        return
    if manifest is None:
        return

    reported = parse_manifest(manifest.read_text())
    names = ("orientation_worktree", "orientation_base_sha", "orientation_write_paths")
    if any(not reported.get(name) for name in names):
        return

    raw_paths = reported["orientation_write_paths"]
    try:
        decoded_paths = json.loads(str(raw_paths))
    except json.JSONDecodeError:
        decoded_paths = raw_paths
    if isinstance(decoded_paths, list) and all(
        isinstance(path, str) for path in decoded_paths
    ):
        reported_paths: Any = sorted(decoded_paths)
    else:
        reported_paths = raw_paths

    expected = {
        "worktree": str(record.get("worktree") or ""),
        "base_sha": str(record.get("base_sha") or ""),
        "write_paths": sorted(
            str(path) for path in (record.get("node") or {}).get("write_paths") or ()
        ),
    }
    actual = {
        "worktree": str(reported["orientation_worktree"]),
        "base_sha": str(reported["orientation_base_sha"]),
        "write_paths": reported_paths,
    }
    mismatches = [name for name in expected if actual[name] != expected[name]]
    if not mismatches:
        record["orientation_check"] = {
            "checked_at": _utc_now(),
            "matched": True,
        }
        return

    detail = "orientation mismatch: " + "; ".join(
        f"{name} expected={json.dumps(expected[name], sort_keys=True)} "
        f"reported={json.dumps(actual[name], sort_keys=True)}"
        for name in mismatches
    )
    record["orientation_check"] = {
        "checked_at": _utc_now(),
        "matched": False,
        "mismatches": mismatches,
        "expected": expected,
        "reported": actual,
        "detail": detail,
    }
    record["phase"] = "blocked"
    record["detail"] = detail


def _record_node_id(record: Mapping[str, Any]) -> str:
    """The node id a run record names, whether it stores the block or the id.

    A live pointer carries the node definition under ``node`` while a promoted
    row stores the id alone there and keeps the definition beside it, so a
    reader of either shape takes the same id from either spelling.
    """
    node = record.get("node")
    if isinstance(node, Mapping):
        return str(node.get("id") or "")
    return str(node or "")


def _record_plan(record: Mapping[str, Any]) -> str:
    """The plan a run record serves, from the node block or the row's own key."""
    node = record.get("node")
    if isinstance(node, Mapping) and node.get("plan"):
        return str(node["plan"])
    return str(record.get("plan") or "")


def _is_review_run(record: Mapping[str, Any]) -> bool:
    """Whether a record is a review, by its role or by its node id.

    The role survives a renamed node id and the prefix survives a record
    written before a role was carried, so either alone recognises a review —
    and a review left unrecognised would take a fresh session where its own
    task has one to continue.
    """
    from reckon.crew.recovery import _is_review_node

    return str(record.get("role") or "") == "review" or _is_review_node(record)


def _reviewed_run_id(source: str, records: Iterable[Mapping[str, Any]]) -> str:
    """Resolve what a review node's source names to the reviewed run's id.

    The review reflex composes a review node's id from the record of the run it
    reviews, so the remainder after the prefix is that run's node id where it
    has one and its run id otherwise. Both spellings resolve here to the
    reviewed run's id: a re-review of one run therefore continues the earlier
    review's session, while a review of a different run starts fresh even when
    the two share a node lineage.
    """
    if not source:
        return ""
    runs = list(records)
    if any(str(item.get("run_id") or "") == source for item in runs):
        return source
    named = [item for item in runs if _record_node_id(item) == source]
    if not named:
        return source
    named.sort(
        key=lambda item: str(
            item.get("completed_at") or item.get("created_at") or ""
        )
    )
    return str(named[-1].get("run_id") or source)


def _task_identity(
    record: Mapping[str, Any],
    project: str,
    records: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    """The task a run belongs to, which is what a session may be continued for.

    An implement, test or investigate run's task is its (project, plan, node
    id); a review's is the run it reviews. Two dispatches may therefore share a
    task without sharing a legacy roster member, and a member may hold sessions
    of several tasks — which is exactly why the member is the wrong key.
    """
    if _is_review_run(record):
        source = _record_node_id(record)[len(REVIEW_NODE_PREFIX) :]
        return ("review", str(project), _reviewed_run_id(source, records))
    return ("node", str(project), _record_plan(record), _record_node_id(record))


def _run_stream_path(record: Mapping[str, Any]) -> Path | None:
    """The stream this run wrote, from its recorded path or its run directory."""
    log = str(record.get("log_path") or "").strip()
    if log:
        return Path(log)
    run_id = str(record.get("run_id") or "").strip()
    if run_id:
        return run_dir(run_id) / "stream.jsonl"
    return None


def _session_too_large_to_continue(record: Mapping[str, Any]) -> str | None:
    """Name why a prior run's session cannot be continued, or None.

    Two endings leave a transcript the endpoint will refuse again: the prompt
    was too long for the model's window, or a compaction announced itself and
    never completed a boundary, so the session is still over the window. Both
    are read from the run's own stream, because a run is promoted only on
    success and the very endings that disqualify its session are the ones no
    promoted row records.
    """
    stream = _run_stream_path(record)
    if stream is None:
        return None
    compaction_announced = False
    boundary_seen = False
    refusal = ""
    try:
        handle = stream.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, Mapping):
                continue
            kind = str(event.get("type") or "")
            if kind == "system":
                subtype = str(event.get("subtype") or "")
                if (
                    subtype == "status"
                    and str(event.get("status") or "") == "compacting"
                ):
                    compaction_announced = True
                elif subtype == "compact_boundary":
                    boundary_seen = True
            elif kind == "result":
                if event.get("is_error") and "Prompt is too long" in str(
                    event.get("result") or ""
                ):
                    refusal = "Prompt is too long"
                elif str(event.get("terminal_reason") or "") == "blocking_limit":
                    refusal = "blocking_limit"
    if refusal:
        return (
            f"the run ended with {refusal!r}, so its session is too large to "
            "continue"
        )
    if compaction_announced and not boundary_seen:
        return (
            "the run announced a compaction that never completed a boundary, "
            "so its session is still too large to continue"
        )
    return None


def _prior_same_task_run(
    identity: tuple[str, ...],
    records: Iterable[Mapping[str, Any]],
    *,
    project: str,
) -> Mapping[str, Any] | None:
    """The most recent prior run of one task that carried a session id.

    Ordered by completion so a redispatch continues the attempt it succeeds
    rather than an older branch of the same task. A shadow run is skipped: it
    is a parallel lineage of the task rather than a prior attempt of it, and
    continuing its conversation would carry the shadow's context into the work
    it was only meant to inform.
    """
    from reckon.crew.resumption import resolve_session

    runs = list(records)
    candidates = []
    for record in runs:
        lineage = record.get("lineage")
        if isinstance(lineage, Mapping) and lineage.get("kind") == "shadow":
            continue
        if _task_identity(record, project, runs) != identity:
            continue
        session = resolve_session(
            str(record.get("run_id") or ""),
            record=record,
            project=project,
            root=record.get("repo"),
        )
        if not session["resolved"]:
            continue
        candidates.append(record)
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: str(
            item.get("completed_at") or item.get("created_at") or ""
        )
    )
    return candidates[-1]


def _prior_session_still_held(
    prior: Mapping[str, Any], live_pointers: Iterable[Mapping[str, Any]]
) -> str | None:
    """Why the prior run's session cannot be continued, or None when it can.

    A session id is a single-writer resource: the backend's thread store
    refuses a second writer for a thread another process still holds, and the
    refused launch exits before its first event. A prior run that is still a
    live pointer is judged by the member guard's own verdict, so the semantic
    that refuses a dispatch onto a busy member is the one that withholds its
    session: the session may be continued only once its worker is proven gone
    on this host. A committed record is a run that has ended, so it carries no
    such question and its session stays continueable; a live pointer whose
    worker is running, or whose liveness cannot be established here, withholds
    the session — continuity is worth less than the node.
    """
    run_id = str(prior.get("run_id") or "")
    live = next(
        (
            pointer
            for pointer in live_pointers
            if str(pointer.get("run_id") or "") == run_id
        ),
        None,
    )
    if live is None:
        return None
    verdict = member_in_flight_verdict(live)
    if not verdict.blocks:
        return None
    return (
        "its worker has not been proven stopped, so continuing its session "
        f"could collide with a live writer ({verdict.reason})"
    )


def _task_session_resolution(
    node: Any,
    project: str,
    *,
    committed_runs: Iterable[Mapping[str, Any]] = (),
    live_pointers: Iterable[Mapping[str, Any]] = (),
    harness: str = "",
) -> dict[str, Any]:
    """Resolve this dispatch's prior same-task session, or name none.

    Selection is keyed to the installation, never to the roster: the session a
    dispatch continues is the one an earlier run *of this task* left behind,
    read from the committed run records and the live pointers. A member that
    last ran another node therefore offers nothing to a new node — the defect
    this removes — and continuing a conversation is decided by what the task
    did, not by which member happened to carry it.

    A prior run whose own stream ended too large to continue is withheld rather
    than composed, and the withholding names it, so a reader sees the refusal
    rather than a bare absence. A prior run that is still a live pointer whose
    worker has not been proven stopped is withheld the same way: its session is
    still held by a writer, and this run starts a fresh conversation instead of
    colliding with it. The substitution is reported rather than silent.
    """
    from reckon.crew.resumption import resolve_session

    records = [*committed_runs, *live_pointers]
    identity = _task_identity(
        {
            "node": {
                "id": str(getattr(node, "id", "") or ""),
                "plan": str(getattr(node, "plan", "") or ""),
            },
            "role": str(getattr(node, "role", "") or ""),
            "session_id": "",
        },
        project,
        records,
    )
    prior = _prior_same_task_run(identity, records, project=project)
    if prior is None:
        return {"session_id": None, "withheld": None}
    session = resolve_session(
        str(prior.get("run_id") or ""),
        record=prior,
        project=project,
        root=prior.get("repo"),
    )
    session_id = str(session.get("session_id") or "").strip()
    owner = str(
        prior.get("session_harness")
        or prior.get("dialect")
        or (prior.get("agent") or {}).get("dialect")
        or ""
    )
    held = _prior_session_still_held(prior, live_pointers)
    if held is not None:
        disqualifier = held
    elif harness and owner != harness:
        disqualifier = (
            f"its session belongs to harness {owner or 'unknown'!r}, not {harness!r}"
        )
    else:
        disqualifier = _session_too_large_to_continue(prior)
    if disqualifier is not None:
        return {
            "session_id": None,
            "withheld": {
                "session_id": session_id or None,
                "owner": None,
                "reason": (
                    f"the prior run {prior.get('run_id')!r} of this task left a "
                    f"session that cannot be continued: {disqualifier}"
                ),
            },
        }
    return {"session_id": session_id, "withheld": None}


def _dispatch_session_absence(
    backend: Mapping[str, Any],
    *,
    reused: str | None,
    withheld: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Name why a freshly dispatched run carries no session id, or None.

    Dispatch is the first of the two points a session id can be attached, and
    a run that reaches it without one must not record a bare null — which reads
    as a verdict on a run that has simply not got one yet. The three situations
    want different operator responses, so each is named: no earlier run of this
    task left a session to continue, one did and its stream ended too large to
    continue, or the resolved backend cannot resume at all. Observation
    replaces this with the id the run's own stream supplies, or with the point
    the capture reached.
    """
    if reused:
        return None
    if withheld:
        return {
            "point": "dispatch-same-task-session-withheld",
            "reason": str(withheld.get("reason") or ""),
        }
    if not backend.get("session_reuse"):
        return {
            "point": "dispatch-session-not-reuseable",
            "reason": (
                "the resolved backend records no session reuse, so no earlier "
                "session is offered to this run"
            ),
        }
    return {
        "point": "dispatch-no-same-task-session",
        "reason": (
            "no earlier run of this task left a session to continue, so the "
            "run starts a fresh conversation"
        ),
    }


def _capture_session_absence(
    record: Mapping[str, Any], session: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Name the point a run's session-id capture reached, or None when it did.

    Called on every observation, so a run that resolves a session records the
    id and drops any earlier absence, and a run that resolves none names which
    of the distinct situations produced it: a launch with no stream to carry
    one, a stream not yet readable, or a stream read and found without one. The
    last is the case that made resume structurally unavailable, and it is a
    measurement rather than an outage — the backend simply has not announced an
    id for this run.
    """
    if session.get("session_id"):
        return None
    if str(record.get("launch") or "") != "cli":
        return {
            "point": "harness-launch",
            "reason": (
                "a session id is read from a backend stream and this launch "
                "writes none, so nothing will ever capture one for it"
            ),
        }
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return {
            "point": "stream-unreadable",
            "reason": (
                f"the recorded stream {str(log)!r} is not a readable file, so "
                "there was nothing to read a session id from"
            ),
        }
    return {
        "point": "stream-without-id",
        "reason": (
            "the run's own stream was read and carries no session id, so the "
            "backend has not announced one for this run"
        ),
    }


def _capture_member_session(record: dict[str, Any]) -> dict[str, Any] | None:
    """Compatibility entry point for promotion; capture writes only the run."""
    return capture_run_session(record)


def _current_harness_session(
    record: Mapping[str, Any], *, config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve only sessions owned by the run's current harness.

    A fresh harness boundary excludes earlier streams even when their event
    vocabulary is readable by the new parser. Once this harness captures a
    session, its pointer also permits recovery across its own resume streams.
    """
    from reckon.crew.resumption import resolve_session

    run_id = str(record.get("run_id") or "")
    resolved = resolve_session(run_id, record=record)
    if record.get("launch") != "cli":
        return resolved
    owner = str(record.get("session_harness") or "")
    boundary = record.get("lane_change") or {}
    changed_harness = boundary.get("session") == "fresh" and boundary.get(
        "from_harness"
    ) != boundary.get("to_harness")
    if not owner and not changed_harness:
        return resolved
    backend = _backend_settings(record, config)
    harness = _backends.dialect_for(backend).name
    if (not changed_harness and (not owner or owner == harness)) or (
        resolved["resolved"] and owner == harness
    ):
        return resolved
    observation = _backends.observe_log(
        backend_name=str(record.get("backend") or ""),
        backend=backend,
        log_path=record.get("log_path", ""),
    )
    found = observation.session_id
    reason = (
        f"the current harness {harness!r} has no captured session; sessions "
        "from another harness cannot be continued"
    )
    return {
        "run_id": run_id,
        "session_id": found,
        "resolved": bool(found),
        "source": "stream" if found else None,
        "consulted": ["current-harness-stream"],
        "detail": None if found else reason,
        "withheld": None
        if found
        else {
            "session_id": resolved.get("session_id") or boundary.get("session_id"),
            "reason": reason,
        },
    }


def _names_the_fence(command: str) -> bool:
    """Whether a recorded command names the fence wrapper rather than a harness.

    The fence binary's own name is matched, bare or absolute.
    """
    return bool(command) and Path(command).name == _backends.FENCE_BINARY


def _harness_behind_the_fence(argv: Any) -> str:
    """The harness command a composed, fenced argv carries, or "".

    A fenced launch is ``<fence> <binds...> -- <harness> ...``, optionally
    behind a scheduler a placement prefixed. The fence element is located first
    and the separator searched for after it, because a scheduler's own options
    may carry a bare ``--`` and its separator is not the fence's. An argv with
    no fence element is not a fenced composition this can read.
    """
    if not isinstance(argv, (list, tuple)):
        return ""
    start = next(
        (
            index
            for index, token in enumerate(argv)
            if Path(str(token)).name == _backends.FENCE_BINARY
        ),
        None,
    )
    if start is None or "--" not in argv[start:]:
        return ""
    tail = argv[argv.index("--", start) + 1 :]
    return str(tail[0]).strip() if tail else ""


def _backend_settings(
    record: Mapping[str, Any], config: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Rebuild the settings a recorded run's stream is read with.

    Two authorities carry different parts of the rebuild, and dropping either
    corrupts the reading. The recorded command is the ground truth for the
    harness, so a run stays observable after its config layer changes — except
    where it names the fence, which is a wrapper rather than a harness: the
    composed argv's first element and the harness are different commands, and
    a rebuild of a command line around the fence is a rebuild of the harness.
    The configured lane supplies the window, the model and the effort, without
    which a stream-announced window substitutes as the utilisation's
    denominator and a resumed turn launches without its recorded model. The two
    are merged rather than one derived from the other, and a value the row
    recorded itself wins the merge: the row is the authority for what it was
    measured against, which a fresh config lookup cannot answer for a run
    already in flight.
    """
    backends = (config or {}).get("backends") or {}
    configured = backends.get(record.get("backend"))
    argv = record.get("argv")
    # The harness is read from the record's own explicit field rather than from
    # argv[0]. A placed launch prefixes the scheduler onto the argv, so the
    # first word of a placed run's argv names the scheduler, and a reader
    # taking the harness from it translates the wrong lane — the launch itself
    # succeeded, and only the identity a later reader infers is wrong. A record
    # written before the field existed falls back to argv[0], which is the
    # harness for every launch that was not placed.
    command = str(record.get("command") or "").strip()
    if not command and isinstance(argv, list) and argv:
        command = str(argv[0])
    # The field is written from the composed argv's first element, which is the
    # fence binary for every launch since the fence became the composition
    # default. Feeding that back as the harness composes a fence inside a fence
    # — bubblewrap is handed its own harness flags and the worker never starts
    # — so the fence name is refused here rather than translated. The field
    # stays authoritative for a run launched unfenced and for a placed launch,
    # where it is the only place the harness was recorded beside the scheduler.
    if _names_the_fence(command):
        command = ""
    # The record's own composition is the second authority, ahead of the
    # configured lane: the harness the argv carries behind its fence is the
    # same fact the explicit field holds for a run that was not fenced, and a
    # lane whose command has since changed must not redefine what an in-flight
    # run was launched as.
    inner_harness = _harness_behind_the_fence(argv)
    if command:
        settings: dict[str, Any] = {"launch": "cli", "command": command}
    elif inner_harness:
        settings = {"launch": "cli", "command": inner_harness}
    elif isinstance(configured, Mapping):
        settings = dict(configured)
    else:
        raise CrewError(
            f"run {record.get('run_id')!r} records no argv and its backend is not "
            "in the supplied config, so its stream cannot be read"
        )
    # The identity the launch resolved to, recorded beside the command. It is
    # consulted when the command's own stem names no dialect, which is the case
    # a placed run produces; a record naming only its lane still resolves.
    identity = str(record.get("dialect") or "").strip() or str(
        record.get("backend") or ""
    ).strip()
    if identity:
        settings.setdefault("dialect", identity)
    for key in ("usable_input_window", "model", "effort"):
        if isinstance(configured, Mapping) and configured.get(key) is not None:
            settings.setdefault(key, configured[key])
    agent = record.get("agent")
    if isinstance(agent, Mapping):
        for key in ("usable_input_window", "model", "effort"):
            if agent.get(key) is not None:
                settings[key] = agent[key]
    return settings


def _recorded_manifest_path(record: Mapping[str, Any], run_id: str) -> str:
    """The manifest path a launch rebuilt from a live pointer must carry.

    A pointer records the path twice — the top-level field a dispatch writes
    and the node definition it was written from — and a record may hold
    neither, or a value that is not absolute. Every launch composed from the
    record derives the run's directory from this path: the harness home and
    the fence roots are both built from its parent, and a path that names no
    location anchors them to the directory of whichever process rebuilt the
    launch instead of to the run. The run directory is the fallback because it
    is absolute by construction; the worker record the run's own supervisor
    writes into it is read first, since that record names the run the launch
    belongs to.
    """
    node = record.get("node")
    candidates = [record.get("manifest_path")]
    if isinstance(node, Mapping):
        candidates.append(node.get("manifest_path"))
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and Path(text).is_absolute():
            return text
    directory = run_dir(run_id)
    try:
        worker = json.loads(
            (directory / WORKER_RECORD_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        worker = None
    if isinstance(worker, Mapping):
        text = str(worker.get("manifest_path") or "").strip()
        if text and Path(text).is_absolute():
            return text
    return str(directory / "manifest.md")


def _carry_declared_placement(
    backend: dict[str, Any],
    record: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> None:
    """Put the lane's declared placement onto a rebuilt backend, in place.

    A resumed run is placed exactly as a dispatch is: its worker belongs in the
    one shared allocation the host holds, and the placement that says so is a
    property of the lane it resumes into rather than of the row it was recorded
    on. The recorded row stays authoritative for the command the run launched
    as, so only the placement is taken from the configured lane — a rebuilt
    backend that already carries one keeps it, and a lane declaring none adds
    nothing, which leaves an unplaced run launched as it always was.
    """
    if flight.placement_for(backend) is not None:
        return
    configured = ((config or {}).get("backends") or {}).get(
        str(record.get("backend") or "")
    )
    placement = flight.placement_for(configured)
    if placement is not None:
        backend["placement"] = placement


def _carry_fence_unprotected(
    record: dict[str, Any],
    plan: _backends.LaunchPlan | None,
    config: Mapping[str, Any] | None,
) -> None:
    """Record the defaults this run's composed fence leaves writable.

    The list is written only when the fence actually composed and a layer named
    a default under ``unprotected_paths``, and removed when it did not, so a run
    carries the key exactly when its fence leaves a default out. A resume and a
    lane change recompose the fence from the resolved config, so the run's own
    record follows the fence its current attempt launched inside rather than the
    one the prior attempt did.
    """
    removed = _backends.fence_unprotected_paths(config=config)
    if removed and _plan_composed_the_fence(plan):
        record["fence_unprotected_paths"] = [str(path) for path in removed]
    else:
        record.pop("fence_unprotected_paths", None)


def resume_plan(
    run_id: str,
    advice: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> _backends.LaunchPlan:
    """Build the invocation that answers a stuck worker in its own session.

    Session reuse is load-bearing rather than an optimisation: the advice only
    makes sense to a worker that still remembers what it tried, so the resumed
    turn must carry the prior context rather than restate it.

    A resumption is judged against the full ceiling rather than the reserved
    portion, because answering a stuck worker is the expenditure the reserve was
    withheld for. It is still held at a genuinely spent quota — a resume into one
    fails anyway, and reporting the reset time is more use than the rejection.
    """
    record = read_pointer(run_id)
    if record.get("launch") != "cli":
        raise CrewError(f"run {run_id!r} is not a spawned run; resume it in-harness")
    if record_process_alive(record, process_alive) is True:
        raise CrewError(
            f"run {run_id!r} still has a live process; observe or stop it before resuming"
        )
    # The ledger and budget lookups below run against the project's own mount,
    # so a run recorded in another repository is refused here and a run whose
    # record names none falls back to the mount rather than to whatever
    # checkout the resuming session happens to stand in.
    resume_project = str(record.get("project") or "")
    resume_root = record.get("repo")
    if resume_project and project_mount_repository(resume_project) is not None:
        resume_root = resolve_project_repository(
            resume_project, resume_root, flag="the run's recorded repository"
        )
    # The pointer is a cache. A stream may already carry the captured session
    # while the next observation has not folded it into that cache yet.
    session = _current_harness_session(record, config=config)
    session_id = str(session.get("session_id") or "")
    fresh_reason = session.get("withheld")
    if not session["resolved"] and not fresh_reason:
        raise CrewError(
            f"run {run_id!r} has no session id in any authority: "
            f"{session.get('detail') or 'no session authority resolved'}"
        )
    backend = _backend_settings(record, config)
    _carry_declared_placement(backend, record, config)
    _carry_declared_gate_documents(backend, record, config)
    lane_gate = _dispatch_lane_gate(backend)
    if lane_gate["state"] in _LANE_GATE_WAITING_STATES:
        raise LanePaused(lane_gate)
    verdict = _budget_verdict(
        project=resume_project,
        root=resume_root,
        config=config,
        backend_name=str(record.get("backend") or ""),
        backend=backend,
        purpose="resume",
    )
    if verdict["held"]:
        raise _actionable_budget_hold(verdict, config=config)
    # A resumed turn re-sends the session's whole context, so a session grown
    # past the lane's input window dies at the endpoint with the attempt file
    # already open, and an attempt whose worker dies at once leaves a delivered
    # manifest reading stale to promotion. The count is the run's own last
    # recorded request input rather than an estimate, and the gate is consulted
    # before the plan is built and before anything is written, so a session the
    # lane cannot hold is refused with a fresh repair node as the remedy.
    window_refusal = resume_window_refusal(
        run_id, record, backend=backend, config=config
    )
    if window_refusal is not None:
        raise window_refusal
    # A second worker on one run is the collision this refuses, and a hand-typed
    # resume starts a worker exactly as the sweep does. The guard above refuses
    # only a process this host found alive, so a run whose end nothing observed —
    # a worker on another machine, or a pointer that never recorded a process —
    # would otherwise be resumed as though its worker were dead. The reading is
    # the sweep's own helper rather than a second composition of it, so what
    # counts as an observed end cannot drift between the two doors, and the
    # refusal names the reading it is holding. Consulted after the launcher's
    # other guards, so the reason reported for a run that fails several is the
    # one the sweep would report for it as well.
    from reckon.crew.resumption import _observed_end_refusal

    observed_end = _observed_end_refusal(record)
    if observed_end is not None:
        raise observed_end
    backend.setdefault("sandbox", record.get("sandbox"))
    # The plan is built — and its executable resolved — before anything is
    # written, so an unresolvable backend refuses a resume exactly as it
    # refuses a dispatch: no pointer field, no advice file, no stream.
    preflight_launch_command(
        str(record.get("backend") or ""), backend, fence=FENCE_WORKERS
    )
    attempt_started_at = _utc_now()
    manifest_path = _recorded_manifest_path(record, run_id)
    resumed_prompt = (
        _lane_prompt(record, advice, fresh_reason["reason"], continued=False)
        if fresh_reason
        else advice
    )
    plan = resolve_launch_executable(
        _backends.launch_plan(
            backend_name=str(record.get("backend") or ""),
            backend=backend,
            prompt=_restate_time_fence(
                resumed_prompt, record, attempt_started_at=attempt_started_at
            ),
            worktree=str(record.get("worktree") or "."),
            manifest_path=manifest_path,
            writable_directories=_fence_write_roots(
                backend=backend,
                repository=str(record.get("repo") or "."),
                run_directory=run_dir(run_id),
                manifest_path=manifest_path,
                worktree=record.get("worktree"),
                declared_write_paths=(record.get("node") or {}).get("write_paths")
                or (),
            ),
            resume_session=session_id or None,
            fence=FENCE_WORKERS,
            fence_config=config,
        )
    )
    # A resume runs where a dispatch runs. A placement-declaring backend's
    # resumed worker must be a step in the one shared allocation a dispatch
    # joins, not a child of the coordinator, so the placement is resolved
    # exactly as a dispatch resolves it — holding or adopting the reservation,
    # then prefixing the overlapping step that names its job id.
    plan = resolve_backend_placement(
        plan, backend, resume_project or None, payload=record
    )
    plan = _worker_runtime_plan(
        plan,
        run_id=run_id,
        manifest_path=manifest_path,
        attempt_started_at=attempt_started_at,
        coordinator_session=str(record.get("session") or ""),
    )

    def capture(current: dict[str, Any]) -> dict[str, Any]:
        # The held reservation's reach statement, resolved above onto the
        # pointer this attempt read, so the pointer persisted below carries it.
        if "placement_reservation" in record:
            current["placement_reservation"] = record["placement_reservation"]
        _carry_fence_unprotected(current, plan, config)
        # The pointer's fence flag describes the attempt that just launched, not
        # the one before it, so a resumed run records what this composition did
        # exactly as the primary dispatch does.
        current["fenced"] = _plan_composed_the_fence(plan)
        current["session_resumed"] = _launched_prior_session(plan) is not None
        if fresh_reason:
            current["session_id"] = None
            current["session_harness"] = None
            current["session_model"] = None
            current["session_withheld"] = fresh_reason
        # Only dispatch measures repository context fit. Resuming a session or
        # starting a replacement must not claim that measurement took place.
        current["context_fit"] = {
            "checked": False,
            "state": "unchecked",
            "window_tokens": backend.get("usable_input_window"),
            "detail": (
                "the resume request proceeds without re-verifying context fit; only "
                "dispatch performs that check against the current repository"
            ),
        }
        return current

    _mutate_pointer(run_id, capture)
    return plan


def _recorded_task_node(record: Mapping[str, Any]) -> TaskNode:
    """Rebuild the immutable dispatch request stored on a live run."""
    data = record.get("node")
    if not isinstance(data, Mapping):
        raise CrewError(f"run {record.get('run_id')!r} records no node definition")
    return TaskNode(
        id=str(data.get("id") or ""),
        goal=str(data.get("goal") or ""),
        plan=str(data.get("plan") or ""),
        section=str(data.get("section") or ""),
        brief=str(data.get("brief") or ""),
        brief_sha256=str(data.get("brief_sha256") or ""),
        brief_path=str(data.get("brief_path") or ""),
        role=str(data.get("role") or record.get("role") or "implement"),
        spec_level=str(data.get("spec_level") or ""),
        done_when=str(data.get("done_when") or ""),
        write_paths=[str(path) for path in data.get("write_paths") or ()],
        time_budget=str(data.get("time_budget") or ""),
        manifest_path=str(
            data.get("manifest_path") or record.get("manifest_path") or ""
        ),
        negative_control=str(data.get("negative_control") or ""),
        estimated_hours=data.get("estimated_hours"),
        requires_decisions=[str(key) for key in data.get("requires_decisions") or ()],
    )


def _worktree_git_read(
    worktree: Path, *arguments: str
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    """Run one bounded, read-only git query inside an inherited worktree."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        return result, detail or f"git {' '.join(arguments)} exited {result.returncode}"
    return result, None


def _inherited_worktree_reading(record: Mapping[str, Any]) -> str:
    """Describe a lane successor's retained worktree without blocking handoff."""
    taken_at = _utc_now()
    worktree_value = str(record.get("worktree") or "").strip()
    worktree = Path(worktree_value) if worktree_value else None
    lines = [
        "INHERITED WORKTREE READING (measured fact)",
        f"Reading taken at: {taken_at}",
        f"Worktree: {worktree_value or '(unset)'}",
    ]
    if worktree is None or not worktree.is_dir():
        return "\n".join(
            [
                *lines,
                "Inherited worktree could not be read.",
                "Reason: the recorded path does not exist or is not a directory.",
            ]
        )

    head_result, head_error = _worktree_git_read(
        worktree, "rev-parse", "--verify", "HEAD"
    )
    status_result, status_error = _worktree_git_read(
        worktree,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--no-renames",
    )
    summary_result, summary_error = _worktree_git_read(
        worktree,
        "diff",
        "--stat",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "HEAD",
        "--",
    )
    failure = head_error or status_error or summary_error
    if failure:
        return "\n".join(
            [
                *lines,
                "Inherited worktree could not be read.",
                f"Reason: {' '.join(str(failure).splitlines())}",
            ]
        )

    assert head_result is not None
    assert status_result is not None
    assert summary_result is not None
    head = head_result.stdout.strip()
    status = status_result.stdout.rstrip("\n")
    recorded_base = str(record.get("base_sha") or record.get("base") or "")
    lines.extend(
        [f"Head commit: {head}", f"Recorded base: {recorded_base or 'not recorded'}"]
    )
    if recorded_base:
        resolved_base = _resolve_commit(worktree, recorded_base)
        if not resolved_base:
            lines.append(
                "Head differs from recorded base: unknown; the recorded base could "
                f"not be resolved ({recorded_base!r} is not a commit)."
            )
        else:
            differs = "yes" if head != resolved_base else "no"
            lines.append(f"Head differs from recorded base: {differs}.")
    else:
        lines.append("Head differs from recorded base: unknown; no base was recorded.")

    if not status:
        lines.extend(
            [
                "Porcelain status: clean (no entries).",
                "Per-file change summary: no changes.",
            ]
        )
        return "\n".join(lines)

    lines.extend(["Porcelain status:", status, "Per-file change summary:"])
    summary = summary_result.stdout.rstrip("\n")
    if summary:
        lines.append(summary)
    untracked = [
        entry[3:]
        for entry in status.splitlines()
        if len(entry) >= 4 and entry.startswith("?? ")
    ]
    lines.extend(f"{path} | untracked" for path in untracked)
    if not summary and not untracked:
        lines.extend(
            f"{entry[3:]} | status {entry[:2]}"
            for entry in status.splitlines()
            if len(entry) >= 4
        )
    lines.append(
        "Checkpoint instruction: Commit the inherited changes before continuing; "
        "an inherited diff is the only copy of that work and a later refusal or "
        "death takes it."
    )
    return "\n".join(lines)


def _lane_prompt(
    record: Mapping[str, Any], advice: str, reason: str, *, continued: bool
) -> str:
    """Return either same-session advice or a complete fresh-start prompt."""
    if continued:
        return advice or f"Continue on the selected backend. Reason: {reason}"
    prompt_path = Path(str(record.get("prompt_path") or ""))
    if not prompt_path.is_file():
        raise CrewError(
            f"run {record.get('run_id')!r} needs a fresh session but its original "
            "prompt is unavailable"
        )
    original = prompt_path.read_text(encoding="utf-8")
    continuation = advice or "Continue the assigned work from its retained worktree."
    reading = _inherited_worktree_reading(record)
    return (
        f"{original.rstrip()}\n\n"
        "EXECUTION BACKEND CHANGED\n"
        f"Reason: {reason}\n\n"
        f"{reading}\n\n"
        "COORDINATOR ADVICE (instruction; passed through unchanged)\n"
        f"{continuation}"
    )


def _restate_time_fence(
    prompt: str, record: Mapping[str, Any], *, attempt_started_at: str
) -> str:
    """Restate the resumed attempt's own time fence on its launch prompt.

    The prompt a resumed attempt launches with — the same-session advice, or a
    fresh-start prompt — was composed for the attempt that already ended, so
    its fence names that attempt's clock. The resumed attempt is given its own
    launch instant and the deadline the recorded budget puts it under, from the
    same instant the launch records as this attempt's start.
    """
    node = record.get("node")
    budget = ""
    if isinstance(node, Mapping):
        budget = str(node.get("time_budget") or "")
    if not budget:
        return prompt
    statement = time_fence_statement(
        time_budget=budget, launch_instant=attempt_started_at
    )
    fence = f"FENCE — TIME (resumed attempt)\n  {statement}\n"
    if not prompt.strip():
        return fence
    return f"{prompt.rstrip()}\n\n{fence}"


def change_lane(
    run_id: str,
    backend_name: str,
    reason: str,
    *,
    config: Mapping[str, Any],
    advice: str = "",
    estimated_hours: float | None = None,
    launch: bool = True,
    launcher=None,
) -> dict[str, Any]:
    """Relaunch one live run elsewhere without replacing its identity.

    A blocked resumption and a working-run redispatch deliberately meet here.
    The destination is fully resolved and budget-checked before the current
    process is stopped. The existing run id, node and worktree stay in place;
    only the execution attempt changes.

    ``estimated_hours`` replaces the estimate the run carried into this attempt
    and is recorded on the run, so an orchestrator can correct an estimate the
    original estimate cannot express without editing the source plan.
    """
    destination = str(backend_name).strip()
    explanation = str(reason).strip()
    if not destination:
        raise CrewError("changing a run's backend requires a destination backend")
    if not explanation:
        raise CrewError("changing a run's backend requires a reason")

    record = read_pointer(run_id)
    source = str(record.get("backend") or "")
    if destination == source:
        raise CrewError(
            f"run {run_id!r} already uses backend {destination!r}; resume it without "
            "a backend override"
        )
    repository = Path(str(record.get("repo") or ".")).resolve()
    node = _recorded_task_node(record)
    if estimated_hours is not None:
        node.estimated_hours = float(estimated_hours)
    resolution = plan_dispatch(
        node=node,
        config=config,
        locked_decisions=node.requires_decisions,
        run_id=run_id,
        project=str(record.get("project") or ""),
        repo=repository,
        base=str(record.get("base_sha") or record.get("base") or "HEAD"),
        execution_override=bool(
            (record.get("execution_fit") or {}).get("override")
            if isinstance(record.get("execution_fit"), Mapping)
            else False
        ),
        backend_override=destination,
        session=str(record.get("session") or ""),
        # A lane change names its destination backend, so the picker has
        # nothing to select and asking it would only be refused.
        route="deterministic",
    )
    if not resolution.validation.ok:
        raise CrewError(
            f"run {run_id!r} cannot move to backend {destination!r} — "
            + "; ".join(
                f"{finding['property']}: {finding['detail']}"
                for finding in resolution.validation.findings
            )
        )
    lane_gate = resolution.lane_gate
    if lane_gate.get("state") in _LANE_GATE_WAITING_STATES:
        raise LanePaused(lane_gate)
    competence = resolution.competence or _competence_verdict(
        resolution=resolution,
        project=str(record.get("project") or ""),
        repo=repository,
    )
    if not competence["allowed"]:
        raise CompetenceLimit(competence)
    backend = resolution.backend_settings
    verdict = _budget_verdict(
        project=str(record.get("project") or ""),
        root=resolve_dispatch_ledger_root(
            resolution.authority
            or resolve_dispatch_authority(str(record.get("project") or ""), repository)
        ),
        config=config,
        backend_name=resolution.backend,
        backend=backend,
        purpose="dispatch",
    )
    if verdict["held"]:
        raise _actionable_budget_hold(verdict, config=config)

    source_launch = str(record.get("launch") or "")
    target_launch = resolution.launch
    source_harness = source_launch
    if source_launch == "cli":
        source_harness = str(record.get("dialect") or "")
        if not source_harness:
            source_harness = _backends.dialect_for(
                _backend_settings(record, config)
            ).name
    target_harness = target_launch
    if target_launch == "cli":
        target_harness = _backends.dialect_for(backend).name
    session = _current_harness_session(record, config=config)
    session_id = str(session.get("session_id") or "")
    continued = bool(
        session["resolved"]
        and source_launch == target_launch == "cli"
        and source_harness == target_harness
    )
    prompt = _lane_prompt(record, advice, explanation, continued=continued)
    attempt = int(record.get("attempt") or 1) + 1
    directory = run_dir(run_id)
    manifest_path = _recorded_manifest_path(record, run_id)
    prompt_path = directory / f"lane-change-{attempt}-prompt.txt"
    log_path = directory / f"lane-change-{attempt}.jsonl"
    stderr_path = directory / f"lane-change-{attempt}.stderr.log"
    final_path = directory / f"lane-change-{attempt}-final.txt"
    lane_change = {
        "from_backend": source,
        "to_backend": resolution.backend,
        "reason": explanation,
        "changed_at": _utc_now(),
        "from_harness": source_harness,
        "to_harness": target_harness,
        "session": "continued" if continued else "fresh",
        "session_id": session_id or None,
        "session_source": session.get("source"),
        "detail": (
            f"continued session {session_id!r} on harness {target_harness!r}"
            if continued
            else (
                "starting fresh because the session cannot follow the move from "
                f"harness {source_harness!r} to {target_harness!r}"
            )
        ),
    }
    # An in-harness attempt is delegated, so its directive is the only place the
    # attempt identity can be carried: the harness exports this environment to
    # the task it spawns. Composed here so the preview and the persisted record
    # name the same environment the launch path attaches.
    directive_environment = _worker_runtime_environment(
        None,
        run_id=run_id,
        manifest_path=manifest_path,
        attempt_started_at=lane_change["changed_at"],
        coordinator_session=str(record.get("session") or ""),
        claude_headers=False,
    )
    target_plan: _backends.LaunchPlan | None = None
    if target_launch == "cli":
        preflight_launch_command(resolution.backend, backend, fence=FENCE_WORKERS)
        target_plan = resolve_launch_executable(
            _backends.launch_plan(
                backend_name=resolution.backend,
                backend=backend,
                prompt=prompt,
                worktree=str(record.get("worktree") or "."),
                manifest_path=manifest_path,
                writable_directories=_fence_write_roots(
                    backend=backend,
                    repository=str(record.get("repo") or "."),
                    run_directory=directory,
                    manifest_path=manifest_path,
                    worktree=record.get("worktree"),
                    declared_write_paths=(record.get("node") or {}).get("write_paths")
                    or (),
                ),
                final_message_path=str(final_path),
                resume_session=session_id if continued else None,
                fence=FENCE_WORKERS,
                fence_config=config,
            )
        )
        target_plan = _worker_runtime_plan(
            target_plan,
            run_id=run_id,
            manifest_path=manifest_path,
            attempt_started_at=lane_change["changed_at"],
            coordinator_session=str(record.get("session") or ""),
        )
    preview: dict[str, Any] = {
        "run_id": run_id,
        "node": record.get("node"),
        "worktree": record.get("worktree"),
        "backend": resolution.backend,
        "launch": target_launch,
        "lane_change": lane_change,
    }
    if target_plan is not None:
        preview.update(target_plan.as_dict())
    else:
        preview["directive"] = {
            "attach_with": f"reckon crew attach --run {run_id} --task <task-id>",
            "environment": directive_environment,
            "prompt_path": str(prompt_path),
            "worktree": str(record.get("worktree") or ""),
        }
    # The launch path's refusals are read once, ahead of the preview return, so
    # a --print-only lane change names the refusal a real call would raise. The
    # resume sweep orders its own gates the same way — both ahead of its dry-run
    # branch — because a prediction that reads differently from the thing it
    # predicts is not a prediction. The stop a launch performs is a side effect
    # and stays below, where a preview cannot reach it.
    if (
        source_launch == "in-harness"
        and record.get("task")
        and str(record.get("phase") or "") not in _TERMINAL_RUN_PHASES
    ):
        raise CrewError(
            f"run {run_id!r} is attached to live harness task {record['task']!r}; "
            "cancel it in that harness before changing backend"
        )
    source_process_alive = (
        source_launch == "cli" and record_process_alive(record, process_alive) is True
    )
    if source_launch == "cli" and not source_process_alive:
        # "Not known to be alive" is not an observed end. A worker whose pointer
        # recorded no process, or whose pid this host cannot answer for, may
        # still be writing, so a fresh worker started over it is the collision
        # the stop below prevents — the same rule the resume door applies. The
        # reading is the sweep's own helper rather than a second composition of
        # it, so what counts as an observed end cannot drift between the two
        # doors, and the refusal names the reading it holds.
        from reckon.crew.resumption import _observed_end_refusal

        refusal = _observed_end_refusal(record)
        if refusal is not None:
            raise refusal

    if not launch:
        return preview

    if source_process_alive:
        _signal_process_group(
            int(record["pid"]),
            record.get("pid_start_time"),
            run_dir=directory,
            reason="lane-change",
        )

    directory.mkdir(parents=True, exist_ok=True)
    # The lane-change prompt was composed for the attempt that already ended —
    # the bare advice for a continued session, or the original prompt with its
    # original fence for a fresh one — so restating the fence for the attempt now
    # starting is what stops the worker reading the first attempt's deadline. The
    # resume path does the same through resume_plan.
    prompt_path.write_text(
        _restate_time_fence(
            prompt, record, attempt_started_at=lane_change["changed_at"]
        ),
        encoding="utf-8",
    )
    spawned_pid: int | None = None
    if target_plan is not None:
        spawn = launcher or _spawn
        spawned_pid = spawn(
            target_plan,
            log_path=log_path,
            stderr_path=stderr_path,
            prompt_path=prompt_path,
        )

    def move(current: dict[str, Any]) -> dict[str, Any]:
        if str(current.get("worktree") or "") != str(record.get("worktree") or ""):
            raise CrewError(f"run {run_id!r} changed worktree during its lane change")
        if isinstance(current.get("node"), dict):
            current["node"]["estimated_hours"] = node.estimated_hours
        history = [dict(item) for item in current.get("lane_changes") or ()]
        history.append(lane_change)
        lineage = {
            "kind": "lane-change",
            "attempt": attempt,
            "root_run_id": run_id,
            "lanes": history,
        }
        current.update(
            {
                "backend": resolution.backend,
                "route": getattr(resolution, "route", current.get("route")),
                "route_override": getattr(
                    resolution, "route_override", current.get("route_override")
                ),
                "launch": target_launch,
                "sandbox": backend.get("sandbox"),
                "sandbox_write_roots": (
                    None
                    if resolution.sandbox_write_roots is None
                    else [str(path) for path in resolution.sandbox_write_roots]
                ),
                "session_reuse_capable": bool(backend.get("session_reuse")),
                "session_resumed": _launched_prior_session(target_plan) is not None,
                "agent": _stamp_agent_display(
                    _agent_configuration(resolution.backend, target_launch, backend),
                    backend,
                ),
                "attempt": attempt,
                "attempt_kind": "lane-change",
                "attempt_started_at": lane_change["changed_at"],
                "estimated_hours": node.estimated_hours,
                "phase": "working" if target_plan is not None else "starting",
                "session_id": session_id if continued else None,
                "session_harness": target_harness if continued else None,
                "session_model": backend.get("model") if continued else None,
                "pid": spawned_pid,
                "pid_start_time": (
                    _process_start_time(spawned_pid)
                    if spawned_pid is not None
                    else None
                ),
                "task": None,
                "prompt_path": str(prompt_path),
                "log_path": str(log_path),
                "stderr_path": str(stderr_path),
                "final_message_path": str(final_path),
                "manifest_baseline_mtime_ns": _manifest_mtime_ns(
                    current.get("manifest_path") or ""
                ),
                "budget": _backends.unknown_budget("no events yet on the new lane"),
                "lane_change": lane_change,
                "lane_changes": history,
                "lineage": lineage,
            }
        )
        _carry_fence_unprotected(current, target_plan, config)
        # The fence flag follows the attempt the lane change just launched. A
        # CLI run moved to an in-harness backend composes no fence at all, so
        # the flag must fall false rather than carry the prior attempt's true
        # beside the absent removed-defaults list.
        current["fenced"] = _plan_composed_the_fence(target_plan)
        if target_plan is not None:
            current.update(
                {
                    "argv": list(target_plan.argv),
                    "command": str(target_plan.argv[0]),
                    "dialect": target_plan.dialect,
                }
            )
            current.pop("directive", None)
        else:
            current.update(
                {
                    "argv": None,
                    "command": None,
                    "dialect": None,
                    "directive": {
                        "attach_with": (
                            f"reckon crew attach --run {run_id} --task <task-id>"
                        ),
                        "fences": {
                            "delivery": str(current.get("manifest_path") or ""),
                            "evidence": node.done_when,
                            "scope": list(node.write_paths),
                            "time": node.time_budget,
                        },
                        "environment": directive_environment,
                        "prompt_path": str(prompt_path),
                        "sandbox": {
                            "tier": backend.get("sandbox"),
                            "write_roots": current["sandbox_write_roots"],
                        },
                        "worktree": str(current.get("worktree") or ""),
                    },
                }
            )
        return current

    return _mutate_pointer(run_id, move)


def terminate(run_id: str) -> dict[str, Any]:
    """Signal a spawned run's process group to stop, and record that."""

    def stop(record: dict[str, Any]) -> dict[str, Any]:
        pid = record.get("pid")
        if not pid:
            raise CrewError(f"run {run_id!r} has no process to stop")
        try:
            _signal_process_group(
                int(pid),
                record.get("pid_start_time"),
                run_dir=run_dir(run_id),
                reason="run-stop",
            )
        except (ProcessLookupError, PermissionError, OSError) as exc:
            record["detail"] = f"could not signal pid {pid} — {exc}"
        else:
            record["detail"] = f"SIGTERM sent to process group of pid {pid}"
        record["phase"] = "stopped"
        record["stopped_at"] = _utc_now()
        return record

    return _mutate_pointer(run_id, stop)


def record_resumption(
    run_id: str,
    *,
    pid: int,
    turn: int,
    log_path: str | Path,
    stderr_path: str | Path,
    attempt_started_at: str = "",
    manifest_baseline_mtime_ns: int | None = None,
) -> dict[str, Any]:
    """Record a launched resumption without overwriting newer observations."""

    def resume(record: dict[str, Any]) -> dict[str, Any]:
        from reckon.crew.resumption import resolve_session

        prior_session_resumed = record.get("session_resumed")
        if prior_session_resumed is None:
            prior_session_resumed = resolve_session(run_id, record=record)["resolved"]
        current_attempt = bool(
            attempt_started_at or manifest_baseline_mtime_ns is not None
        )
        record.update(
            {
                "pid": pid,
                "pid_start_time": _process_start_time(pid),
                "phase": "working",
                "attempt": int(record.get("attempt") or 1) + 1,
                "attempt_kind": "resume",
                # A harness change may require a fresh session even though
                # this attempt was requested through the resume command.
                "session_resumed": bool(prior_session_resumed),
                "attempt_started_at": attempt_started_at or _utc_now(),
                "manifest_baseline_mtime_ns": (
                    _manifest_mtime_ns(record.get("manifest_path") or "")
                    if manifest_baseline_mtime_ns is None
                    else manifest_baseline_mtime_ns
                ),
                "resumed_turn": turn,
                "log_path": (
                    str(log_path) if current_attempt else record.get("log_path")
                ),
                "stderr_path": str(stderr_path),
                # A new attempt has made no observations, so it must not inherit
                # the previous attempt's folded budget observation: a refusal
                # folded from the superseded stream would otherwise short-circuit
                # the refusal classifier before it reads the live resume stream.
                # The honest unknown-headroom state keeps the reader on the
                # stream that is actually running.
                "budget": _backends.unknown_budget(
                    "no events yet on the resumed attempt"
                ),
            }
        )
        return record

    return _mutate_pointer(run_id, resume)

from .dispatch_admission import (  # noqa: E402
    LanePaused,
    _LANE_GATE_WAITING_STATES,
    _actionable_budget_hold,
    _dispatch_lane_gate,
    _fence_write_roots,
)

from .dispatch_launch import (  # noqa: E402
    LAUNCH_FAILED_PHASE,
    WORKER_RECORD_NAME,
    _empty_stream_launch_failure,
    _launched_prior_session,
    _plan_composed_the_fence,
    _spawn,
    _worker_runtime_environment,
    _worker_runtime_plan,
    preflight_launch_command,
    resolve_backend_placement,
    resolve_launch_executable,
)

from .dispatch_peer import (  # noqa: E402
    _stamp_agent_display,
)

from .dispatch_plan import (  # noqa: E402
    _carry_declared_gate_documents,
    plan_dispatch,
)

from .dispatch_sections import (  # noqa: E402
    project_mount_repository,
    resolve_project_repository,
)
