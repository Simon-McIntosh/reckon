from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from reckon import _backends, capability, ledger
from reckon.crew.node import BudgetHold, CompetenceLimit, CrewError, DEFAULT_MEMBER_IDLE_WINDOW, MemberInFlight, NodeValidation, TaskNode, UnreconciledRuns, WatcherRequired, _SAFE_ID, _TERMINAL_RUN_PHASES, normalize_section, validate_node
from reckon.crew.prompts import compose_prompt
from reckon.crew.routing import _agent_configuration, _budget_verdict, _competence_verdict, _create_worktree, _register_session_member, _remove_worktree, _require_write_paths_in_repository, _session_member_id, _signal_process_group, _workspace_roots, reap_idle_session_members, require_plan_section_visible, resolve_dispatch_authority, resolve_role, resolved_time_budget, resolved_time_ceiling
from reckon.crew.runs import _live_scope_claims, _manifest_freshness, _manifest_mtime_ns, _merge_peer_scopes, _mutate_pointer, _process_start_time, _project_derivations, _raise_live_scope_conflict, _utc_now, _watch_arming_line, _write_json, list_live, new_run_id, pointer_path, process_alive, read_pointer, run_dir, watch_state

@dataclass
class DispatchPlan:
    """Everything a dispatch resolved, before anything on disk has changed.

    Separating resolution from effect is what lets a dry run be the *same*
    decision as a real dispatch rather than a second implementation of it: a
    caller can see the routing, the filled-in defaults and the verdict without
    a worktree or a process existing.
    """

    run_id: str
    backend: str
    launch: str
    backend_settings: dict[str, Any]
    node: TaskNode
    budget_ceiling: str
    validation: NodeValidation
    execution_fit: capability.ExecutionFit
    warnings: list[str] = field(default_factory=list)
    competence: dict[str, Any] | None = None
    authority: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "backend": self.backend,
            "execution_fit": self.execution_fit.as_dict(),
            "launch": self.launch,
            "node": self.node.as_dict(),
            "run_id": self.run_id,
            "time_budget": self.node.time_budget,
            "validation": self.validation.as_dict(),
            "write_paths": list(self.node.write_paths),
            "warnings": list(self.warnings),
        }
        if self.competence is not None:
            payload["competence"] = dict(self.competence)
        if self.authority is not None:
            payload["authority"] = dict(self.authority)
        return payload


def _path_is_tmpfs(path: str | Path) -> bool:
    """Return whether a path resolves beneath a tmpfs or ramfs mount."""
    target = Path(path).expanduser().resolve()
    best: tuple[int, str] | None = None
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        fields, separator, trailing = line.partition(" - ")
        if not separator:
            continue
        parts = fields.split()
        trailing_parts = trailing.split()
        if len(parts) < 5 or not trailing_parts:
            continue
        mount = Path(parts[4].replace("\\040", " ")).resolve()
        if target == mount or target.is_relative_to(mount):
            candidate = (len(mount.parts), trailing_parts[0])
            if best is None or candidate[0] > best[0]:
                best = candidate
    return bool(best and best[1] in {"tmpfs", "ramfs"})


def plan_dispatch(
    *,
    node: TaskNode,
    config: Mapping[str, Any],
    locked_decisions: Iterable[str] = (),
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
    run_id: str | None = None,
    project: str = "",
    repo: str | Path | None = None,
    base: str = "HEAD",
    execution_override: bool = False,
    authority: Mapping[str, Any] | None = None,
) -> DispatchPlan:
    """Resolve routing and defaults for one node and judge it. No side effects.

    Mutates only the node it was handed, filling the defaults a dispatch would
    fill — the time budget from the resolved fence and the manifest path from
    the run directory — so the verdict is the one a real dispatch would reach.
    """
    if not _SAFE_ID.fullmatch(node.id):
        raise CrewError(f"node id {node.id!r} must match {_SAFE_ID.pattern}")
    if node.spec_level not in ("", "exact", "guided", "open"):
        raise CrewError(
            f"spec level {node.spec_level!r} is not one of exact, guided, open, "
            "or empty (undeclared)"
        )
    backend_name, backend = resolve_role(config, node.role, node.spec_level)
    launch_kind = backend.get("launch")
    if launch_kind not in ("cli", "in-harness"):
        raise CrewError(
            f"backend {backend_name!r} declares launch {launch_kind!r}; "
            "expected 'cli' or 'in-harness'"
        )
    default_budget = resolved_time_budget(config, backend)
    budget_ceiling = resolved_time_ceiling(config)
    node.time_budget = node.time_budget or default_budget
    node.section = normalize_section(node.section)
    resolved_run_id = run_id or new_run_id(node.id)
    durable_manifest = str(run_dir(resolved_run_id) / "manifest.md")
    caller_manifest = bool(node.manifest_path)
    node.manifest_path = node.manifest_path or durable_manifest
    warnings = list(getattr(config, "warnings", ()))
    if caller_manifest and _path_is_tmpfs(node.manifest_path):
        warnings.append(
            f"manifest path {node.manifest_path!r} is on tmpfs; use the durable "
            f"default {durable_manifest!r} so delivery survives session cleanup"
        )
    node.peer_scopes = {
        name: list(paths) for name, paths in (peer_scopes or {}).items()
    }
    execution_fit = capability.assess_execution_fit(
        node.done_when,
        role=node.role,
        execution_capable=backend.get("execution_capable"),
        override=execution_override,
    )
    verdict = validate_node(
        node, locked_decisions=locked_decisions, budget_ceiling=budget_ceiling
    )
    if not execution_fit.allowed:
        verdict = NodeValidation(
            ok=False,
            findings=[
                *verdict.findings,
                {
                    "property": "fully-specified",
                    "detail": execution_fit.refusal_detail(),
                },
            ],
        )
    resolved_authority: dict[str, Any] | None = None
    if verdict.ok and repo is not None:
        resolved_authority = dict(
            authority or resolve_dispatch_authority(project, repo)
        )
        _require_write_paths_in_repository(node, resolved_authority)
        plan_commit = require_plan_section_visible(
            node=node,
            project=project,
            repo=repo,
            base=base,
            authority=resolved_authority,
        )
        resolved_authority["plan"] = {
            **resolved_authority["plan"],
            "base_sha": plan_commit,
        }
    resolution = DispatchPlan(
        run_id=resolved_run_id,
        backend=backend_name,
        launch=str(launch_kind),
        backend_settings=backend,
        node=node,
        budget_ceiling=budget_ceiling,
        validation=verdict,
        execution_fit=execution_fit,
        warnings=warnings,
        authority=resolved_authority,
    )
    if verdict.ok and repo is not None:
        resolution.competence = _competence_verdict(
            resolution=resolution, project=project, repo=Path(repo).resolve()
        )
    return resolution


def dispatch(
    *,
    node: TaskNode,
    project: str,
    repo: str | Path,
    config: Mapping[str, Any],
    session: str,
    base: str = "HEAD",
    locked_decisions: Iterable[str] = (),
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
    member: str = "",
    launcher=None,
    check_budget: bool = True,
    budget_state: Mapping[str, Any] | None = None,
    execution_override: bool = False,
    unreconciled_override: bool = False,
    watch_required: bool = False,
    watch_override: bool = False,
) -> dict[str, Any]:
    """Validate, prepare and launch one node; return its run record.

    The single branch is on launch kind. A ``cli`` backend is spawned here and
    the caller yields on the returned run id. An ``in-harness`` backend cannot
    be spawned by reckon at all, so everything a worker needs is prepared and
    returned as a directive the calling harness dispatches itself, binding its
    task back with :func:`attach`.

    Naming a roster ``member`` routes the node into that member's long-lived
    session. Omitting it provisions a private member derived from the dispatching
    session, so independent coordinators never shop from a shared free list. A
    member whose worker session is still null gets one captured on its first run.

    A node whose backend has no headroom left is *held* rather than dispatched:
    :class:`BudgetHold` is raised before any worktree exists, so the node stays
    ready and nothing has to be judged or unwound. Holding costs nothing; a wave
    launched into a spent quota costs its whole setup plus half-finished commits.

    Either way the operation is atomic: a failure after the worktree exists
    removes it and writes no pointer, so no orphan is left holding write scope.
    An execution-fit override is an explicit exception to a heuristic refusal;
    the matched measure and resolved role stay on the run record so the exception
    remains visible after the request that supplied it is gone.

    An unreconciled-run override is narrower: it waives only the terminal
    backlog observed by this dispatch. The exact runs and resolving commands
    are copied onto the new record so the exception survives its command line.

    Process-launching dispatches require a live project watcher.
    ``watch_required`` is explicit here so library callers and in-harness
    preparation, which launches no worker, keep control of that policy. A watch
    override records both the arming command and the liveness observed at the
    dispatch gate.
    """
    repo_root = Path(repo).resolve()
    authority = resolve_dispatch_authority(project, repo_root)
    resolution = plan_dispatch(
        node=node,
        config=config,
        locked_decisions=locked_decisions,
        peer_scopes=peer_scopes,
        project=project,
        repo=repo_root,
        base=base,
        execution_override=execution_override,
        authority=authority,
    )
    if not resolution.validation.ok:
        raise CrewError(
            "node is not dispatchable — "
            + "; ".join(
                f"{finding['property']}: {finding['detail']}"
                for finding in resolution.validation.findings
            )
        )

    script = repo_root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py"
    if not script.is_file():
        raise CrewError(
            f"worktree fleet script is missing: {script}; run `reckon sync` "
            "for this repository to install it"
        )
    _workspace_roots(repo_root)
    work_projects = authority["write"]["projects"]
    derivations = _project_derivations(work_projects[0], repo_root)
    live_claims = _live_scope_claims(project, repo_root, derivations)

    competence = resolution.competence or _competence_verdict(
        resolution=resolution, project=project, repo=repo_root
    )
    if not competence["allowed"]:
        raise CompetenceLimit(competence)

    fences = config.get("fences") or {}
    unreconciled_grace = str(fences.get("unreconciled_run_grace") or "")
    from reckon.crew.recovery import overdue_unreconciled_runs

    unreconciled = overdue_unreconciled_runs(
        project=project,
        grace=unreconciled_grace,
    )
    if unreconciled and not unreconciled_override:
        raise UnreconciledRuns(unreconciled, unreconciled_grace)
    waiver = (
        {
            "requested": True,
            "grace": unreconciled_grace,
            "waived_runs": unreconciled,
        }
        if unreconciled_override
        else None
    )

    budget_warnings: list[str] = []
    if check_budget:
        # Before the worktree, not after: a hold that had already cut a worktree
        # would leave write scope claimed by a node nobody is running.
        verdict = _budget_verdict(
            project=project,
            root=repo_root,
            config=config,
            backend_name=resolution.backend,
            backend=resolution.backend_settings,
            purpose="dispatch",
            budget_state=budget_state,
        )
        budget_warnings.extend(verdict.get("warnings") or ())
        if verdict["held"]:
            raise BudgetHold(verdict)

    backend_name = resolution.backend
    backend = resolution.backend_settings
    launch_kind = resolution.launch
    run_id = resolution.run_id
    directory = run_dir(run_id)
    peers = _merge_peer_scopes(live_claims, node.peer_scopes)
    node.peer_scopes = peers

    reap_idle_session_members(
        project,
        root=repo_root,
        idle_window=str(
            fences.get("member_idle_window") or DEFAULT_MEMBER_IDLE_WINDOW
        ),
    )
    named_member = bool(member)
    effective_member = member or _session_member_id(session)
    roster_member = ledger.member(project, effective_member, root=repo_root)
    if named_member:
        if roster_member is None:
            raise CrewError(
                f"project {project!r} has no crew member {member!r}; register it "
                "with `reckon crew member add` before dispatching to it"
            )
    if roster_member is not None:
        for pointer in list_live(project=project):
            if (
                Path(str(pointer.get("repo") or "")).resolve() == repo_root
                and pointer.get("member") == effective_member
                and pointer.get("phase") not in _TERMINAL_RUN_PHASES
            ):
                raise MemberInFlight(
                    effective_member, str(pointer.get("run_id") or "unknown")
                )
    _raise_live_scope_conflict(node, live_claims, repo_root, derivations)
    reuse_session = (
        str(roster_member.get("session_id"))
        if roster_member
        and roster_member.get("session_id")
        and backend.get("session_reuse")
        else None
    )
    prior_node_runs = [
        item
        for item in ledger.runs(project, root=repo_root)
        if str(item.get("node") or "") == node.id
    ]
    lineage = None
    attempt = 1
    if prior_node_runs:
        previous = prior_node_runs[-1]
        previous_lineage = previous.get("lineage") or {}
        previous_attempt = previous.get("attempt") or previous_lineage.get("attempt")
        try:
            attempt = int(previous_attempt) + 1
        except (TypeError, ValueError):
            attempt = len(prior_node_runs) + 1
        lineage = {
            "kind": "redispatch",
            "attempt": attempt,
            "root_run_id": previous_lineage.get("root_run_id")
            or str(prior_node_runs[0].get("run_id") or ""),
            "previous_run_id": str(previous.get("run_id") or ""),
        }

    dispatch_watch = watch_state(project)
    starts_worker = resolution.launch == "cli"
    if (
        watch_required
        and starts_worker
        and not dispatch_watch["watcher_live"]
        and not watch_override
    ):
        raise WatcherRequired(project, dispatch_watch)
    watcher_waiver = (
        {
            "requested": True,
            "arming_line": dispatch_watch["arming_line"],
            "watcher_live": bool(dispatch_watch["watcher_live"]),
        }
        if watch_override
        else None
    )

    worktree = _create_worktree(repo_root, session, node.id, base)
    spawned_pid: int | None = None
    spawned_start_time: str | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        working_directory = worktree["path"]
        if launch_kind == "cli":
            working_directory = _backends.launch_working_directory(
                backend=backend,
                worktree=worktree["path"],
                manifest_path=node.manifest_path,
            )
        prompt = compose_prompt(
            node=node,
            project=project,
            worktree=worktree["path"],
            working_directory=working_directory,
            manifest_path=node.manifest_path,
            time_budget=node.time_budget,
            needs_help_after_failures=int(fences.get("needs_help_after_failures", 2)),
            peer_scopes=peers,
        )
        prompt_path = directory / "prompt.txt"
        prompt_path.write_text(prompt)
        log_path = directory / "stream.jsonl"
        stderr_path = directory / "stderr.log"
        final_path = directory / "final.txt"

        record: dict[str, Any] = {
            "run_id": run_id,
            "project": project,
            "repo": str(repo_root),
            "authority": resolution.authority,
            "session": session,
            "node": node.as_dict(),
            "role": node.role,
            "backend": backend_name,
            "execution_fit": resolution.execution_fit.as_dict(),
            "launch": launch_kind,
            "sandbox": backend.get("sandbox"),
            "session_reuse": bool(backend.get("session_reuse")),
            "member": effective_member,
            # The configuration that actually ran the node, recorded now because
            # a later config layer change makes it unreconstructable — and
            # without it a measured duration cannot be attributed to anything.
            "agent": _agent_configuration(backend_name, launch_kind, backend),
            "competence": competence,
            "worktree": worktree["path"],
            "base": worktree["base"],
            "base_sha": worktree["base_sha"],
            "prompt_path": str(prompt_path),
            "log_path": str(log_path),
            "stderr_path": str(stderr_path),
            "final_message_path": str(final_path),
            "manifest_path": node.manifest_path,
            "manifest_baseline_mtime_ns": _manifest_mtime_ns(node.manifest_path),
            "peer_scopes": {name: sorted(paths) for name, paths in peers.items()},
            "created_at": _utc_now(),
            "attempt": attempt,
            "attempt_kind": "redispatch" if lineage else "dispatch",
            "attempt_started_at": _utc_now(),
            "phase": "starting",
            "session_id": reuse_session,
            "task": None,
            "pid": None,
            "argv": None,
            "dialect": None,
            "budget": _backends.unknown_budget("no events yet"),
            "warnings": [*resolution.warnings, *budget_warnings],
            "lineage": lineage,
            "unreconciled_override": waiver,
            "watch_override": watcher_waiver,
            "watch": {
                "arming_line": _watch_arming_line(project),
                "watcher_live": False,
                "watcher": {},
            },
        }

        if launch_kind == "cli":
            plan = _backends.launch_plan(
                backend_name=backend_name,
                backend=backend,
                prompt=prompt,
                worktree=worktree["path"],
                manifest_path=node.manifest_path,
                final_message_path=str(final_path),
                resume_session=reuse_session,
            )
            spawn = launcher or _spawn
            spawned_pid = spawn(
                plan,
                log_path=log_path,
                stderr_path=stderr_path,
                prompt_path=prompt_path,
            )
            spawned_start_time = _process_start_time(spawned_pid)
            record.update(
                {
                    "pid": spawned_pid,
                    "pid_start_time": spawned_start_time,
                    "argv": list(plan.argv),
                    "dialect": plan.dialect,
                }
            )
        else:
            record["directive"] = {
                "attach_with": f"reckon crew attach --run {run_id} --task <task-id>",
                "fences": {
                    "delivery": node.manifest_path,
                    "evidence": node.done_when,
                    "scope": list(node.write_paths),
                    "time": node.time_budget,
                },
                "prompt_path": str(prompt_path),
                "worktree": worktree["path"],
            }

        # Publish the pointer before probing the watcher. Otherwise a watcher
        # could drain an empty fleet between the probe and this write, leaving
        # a new run behind a payload that incorrectly said it was watched.
        _write_json(pointer_path(run_id), record)
        record["watch"] = watch_state(project)
        _write_json(pointer_path(run_id), record)
        if roster_member is None:
            _register_session_member(
                project,
                effective_member,
                backend=backend_name,
                role=node.role,
                root=repo_root,
            )
    except Exception:
        if spawned_pid is not None:
            try:
                _signal_process_group(spawned_pid, spawned_start_time)
            except (CrewError, OSError):
                pass
        _remove_worktree(repo_root, worktree["path"])
        shutil.rmtree(directory, ignore_errors=True)
        pointer_path(run_id).unlink(missing_ok=True)
        raise
    return record


def _spawn(
    plan: _backends.LaunchPlan,
    *,
    log_path: Path,
    stderr_path: Path,
    prompt_path: Path,
) -> int:
    """Start the backend detached, with its event stream landing on disk.

    The prompt is fed from a file rather than a pipe so the caller never blocks
    on a full pipe buffer, and so the exact prompt stays recoverable beside the
    stream it produced. ``start_new_session`` detaches the worker from the
    caller's process group: a dispatching agent that ends its turn must not take
    its workers down with it.
    """
    with (
        open(prompt_path, "rb") as stdin,
        open(log_path, "wb") as stdout,
        open(stderr_path, "wb") as stderr,
    ):
        process = subprocess.Popen(
            plan.argv,
            cwd=plan.cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    return process.pid


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


def observe(run_id: str, *, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from reckon.crew.recovery import _apply_budget_watchdog
    from reckon.crew.reports import parse_manifest

    """Fold a run's on-disk evidence back into its pointer and return it.

    Reads the event stream, the manifest path and process liveness, then writes
    the derived phase, session id and budget block into the record. Everything
    it reports is recoverable from disk, so a fresh session can observe a run it
    did not dispatch.
    """

    def fold(record: dict[str, Any]) -> dict[str, Any]:
        backend_name = str(record.get("backend") or "")
        manifest = Path(record.get("manifest_path") or "")
        manifest_file_present, manifest_fresh = _manifest_freshness(record)
        record["manifest_file_present"] = manifest_file_present
        record["manifest_fresh"] = manifest_fresh
        record["manifest_present"] = manifest_fresh
        record["process_alive"] = process_alive(record.get("pid"))
        record["observed_at"] = _utc_now()
        stopped = record.get("phase") == "stopped"

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
            record["phase"] = "stopped" if stopped else data["phase"]
            if (
                not stopped
                and record.get("attempt_kind") == "resume"
                and record["process_alive"] is True
                and record["phase"] in _TERMINAL_RUN_PHASES
            ):
                record["phase"] = "working"
            record["session_id"] = data["session_id"] or record.get("session_id")
            if data["detail"]:
                record["detail"] = data["detail"]
            final_file = Path(record.get("final_message_path") or "")
            if not record["final_message"] and final_file.is_file():
                record["final_message"] = final_file.read_text().strip() or None
            if (
                not stopped
                and data["phase"] in ("starting", "working")
                and record["process_alive"] is False
            ):
                # A dead process with no terminal event is a recoverable orphan,
                # not a finished run. An empty log counts because argument
                # failures can exit before the first event is written.
                record["phase"] = "orphaned"
                record["detail"] = (
                    "process exited without a terminal event in its log; "
                    f"check {record.get('stderr_path')}"
                )
        elif record.get("task") and record["manifest_present"] and not stopped:
            manifest_status = str(
                parse_manifest(manifest.read_text()).get("status") or ""
            ).strip()
            if manifest_status:
                record["phase"] = manifest_status

        _apply_budget_watchdog(record, config)

        capture = _capture_member_session(record)
        if capture is not None:
            record["session_capture"] = capture
        return record

    return _mutate_pointer(run_id, fold)


def _capture_member_session(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Persist a run's session id onto its roster member, if it has one.

    Observation is where a backend's session id first becomes knowable, so it is
    also where the roster learns it — waiting for completion would leave a second
    node dispatched in the meantime unable to reach the same session.
    """
    member = record.get("member")
    session_id = record.get("session_id")
    if not member or not session_id:
        return None
    try:
        return ledger.capture_session(
            str(record.get("project") or ""),
            str(member),
            str(session_id),
            root=record.get("repo"),
        )
    except (ledger.LedgerError, OSError) as exc:
        # The record being promoted carries the session id anyway, so a roster
        # write that cannot happen must not fail the promotion around it.
        return {"captured": False, "member": None, "detail": str(exc)}


def _backend_settings(
    record: Mapping[str, Any], config: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Recover the backend settings needed to read a run's stream.

    Only the command matters for reading, and the recorded argv already holds
    it, so a run stays observable after its config layer changes — which is the
    difference between a durable record and one that decays.
    """
    argv = record.get("argv")
    if isinstance(argv, list) and argv:
        return {"launch": "cli", "command": argv[0]}
    backends = (config or {}).get("backends") or {}
    backend = backends.get(record.get("backend"))
    if isinstance(backend, Mapping):
        return dict(backend)
    raise CrewError(
        f"run {record.get('run_id')!r} records no argv and its backend is not "
        "in the supplied config, so its stream cannot be read"
    )


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
    if process_alive(record.get("pid")) is True:
        raise CrewError(
            f"run {run_id!r} still has a live process; observe or stop it before resuming"
        )
    session_id = record.get("session_id")
    if not session_id:
        record = observe(run_id, config=config)
        session_id = record.get("session_id")
    if not session_id:
        raise CrewError(f"run {run_id!r} has no session id in its current stream")
    backend = _backend_settings(record, config)
    verdict = _budget_verdict(
        project=str(record.get("project") or ""),
        root=record.get("repo"),
        config=config,
        backend_name=str(record.get("backend") or ""),
        backend=backend,
        purpose="resume",
    )
    if verdict["held"]:
        raise BudgetHold(verdict)
    backend.setdefault("sandbox", record.get("sandbox"))
    return _backends.launch_plan(
        backend_name=str(record.get("backend") or ""),
        backend=backend,
        prompt=advice,
        worktree=str(record.get("worktree") or "."),
        manifest_path=str(record.get("manifest_path") or ""),
        resume_session=str(session_id),
    )


def terminate(run_id: str) -> dict[str, Any]:
    """Signal a spawned run's process group to stop, and record that."""

    def stop(record: dict[str, Any]) -> dict[str, Any]:
        pid = record.get("pid")
        if not pid:
            raise CrewError(f"run {run_id!r} has no process to stop")
        try:
            _signal_process_group(int(pid), record.get("pid_start_time"))
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
            }
        )
        return record

    return _mutate_pointer(run_id, resume)
