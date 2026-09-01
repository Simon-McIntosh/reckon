"""Compatibility facade for worker dispatch and run coordination."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import import_module as _import_module
from pathlib import Path
from types import ModuleType as _ModuleType
from typing import Any, Callable, Iterable, Mapping

from reckon import _backends, _plan_html, capabilities, capability, flight, ledger
from reckon._store import _config_home
from reckon.calibration import agent_configuration_key

# A module may expose submodules when it supplies a package search path. Keeping
# this file as the canonical import target preserves every existing caller while
# the implementation lives in the adjacent concern modules.
__path__ = [str(Path(__file__).with_suffix(""))]

_MODULE_EXPORTS = {'node': ('NODE_PROPERTIES', 'FENCES', '_TERMINAL_RUN_PHASES', 'RUN_DRAIN_DISPOSITIONS', 'DEFAULT_MEMBER_IDLE_WINDOW', 'LOG_STALE_AFTER_SECONDS', 'DEFAULT_WATCH_STALL_WINDOW', 'SUBJECTIVE_TERMS', '_DELIVERABLE_ACTIONS', '_NOUN_OR_ACTION_CONJUNCTIONS', '_DELIVERABLE_SEPARATORS', '_EVIDENCE_SIGNALS', '_UNSPECIFIED', '_DECISION_DEFERRED', '_SUBJECTIVE_PREDICATE', '_DURATION', '_DURATION_SECONDS', 'STALL_BUDGET_MULTIPLE', '_SAFE_ID', '_deliverable_conjunction', 'NEEDS_HELP_MARKER', 'NEEDS_HELP_FIELDS', 'SUMMARY_AXES', 'CHAIN_CLOSED_MARKERS', 'CrewError', 'MemberInFlight', 'ScopeConflict', 'UnreconciledRuns', 'WatcherRequired', 'BudgetHold', 'CompetenceLimit', 'PlanVisibilityError', 'TaskNode', 'normalize_section', 'NodeValidation', 'parse_duration', 'validate_node'), 'routing': ('resolve_role', '_budget_verdict', 'resolved_time_budget', 'resolved_time_ceiling', '_git', '_workspace_roots', '_registered_worktrees', '_inspect_workspace', '_ledgered_run_ids', 'garbage_collect', 'fleet_script', '_create_worktree', '_remove_worktree', '_signal_process_group', '_base_commit', '_contains_plan_section', 'require_plan_section_visible', 'resolve_dispatch_authority', '_require_write_paths_in_repository', '_agent_configuration', '_session_member_id', '_register_session_member', '_parse_utc_timestamp', 'reap_idle_session_members', '_estimated_hours', '_competence_verdict'), 'runs': ('crew_home', 'live_dir', 'runs_dir', 'reports_dir', 'run_dir', 'pointer_path', '_manifest_mtime_ns', '_manifest_freshness', 'watch_lock_path', '_utc_now', 'new_run_id', '_write_json', '_pointer_lock', '_mutate_pointer', 'read_pointer', 'list_live', '_LiveScopeClaim', '_repository_relative_scope', '_scopes_overlap', '_scope_contains', '_normalized_derivations', '_expanded_scope_paths', '_live_scope_claims', 'scope_claims', '_candidate_nodes', '_scope_intersections', 'plan_scope_lanes', '_project_derivations', '_raise_live_scope_conflict', '_merge_peer_scopes', 'record_run_disposition', 'drain', '_read_watch_record', '_write_watch_record', '_project_watch_claim', 'watch_state', '_watch_arming_line', '_stream_quiet_seconds', '_watch_event', 'watch', '_pointer_claims_worktree', '_live_worktree_claims', 'process_alive', '_process_start_time'), 'prompts': ('compose_prompt',), 'dispatch': ('DispatchPlan', '_path_is_tmpfs', 'plan_dispatch', 'dispatch', '_spawn', 'attach', 'observe', '_capture_member_session', '_backend_settings', 'resume_plan', 'terminate', 'record_resumption'), 'promotion': ('scoped_diff_stat', '_elapsed_seconds', '_assume_utc_if_naive', '_wall_exceeded_budget', '_run_streams', 'StreamMeasures', '_terminal_stream_data', 'complete', '_complete_locked', 'discard'), 'recovery': ('RECOVERY_CLASSES', '_budget_timing', '_apply_budget_watchdog', 'classify_pointer', 'overdue_unreconciled_runs', '_utc_seconds', 'recover'), 'reports': ('_MANIFEST_LIST_KEYS', '_NONE_VALUES', 'parse_manifest', '_as_list', 'parse_needs_help', 'audit_manifest', 'followup_ops_from_manifest'), 'summary': ('validate_summary',)}
_CONCERN_MODULES = tuple(
    _import_module(f"{__name__}.{module_name}") for module_name in _MODULE_EXPORTS
)
for _module_name in _MODULE_EXPORTS:
    globals().pop(_module_name, None)
for _module, _names in zip(_CONCERN_MODULES, _MODULE_EXPORTS.values(), strict=True):
    for _name in _names:
        _value = getattr(_module, _name)
        if getattr(_value, "__module__", None) == _module.__name__:
            _value.__module__ = __name__
        globals()[_name] = _value


class _CrewFacade(_ModuleType):
    """Keep facade-level runtime overrides visible to moved function globals."""

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for module in _CONCERN_MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _CrewFacade
