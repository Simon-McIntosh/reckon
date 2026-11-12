"""Bindings shared by the dispatch facade and its concern modules."""

# ruff: noqa: I001, UP035, F401, PLC0414
from __future__ import annotations

import argparse as argparse


import ast as ast


import contextlib as contextlib


import ctypes as ctypes


import dataclasses as dataclasses


import errno as errno


import fcntl as fcntl


import hashlib as hashlib


import inspect as inspect


import json as json


import os as os


import re as re


import select as select


import shutil as shutil


import signal as signal


import socket as socket


import subprocess as subprocess


import sys as sys


import threading as threading


import time as time


import uuid as uuid


from collections import (
    defaultdict as defaultdict,
)


from dataclasses import (
    dataclass as dataclass,
    field as field,
)


from datetime import (
    UTC as UTC,
    datetime as datetime,
    timedelta as timedelta,
)


from pathlib import (
    Path as Path,
)


from typing import (
    Any as Any,
    Callable as Callable,
    Iterable as Iterable,
    Iterator as Iterator,
    Mapping as Mapping,
    Sequence as Sequence,
)


from reckon import (
    _backends as _backends,
    _store as _store,
    capability as capability,
    flight as flight,
    ledger as ledger,
)


from reckon.calibration import calibration_configuration_key


from reckon.figures import rasterise_svg


from reckon._plan_html import (
    _strip_tags as _strip_tags,
    plan_headings as plan_headings,
    section_id_candidates as section_id_candidates,
    section_prose as section_prose,
)


from reckon._timestamps import (
    parse_iso as parse_iso,
    parse_utc as parse_utc,
)


from reckon.crew import (
    bar as bar_module,
)


from reckon.crew import (
    lane_document as _lane_document,
)


from reckon.crew import (
    prescription as prescription_module,
)


from reckon.crew import (
    summary as summary,
)


from reckon.crew.fleet_supervisor import (
    REQUEST_FIFO_NAME as REQUEST_FIFO_NAME,
)


from reckon.crew.node import (
    _SAFE_ID as _SAFE_ID,
    _TERMINAL_RUN_PHASES as _TERMINAL_RUN_PHASES,
    DEFAULT_MEMBER_IDLE_WINDOW as DEFAULT_MEMBER_IDLE_WINDOW,
    NEEDS_HELP_MARKER as NEEDS_HELP_MARKER,
    BudgetHold as BudgetHold,
    CompetenceLimit as CompetenceLimit,
    CrewError as CrewError,
    NodeValidation as NodeValidation,
    PlanVisibilityError as PlanVisibilityError,
    ScopeConflict as ScopeConflict,
    TaskNode as TaskNode,
    UnreconciledRuns as UnreconciledRuns,
    WatcherRequired as WatcherRequired,
    claim_disposition as claim_disposition,
    claim_repository as claim_repository,
    done_when_warnings as done_when_warnings,
    gate_population_finding as gate_population_finding,
    member_in_flight_verdict as member_in_flight_verdict,
    negative_control_finding as negative_control_finding,
    normalize_section as normalize_section,
    parse_duration as parse_duration,
    placement_query_undeclared as placement_query_undeclared,
    placement_requirement_node_local as placement_requirement_node_local,
    placement_requirement_unmet as placement_requirement_unmet,
    refuse_member_in_flight as refuse_member_in_flight,
    repository_identity as repository_identity,
    role_may_write_repository_paths as role_may_write_repository_paths,
    validate_node as validate_node,
)


from reckon.crew.prompts import (
    compose_prompt as compose_prompt,
    domain_rules_block as domain_rules_block,
    time_fence_statement as time_fence_statement,
    with_anchored_edit_instruction as _with_anchored_edit_instruction,
)


from reckon.crew.recovery import (
    REVIEW_NODE_PREFIX as REVIEW_NODE_PREFIX,
    _resolve_commit as _resolve_commit,
    resume_window_refusal as resume_window_refusal,
    stream_paths_newest_first as stream_paths_newest_first,
)


from reckon.crew.dispatch_refusals import (
    brief_reduction as brief_reduction,
    consume_context_refusal as consume_context_refusal,
    record_context_refusal as record_context_refusal,
    refuse_a_released_launch as refuse_a_released_launch,
)


from reckon.crew.refusals import (
    format_refusal as format_refusal,
)


from reckon.crew.reserve import (
    admit_windows as reserve_admit_windows,
)


from reckon.crew.review import (
    review_store_root as review_store_root,
)


from reckon.crew.routing import (
    _agent_configuration as _agent_configuration,
    _boundary_tree_roots as _boundary_tree_roots,
    _budget_verdict as _budget_verdict,
    _competence_verdict as _competence_verdict,
    _context_fit_verdict as _context_fit_verdict,
    _context_refusal as _context_refusal,
    _create_worktree as _create_worktree,
    _disposable_member_id as _disposable_member_id,
    _fleet_script as _fleet_script,
    _remove_worktree as _remove_worktree,
    _repository_tree_snapshot as _repository_tree_snapshot,
    _signal_process_group as _signal_process_group,
    _workspace_roots as _workspace_roots,
    mounted_repository_projects as mounted_repository_projects,
    reap_idle_session_members as reap_idle_session_members,
    require_plan_reviewed as require_plan_reviewed,
    require_plan_section_visible as require_plan_section_visible,
    resolve_budget_fallback as resolve_budget_fallback,
    resolve_dispatch_authority as resolve_dispatch_authority,
    resolve_dispatch_ledger_root as resolve_dispatch_ledger_root,
    resolve_role as resolve_role,
    resolve_role_override as resolve_role_override,
    resolve_scope_repository as resolve_scope_repository,
    resolve_section_routing as resolve_section_routing,
    resolved_grace_multiple as resolved_grace_multiple,
    resolved_time_budget as resolved_time_budget,
    resolved_time_ceiling as resolved_time_ceiling,
    shadow_worktree_session as shadow_worktree_session,
    shared_verdict_inputs as shared_verdict_inputs,
    signal_worker as signal_worker,
)


from reckon.crew.runs import (
    WATCH_LOG_ENV as WATCH_LOG_ENV,
    _expanded_scope_paths as _expanded_scope_paths,
    _manifest_freshness as _manifest_freshness,
    _manifest_mtime_ns as _manifest_mtime_ns,
    _merge_peer_scopes as _merge_peer_scopes,
    _mutate_pointer as _mutate_pointer,
    _pointer_lock as _pointer_lock,
    _process_start_time as _process_start_time,
    _project_derivations as _project_derivations,
    _repository_relative_scope as _repository_relative_scope,
    _scopes_overlap as _scopes_overlap,
    _shared_write_paths as _shared_write_paths,
    _utc_now as _utc_now,
    _watch_arming_line as _watch_arming_line,
    _watch_attach_line as _watch_attach_line,
    _write_json as _write_json,
    capture_run_session as capture_run_session,
    crew_home as crew_home,
    delivery_roots as delivery_roots,
    existing_node_run as existing_node_run,
    list_live as list_live,
    new_run_id as new_run_id,
    placement_job_alive as placement_job_alive,
    pointer_path as pointer_path,
    process_alive as process_alive,
    QueueFull as QueueFull,
    queue_dispatch as queue_dispatch,
    read_pointer as read_pointer,
    read_dispatch_detail as read_dispatch_detail,
    record_process_alive as record_process_alive,
    record_stacked_dispatch as record_stacked_dispatch,
    reports_dir as reports_dir,
    run_dir as run_dir,
    scheduler_job_reason as scheduler_job_reason,
    scheduler_job_state as scheduler_job_state,
    scheduler_kill_class as scheduler_kill_class,
    watch_lock_path as watch_lock_path,
    watch_log_path as watch_log_path,
    watch_observer_alive as watch_observer_alive,
    watch_state as watch_state,
    watch_stream_path as watch_stream_path,
)


from reckon.crew.dispatch_accounting import (
    _ancestor_processes as _ancestor_processes,
    _charged_claude_tokens as _charged_claude_tokens,
    _claude_authoring_turn as _claude_authoring_turn,
    _codex_authoring_turn as _codex_authoring_turn,
    _coordinator_accounting as _coordinator_accounting,
    _coordinator_runtime as _coordinator_runtime,
    _jsonl_events as _jsonl_events,
    _latest_transcript as _latest_transcript,
)


from reckon.crew.dispatch_admission import (
    LANE_GATE_READ_DEADLINE_SECONDS as LANE_GATE_READ_DEADLINE_SECONDS,
    LaneHeld as LaneHeld,
    LanePaused as LanePaused,
    ORCHESTRATOR_LANE_DECLARATION_KEY as ORCHESTRATOR_LANE_DECLARATION_KEY,
    ORCHESTRATOR_LANE_STOP_SEVERITY as ORCHESTRATOR_LANE_STOP_SEVERITY,
    _GateReadDeadline as _GateReadDeadline,
    _LANE_ADVISORY_DEFAULT_HORIZON_SECONDS as _LANE_ADVISORY_DEFAULT_HORIZON_SECONDS,
    _LANE_ADVISORY_MINIMUM_SAMPLES as _LANE_ADVISORY_MINIMUM_SAMPLES,
    _LANE_GATE_CONFIG_PATH_KEY as _LANE_GATE_CONFIG_PATH_KEY,
    _LANE_GATE_PAUSED_KEY as _LANE_GATE_PAUSED_KEY,
    _LANE_GATE_REASON_KEY as _LANE_GATE_REASON_KEY,
    _LANE_GATE_WAITING_STATES as _LANE_GATE_WAITING_STATES,
    _REQUIREMENT_PROBE_TIMEOUT_SECONDS as _REQUIREMENT_PROBE_TIMEOUT_SECONDS,
    _actionable_budget_hold as _actionable_budget_hold,
    _brief_digest as _brief_digest,
    _brief_record as _brief_record,
    _brief_text as _brief_text,
    _dispatch_fleet_gate as _dispatch_fleet_gate,
    _dispatch_lane_advisory as _dispatch_lane_advisory,
    _dispatch_lane_gate as _dispatch_lane_gate,
    _dispatch_lane_observation as _dispatch_lane_observation,
    _dispatch_lane_reading as _dispatch_lane_reading,
    _dispatch_orchestrator_lane_stop as _dispatch_orchestrator_lane_stop,
    _endpoint_answers as _endpoint_answers,
    _fence_write_roots as _fence_write_roots,
    _fleet_gate_text_reader as _fleet_gate_text_reader,
    _gate_path_check as _gate_path_check,
    _gate_paths_agree as _gate_paths_agree,
    _gate_rows_from_payload as _gate_rows_from_payload,
    _gate_text_reader as _gate_text_reader,
    _is_node_local_path as _is_node_local_path,
    _lane_advisory_cheaper_lane as _lane_advisory_cheaper_lane,
    _lane_advisory_costs as _lane_advisory_costs,
    _lane_advisory_horizon_seconds as _lane_advisory_horizon_seconds,
    _lane_advisory_instant as _lane_advisory_instant,
    _lane_advisory_lane as _lane_advisory_lane,
    _lane_advisory_ledger_runs as _lane_advisory_ledger_runs,
    _lane_declaration_evidence as _lane_declaration_evidence,
    _lane_declaration_finding as _lane_declaration_finding,
    _lane_reading_carry as _lane_reading_carry,
    _lane_reading_unknown as _lane_reading_unknown,
    _live_runs_across_backends as _live_runs_across_backends,
    _live_runs_on_backend as _live_runs_on_backend,
    _metric_number as _metric_number,
    _orchestrator_lane_discharge_candidates as _orchestrator_lane_discharge_candidates,
    _orchestrator_lane_stop_line as _orchestrator_lane_stop_line,
    _path_is_tmpfs as _path_is_tmpfs,
    _placement_requirement_targets as _placement_requirement_targets,
    _read_text_under_deadline as _read_text_under_deadline,
    _refuse_against_the_bookend_reserve as _refuse_against_the_bookend_reserve,
    _refuse_over_concurrency_ceiling as _refuse_over_concurrency_ceiling,
    _refuse_over_reservation_roster as _refuse_over_reservation_roster,
    _require_fleet_gate_open as _require_fleet_gate_open,
    _require_write_paths_in_authority as _require_write_paths_in_authority,
    _resolved_token_budget as _resolved_token_budget,
    _resolved_write_paths as _resolved_write_paths,
    _sandbox_reachability as _sandbox_reachability,
    _store_brief as _store_brief,
    _unmetered_dispatch_alternatives as _unmetered_dispatch_alternatives,
    check_placement_requirements as check_placement_requirements,
    fleet_gate_path as fleet_gate_path,
)


from reckon.crew.dispatch_claims import (
    DirectoryClaimConflict as DirectoryClaimConflict,
    NODE_DISPATCH_CLAIM_DIRECTORY as NODE_DISPATCH_CLAIM_DIRECTORY,
    RACING_WINNER_POLL_SECONDS as RACING_WINNER_POLL_SECONDS,
    _NODE_CLAIM_ATTEMPTS as _NODE_CLAIM_ATTEMPTS,
    _NODE_CLAIM_EMPTY_RECORD_STALE_SECONDS as _NODE_CLAIM_EMPTY_RECORD_STALE_SECONDS,
    _NODE_CLAIM_RECORD_ATTEMPTS as _NODE_CLAIM_RECORD_ATTEMPTS,
    _NODE_CLAIM_RECORD_INTERVAL_SECONDS as _NODE_CLAIM_RECORD_INTERVAL_SECONDS,
    _NodeDispatchClaim as _NodeDispatchClaim,
    _RepositoryScopeClaim as _RepositoryScopeClaim,
    _UNLAUNCHED_CLAIM_PHASES as _UNLAUNCHED_CLAIM_PHASES,
    _absent_path_names_a_directory as _absent_path_names_a_directory,
    _can_write_worktree as _can_write_worktree,
    _candidate_scope_entries as _candidate_scope_entries,
    _claim_holder_is_alive as _claim_holder_is_alive,
    _claim_node_dispatch as _claim_node_dispatch,
    _claim_released_on_refusal as _claim_released_on_refusal,
    _compose_dispatch_prompt as _compose_dispatch_prompt,
    _directory_claim_acceptance_kwargs as _directory_claim_acceptance_kwargs,
    _directory_claim_acceptance_line as _directory_claim_acceptance_line,
    _directory_claim_alternatives as _directory_claim_alternatives,
    _directory_claim_overlaps as _directory_claim_overlaps,
    _directory_claim_row as _directory_claim_row,
    _directory_claim_warning_line as _directory_claim_warning_line,
    _empty_claim_is_stale as _empty_claim_is_stale,
    _fractional_digits as _fractional_digits,
    _grant_landing_write_paths as _grant_landing_write_paths,
    _granted_landing_paths as _granted_landing_paths,
    _landing_fragment_paths as _landing_fragment_paths,
    _live_conflict_is_a_directory_claim as _live_conflict_is_a_directory_claim,
    _live_conflict_path as _live_conflict_path,
    _live_conflict_rows as _live_conflict_rows,
    _node_dispatch_claim_path as _node_dispatch_claim_path,
    _node_dispatch_in_flight_text as _node_dispatch_in_flight_text,
    _peer_claim_is_a_later_racing_arrival as _peer_claim_is_a_later_racing_arrival,
    _peer_claim_is_an_unlaunched_racing_winner as _peer_claim_is_an_unlaunched_racing_winner,
    _peer_scopes_without_shared_landing_paths as _peer_scopes_without_shared_landing_paths,
    _publish_launch_claim as _publish_launch_claim,
    advance_launch_step as advance_launch_step,
    LAUNCH_WORKER_STEP as LAUNCH_WORKER_STEP,
    LAUNCH_IDENTITY_STEP as LAUNCH_IDENTITY_STEP,
    _racing_claim_current as _racing_claim_current,
    _racing_clock as _racing_clock,
    _refuse_a_discarded_launch as _refuse_a_discarded_launch,
    _racing_pause as _racing_pause,
    _raise_repository_scope_conflict as _raise_repository_scope_conflict,
    _read_node_dispatch_claim as _read_node_dispatch_claim,
    _reclaim_stale_node_dispatch_claim as _reclaim_stale_node_dispatch_claim,
    _release_launch_claim as _release_launch_claim,
    _repository_scope_claims as _repository_scope_claims,
    _resolve_declared_path as _resolve_declared_path,
    _resolved_node_scope_entries as _resolved_node_scope_entries,
    _resolved_scope_entries as _resolved_scope_entries,
    _scope_derivation_project as _scope_derivation_project,
    _settle_racing_winner as _settle_racing_winner,
    _shared_landing_paths as _shared_landing_paths,
    _writes_its_landing_fragment as _writes_its_landing_fragment,
    refuse_widen_scope_conflicts as refuse_widen_scope_conflicts,
)


from reckon.crew.dispatch_launch import (
    ATTEMPT_RECORD_NAME as ATTEMPT_RECORD_NAME,
    CREW_STATE_ENVIRONMENT as CREW_STATE_ENVIRONMENT,
    EXIT_RECORD_NAME as EXIT_RECORD_NAME,
    FLEET_FIFO_NAME as FLEET_FIFO_NAME,
    FLEET_RECORD_PATH_ENV as FLEET_RECORD_PATH_ENV,
    FLEET_REQUEST_POLL_SECONDS as FLEET_REQUEST_POLL_SECONDS,
    FLEET_SPAWN_ACK_BOUND_SECONDS as FLEET_SPAWN_ACK_BOUND_SECONDS,
    FLEET_SPAWN_ACK_NAME as FLEET_SPAWN_ACK_NAME,
    FLEET_SPAWN_ENV as FLEET_SPAWN_ENV,
    LAUNCH_FAILED_PHASE as LAUNCH_FAILED_PHASE,
    LaunchResolutionError as LaunchResolutionError,
    SANDBOX_STARTUP_WINDOW_SECONDS as SANDBOX_STARTUP_WINDOW_SECONDS,
    STOP_GRACE_DEFAULT as STOP_GRACE_DEFAULT,
    STOP_GRACE_ENV as STOP_GRACE_ENV,
    SUPERVISOR_ENTRY as SUPERVISOR_ENTRY,
    SUPERVISOR_SPEC_NAME as SUPERVISOR_SPEC_NAME,
    SUPERVISOR_SURVIVAL_POLL_SECONDS as SUPERVISOR_SURVIVAL_POLL_SECONDS,
    SUPERVISOR_SURVIVAL_SECONDS as SUPERVISOR_SURVIVAL_SECONDS,
    TERMINAL_MANIFEST_GRACE_DEFAULT as TERMINAL_MANIFEST_GRACE_DEFAULT,
    TERMINAL_MANIFEST_GRACE_ENV as TERMINAL_MANIFEST_GRACE_ENV,
    TREE_SNAPSHOT_NAME as TREE_SNAPSHOT_NAME,
    TmpHeadroomError as TmpHeadroomError,
    VANISHED_BIND_STARTUP_POLL_SECONDS as VANISHED_BIND_STARTUP_POLL_SECONDS,
    WORKER_RECORD_NAME as WORKER_RECORD_NAME,
    WORKER_SCRATCH_BUDGET_BYTES as WORKER_SCRATCH_BUDGET_BYTES,
    WORKER_SCRATCH_ROOT_DEFAULT as WORKER_SCRATCH_ROOT_DEFAULT,
    WORKER_SCRATCH_ROOT_ENV as WORKER_SCRATCH_ROOT_ENV,
    WORKER_SCRATCH_ROOT_NAME as WORKER_SCRATCH_ROOT_NAME,
    _LAUNCHED_WORKERS as _LAUNCHED_WORKERS,
    _LAUNCHED_WORKERS_HANDOVER_ENV as _LAUNCHED_WORKERS_HANDOVER_ENV,
    _LAUNCHED_WORKERS_LOCK as _LAUNCHED_WORKERS_LOCK,
    _LAUNCHED_WORKERS_WAKE as _LAUNCHED_WORKERS_WAKE,
    _LAUNCHED_WORKER_REAPER as _LAUNCHED_WORKER_REAPER,
    _LAUNCHED_WORKER_RUNS as _LAUNCHED_WORKER_RUNS,
    _LAUNCH_FAILURE_STDERR_BYTES as _LAUNCH_FAILURE_STDERR_BYTES,
    _PLACEMENT_PROBE_ATTEMPTS as _PLACEMENT_PROBE_ATTEMPTS,
    _PLACEMENT_PROBE_TIMEOUT_SECONDS as _PLACEMENT_PROBE_TIMEOUT_SECONDS,
    _PR_SET_CHILD_SUBREAPER as _PR_SET_CHILD_SUBREAPER,
    _SUPERVISOR_LAUNCHER_SOURCE as _SUPERVISOR_LAUNCHER_SOURCE,
    _VANISHED_BIND_REFUSAL_PHRASES as _VANISHED_BIND_REFUSAL_PHRASES,
    _WORKER_DONE_MANIFEST_STATUSES as _WORKER_DONE_MANIFEST_STATUSES,
    _WORKER_GRACE_KILL_SECONDS as _WORKER_GRACE_KILL_SECONDS,
    _WORKER_MANIFEST_CEILING_SECONDS as _WORKER_MANIFEST_CEILING_SECONDS,
    _WORKER_MANIFEST_POLL_SECONDS as _WORKER_MANIFEST_POLL_SECONDS,
    _WORKER_MANIFEST_QUIET_SECONDS as _WORKER_MANIFEST_QUIET_SECONDS,
    _WorkerPid as _WorkerPid,
    _adopt_launched_workers_from_reexec as _adopt_launched_workers_from_reexec,
    _attempt_artifact_path as _attempt_artifact_path,
    _attempt_is_current as _attempt_is_current,
    _attempt_started_ns as _attempt_started_ns,
    _become_child_subreaper as _become_child_subreaper,
    _boundary_snapshot_roots as _boundary_snapshot_roots,
    _carried_crew_environment as _carried_crew_environment,
    _child_processes_of as _child_processes_of,
    _confirm_supervisor_survived as _confirm_supervisor_survived,
    _current_host_facts as _current_host_facts,
    _delivered_phase as _delivered_phase,
    _died_over_a_vanished_bind_source as _died_over_a_vanished_bind_source,
    _empty_stream_launch_failure as _empty_stream_launch_failure,
    _ensure_launched_worker_reaper as _ensure_launched_worker_reaper,
    _export_launched_workers_for_reexec as _export_launched_workers_for_reexec,
    _fleet_runtime_dir as _fleet_runtime_dir,
    _fleet_spawn_enabled as _fleet_spawn_enabled,
    _intermediate_failure_detail as _intermediate_failure_detail,
    _iso_stamp_to_ns as _iso_stamp_to_ns,
    _launch_failure_record as _launch_failure_record,
    _launched_prior_session as _launched_prior_session,
    _launched_worker_record as _launched_worker_record,
    _persisted_worker_environment as _persisted_worker_environment,
    _placed_record_identity as _placed_record_identity,
    _placement_hold_payload as _placement_hold_payload,
    _placement_job_alive as _placement_job_alive,
    _placement_job_state as _placement_job_state,
    _plan_composed_the_fence as _plan_composed_the_fence,
    _prepare_attempt_records as _prepare_attempt_records,
    _publish_stored_phase as _publish_stored_phase,
    _read_fleet_record as _read_fleet_record,
    _read_only_bind_sources as _read_only_bind_sources,
    _read_spawn_ack as _read_spawn_ack,
    _reap_launched_workers as _reap_launched_workers,
    _reap_the_launched_worker as _reap_the_launched_worker,
    _reap_worker_on_its_terminal_manifest as _reap_worker_on_its_terminal_manifest,
    _record_is_this_attempt as _record_is_this_attempt,
    _record_launch_abandoned_before_spawn as _record_launch_abandoned_before_spawn,
    _record_launch_failure as _record_launch_failure,
    _record_stop_before_spawn as _record_stop_before_spawn,
    _record_unreached_launch as _record_unreached_launch,
    _refuse_superseded_launch as _refuse_superseded_launch,
    _refusal_names_a_bind_source as _refusal_names_a_bind_source,
    _removable_scratch_target as _removable_scratch_target,
    _run_supervisor as _run_supervisor,
    _runs_inside_fleet_allocation as _runs_inside_fleet_allocation,
    _shim_names as _shim_names,
    _spawn as _spawn,
    _spawn_detached_supervisor as _spawn_detached_supervisor,
    _spawn_detached_worker as _spawn_detached_worker,
    _spawn_through_fleet as _spawn_through_fleet,
    _spawn_worker_retrying_a_vanished_bind as _spawn_worker_retrying_a_vanished_bind,
    _start_supervisor as _start_supervisor,
    _started_phase as _started_phase,
    _stop_grace_seconds as _stop_grace_seconds,
    _stop_is_requested as _stop_is_requested,
    _stream_record_facts as _stream_record_facts,
    _supervisor_argv as _supervisor_argv,
    _supervisor_exit_detail as _supervisor_exit_detail,
    _supervisor_exit_record as _supervisor_exit_record,
    _supervisor_launched_worker as _supervisor_launched_worker,
    _supervisor_manifest_baseline_ns as _supervisor_manifest_baseline_ns,
    _supervisor_manifest_path as _supervisor_manifest_path,
    _supervisor_spawn_worker as _supervisor_spawn_worker,
    _supervisor_spec as _supervisor_spec,
    _supervisor_tree_snapshot as _supervisor_tree_snapshot,
    _supervisor_write as _supervisor_write,
    _terminal_manifest_grace_seconds as _terminal_manifest_grace_seconds,
    _unresolved_backend_command as _unresolved_backend_command,
    _wait_status_from_returncode as _wait_status_from_returncode,
    _wait_status_record as _wait_status_record,
    _watch_request_slug as _watch_request_slug,
    _worker_default_signals as _worker_default_signals,
    _worker_git_shim_directory as _worker_git_shim_directory,
    _worker_host_line as _worker_host_line,
    _worker_manifest_done_status as _worker_manifest_done_status,
    _worker_manifest_is_whole as _worker_manifest_is_whole,
    _worker_process_environment as _worker_process_environment,
    _worker_reaper_loop as _worker_reaper_loop,
    _worker_runtime_environment as _worker_runtime_environment,
    _worker_runtime_plan as _worker_runtime_plan,
    _worker_shim_directory as _worker_shim_directory,
    _write_attempt_artifact as _write_attempt_artifact,
    _write_boundary_tree_snapshot as _write_boundary_tree_snapshot,
    _write_fleet_request as _write_fleet_request,
    apply_backend_placement as apply_backend_placement,
    assert_routable_backends_resolvable as assert_routable_backends_resolvable,
    ensure_worker_scratch as ensure_worker_scratch,
    harness_command_index as harness_command_index,
    launch_search_path as launch_search_path,
    placement_job_id as placement_job_id,
    preflight_launch_command as preflight_launch_command,
    remove_worker_scratch as remove_worker_scratch,
    require_worker_scratch_headroom as require_worker_scratch_headroom,
    resolve_backend_placement as resolve_backend_placement,
    resolve_launch_executable as resolve_launch_executable,
    supervised_launch as supervised_launch,
    tree_size_bytes as tree_size_bytes,
    worker_scratch_dir as worker_scratch_dir,
    worker_scratch_root as worker_scratch_root,
)


from reckon.crew.dispatch_peer import (
    _INOTIFY_EVENTS as _INOTIFY_EVENTS,
    _adjacent_live_peers as _adjacent_live_peers,
    _channel_root as _channel_root,
    _dotted_name as _dotted_name,
    _inotify_descriptor as _inotify_descriptor,
    _module_aliases as _module_aliases,
    _needs_help_for_question as _needs_help_for_question,
    _nodes_are_adjacent as _nodes_are_adjacent,
    _peer_command as _peer_command,
    _python_references as _python_references,
    _question_path as _question_path,
    _read_json as _read_json,
    _references_any_module as _references_any_module,
    _resolve_peer as _resolve_peer,
    _scoped_python_files as _scoped_python_files,
    _stamp_agent_display as _stamp_agent_display,
    _supervisor_command as _supervisor_command,
    _unwire_peer_channels as _unwire_peer_channels,
    _update_peer_index as _update_peer_index,
    _wait_seconds as _wait_seconds,
    _wire_peer_channels as _wire_peer_channels,
    peer_ask as peer_ask,
    peer_list as peer_list,
    peer_read as peer_read,
    peer_reply as peer_reply,
)


from reckon.crew.dispatch_picker import (
    PICKER_DISPATCH_TIMEOUT_SECONDS as PICKER_DISPATCH_TIMEOUT_SECONDS,
    _PICKER_PER_CALL_FIELDS as _PICKER_PER_CALL_FIELDS,
    _attach_shadow_picker_selection as _attach_shadow_picker_selection,
    _picker_budget_snapshot as _picker_budget_snapshot,
    _picker_fallback as _picker_fallback,
    _picker_input as _picker_input,
    _picker_ledger_rows as _picker_ledger_rows,
    _picker_refusal_reasons as _picker_refusal_reasons,
    _picker_verdict_inputs as _picker_verdict_inputs,
    _record_shadow_picker_selection as _record_shadow_picker_selection,
    _reportable_picker_selection as _reportable_picker_selection,
    _start_shadow_picker_selection as _start_shadow_picker_selection,
    _write_existing_pointer as _write_existing_pointer,
    build_picker_inputs as build_picker_inputs,
    dispatch_picker_selection as dispatch_picker_selection,
    resolve_dispatch_route as resolve_dispatch_route,
)


from reckon.crew.dispatch_plan import (
    DispatchPlan as DispatchPlan,
    ROLE_OPENNESS as ROLE_OPENNESS,
    SPEC_LEVEL_OPENNESS as SPEC_LEVEL_OPENNESS,
    UNKNOWN_OPENNESS as UNKNOWN_OPENNESS,
    _carry_declared_gate_documents as _carry_declared_gate_documents,
    _dispatch_lane_allowance as _dispatch_lane_allowance,
    _lane_allowance_unknown as _lane_allowance_unknown,
    _lane_worker_allowance as _lane_worker_allowance,
    _offered_local_backend as _offered_local_backend,
    _plan_impl_at_dispatch as _plan_impl_at_dispatch,
    _repairs_ledger_root as _repairs_ledger_root,
    _require_repairs_target as _require_repairs_target,
    _resolved_wave_id as _resolved_wave_id,
    continue_hold_on_its_offered_local_lane as continue_hold_on_its_offered_local_lane,
    open_endedness_score as open_endedness_score,
    plan_dispatch as _raw_plan_dispatch,
)


from reckon.crew.dispatch_sections import (
    DONE_WHEN_PLAN_TEXT_SPAN_WORDS as DONE_WHEN_PLAN_TEXT_SPAN_WORDS,
    _dispatch_section_routing as _dispatch_section_routing,
    _done_when_plan_overlap_warning as _done_when_plan_overlap_warning,
    _longest_contiguous_word_span as _longest_contiguous_word_span,
    _normalised_words as _normalised_words,
    _plan_section_text as _plan_section_text,
    _resolved_plan_section_text as _resolved_plan_section_text,
    _section_heading_matches as _section_heading_matches,
    _section_routing_evidence as _section_routing_evidence,
    _section_routing_failure as _section_routing_failure,
    project_mount_repository as project_mount_repository,
    resolve_project_repository as resolve_project_repository,
)


from reckon.crew.dispatch_sessions import (
    FENCE_WORKERS as FENCE_WORKERS,
    _apply_orientation_check as _apply_orientation_check,
    _backend_settings as _backend_settings,
    _capture_session_absence as _capture_session_absence,
    _carry_declared_placement as _carry_declared_placement,
    _carry_fence_unprotected as _carry_fence_unprotected,
    _current_harness_session as _current_harness_session,
    _dispatch_session_absence as _dispatch_session_absence,
    _harness_behind_the_fence as _harness_behind_the_fence,
    _inherited_worktree_reading as _inherited_worktree_reading,
    _lane_prompt as _lane_prompt,
    _names_the_fence as _names_the_fence,
    _prior_same_task_run as _prior_same_task_run,
    _prior_session_still_held as _prior_session_still_held,
    _record_node_id as _record_node_id,
    _record_plan as _record_plan,
    _recorded_manifest_path as _recorded_manifest_path,
    _recorded_task_node as _recorded_task_node,
    _restate_time_fence as _restate_time_fence,
    _reviewed_run_id as _reviewed_run_id,
    _run_stream_path as _run_stream_path,
    _shadow_mask_paths as _shadow_mask_paths,
    _session_too_large_to_continue as _session_too_large_to_continue,
    _stored_phase_survives as _stored_phase_survives,
    _task_identity as _task_identity,
    _task_session_resolution as _task_session_resolution,
    _terminal_phase_survives as _terminal_phase_survives,
    _worktree_git_read as _worktree_git_read,
    attach as attach,
    change_lane as change_lane,
    observe as observe,
    record_resumption as record_resumption,
    resume_plan as resume_plan,
    terminate as terminate,
)


from reckon.crew.dispatch_watch import (
    SESSION_HOST_DIRECTORY as SESSION_HOST_DIRECTORY,
    WATCHER_LOAD_BOUND_SECONDS as WATCHER_LOAD_BOUND_SECONDS,
    WATCH_ARMING_ENV as WATCH_ARMING_ENV,
    _FollowerAdmissionUnmet as _FollowerAdmissionUnmet,
    _PYTEST_TEMPORARY_ROOT as _PYTEST_TEMPORARY_ROOT,
    _SpawnedHandle as _SpawnedHandle,
    _WATCH_PRODUCER_SUPERVISOR as _WATCH_PRODUCER_SUPERVISOR,
    _ask_session_host_for_follower as _ask_session_host_for_follower,
    _current_and_ancestor_argvs as _current_and_ancestor_argvs,
    _declared_basetemp as _declared_basetemp,
    _ensure_watch_producer as _ensure_watch_producer,
    _open_request_fifo as _open_request_fifo,
    _refuse_arming_under_a_throwaway_home as _refuse_arming_under_a_throwaway_home,
    _released_follower_warning as _released_follower_warning,
    _running_under_pytest as _running_under_pytest,
    _session_host_fifo as _session_host_fifo,
    _session_host_fifo_path as _session_host_fifo_path,
    _session_host_owner as _session_host_owner,
    _session_host_record_path as _session_host_record_path,
    _session_host_runs_follower as _session_host_runs_follower,
    _session_host_runtime_root as _session_host_runtime_root,
    _session_host_waiting as _session_host_waiting,
    _start_watch_producer as _start_watch_producer,
    _stop_watch_producer_within as _stop_watch_producer_within,
    _temporary_home_root as _temporary_home_root,
    _unmet_follower_conditions as _unmet_follower_conditions,
    _watch_arming_intent as _watch_arming_intent,
    _watch_executable as _watch_executable,
    _watch_producer_argv as _watch_producer_argv,
    _watcher_delivery_admission as _watcher_delivery_admission,
    watch_arming_suppressed as watch_arming_suppressed,
)


_SUITE_TEMPLATE_SPELLING = "<node tests>"


REVIEW_FOLLOWS_RUN_ORIGIN = "review-follows-run"
