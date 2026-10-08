# ruff: noqa: F401, PLC0414
import contextlib as contextlib
import functools as functools
import hashlib as hashlib
import json as json
import os as os
import shlex as shlex
import shutil as shutil
import signal as signal
import subprocess as subprocess
import sys as sys
import threading as threading
import time as time
from collections.abc import Iterable as Iterable
from collections.abc import Iterator as Iterator
from collections.abc import Mapping as Mapping
from datetime import UTC as UTC
from datetime import datetime as datetime
from pathlib import Path as Path
from typing import Any as Any
from typing import NamedTuple as NamedTuple

import click as click

from reckon import __version__ as __version__
from reckon import pages as pages
from reckon._store import _config_home as _config_home
from reckon._store import _state_root as _state_root
from reckon._store import write_json_atomically as write_json_atomically
from reckon._timestamps import parse_utc as parse_utc
from reckon.cli_entry import (
    _CREW_HOST_PLUGIN_REL as _CREW_HOST_PLUGIN_REL,
)
from reckon.cli_entry import (
    CLAUDE_SKILLS_DIR_ENV as CLAUDE_SKILLS_DIR_ENV,
)
from reckon.cli_entry import (
    CREW_HOST_DANGLING as CREW_HOST_DANGLING,
)
from reckon.cli_entry import (
    CREW_HOST_ELSEWHERE as CREW_HOST_ELSEWHERE,
)
from reckon.cli_entry import (
    CREW_HOST_MISSING as CREW_HOST_MISSING,
)
from reckon.cli_entry import (
    CREW_HOST_PLUGIN_NAME as CREW_HOST_PLUGIN_NAME,
)
from reckon.cli_entry import (
    CREW_HOST_VALID as CREW_HOST_VALID,
)
from reckon.cli_entry import (
    CREW_HOST_WORKTREE as CREW_HOST_WORKTREE,
)
from reckon.cli_entry import (
    CrewHostLink as CrewHostLink,
)
from reckon.cli_entry import (
    _asset_root as _asset_root,
)
from reckon.cli_entry import (
    _claude_plugin_validate as _claude_plugin_validate,
)
from reckon.cli_entry import (
    _configure_crew_guards as _configure_crew_guards,
)
from reckon.cli_entry import (
    _copied_where_linked as _copied_where_linked,
)
from reckon.cli_entry import (
    _copy_asset_directory as _copy_asset_directory,
)
from reckon.cli_entry import (
    _crew_guard_path as _crew_guard_path,
)
from reckon.cli_entry import (
    _crew_state_exists as _crew_state_exists,
)
from reckon.cli_entry import (
    _git_capture as _git_capture,
)
from reckon.cli_entry import (
    _in_linked_worktree as _in_linked_worktree,
)
from reckon.cli_entry import (
    _main_checkout as _main_checkout,
)
from reckon.cli_entry import (
    _merge_records_by_id as _merge_records_by_id,
)
from reckon.cli_entry import (
    _native_agent_guard_path as _native_agent_guard_path,
)
from reckon.cli_entry import (
    _personal_skills_dir as _personal_skills_dir,
)
from reckon.cli_entry import (
    _project_docs_root as _project_docs_root,
)
from reckon.cli_entry import (
    _reckon_checkout as _reckon_checkout,
)
from reckon.cli_entry import (
    _skills_source as _skills_source,
)
from reckon.cli_entry import (
    _sync_crew_host_plugin as _sync_crew_host_plugin,
)
from reckon.cli_entry import (
    _worker_git_guard_path as _worker_git_guard_path,
)
from reckon.cli_entry import (
    _worker_message_guard_path as _worker_message_guard_path,
)
from reckon.cli_entry import (
    crew_host_link_state as crew_host_link_state,
)
from reckon.cli_entry import (
    link_crew_host_plugin as link_crew_host_plugin,
)
from reckon.cli_entry import (
    main as main,
)
from reckon.crew_dispatch_commands import (
    _config_value_at as _config_value_at,
)
from reckon.crew_dispatch_commands import (
    _crew_modules as _crew_modules,
)
from reckon.crew_dispatch_commands import (
    _crew_result_ok as _crew_result_ok,
)
from reckon.crew_dispatch_commands import (
    _dispatch_override_resolution as _dispatch_override_resolution,
)
from reckon.crew_dispatch_commands import (
    _dispatch_resolved_flight as _dispatch_resolved_flight,
)
from reckon.crew_dispatch_commands import (
    _emit as _emit,
)
from reckon.crew_dispatch_commands import (
    _emit_crew_result as _emit_crew_result,
)
from reckon.crew_dispatch_commands import (
    _emit_dry_run_request_error as _emit_dry_run_request_error,
)
from reckon.crew_dispatch_commands import (
    _flight_default_backend_override as _flight_default_backend_override,
)
from reckon.crew_dispatch_commands import (
    _holds_stdout_for_one_document as _holds_stdout_for_one_document,
)
from reckon.crew_dispatch_commands import (
    _lane_paused_detail as _lane_paused_detail,
)
from reckon.crew_dispatch_commands import (
    _layer_flight_config as _layer_flight_config,
)
from reckon.crew_dispatch_commands import (
    _model_availability_refusal as _model_availability_refusal,
)
from reckon.crew_dispatch_commands import (
    _OneDocumentStdout as _OneDocumentStdout,
)
from reckon.crew_dispatch_commands import (
    _parse_ready_node as _parse_ready_node,
)
from reckon.crew_dispatch_commands import (
    _peer_scopes as _peer_scopes,
)
from reckon.crew_dispatch_commands import (
    _picker_routed_backend as _picker_routed_backend,
)
from reckon.crew_dispatch_commands import (
    _repo_root as _repo_root,
)
from reckon.crew_dispatch_commands import (
    _require_configured_override_path as _require_configured_override_path,
)
from reckon.crew_dispatch_commands import (
    _require_configured_override_paths as _require_configured_override_paths,
)
from reckon.crew_dispatch_commands import (
    _resolved_flight as _resolved_flight,
)
from reckon.crew_dispatch_commands import (
    _resolved_gc_repo as _resolved_gc_repo,
)
from reckon.crew_dispatch_commands import (
    _resolved_session as _resolved_session,
)
from reckon.crew_dispatch_commands import (
    _single_document_stdout as _single_document_stdout,
)
from reckon.crew_dispatch_commands import (
    _validation_detail as _validation_detail,
)
from reckon.crew_dispatch_commands import (
    _with_resolved_overrides as _with_resolved_overrides,
)
from reckon.crew_dispatch_commands import (
    crew as crew,
)
from reckon.crew_dispatch_commands import (
    crew_attach as crew_attach,
)
from reckon.crew_dispatch_commands import (
    crew_dispatch as crew_dispatch,
)
from reckon.crew_dispatch_commands import (
    crew_gate as crew_gate,
)
from reckon.crew_dispatch_commands import (
    crew_observe as crew_observe,
)
from reckon.crew_dispatch_commands import (
    crew_pick as crew_pick,
)
from reckon.crew_dispatch_commands import (
    crew_preflight as crew_preflight,
)
from reckon.crew_dispatch_commands import (
    crew_review_plan as crew_review_plan,
)
from reckon.crew_dispatch_commands import (
    crew_shadow as crew_shadow,
)
from reckon.crew_follow_commands import (
    _ATTENTION_DEPRECATION as _ATTENTION_DEPRECATION,
)
from reckon.crew_follow_commands import (
    _ATTENTION_REMOVED_AFTER as _ATTENTION_REMOVED_AFTER,
)
from reckon.crew_follow_commands import (
    _FOLLOW_REATTACH_FRAME as _FOLLOW_REATTACH_FRAME,
)
from reckon.crew_follow_commands import (
    _FOLLOWER_CHECKPOINT_ENV as _FOLLOWER_CHECKPOINT_ENV,
)
from reckon.crew_follow_commands import (
    _FOLLOWER_LIFETIME_ENV as _FOLLOWER_LIFETIME_ENV,
)
from reckon.crew_follow_commands import (
    _FOLLOWER_RELOAD_PROBE as _FOLLOWER_RELOAD_PROBE,
)
from reckon.crew_follow_commands import (
    _FOLLOWER_RELOAD_PROBE_TIMEOUT as _FOLLOWER_RELOAD_PROBE_TIMEOUT,
)
from reckon.crew_follow_commands import (
    _HISTORY_FRAME as _HISTORY_FRAME,
)
from reckon.crew_follow_commands import (
    _SNAPSHOT_READER as _SNAPSHOT_READER,
)
from reckon.crew_follow_commands import (
    FOLLOWER_END_EVENT as FOLLOWER_END_EVENT,
)
from reckon.crew_follow_commands import (
    FOLLOWER_FORMAT_EVENT as FOLLOWER_FORMAT_EVENT,
)
from reckon.crew_follow_commands import (
    FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT as FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT,
)
from reckon.crew_follow_commands import (
    FOLLOWER_PRODUCER_RELOADING_EVENT as FOLLOWER_PRODUCER_RELOADING_EVENT,
)
from reckon.crew_follow_commands import (
    FOLLOWER_PRODUCER_STOPPED_EVENT as FOLLOWER_PRODUCER_STOPPED_EVENT,
)
from reckon.crew_follow_commands import (
    FOLLOWER_REATTACH_EVENT as FOLLOWER_REATTACH_EVENT,
)
from reckon.crew_follow_commands import (
    FOLLOWER_RESUME_EVENT as FOLLOWER_RESUME_EVENT,
)
from reckon.crew_follow_commands import (
    FOLLOWER_STALE_PRODUCER_EVENT as FOLLOWER_STALE_PRODUCER_EVENT,
)
from reckon.crew_follow_commands import (
    HISTORY_DIM as HISTORY_DIM,
)
from reckon.crew_follow_commands import (
    HISTORY_RESET as HISTORY_RESET,
)
from reckon.crew_follow_commands import (
    PRODUCER_POLL_INTERVAL_CAP_SECONDS as PRODUCER_POLL_INTERVAL_CAP_SECONDS,
)
from reckon.crew_follow_commands import (
    PRODUCER_RELOAD_WINDOW_SECONDS as PRODUCER_RELOAD_WINDOW_SECONDS,
)
from reckon.crew_follow_commands import (
    TICKER_THEMES as TICKER_THEMES,
)
from reckon.crew_follow_commands import (
    _carried_lifetime_deadline as _carried_lifetime_deadline,
)
from reckon.crew_follow_commands import (
    _dim_history_line as _dim_history_line,
)
from reckon.crew_follow_commands import (
    _echo_follow_line as _echo_follow_line,
)
from reckon.crew_follow_commands import (
    _fleet_replay as _fleet_replay,
)
from reckon.crew_follow_commands import (
    _follow_boundary as _follow_boundary,
)
from reckon.crew_follow_commands import (
    _follow_gap_rows as _follow_gap_rows,
)
from reckon.crew_follow_commands import (
    _follow_history_burst as _follow_history_burst,
)
from reckon.crew_follow_commands import (
    _follow_history_caps as _follow_history_caps,
)
from reckon.crew_follow_commands import (
    _follow_reattach_line as _follow_reattach_line,
)
from reckon.crew_follow_commands import (
    _follow_render_event as _follow_render_event,
)
from reckon.crew_follow_commands import (
    _follow_replay_visible as _follow_replay_visible,
)
from reckon.crew_follow_commands import (
    _follow_resume_plan as _follow_resume_plan,
)
from reckon.crew_follow_commands import (
    _follow_row_stamp as _follow_row_stamp,
)
from reckon.crew_follow_commands import (
    _follow_selects as _follow_selects,
)
from reckon.crew_follow_commands import (
    _follow_watch_lines as _follow_watch_lines,
)
from reckon.crew_follow_commands import (
    _follower_end_event as _follower_end_event,
)
from reckon.crew_follow_commands import (
    _follower_end_line as _follower_end_line,
)
from reckon.crew_follow_commands import (
    _FollowerReloader as _FollowerReloader,
)
from reckon.crew_follow_commands import (
    _import_root as _import_root,
)
from reckon.crew_follow_commands import (
    _logged_producer_stop as _logged_producer_stop,
)
from reckon.crew_follow_commands import (
    _needs_you_runs as _needs_you_runs,
)
from reckon.crew_follow_commands import (
    _population_has_live_work as _population_has_live_work,
)
from reckon.crew_follow_commands import (
    _recorded_fleet_times as _recorded_fleet_times,
)
from reckon.crew_follow_commands import (
    _row_is_stale_inventory as _row_is_stale_inventory,
)
from reckon.crew_follow_commands import (
    _seed_ticker_memory as _seed_ticker_memory,
)
from reckon.crew_follow_commands import (
    _short_code_stamp as _short_code_stamp,
)
from reckon.crew_follow_commands import (
    _snapshot_module as _snapshot_module,
)
from reckon.crew_follow_commands import (
    _StampPoll as _StampPoll,
)
from reckon.crew_follow_commands import (
    _stream_events_upto as _stream_events_upto,
)
from reckon.crew_follow_commands import (
    _sweep_lapsed_holds as _sweep_lapsed_holds,
)
from reckon.crew_follow_commands import (
    _take_follower_checkpoint as _take_follower_checkpoint,
)
from reckon.crew_follow_commands import (
    _ticker_grid as _ticker_grid,
)
from reckon.crew_follow_commands import (
    _ticker_layout_signature as _ticker_layout_signature,
)
from reckon.crew_follow_commands import (
    _ticker_options as _ticker_options,
)
from reckon.crew_follow_commands import (
    crew_follow as crew_follow,
)
from reckon.crew_follow_commands import (
    crew_host as crew_host,
)
from reckon.crew_follow_commands import (
    crew_watch as crew_watch,
)
from reckon.crew_follow_commands import (
    follower_row_path as follower_row_path,
)
from reckon.crew_run_commands import (
    _PATH_KINDS as _PATH_KINDS,
)
from reckon.crew_run_commands import (
    WIDEN_REFUSING_MANIFEST_STATUSES as WIDEN_REFUSING_MANIFEST_STATUSES,
)
from reckon.crew_run_commands import (
    WIDEN_RULE as WIDEN_RULE,
)
from reckon.crew_run_commands import (
    WIDENABLE_PHASE as WIDENABLE_PHASE,
)
from reckon.crew_run_commands import (
    _ledger_module as _ledger_module,
)
from reckon.crew_run_commands import (
    _manifest_reported_status as _manifest_reported_status,
)
from reckon.crew_run_commands import (
    _reviewed_head_from_run_records as _reviewed_head_from_run_records,
)
from reckon.crew_run_commands import (
    _suite_project_root as _suite_project_root,
)
from reckon.crew_run_commands import (
    _widen_eligibility as _widen_eligibility,
)
from reckon.crew_run_commands import (
    _widen_refusal as _widen_refusal,
)
from reckon.crew_run_commands import (
    _widen_running_worker as _widen_running_worker,
)
from reckon.crew_run_commands import (
    crew_ack as crew_ack,
)
from reckon.crew_run_commands import (
    crew_budget_reset as crew_budget_reset,
)
from reckon.crew_run_commands import (
    crew_check_manifest as crew_check_manifest,
)
from reckon.crew_run_commands import (
    crew_complete as crew_complete,
)
from reckon.crew_run_commands import (
    crew_directory as crew_directory,
)
from reckon.crew_run_commands import (
    crew_discard as crew_discard,
)
from reckon.crew_run_commands import (
    crew_dispose as crew_dispose,
)
from reckon.crew_run_commands import (
    crew_drain as crew_drain,
)
from reckon.crew_run_commands import (
    crew_gc as crew_gc,
)
from reckon.crew_run_commands import (
    crew_ledger as crew_ledger,
)
from reckon.crew_run_commands import (
    crew_list as crew_list,
)
from reckon.crew_run_commands import (
    crew_member as crew_member,
)
from reckon.crew_run_commands import (
    crew_member_add as crew_member_add,
)
from reckon.crew_run_commands import (
    crew_member_list as crew_member_list,
)
from reckon.crew_run_commands import (
    crew_path as crew_path,
)
from reckon.crew_run_commands import (
    crew_placement as crew_placement,
)
from reckon.crew_run_commands import (
    crew_recover as crew_recover,
)
from reckon.crew_run_commands import (
    crew_redispatch as crew_redispatch,
)
from reckon.crew_run_commands import (
    crew_repair_completion as crew_repair_completion,
)
from reckon.crew_run_commands import (
    crew_repair_status as crew_repair_status,
)
from reckon.crew_run_commands import (
    crew_resume as crew_resume,
)
from reckon.crew_run_commands import (
    crew_resume_ready as crew_resume_ready,
)
from reckon.crew_run_commands import (
    crew_split_runs as crew_split_runs,
)
from reckon.crew_run_commands import (
    crew_stop as crew_stop,
)
from reckon.crew_run_commands import (
    crew_suite as crew_suite,
)
from reckon.crew_run_commands import (
    crew_suite_run as crew_suite_run,
)
from reckon.crew_run_commands import (
    crew_suite_waive as crew_suite_waive,
)
from reckon.crew_run_commands import (
    crew_unwatch as crew_unwatch,
)
from reckon.crew_run_commands import (
    crew_velocity as crew_velocity,
)
from reckon.crew_run_commands import (
    crew_verify_gate as crew_verify_gate,
)
from reckon.crew_run_commands import (
    crew_widen as crew_widen,
)
from reckon.hooks import install as hook_installer
from reckon.project_maintenance_commands import (
    _CI_WORKFLOW_TEMPLATE as _CI_WORKFLOW_TEMPLATE,
)
from reckon.project_maintenance_commands import (
    _echo_lifecycle_findings as _echo_lifecycle_findings,
)
from reckon.project_maintenance_commands import (
    _echo_render_contract_failures as _echo_render_contract_failures,
)
from reckon.project_maintenance_commands import (
    _print_roadmap_report as _print_roadmap_report,
)
from reckon.project_maintenance_commands import (
    _project_environment_drift as _project_environment_drift,
)
from reckon.project_maintenance_commands import (
    _served_code_line as _served_code_line,
)
from reckon.project_maintenance_commands import (
    _service_call as _service_call,
)
from reckon.project_maintenance_commands import (
    _service_module as _service_module,
)
from reckon.project_maintenance_commands import (
    archive as archive,
)
from reckon.project_maintenance_commands import (
    audit as audit,
)
from reckon.project_maintenance_commands import (
    audit_doc as audit_doc,
)
from reckon.project_maintenance_commands import (
    build as build,
)
from reckon.project_maintenance_commands import (
    doctor as doctor,
)
from reckon.project_maintenance_commands import (
    evidence as evidence,
)
from reckon.project_maintenance_commands import (
    fleet_node_migrate as fleet_node_migrate,
)
from reckon.project_maintenance_commands import (
    hooks as hooks,
)
from reckon.project_maintenance_commands import (
    hooks_install as hooks_install,
)
from reckon.project_maintenance_commands import (
    install_skills as install_skills,
)
from reckon.project_maintenance_commands import (
    migrate_layout as migrate_layout,
)
from reckon.project_maintenance_commands import (
    roadmap as roadmap,
)
from reckon.project_maintenance_commands import (
    service as service,
)
from reckon.project_maintenance_commands import (
    service_install as service_install,
)
from reckon.project_maintenance_commands import (
    service_logs as service_logs,
)
from reckon.project_maintenance_commands import (
    service_restart as service_restart,
)
from reckon.project_maintenance_commands import (
    service_start as service_start,
)
from reckon.project_maintenance_commands import (
    service_status as service_status,
)
from reckon.project_maintenance_commands import (
    service_stop as service_stop,
)
from reckon.project_maintenance_commands import (
    service_uninstall as service_uninstall,
)
from reckon.project_maintenance_commands import (
    stamp_superseded_state as stamp_superseded_state,
)
from reckon.project_maintenance_commands import (
    sync as sync,
)
from reckon.project_maintenance_commands import (
    synthesize_evidence as synthesize_evidence,
)
from reckon.project_setup_commands import (
    agent_context as agent_context,
)
from reckon.project_setup_commands import (
    agent_context_doctor as agent_context_doctor,
)
from reckon.project_setup_commands import (
    badge_command as badge_command,
)
from reckon.project_setup_commands import (
    capabilities_command as capabilities_command,
)
from reckon.project_setup_commands import (
    fleet as fleet,
)
from reckon.project_setup_commands import (
    fleet_node_group as fleet_node_group,
)
from reckon.project_setup_commands import (
    fleet_node_hold as fleet_node_hold,
)
from reckon.project_setup_commands import (
    fleet_node_place as fleet_node_place,
)
from reckon.project_setup_commands import (
    fleet_node_status as fleet_node_status,
)
from reckon.project_setup_commands import (
    flight as flight,
)
from reckon.project_setup_commands import (
    mcp as mcp,
)
from reckon.project_setup_commands import (
    paste_command as paste_command,
)
from reckon.project_setup_commands import (
    probe_held_blocker as probe_held_blocker,
)
from reckon.project_setup_commands import (
    serve as serve,
)
from reckon.project_setup_commands import (
    tag as tag,
)
from reckon.project_setup_commands import (
    tag_backfill as tag_backfill,
)
from reckon.project_setup_commands import (
    tag_rename as tag_rename,
)
