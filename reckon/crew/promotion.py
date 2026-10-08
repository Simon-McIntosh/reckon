from __future__ import annotations

# This module retains its imported names as the package compatibility surface.
# ruff: noqa: F401,E402
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import (
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import (
    contextmanager,
)
from dataclasses import (
    dataclass,
    field,
)
from datetime import (
    UTC,
    datetime,
    timezone,
)
from pathlib import (
    Path,
)
from typing import (
    Any,
)

from reckon import (
    _backends,
    _store,
    capabilities,
    clones,
    flight,
    ledger,
    review_tiers,
)
from reckon._plan_html import (
    section_anchor,
    section_record_id,
)
from reckon._schema import (
    is_implementable_section,
)
from reckon._timestamps import (
    parse_iso,
    parse_utc,
)
from reckon.crew import (
    plan_review,
    rollout,
)
from reckon.crew import (
    review as review_module,
)
from reckon.crew.dispatch import (
    WORKER_SCRATCH_BUDGET_BYTES,
    _backend_settings,
    _capture_member_session,
    project_mount_repository,
    remove_worker_scratch,
    resolve_project_repository,
    tree_size_bytes,
    worker_scratch_root,
)
from reckon.crew.node import (
    NEGATIVE_CONTROL_FIELD,
    NEGATIVE_CONTROL_NONE,
    STALL_BUDGET_MULTIPLE,
    CrewError,
    is_test_path,
    negative_control_is_none,
    negative_control_reason,
    parse_duration,
    role_may_write_repository_paths,
)
from reckon.crew.plan_review import (
    RUN_COMMENT_PREFIX,
    _is_run_comment,
)
from reckon.crew.recovery import (
    _resolve_commit,
)
from reckon.crew.reports import (
    _NONE_VALUES as _MANIFEST_NOTHING,
)
from reckon.crew.reports import (
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    parse_manifest,
    path_within_declared_scope,
)
from reckon.crew.routing import (
    RECLAIMABLE_CLASSES,
    WITHHELD_REASONS,
    _boundary_tree_roots,
    _disposable_member_id,
    _git,
    _inspect_workspace,
    _live_pointer_worktrees,
    _repository_tree_snapshot,
    _shadow_patch_retained,
    _shadow_worktree_records,
    _signal_process_group,
    mounted_repository_projects,
    run_directory_of,
)
from reckon.crew.runs import (
    _drain_row,
    _live_worktree_claims,
    _manifest_freshness,
    _pointer_lock,
    _shared_write_paths,
    _utc_now,
    _write_json,
    drain,
    list_live,
    pointer_path,
    process_alive,
    read_pointer,
    record_process_alive,
    run_dir,
)
from reckon.evidence import (
    EXECUTABLE_SECTION_ROLES,
)


def complete(
    run_id: str,
    *,
    gate: str,
    failure_classification: str = "",
    commits: Iterable[str] = (),
    outcome: str = "",
    tests_added: int | None = None,
    scope_changed: bool = False,
    changed_lines: Mapping[str, Any] | None = None,
    completed_at: str = "",
    root: str | Path | None = None,
    gate_check: Mapping[str, Any] | None = None,
    require_gate_check: bool = False,
    no_commit: str = "",
    suite_delta_waiver: str = "",
    boundary_waiver: str = "",
    resume_waiver: str = "",
    review_waiver: str = "",
    negative_control_waiver: str | None = None,
    discard_resume_worktree: bool = False,
    accepted_paths: Mapping[str, str] | None = None,
    no_impl_change: str = "",
    live_run_waiver: str = "",
    plan_link: str = "",
    unplanned_reason: str = "",
    promoted_by: str = "",
) -> dict[str, Any]:
    """Promote a run, or finish cleanup when its record already landed."""
    verdict = str(gate).strip().lower()
    if verdict not in ledger.GATE_VERDICTS:
        raise ledger.LedgerError(
            f"gate verdict {gate!r} is not one of "
            f"{', '.join(ledger.GATE_VERDICTS)}; a gate whose evidence could "
            "not be produced is 'not-run'"
        )
    classification = str(failure_classification).strip().lower()
    if verdict == "failed" and classification not in ledger.FAILURE_CLASSIFICATIONS:
        raise CrewError(
            "a failing gate requires --failure-classification from: "
            + ", ".join(ledger.FAILURE_CLASSIFICATIONS)
        )
    if verdict != "failed" and classification:
        raise CrewError("--failure-classification is valid only when --gate failed")
    commit_list = tuple(str(sha) for sha in commits if str(sha).strip())
    with _pointer_lock(run_id):
        record = _read_pointer_or_rebuild(run_id, root=root)
        # A dispatch whose launch was refused leaves a run that names no
        # project: the refusal removed the pointer or never let it carry a
        # project, and the run directory it left behind holds no supervisor
        # record either, so reconstruction resolves no project from it. There
        # is nothing to promote and no project ledger to hold a row, so the run
        # is reported as the withdrawal it is rather than failing validation on
        # the empty project name further down.
        if not str(record.get("project") or "").strip():
            return _complete_withdrawn_run(run_id, record)
        # A review run's outcome is the review it stored, so the operator's
        # hand is not the only source for a non-passing gate's summary; the
        # refusal below stands for every run with no stored review to read.
        outcome = _resolve_promotion_outcome(
            run_id, record, verdict=verdict, outcome=outcome
        )
        # A promotion writes the ledger row into the project's own mount, so
        # the repository is resolved from that mount before anything is
        # written: a checkout named from elsewhere is refused, and a run whose
        # record names none is given the mount rather than the caller's
        # enclosing repository. A project with no mount keeps the caller's
        # checkout, which is the only root available to it. Judged ahead of the
        # per-run evidence so a repository defect is reported as itself rather
        # than as a missing commit further down.
        landing_project = str(record.get("project") or "")
        # Gate command, exit status, log path and commits default to what the
        # worker's manifest already states, so a passing run whose evidence is
        # on disk promotes without the coordinator retyping figures the manifest
        # holds. Only absent values are filled: an operator's own flag always
        # wins, and a manifest that states nothing leaves the guard's ordinary
        # refusal in place.
        gate_check, commit_list = _default_gate_evidence_from_manifest(
            record,
            verdict=verdict,
            gate_check=gate_check,
            commits=commit_list,
            no_commit_reason=no_commit,
        )
        # Every precondition that can be judged on its own is judged here, in
        # the order the promotion checks them, and reported in one refusal: a
        # coordinator learns every way the record falls short from one call
        # rather than one per attempt. The resolutions a gate needs are read
        # inside that gate's own check, so a refusal never leaves a later check
        # reading a half-built value.
        resolved: dict[str, Any] = {}

        def _repository_root() -> str | Path | None:
            if landing_project and project_mount_repository(landing_project) is not None:
                resolved["root"] = resolve_project_repository(
                    landing_project, root, flag="--checkout-path"
                )
            else:
                resolved["root"] = root
            return resolved["root"]

        def _review_gate() -> dict[str, str] | None:
            from reckon.crew.recovery import classify_pointer

            classified = classify_pointer(record)
            resolved["classified"] = classified
            resolved["classification"] = str(classified.get("classification") or "")
            # The tier and the promoted revision read the commits the run
            # presents, so a commitless declaration is read as none here too:
            # the run changed nothing in the repository, and its tier is
            # resolved from what its manifest declares rather than from a
            # sentence nothing can resolve.
            gate_commits = _presented_commits_without_a_declaration(record, commit_list)
            # The revision this promotion asserts, resolved before the gate
            # reads the store, so a review of an earlier revision is refused
            # rather than accepted as evidence about code the repair has
            # already moved past.
            resolved["promoted_revision"] = _run_promoted_revision(record, gate_commits)
            reviewed, stale_review_head = _review_for_promotion(
                landing_project,
                run_id,
                promoted_revision=resolved["promoted_revision"],
                tree=_record_tree(record),
            )
            resolved["reviewed"] = reviewed
            # The tier is computed before the gate reads it, from what the run
            # actually changed at the promoted head, so the refusal and the row
            # it would have written agree about which review the run owes.
            resolved["review_tier"] = _run_review_tier(
                run_id,
                record,
                commit_list=gate_commits,
                root=resolved.get("root", root),
            )
            return _require_review_waiver(
                run_id,
                record,
                verdict=verdict,
                classification=resolved["classification"],
                review=reviewed,
                review_action=str(classified.get("next_action") or ""),
                waiver_reason=review_waiver,
                review_tier=resolved["review_tier"],
                promoted_head=resolved["promoted_revision"],
                stale_head=stale_review_head,
                manifest_commits=commit_list,
            )

        def _resume_gate() -> dict[str, str] | None:
            classified = resolved.get("classified")
            if not isinstance(classified, Mapping):
                classified = {}
            classification_name = str(resolved.get("classification") or "")
            candidate_remedy = classified.get("resume_remedy")
            resume_remedy = (
                dict(candidate_remedy) if isinstance(candidate_remedy, Mapping) else None
            )
            candidate_resolution = classified.get("session_resolution")
            if classification_name == "blocked" and isinstance(
                candidate_resolution, Mapping
            ):
                recoverable_session = (
                    {
                        "session_id": str(candidate_resolution["session_id"]),
                        "source": str(candidate_resolution["source"]),
                    }
                    if candidate_resolution.get("resolved")
                    else None
                )
            elif resume_remedy is not None:
                recoverable_session = {
                    "session_id": str(resume_remedy["session_id"]),
                    "source": str(resume_remedy["source"]),
                }
            else:
                recoverable_session = _recoverable_session(record)
            resolved["resume_remedy"] = resume_remedy
            resolved["recoverable_session"] = recoverable_session
            waived = _require_resume_waiver(
                run_id,
                verdict=verdict,
                waiver_reason=resume_waiver,
                classification=classification_name,
                recoverable_session=recoverable_session,
            )
            if discard_resume_worktree and waived is None:
                raise CrewError(
                    "discard_resume_worktree requires a reasoned resume waiver "
                    "for a recoverable non-passing run"
                )
            return waived

        def _landing_gate() -> dict[str, Any]:
            landing_root = resolved.get("root", root)
            if landing_root is None:
                landing_root = record.get("repo")
            checkout = (
                Path(landing_root).expanduser().resolve()
                if landing_root is not None
                else None
            )
            return _landing_preconditions(
                run_id,
                record,
                checkout=checkout,
                ledger_root=landing_root,
                commits=commit_list,
                gate=gate,
                failure_classification=classification,
                no_impl_change=no_impl_change,
                plan_link=plan_link,
                unplanned_reason=unplanned_reason,
                boundary_waiver=boundary_waiver,
                negative_control_waiver=negative_control_waiver,
                accepted_paths=accepted_paths,
                gate_check=gate_check,
                require_gate_check=require_gate_check,
            )

        (
            root,
            overridden_worktree_changes,
            _manifest_status,
            live_run_waived,
            _shadow_commit,
            commit_list_shortfall,
            _commits_beyond_base,
            _gate_log_agrees,
            _verdict_matches_exit_status,
            _runnable_gate_command,
            _executable_gate_command,
            review_waived,
            _misdelivered_review,
            _standing_suite,
            resume_waived,
            landing,
        ) = _require_independently(
            [
                _repository_root,
                lambda: _require_commit_for_changed_manifest(
                    run_id, record, no_commit_reason=no_commit
                ),
                lambda: _require_recognised_manifest_status(run_id, record),
                # A run whose own worker is still alive and not finished is
                # refused before any store is written: deleting its live
                # pointer now would orphan the process until it exits on its
                # own. The waiver names the reason and rides the promoted row.
                lambda: _require_worker_stopped_before_promotion(
                    run_id, record, waiver_reason=live_run_waiver
                ),
                lambda: _refuse_commits_for_a_shadow(run_id, record, commit_list),
                lambda: _require_gate_evidence(
                    run_id,
                    record,
                    verdict=verdict,
                    commits=commit_list,
                    no_commit_reason=no_commit,
                ),
                # The citations themselves must be the run's own work, judged
                # before any store is written so a run that cannot be asserted
                # truthfully is refused with nothing landed and nothing to
                # unwind.
                lambda: _require_commits_beyond_base(run_id, record, commit_list),
                lambda: _require_gate_log_agrees(run_id, gate_check, verdict=verdict),
                # The verdict and the asserted status are two statements about
                # one run: a passing verdict beside a nonzero exit status is
                # refused from those two facts alone, whether or not the log
                # carries a terminal EXIT record of its own for the comparison
                # above to read.
                lambda: _require_verdict_matches_exit_status(
                    run_id, record, gate_check, verdict=verdict
                ),
                # The recorded command is what the integration re-run executes,
                # so a text that describes the command or names no program must
                # be refused here rather than land on a row that later reports a
                # failure the merge did not cause.
                lambda: _require_runnable_gate_command(run_id, gate_check),
                lambda: _require_executable_gate_command(run_id, gate_check),
                _review_gate,
                # A plan review that wrote its report outside its assigned
                # directory leaves the delivery read by neither the store nor
                # promotion, so the review is lost silently. Refuse it, naming
                # both directories, rather than promote a run whose report sits
                # where nothing joins it.
                lambda: _require_plan_review_delivered_in_its_directory(
                    landing_project, run_id, record
                ),
                # The project's declared suite is the gate that sees the whole
                # tree, and the lighter promotions wait on it. The tier is the
                # one just resolved, so a full review -- which reads the run for
                # itself -- is never held.
                lambda: _require_standing_suite(
                    landing_project,
                    resolved.get("review_tier") or review_tiers.FULL,
                    resolved.get("root", root),
                ),
                _resume_gate,
                _landing_gate,
            ]
        )
        suite_delta = _evaluate_suite_delta(
            run_id,
            record,
            waiver_reason=suite_delta_waiver,
        )
        result = _complete_locked(
            run_id,
            record=record,
            gate=gate,
            failure_classification=classification,
            commits=commit_list,
            no_commit=no_commit,
            no_commit_uncommitted=overridden_worktree_changes,
            outcome=outcome,
            tests_added=tests_added,
            scope_changed=scope_changed,
            changed_lines=changed_lines,
            completed_at=completed_at,
            root=root,
            gate_check=gate_check,
            require_gate_check=require_gate_check,
            suite_delta=suite_delta,
            boundary_waiver=boundary_waiver,
            resume_remedy=resolved.get("resume_remedy"),
            resume_waived=resume_waived,
            reviewed=resolved.get("reviewed"),
            review_waived=review_waived,
            review_tier=resolved.get("review_tier") or "",
            negative_control_waiver=negative_control_waiver,
            recoverable_session=resolved.get("recoverable_session"),
            discard_resume_worktree=discard_resume_worktree,
            accepted_paths=accepted_paths,
            commit_list_shortfall=commit_list_shortfall,
            no_impl_change=no_impl_change,
            live_run_waived=live_run_waived,
            plan_link=plan_link,
            unplanned_reason=unplanned_reason,
            promoted_by=promoted_by,
            landing=landing,
        )
        if commit_list_shortfall is not None:
            result["commit_list_shortfall"] = dict(commit_list_shortfall)
        return result


def _complete_locked(
    run_id: str,
    *,
    record: Mapping[str, Any] | None = None,
    gate: str,
    failure_classification: str = "",
    commits: Iterable[str] = (),
    no_commit: str = "",
    no_commit_uncommitted: Iterable[str] = (),
    outcome: str = "",
    tests_added: int | None = None,
    scope_changed: bool = False,
    changed_lines: Mapping[str, Any] | None = None,
    completed_at: str = "",
    root: str | Path | None = None,
    gate_check: Mapping[str, Any] | None = None,
    require_gate_check: bool = False,
    suite_delta: Mapping[str, Any] | None = None,
    boundary_waiver: str = "",
    resume_remedy: Mapping[str, str] | None = None,
    resume_waived: Mapping[str, str] | None = None,
    reviewed: Mapping[str, Any] | None = None,
    review_waived: Mapping[str, str] | None = None,
    review_tier: str = "",
    negative_control_waiver: str | None = None,
    recoverable_session: Mapping[str, str] | None = None,
    discard_resume_worktree: bool = False,
    accepted_paths: Mapping[str, str] | None = None,
    commit_list_shortfall: Mapping[str, Any] | None = None,
    no_impl_change: str = "",
    live_run_waived: Mapping[str, str] | None = None,
    plan_link: str = "",
    unplanned_reason: str = "",
    landing: Mapping[str, Any] | None = None,
    promoted_by: str = "",
) -> dict[str, Any]:
    """Promote a finished run into the owning repository's committed ledger.

    The plan comment and ledger append both happen before the pointer is
    deleted. The comment uses a stable run-derived id, so a retry after an
    interruption cannot duplicate the narrative.

    Worker-time spans the first and last timestamped stream events. The stream
    is read after a bounded settle, so the terminal record the worker's harness
    writes after the manifest is not missed. A live process is normally never
    waited on — but when promotion is about to end that process in its release
    step anyway, it ends it first and then settles the tail the shutdown writes,
    folding the run's own record instead of a file mtime; a promotion that will
    not end the process keeps the live-process behaviour. A healthy
    timestamp-less stream falls back to wall duration with an explicit source;
    a stalled run keeps that duration absent. Promotion time remains an
    explicit completion fallback when no stream survives.

    ``record`` is the run's own record, passed by :func:`complete` so a run
    rebuilt from its directory and one read from its live pointer reach the
    same body. A caller that omits it has the pointer read here, which keeps
    the direct callers that predate the reconcile path working unchanged.
    """
    if record is None:
        record = read_pointer(run_id)
    project = str(record.get("project") or "")
    node = record.get("node") or {}
    shadow = _is_shadow(record)
    ledger_root = root if root is not None else record.get("repo")
    checkout = (
        Path(ledger_root).expanduser().resolve() if ledger_root is not None else None
    )
    # Promotion commits the stores it writes, so a checkout that cannot host
    # that commit refuses before either store is written rather than leaving a
    # half-landed, uncommitted state behind.
    _require_committable_checkout(checkout, run_id)
    tree = _record_tree(record)
    # A landing that will carry the plan file must not sweep an unrelated
    # uncommitted edit into its commit. The check runs here, before either
    # store is written, so a refusal leaves no ledger row and no plan comment
    # for the next promotion to read as an unrelated edit.
    if not shadow:
        _refuse_unrelated_plan_edit(
            project=project,
            plan=str(node.get("plan") or ""),
            root=ledger_root,
            checkout=checkout,
        )
    ledger_data, ledger_version = ledger.load(project, root=ledger_root)
    existing = next(
        (
            item
            for item in ledger_data["runs"]
            if str(item.get("run_id") or "") == run_id
        ),
        None,
    )
    if existing is not None:
        existing_path = ledger.run_path(project, run_id, ledger_root)
        if not existing_path.is_file():
            existing_path = ledger.ledger_path(project, ledger_root)
        with _report_written_ledger_row(
            run_id,
            ledger_path=existing_path,
            row_present=lambda: _ledger_holds_row(project, ledger_root, run_id),
        ):
            comment = (
                {"recorded": False, "reason": "shadow evidence does not land code"}
                if shadow
                else _record_landing_comment(
                    project=project,
                    plan=str(node.get("plan") or ""),
                    section=str(node.get("section") or ""),
                    run_id=run_id,
                    narrative=outcome,
                    author=_COORDINATOR_LANDING_AUTHOR,
                    when=str(existing.get("completed_at") or _utc_now()),
                    root=ledger_root,
                    worker_tree=tree,
                    worker_commits=_declared_manifest_commits(record),
                    landing=_declared_manifest_landing(record),
                )
            )
            _commit_landing_writes(
                run_id=run_id,
                verdict=str(gate).strip().lower(),
                checkout=checkout,
                paths=_plan_comment_store_path(
                    project=project,
                    plan=str(node.get("plan") or ""),
                    comment=comment,
                    root=ledger_root,
                    checkout=checkout,
                ),
            )
            capture = _capture_member_session(record)
            path = pointer_path(run_id)
            path.unlink(missing_ok=True)
            retention = existing.get("worktree_retention")
            release = _release_after_promotion(
                run_id,
                record,
                retention if isinstance(retention, Mapping) else None,
                gate=str(gate),
            )
            release, recorded = _record_release_on_ledger(
                project=project,
                root=ledger_root,
                run_id=run_id,
                release=release,
                checkout=checkout,
            )
            release.update(_retire_disposable_identity(record))
            if recorded is not None:
                existing = recorded
            result = {
                "run_id": run_id,
                "project": project,
                "ledger_path": str(existing_path),
                "ledger_version": ledger_version,
                "pointer_removed": not path.exists(),
                "record": dict(existing),
                "already_promoted": True,
                "session_capture": capture,
                "plan_comment": comment,
                "release": release,
            }
            lane_receipt = existing.get("lane_receipt")
            if isinstance(lane_receipt, Mapping):
                result["lane_receipt"] = dict(lane_receipt)
            # This is a bounded fleet reading, not a readiness recommendation: the
            # result states only what promotion observed, and the orchestrator owns
            # every decision about what to do next.
            result["fleet_state"] = _fleet_state_reading(project)
            return result

    # A writer still alive when a prompt promotion arrives is about to be ended
    # by this promotion's own release step; end it before the observation so the
    # bounded settle can fold the terminal record its shutdown writes. A run the
    # release would not signal — no terminal manifest, or a non-cli launch —
    # keeps the existing live-process behaviour, and nothing waits on a process
    # this promotion is not going to end.
    ended_writer = _end_live_writer_for_settle(record)
    stream = _promotion_terminal_observation(record, settle_even_if_alive=ended_writer)
    if completed_at:
        finished = _assume_utc_if_naive(completed_at)
        completion_source = "provided"
    elif stream.completed_at:
        finished = stream.completed_at
        completion_source = stream.completion_source or "terminal_event"
    else:
        finished = _utc_now()
        completion_source = "promotion_time"
    worktree_retention = _resume_worktree_retention(
        record,
        recoverable_session,
        retained_at=finished,
        discard=discard_resume_worktree,
        gate=str(gate),
    )
    if landing is None:
        landing = _landing_preconditions(
            run_id,
            record,
            checkout=checkout,
            ledger_root=ledger_root,
            commits=commits,
            gate=gate,
            failure_classification=failure_classification,
            no_impl_change=no_impl_change,
            plan_link=plan_link,
            unplanned_reason=unplanned_reason,
            boundary_waiver=boundary_waiver,
            negative_control_waiver=negative_control_waiver,
            accepted_paths=accepted_paths,
            gate_check=gate_check,
            require_gate_check=require_gate_check,
        )
    commit_list = list(landing["commits"])
    shadow_patch = str(landing["shadow_patch"] or "")
    changed_lines = landing["changed_lines"]
    scope_acceptances = list(landing["scope_acceptances"] or [])
    boundary_waived = landing["boundary_waived"]
    brief_owner = landing["brief_owner"]
    impl_move = landing["impl_move"]
    manifest_text = landing["manifest_text"]
    manifest = landing["manifest"]
    negative_control = landing["negative_control"]

    from reckon.crew.resumption import resolve_session

    session_id = resolve_session(
        run_id,
        record=record,
        project=str(record.get("project") or ""),
        root=record.get("repo"),
    )["session_id"]
    lane_receipt = _harvest_lane_receipt(
        record,
        session_id=session_id,
        observed_at=finished,
    )
    previous = next(
        (
            item
            for item in reversed(ledger_data["runs"])
            if session_id and item.get("session_id") == session_id
        ),
        None,
    )
    measured_budget = ledger.per_run_budget(stream.budget, previous)
    wall_seconds = _elapsed_seconds(record.get("created_at"), finished)
    stalled = _wall_exceeded_budget(wall_seconds, node.get("time_budget"))
    worker_seconds = stream.worker_seconds
    if worker_seconds is not None:
        worker_seconds_source = "stream_events"
    elif stream.completion_source == "stream_mtime" and stalled:
        worker_seconds_source = "stalled"
    elif stream.completion_source == "stream_mtime" and wall_seconds is not None:
        worker_seconds = wall_seconds
        worker_seconds_source = "wall_fallback"
    else:
        worker_seconds_source = "unavailable"

    # Routing evidence is read from the delivered manifest at promotion, so a
    # later measure can separate a followup touch from a defect on the row
    # alone; the run directory survives, pruned only later by crew gc. An
    # unreadable manifest
    # leaves follow-on paths unmeasured (key absent) and the dispute count
    # "unknown" — a node never measured is not one that measured zero.
    follow_on_paths = (
        None if manifest is None else ledger.follow_on_paths(manifest.get("follow_ons"))
    )
    predecessor = ledger.predecessor_run_id(
        supplied=_coordinator_supplied_predecessor(record),
        base_sha=str(record.get("base_sha") or record.get("base") or ""),
        runs=ledger_data["runs"],
        exclude_run_id=run_id,
    )
    dispute_count = (
        "unknown"
        if manifest_text is None
        else ledger.stated_correction_count(manifest_text)
    )
    # An attached review is copied onto the committed row at promotion, so its
    # dimension scores and their total survive the loss of the crew
    # configuration home. The store keeps the verbatim text and findings; the
    # row keeps the compact block that joins to the run which earned it.
    # The cited gate log is copied into the row's own run directory before the
    # record is built, so the row names a path that outlives the worktree and the
    # reaper rather than one that was true only when it was written. The step is
    # best-effort: a log already in the run directory, a citation that does not
    # resolve on this machine, and a copy that cannot be read or written all
    # leave the check as given, because preservation must never decide the verdict.
    gate_check = _preserve_cited_gate_log(run_id, gate_check)
    # The worker's own exit record is copied onto the row before promotion
    # releases the live pointer and the worktree. The run directory survives
    # until crew gc prunes it, but the row must carry the record regardless: the
    # facts a census reads (the terminating signal and whether the exit landed
    # mid-work either way) then outlive the pointer. The row's ``exit_status`` is
    # left as the gate command's status; the worker's exit is a separate fact
    # under its own key, so a run with no exit record carries no ``worker_exit``
    # key rather than an empty one.
    worker_exit = _promoted_worker_exit(run_id)
    # The revision this promotion asserts landed, resolved while the run's tree
    # is still present. A shadow asserts no code, so it records none.
    promoted_revision = "" if shadow else _run_promoted_revision(record, commit_list)
    # The six-line clone detector reports each function this run added or
    # modified whose normalised window duplicates another function already in
    # reckon/ or tests/ at the promoted revision. It is a review signal: a
    # readable revision yields the list of matches (empty when none), and a
    # case no revision pair can be measured for records the unmeasured marker
    # rather than the empty list, so the two never read alike. Neither fails
    # the promotion.
    if shadow:
        clone_matches: Any = ledger.unmeasured_clone_report("shadow run lands no code")
    elif not (commit_list and promoted_revision):
        clone_matches = ledger.unmeasured_clone_report(
            "the run asserted no revision pair to compare"
        )
    else:
        measured = clones.promotion_clone_matches(
            tree,
            base_sha=str(record.get("base_sha") or record.get("base") or ""),
            tip=promoted_revision,
        )
        clone_matches = (
            measured
            if measured is not None
            else ledger.unmeasured_clone_report("the revision pair could not be read")
        )
    # A brief run carries the digest and stored path of the brief it read in
    # place of a plan section, so a promoted row still points at the exact text
    # the worker saw. The block is present only for a brief run, which is what
    # makes the row's ``plan`` null rather than empty.
    brief_run = str(node.get("brief") or "").strip()
    brief_block = (
        {
            "sha256": str(node.get("brief_sha256") or ""),
            "path": str(node.get("brief_path") or ""),
        }
        if brief_run
        else None
    )
    # The crew session that dispatched this run rides the committed row beside
    # the run it belongs to, because this promotion deletes the live pointer
    # that carried it while ``session_id`` names the worker's own harness
    # session rather than the coordinator's. It is null rather than absent when
    # no pointer named one, because every key in ``RECORD_FIELDS`` is present on
    # every promoted row.
    pointer_session = str(record.get("session") or "").strip() or None
    # The disposition verb rides the committed row for the same reason: the
    # live pointer that carries it is deleted by this promotion, so a row that
    # does not read it here can never recover it. A run with no recorded
    # disposition records null, never an inferred verb.
    disposition = _recorded_disposition(record)
    # A review run's deliverable is the record it stored for the subject it
    # read, so landing the run lands the record with it — and every earlier
    # round of that subject too, so a review of another head is not left only in
    # the host staging store. The rounds are read here, before the ledger row is
    # assembled, so the round this run produced can name the row and every
    # round's committed write can join the row's commit below. A run that is not
    # a review, or a review run that delivered no readable round, leaves the
    # row's review block as the review gate resolved it.
    committed_review_payloads: list[dict[str, Any]] = []
    if not shadow:
        committed_review_payloads = _delivered_review_payloads_for_commit(
            record, project, run_id
        )
    committed_review_payload = next(
        (
            payload
            for payload in committed_review_payloads
            if str(payload.get("review_run_id") or "").strip() == run_id
        ),
        None,
    )
    if committed_review_payload is None and committed_review_payloads:
        # A round whose own body carried no review run id is filed under this
        # run's id above, so it is normally the selected one; if none names the
        # row, the first round still stands in so the row's review block is not
        # left absent.
        committed_review_payload = committed_review_payloads[0]
    if committed_review_payload is not None:
        committed_block = review_module.ledger_block(committed_review_payload)
        if committed_block is not None:
            committed_block["id"] = run_id
            reviewed = committed_block
    run = ledger.build_record(
        run_id=run_id,
        plan=str(node.get("plan") or ""),
        section=str(node.get("section") or ""),
        brief=brief_block,
        plan_link=str((brief_owner or {}).get("plan_link") or ""),
        unplanned_reason=str((brief_owner or {}).get("unplanned_reason") or ""),
        node=str(node.get("id") or ""),
        node_definition=node,
        role=str(record.get("role") or ""),
        spec_level=str(node.get("spec_level") or ""),
        member_id=str(record.get("member") or ""),
        backend=str(
            record.get("backend") or (record.get("agent") or {}).get("backend") or ""
        ),
        agent=record.get("agent") or {},
        dispatched_at=str(record.get("created_at") or ""),
        completed_at=finished,
        completed_at_source=completion_source,
        worker_seconds=worker_seconds,
        worker_seconds_source=worker_seconds_source,
        wall_seconds=wall_seconds,
        stalled=stalled,
        time_budget=str(node.get("time_budget") or ""),
        base_sha=str(record.get("base_sha") or ""),
        commits=commit_list,
        changed_lines=changed_lines,
        tests_added=tests_added,
        gate=gate,
        failure_classification=failure_classification,
        outcome=outcome,
        disposition=disposition,
        manifest_path=str(record.get("manifest_path") or ""),
        scope_changed=scope_changed,
        session=pointer_session,
        session_id=session_id,
        session_harness=(record.get("session_harness") or record.get("dialect"))
        if session_id
        else None,
        session_model=(
            record.get("session_model") or (record.get("agent") or {}).get("model")
        )
        if session_id
        else None,
        budget=measured_budget,
        lane_receipt=lane_receipt,
        throughput=stream.throughput,
        budget_fallback=record.get("budget_fallback"),
        lineage=record.get("lineage"),
        shadow_patch=shadow_patch,
        unreconciled_override=record.get("unreconciled_override"),
        orchestrator_lane_override=record.get("orchestrator_lane_override"),
        gate_check=gate_check,
        require_gate_check=require_gate_check,
        suite_delta=suite_delta,
        resume_remedy=resume_remedy,
        follow_on_paths=follow_on_paths,
        predecessor_run=predecessor,
        dispute_count=dispute_count,
        review=reviewed,
        clone_matches=clone_matches,
        promoted_by=promoted_by,
    )
    run["attempt"] = int(record.get("attempt") or 1)
    run["attempt_kind"] = str(record.get("attempt_kind") or "dispatch")
    # The worker's exit record rides the row verbatim, and the key is written
    # only when the run directory held one: a present-but-empty key would read
    # as a supervisor that ran and recorded nothing.
    if worker_exit is not None:
        run["worker_exit"] = worker_exit
    # The revision the promotion asserts landed rides the row so a later sweep
    # can ask about it without the worktree, which promotion is about to
    # release. A run that asserted no code leaves the key absent rather than
    # recording a base it never touched, so an absent key never reads as a
    # promotion whose work was verified as landed.
    if promoted_revision:
        run["promoted_revision"] = promoted_revision
    # The job a placed launch was charged to rides the committed row beside the
    # run it belongs to, because the ledger row is the durable record a later
    # attribution reads; the live pointer it was first written on is removed by
    # this promotion. A run that declared no placement records the key absent
    # rather than zero, so an unplaced run is distinguishable from a placed one
    # whose scheduler never answered.
    job_id = record.get("job_id")
    if job_id is not None:
        run["job_id"] = str(job_id)
    # A deliberate commitless promotion survives on the record with its reason,
    # so a later reader can tell it from one that recorded nothing by accident.
    if str(no_commit).strip():
        run["no_commit"] = str(no_commit).strip()
    # A deliberate override of the worktree-evidence guard records the paths it
    # covered, so the ledger says which uncommitted repository work was declined
    # a commit beside the reason given, rather than only that a commit was
    # declined. No manifest field can put a path here.
    uncommitted = sorted(
        {str(path).strip() for path in no_commit_uncommitted if str(path).strip()}
    )
    if uncommitted:
        run["no_commit_uncommitted_paths"] = uncommitted
    # The impl comparison and its outcome ride the row, so a later audit can
    # separate a plan that moved from one promoted against a recorded waiver.
    if impl_move.get("at_dispatch") is not None:
        run["plan_impl_at_dispatch"] = impl_move["at_dispatch"]
    run["impl_move"] = dict(impl_move)
    # The negative-control verdict rides the row, so an audit can separate a
    # red log that was delivered and matched from a declaration of none that was
    # recorded with its reason rather than refused.
    run["negative_control"] = dict(negative_control)
    # A presented-list shortfall survives on the record so a reader of the
    # ledger sees the boundary check may have been under-scoped, not only the
    # coordinator that was looking at the immediate report.
    if commit_list_shortfall:
        run["commit_list_shortfall"] = dict(commit_list_shortfall)
    if boundary_waived is not None:
        run["boundary_waiver"] = boundary_waived
    if scope_acceptances:
        run["scope_acceptances"] = scope_acceptances
    # A discarded resume path survives on the row with the session it discarded
    # and the reason given, so an audit can tell a deliberate discard from the
    # accidental one this refusal exists to prevent.
    if resume_waived is not None:
        run["resume_waiver"] = dict(resume_waived)
        if discard_resume_worktree:
            run["resume_waiver"]["worktree_discarded"] = True
    if review_waived is not None:
        run["review_waiver"] = dict(review_waived)
    # The tier rides the row on every promotion. For a run that changed no
    # runtime source it is the recorded reason no review exists — the merged-head
    # gate re-run and the node's negative control are what checked it instead —
    # so a later reader can tell a deliberate no-review tier from a review that
    # was simply never produced.
    if review_tier:
        run["review_tier"] = review_tier
    # A run promoted while its own worker was still alive survives on the row
    # with the reason given, so an audit can tell a deliberate promotion of a
    # live worker from the accidental orphan this refusal exists to prevent.
    if live_run_waived is not None:
        run["live_run_waiver"] = dict(live_run_waived)
    if worktree_retention is not None:
        run["worktree_retention"] = dict(worktree_retention)
    watch_override = record.get("watch_override")
    if isinstance(watch_override, Mapping):
        run["watch_override"] = dict(watch_override)
    execution_fit = record.get("execution_fit")
    if isinstance(execution_fit, Mapping):
        run["execution_fit"] = dict(execution_fit)
    # A run launched without the fence carries the reason it was waived onto the
    # committed row, so the ledger says which runs ran unprotected and why. The
    # pointer holds it only until promotion deletes it, and a reader asking why
    # a worker could write outside its grant needs it in the durable row. An
    # ordinary fenced dispatch records no such key.
    fence_waiver = record.get("fence_waiver")
    if isinstance(fence_waiver, Mapping):
        run["fence_waiver"] = dict(fence_waiver)
    # The defaults a layer left writable ride the row beside the waiver, for the
    # same reason: the pointer that held them is deleted at promotion, and a
    # reader asking whether a run could write outside its fence's own grant needs
    # them in the durable row. A run whose fence removed nothing records no such
    # key, so the row never carries an empty list that would read as a fence
    # built and found whole.
    fence_unprotected = record.get("fence_unprotected_paths")
    if fence_unprotected:
        run["fence_unprotected_paths"] = [str(path) for path in fence_unprotected]
    # The advisory a dispatch computed rides the committed row the same way it
    # rode the live pointer, together with the lane declaration and reading it
    # was derived from. Promotion deletes that pointer, so an advisory reaching
    # only the pointer is readable exactly until the run becomes evidence, and
    # the row is where a later reader asks whether the advice was taken --
    # ``backend`` already names the lane that was chosen, so the pair on the row
    # is what tells taking the advice apart from declining it. Each key is
    # written only when the pointer carried it: a row holding a null advisory
    # would read as a lane that was assessed and found quiet, which is the
    # opposite of a run whose dispatch never emitted one.
    for lane_key in ("lane_advisory", "lane_declaration", "lane_reading"):
        lane_value = record.get(lane_key)
        if lane_value is None:
            continue
        run[lane_key] = dict(lane_value) if isinstance(lane_value, Mapping) else lane_value
    # A shadow whose stream read its primary's landed answer is void as
    # calibration evidence; the stream is still on disk at this point (the run
    # directory survives until crew gc) so promotion scans it rather than
    # trusting a worker's self-report. Contamination recreates the primary's
    # answer from the object store, never from the shadow's own patch.
    if shadow:
        contamination = _shadow_stream_contamination(record, ledger_data["runs"], tree)
        if contamination:
            run["shadow_contaminated"] = contamination
    # A terminal run's stream is immutable, so its two figures are computed once
    # here, from the run's own stream, and recorded on the row; a later derive
    # then reads the ledger instead of reopening the stream. A stream that is
    # already gone records each figure as absent rather than as zero, so a
    # missing measurement never reads as a free run.
    run.update(capabilities.derive_run_figures(run))
    # The landing comment is written last of the plan-facing steps: the record
    # is assembled, and every refusal it raises is raised, before anything is
    # written to the plan. A promotion that refuses after writing the comment
    # leaves the plan mutated for the caller that retries it, and the first
    # attempt's narrative pins the outcome every later attempt must repeat
    # byte-identical to avoid the narrative-conflict refusal. None of the
    # checks above needs the comment to exist, so none of them follows it.
    comment = (
        {"recorded": False, "reason": "shadow evidence does not land code"}
        if shadow
        else _record_landing_comment(
            project=project,
            plan=str(node.get("plan") or ""),
            section=str(node.get("section") or ""),
            run_id=run_id,
            narrative=outcome,
            author=_COORDINATOR_LANDING_AUTHOR,
            when=finished,
            root=ledger_root,
            worker_tree=tree,
            worker_commits=commit_list or _declared_manifest_commits(record),
            landing=_declared_manifest_landing(record),
        )
    )
    # The narrative the comment recorded lives in the plan, so the row carries
    # its outcome empty rather than twice.
    if comment.get("recorded"):
        run["outcome"] = ""
    already_promoted = False
    try:
        written = ledger.append_run(project, run, root=ledger_root)
    except ledger.LedgerError:
        # Another completion can land after the read above. Treat only an
        # observed matching record as success; every other ledger error is
        # still a refusal.
        refreshed, ledger_version = ledger.load(project, root=ledger_root)
        existing = next(
            (
                item
                for item in refreshed["runs"]
                if str(item.get("run_id") or "") == run_id
            ),
            None,
        )
        if existing is None:
            raise
        already_promoted = True
        existing_path = ledger.run_path(project, run_id, ledger_root)
        if not existing_path.is_file():
            existing_path = ledger.ledger_path(project, ledger_root)
        written = {
            "path": str(existing_path),
            "version": ledger_version,
            "run": dict(existing),
        }
    committed_review_paths: list[Path] = []
    with _report_written_ledger_row(
        run_id,
        ledger_path=written["path"],
        row_present=lambda: _ledger_holds_row(project, ledger_root, run_id),
    ):
        # The shadow store outcome rides on the ordinary payload, not a flag or a
        # log stream: a promotion that otherwise succeeded is the exact consumer
        # that must see a silently failing shadow. When this call did not perform
        # the append, the outcome is the one already recorded on the committed row.
        store_outcome = written.get("store")
        if store_outcome is None:
            recorded = written["run"].get("store_write")
            store_outcome = dict(recorded) if isinstance(recorded, Mapping) else None
        store_row_written = (
            not already_promoted
            and isinstance(store_outcome, Mapping)
            and store_outcome.get("status") == "written"
        )
        # A promoting review run's stored rounds are written into the committed
        # reviews tree now, before the landing commit: the run's own per-run file
        # was just written, so it supplies the records' dispatch and completion
        # stamps, and the records then join the ledger row's commit rather than
        # adding one. Every round of the subject is written, not only the round
        # this run selected. The store refuses a record it cannot key or time,
        # and that refusal takes back every record already written this attempt
        # and the row it appended, so neither a record nor the ledger row is
        # committed and the retry re-promotes cleanly.
        if committed_review_payloads and not already_promoted:
            try:
                committed_review_paths.extend(
                    review_module.store_committed_review(
                        payload, project=project, root=ledger_root
                    )
                    for payload in committed_review_payloads
                )
            except ValueError as exc:
                rollback = _restore_landing_writes(
                    checkout, [Path(written["path"]), *committed_review_paths]
                )
                if store_row_written:
                    _discard_run_store_row(run_id)
                raise _landing_refusal(
                    f"cannot store the review record for run {run_id!r} in the "
                    f"committed reviews tree, so the landing was rolled back: {exc}",
                    rollback,
                ) from exc

        # The two tracked stores this promotion wrote (the ledger row and, when a
        # narrative landed, the plan comment) are committed as one landing, so the
        # checkout carries no uncommitted state the next reader would trip on. A
        # refused landing then takes back every write it made, including the store
        # row this attempt's own append inserted, so a retry promotes cleanly.
        _commit_landing_writes(
            run_id=run_id,
            verdict=str(gate).strip().lower(),
            checkout=checkout,
            paths=[
                Path(written["path"]),
                *_plan_comment_store_path(
                    project=project,
                    plan=str(node.get("plan") or ""),
                    comment=comment,
                    root=ledger_root,
                    checkout=checkout,
                ),
                *committed_review_paths,
            ],
            store_row_written=store_row_written,
        )

        # Return capture metadata while the pointer still exists. Session
        # ownership has already been copied onto the committed ledger row.
        capture = _capture_member_session(record)
        pointer_path(run_id).unlink(missing_ok=True)
        release = _release_after_promotion(
            run_id,
            record,
            worktree_retention,
            process_already_ended=ended_writer,
            gate=str(gate),
        )
        release, recorded = _record_release_on_ledger(
            project=project,
            root=ledger_root,
            run_id=run_id,
            release=release,
            checkout=checkout,
        )
        release.update(_retire_disposable_identity(record))
        if recorded is not None:
            written["run"] = recorded
        # This is a bounded fleet reading, not a readiness recommendation: the
        # result states only what promotion observed, and the orchestrator owns
        # every decision about what to do next.
        return {
            "run_id": run_id,
            "project": project,
            "ledger_path": written["path"],
            "ledger_version": written["version"],
            "pointer_removed": not pointer_path(run_id).exists(),
            "record": written["run"],
            "lane_receipt": dict(written["run"]["lane_receipt"]),
            "fleet_state": _fleet_state_reading(project),
            "already_promoted": already_promoted,
            "session_capture": capture,
            "plan_comment": comment,
            "release": release,
            "store": store_outcome,
            "impl_move": dict(impl_move),
            "negative_control": dict(negative_control),
        }


DISCARD_RECORD_NAME = "discard.json"


def discard_record_path(run_id: str) -> Path:
    """Path of the marker a discard leaves in one run's directory."""
    return run_dir(run_id) / DISCARD_RECORD_NAME


def discard(run_id: str) -> dict[str, Any]:
    """Remove a stopped or abandoned pointer without promoting it.

    The departure is recorded in the run directory before the pointer goes, so
    a reader sees a discard rather than the same absence a reaped or
    hand-removed pointer produces. The record is never written into a directory
    that does not already exist: the run directory is the run's own home, and a
    write that recreated it would bring a discarded run back into existence.

    The run's own worktree is released through the same audit promotion applies
    on release. A discarded run whose tree is clean and whose head adds no
    commit the integration branch lacks holds nothing to preserve, and leaving
    it on disk refuses a redispatch of the same node because the worktree path
    already exists. A dirty or unintegrated tree is withheld with the audit's
    own reason.
    """
    with _pointer_lock(run_id):
        record = read_pointer(run_id)
        pid = record.get("pid")
        if record_process_alive(record, process_alive) is True:
            raise CrewError(
                f"cannot discard live run {run_id!r}: recorded pid {pid} is alive"
            )
        path = pointer_path(run_id)
        # Before the pointer goes: a reader that sees the absence must find the
        # record behind it, and a record written afterwards leaves a window in
        # which the departure is indistinguishable from a vanished pointer.
        written = _write_discard_record(run_id, record)
        path.unlink()
        return {
            "run_id": run_id,
            "pointer_path": str(path),
            "pointer_removed": not path.exists(),
            "removed": record,
            "discard_record": written,
            **_remove_discarded_worktree(record),
        }


def _remove_discarded_worktree(record: Mapping[str, Any]) -> dict[str, Any]:
    """Release a discarded run's worktree through the release audit.

    Reuses ``_release_run_workspace`` — the same classification and withheld
    reasons ``crew complete`` applies on release — so a discard and a promotion
    cannot disagree about which trees are safe to remove. Worktree and scratch
    fields are surfaced; process fields cannot matter here, because discard has
    already refused a run whose recorded process is alive.
    """
    try:
        release = _release_run_workspace(record)
    except Exception as exc:  # noqa: BLE001 - the pointer is already gone
        fallback = {
            "worktree_released": False,
            "worktree_withheld": (
                f"run {record.get('run_id')!r} release step raised: {exc}"
            ),
        }
        fallback.update(_release_scratch_when_release_raised(record))
        return fallback
    return {
        key: release[key]
        for key in (
            "worktree_released",
            "worktree_withheld",
            "worktree_audit",
            "scratch_removed",
            "scratch_path",
            "scratch_withheld",
            "scratch_bytes",
            "scratch_removed_paths",
            "scratch_warning",
            "arms_removed_paths",
            "arms_kept",
            "arms_logs_copied",
            "arms_withheld",
        )
        if key in release
    }


def _write_discard_record(
    run_id: str, record: Mapping[str, Any]
) -> str | None:
    """Record a deliberate discard in the run directory, or report its absence.

    Returns the record's path when the run directory was there to hold it, and
    ``None`` when the directory had already gone — a run whose home is gone has
    nothing left to mark, and creating the directory here would resurrect it.
    """
    path = discard_record_path(run_id)
    if not path.parent.is_dir():
        return None
    _write_json(
        path,
        {
            "run_id": run_id,
            "discarded_at": datetime.now(UTC).isoformat(),
            "phase": record.get("phase"),
            "node": record.get("node"),
            "pointer_path": str(pointer_path(run_id)),
        },
    )
    return str(path)


def sweep_promoted_revisions(
    project: str,
    *,
    target_head: str = "HEAD",
    markers: Iterable[Mapping[str, Any]] = (),
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Report every promoted run whose asserted work is not in the target head.

    A promotion records the revision it asserts landed; nothing at promotion
    time can know whether the merge carrying it into the integration branch
    happens later, or happens and then drops the content. This sweep answers
    both questions per promoted run:

    * is the recorded revision an ancestor of ``target_head``; and
    * does a marker the change introduced survive in ``target_head`` — or, for a
      change whose purpose was a removal, is the removed marker still gone?

    ``markers`` carries what only a caller can know about a change: the literal
    that proves the content landed and whether its passing answer is presence or
    absence. Each entry is a mapping with ``run_id``, ``marker``, an optional
    ``path`` to search, and ``expect`` of ``"present"`` (the default) or
    ``"absent"``.

    Neither half subsumes the other. A run that was never merged fails ancestry
    and, in the ordinary case, the marker too. A run whose work was merged and
    then reverted — a merge conclusion committing an index that dropped it —
    passes ancestry and fails only the marker. A run that removed something
    passes both unless the marker's absence is required, which is why a removal
    assertion exists at all: presence and absence are not one measurement.
    Nothing is reported for a run whose work landed intact, because a check that
    fires on the healthy case is one nobody keeps.

    The sweep reads the ledger and the branch and writes neither: a promotion
    does not merge, and this is a later read, not a read side of landing.
    """
    checkout = (
        resolve_project_repository(project, root, flag="root")
        if root is not None
        else project_mount_repository(project)
    )
    if checkout is None:
        raise CrewError(
            f"the promoted-revision sweep for {project!r} has no repository to "
            "read: pass root=<checkout> or register the project's mount"
        )
    target = _commit_canonical_id(checkout, target_head)
    if target is None:
        raise CrewError(
            f"target head {target_head!r} does not resolve to a commit in "
            f"{checkout}, so no promotion can be compared against it"
        )
    wanted: dict[str, list[dict[str, str]]] = {}
    for entry in markers:
        run_id = str(entry.get("run_id") or "").strip()
        marker = str(entry.get("marker") or "")
        if not run_id or not marker:
            raise CrewError(
                "a sweep marker needs both a run_id and a marker; one without "
                "either names nothing to search for or nothing to search"
            )
        expect = str(entry.get("expect") or "present").strip().lower()
        if expect not in ("present", "absent"):
            raise CrewError(
                f"sweep marker for {run_id!r} has expect={expect!r}; it must be "
                "'present' or 'absent'"
            )
        wanted.setdefault(run_id, []).append(
            {"marker": marker, "path": str(entry.get("path") or ""), "expect": expect}
        )
    records = ledger.runs(project, root=root)
    findings: list[dict[str, Any]] = []
    matched: set[str] = set()
    checked = 0
    for record in records:
        revision = str(record.get("promoted_revision") or "").strip()
        if not revision:
            # Rows promoted before the revision was recorded, and runs that
            # asserted no code, carry no claim to test. An absent key is not a
            # passing revision; it is a row this sweep cannot speak for.
            continue
        checked += 1
        run_id = str(record.get("run_id") or "")
        finding: dict[str, Any] = {
            "run_id": run_id,
            "node": str(record.get("node") or ""),
            "promoted_revision": revision,
            "reasons": [],
            "markers": [],
        }
        if not _commit_resolves_in(checkout, revision):
            finding["reasons"].append("unresolvable-revision")
        elif not _revision_is_ancestor(checkout, revision, target):
            finding["reasons"].append("not-an-ancestor")
        for spec in wanted.get(run_id, ()):
            matched.add(run_id)
            present = _marker_present_in(
                checkout,
                target,
                marker=spec["marker"],
                path=spec["path"],
            )
            finding["markers"].append(
                {
                    "marker": spec["marker"],
                    "path": spec["path"],
                    "expect": spec["expect"],
                    "present": present,
                }
            )
            if spec["expect"] == "present" and not present:
                finding["reasons"].append("marker-absent")
            elif spec["expect"] == "absent" and present:
                finding["reasons"].append("removed-marker-still-present")
        if finding["reasons"]:
            findings.append(finding)
    unresolved = sorted(run_id for run_id in wanted if run_id not in matched)
    return {
        "project": project,
        "checkout": str(checkout),
        "target_head": target,
        "checked": checked,
        "findings": findings,
        "unresolved_markers": unresolved,
    }


def _marker_present_in(
    checkout: Path, target: str, *, marker: str, path: str = ""
) -> bool:
    """Report whether ``marker`` occurs in ``target``'s tree, optionally at ``path``.

    The search is fixed-string and anchored to the target revision, so a marker
    that survives only in a worktree file or in another branch is not counted. A
    git failure raises rather than answering absent: an unreadable tree and a
    genuinely absent marker otherwise return the same empty result.
    """
    argv = ["git", "grep", "-q", "-F", "-e", marker, target]
    if path:
        argv += ["--", path]
    result = subprocess.run(
        argv, cwd=checkout, capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise CrewError(
        f"git could not search {target} in {checkout} for the marker: "
        f"{result.stderr.strip() or result.stdout.strip() or result.returncode}"
    )



from reckon.crew.promotion_checks import (
    _IMPL_MOVE_CORRECTIVE_ATTEMPT_KINDS,
    _IMPL_MOVE_EXEMPT_CLASSIFICATIONS,
    _STREAM_SETTLE_MAX_SECONDS,
    _STREAM_SETTLE_POLL_SECONDS,
    _STREAM_SETTLE_QUIESCENCE_SECONDS,
    StreamMeasures,
    _baseline_suite_failure_ids,
    _capability_risk_of,
    _combined_refusal_text,
    _control_failure_ids,
    _delivered_review_payloads_for_commit,
    _end_live_writer_for_settle,
    _fresh_manifest,
    _head_suite_failure_ids,
    _landed_sections,
    _landing_preconditions,
    _landing_scope_products,
    _light_changed_line_ceiling,
    _manifest_repository_paths,
    _misdelivered_plan_review_directories,
    _negative_control_log_text,
    _newest_stream_mtime,
    _plan_impl_from_state,
    _plan_remaining_sections,
    _plan_review_record_for_promoting_run,
    _plan_state_for_run,
    _promotion_terminal_observation,
    _PromotionRefusalError,
    _recoverable_session,
    _refuse_commits_for_a_shadow,
    _require_brief_owner,
    _require_declared_negative_control,
    _require_gate_check_precondition,
    _require_impl_moved,
    _require_independently,
    _require_plan_review_delivered_in_its_directory,
    _require_recognised_manifest_status,
    _require_resume_waiver,
    _require_review_waiver,
    _require_standing_suite,
    _require_worker_stopped_before_promotion,
    _resolve_promotion_outcome,
    _review_changed_scope,
    _review_outcome_summary,
    _run_capability_risk,
    _run_review_tier,
    _staging_review_record_by_run,
    _terminal_stream_data,
    _unreviewed_refusal,
    _wait_out_stream_tail,
    _zone_aware_stream_timestamp,
    plan_impl_at,
)
from reckon.crew.promotion_evidence import (
    _ABSENCE_BOUNDARY,
    _ATTEMPT_RECORD_NAME,
    _COMMITLESS_ROLES,
    _COORDINATOR_LANDING_AUTHOR,
    _EXIT_RECORD,
    _FIELD_LINE,
    _PROVISIONED_WORKTREE_ENTRIES,
    _RUNNER_SUMMARY,
    _UNEXECUTED_EXIT_CODES,
    _WORKER_RECORD_NAME,
    _arm_without_completion,
    _commit_canonical_id,
    _commit_resolves_in,
    _commitless_changed_paths_declares_absence,
    _commitless_raw_field,
    _commits_field_declares_absence,
    _committed_scope,
    _CumulativeDiff,
    _declared_repository_roots,
    _declares_absent_commits,
    _first_command_not_found_line,
    _foreign_repository,
    _head_arm_log_failure_ids,
    _log_shows_the_command_ran,
    _manifest_declares_no_change,
    _manifest_text,
    _opens_with_an_absence_word,
    _presented_commits_without_a_declaration,
    _promoted_worker_exit,
    _raw_manifest_field,
    _recorded_disposition,
    _recorded_exit_status,
    _registered_repository_roots,
    _require_commits_beyond_base,
    _require_gate_evidence,
    _require_gate_log_agrees,
    _require_verdict_matches_exit_status,
    _run_changed_nothing,
    _run_commit_directory,
    _uncommitted_paths,
    _unresolved_citations,
    _worktree_git_paths,
    _worktree_repository_changes,
    _worktree_unchanged_since_base,
    _worktree_untracked_paths,
    _zero_added_against_a_red_base,
    scoped_diff_stat,
)
from reckon.crew.promotion_gate import (
    _GATE_COMMAND_ELLIPSIS,
    _GATE_COMMAND_PARENTHETICAL,
    _GATE_COMMAND_PLACEHOLDER,
    _GATE_COMMAND_SHELL_OPERATORS,
    _GATE_LOG_DURATION,
    _PRESERVED_GATE_LOG_NAME,
    _REPLAY_BOUND_CEILING_SECONDS,
    _REPLAY_BOUND_DEFAULT_SECONDS,
    _REPLAY_BOUND_HEADROOM,
    _REPLAY_GATE_LOG_NAME,
    _SHELL_ASSIGNMENT,
    _cited_changed_paths,
    _merged_gate_finding,
    _paths_differing_between,
    _preserve_cited_gate_log,
    _recorded_gate_seconds,
    _recorded_worktree_roots,
    _replay_bound,
    _replay_cut_short_reason,
    _replay_log_header,
    _require_executable_gate_command,
    _require_runnable_gate_command,
    _rewrite_worktree_roots,
    _write_replay_log,
    gate_command_prose,
    record_gate_rerun_at_integrated_revision,
    rerun_gate_at_integrated_revision,
)
from reckon.crew.promotion_records import (
    _complete_withdrawn_run,
    _default_gate_evidence_from_manifest,
    _evaluate_suite_delta,
    _manifest_reads_blocked,
    _manifest_relative_path,
    _normalized_command,
    _project_for_repository,
    _read_json_object,
    _read_pointer_or_rebuild,
    _rebuild_record_from_run_directory,
    _release_scratch_when_release_raised,
    _release_terminal_manifest,
    _resume_worktree_retention,
    _retire_disposable_identity,
    _worktree_audit,
)
from reckon.crew.promotion_release import (
    _ARM_ARTIFACT_FIELD,
    _ARM_LOG_FIELDS,
    _ARM_SUITE_FIELDS,
    _BASETEMP_IN_COMMAND,
    _CITED_ABSOLUTE_PATH,
    _CITED_PATH_EDGE_PUNCTUATION,
    _FLEET_UNMEASURED_CAUSE_LIMIT,
    _absolute_paths_in,
    _agent_model_identifier,
    _arm_citations_of,
    _artifact_candidates,
    _coordinator_supplied_predecessor,
    _declared_arm_paths,
    _declared_manifest,
    _fleet_state_reading,
    _harvest_lane_receipt,
    _other_live_run_citations,
    _receipt_unmeasured_reason,
    _record_release_on_ledger,
    _record_tree,
    _record_worktree,
    _release_after_promotion,
    _release_run_workspace,
    _review_for_promotion,
    _run_promoted_revision,
    _serialize_notional_figure,
    _serialize_rate_basis,
    _string_leaves,
    _suite_record,
    _tree_for_measurement,
    _unavailable_fleet_reading,
    _unreconciled_live_runs,
    _update_run_record,
    remove_cited_arms,
)
from reckon.crew.promotion_scope import (
    _OPEN_OPERATION_MARKERS,
    _RECORD_ATTRIBUTES,
    _ZERO_COUNT,
    BOUNDARY_REFERENT_MAX_AGE_SECONDS,
    LANDING_ROLLBACK_ATTRIBUTE,
    _accepted_scope_exceptions,
    _assume_utc_if_naive,
    _boundary_has_no_referent,
    _changed_paths_declare_no_paths,
    _changed_paths_inside_repository,
    _collapse_inter_tag,
    _commit_landing_writes,
    _declared_manifest_commits,
    _declared_manifest_landing,
    _descendant_commit,
    _discard_run_store_row,
    _elapsed_seconds,
    _is_shadow,
    _landing_refusal,
    _ledger_holds_row,
    _ledger_row_was_rolled_back,
    _manifest_cites_a_commit,
    _merge_revisions,
    _open_operation_state,
    _outside_declared_scope,
    _path_change_reaches_integration_head,
    _path_differs_from_head,
    _path_has_a_staged_change,
    _plan_comment_store_path,
    _plan_differs_only_by_the_stores_own_writes,
    _primary_read_targets,
    _promoted_revision,
    _prose_changed_paths_name_no_paths,
    _record_landing_comment,
    _record_stream_paths,
    _refuse_unrelated_plan_edit,
    _report_written_ledger_row,
    _repository_scope_paths,
    _repository_tree_boundary_violations,
    _require_commit_for_changed_manifest,
    _require_committable_checkout,
    _require_repository_tree_boundary,
    _resolve_commits,
    _resolved_path_key,
    _restore_landing_writes,
    _restore_one_landing_write,
    _revision_is_ancestor,
    _run_directory_tree_snapshot,
    _run_streams,
    _scope_worktree,
    _shadow_patch_stat,
    _shadow_stream_contamination,
    _snapshot_entries,
    _strip_store_owned_content,
    _wall_exceeded_budget,
    _worker_authored_landing_record,
    _write_shadow_patch,
)
