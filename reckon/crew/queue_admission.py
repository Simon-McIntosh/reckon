"""Compatibility facade for admission's responsibility modules.

Every existing import and patch target is retained here. Attribute reads reach
its owning module; patch writes also update dependency bindings imported by
other admission modules, so a patch at this path changes the code being called.
"""

import sys as _sys
from types import ModuleType as _ModuleType

from .admission import (
    holds as _holds,
)
from .admission import (
    landing as _landing,
)
from .admission import (
    launching as _launching,
)
from .admission import (
    ordering as _ordering,
)
from .admission import (
    readiness as _readiness,
)
from .admission import (
    release as _release,
)
from .admission import (
    reviews as _reviews,
)
from .admission import (
    selection as _selection,
)
from .admission import (
    service as _service,
)
from .admission import (
    shared as _shared,
)
from .admission import (
    verdicts as _verdicts,
)
from .admission import (
    wait_refs as _wait_refs,
)

_HOMES = (
    _holds,
    _landing,
    _launching,
    _ordering,
    _readiness,
    _release,
    _reviews,
    _selection,
    _service,
    _shared,
    _verdicts,
    _wait_refs,
)

_EXPORTS = {
    **dict.fromkeys(
        (
            "_candidate_route",
            "_claim_collision",
            "_claim_disposition_of",
            "_competence_shortfall",
            "_declared_scope_paths",
            "_dependency_guard",
            "_earlier_overlapping_request",
            "_failure_text",
            "_file_stamp",
            "_is_lane_hold",
            "_is_plan_visibility_refusal",
            "_is_until_changed_failure",
            "_landed_review_subject",
            "_lane_gate_hold",
            "_live_claim_detail",
            "_live_claim_hold",
            "_local_fallback",
            "_named_runs",
            "_non_binding_holds",
            "_pass_claims",
            "_plan_committed_stamp",
            "_plan_document_path",
            "_queued_requests_overlap",
            "_record_metered_hold",
            "_record_pass_hold",
            "_record_queued_on",
            "_record_retirement",
            "_refreshed_claim",
            "_refusal_hold",
            "_report_queued_failure",
            "_resolved_request_claims",
            "_retire_landed_review",
            "_retry_backoff_hold",
            "_retry_delay",
            "_review_dispose_commands",
            "_review_record_refusal_subject",
            "_route_competence_refusal",
            "_service_may_launch",
            "_session_allowance_read",
            "_stamp_failure",
            "_until_changed_hold",
            "recorded_pass_hold",
        ),
        _holds,
    ),
    **dict.fromkeys(
        ("_LandingWorker",),
        _landing,
    ),
    **dict.fromkeys(
        (
            "_PointerWritebackError",
            "_admission_history_entry",
            "_announce_submission",
            "_archive_queued_life",
            "_attempt_with_a_result",
            "_await_moved_launch",
            "_carry_section_rederivation",
            "_discarded_since",
            "_inside_run_directory",
            "_join_admitted_plan_review",
            "_launch_queued",
            "_launch_reread_refusal",
            "_node_from_pointer",
            "_record_queued_failure",
            "_release_stranded_launches",
            "_requeue_admitted_failures",
            "_require_launched_record",
            "_require_live_writeback",
            "_resolve_lane_config",
            "_restore_queued",
            "_retain_launched_record",
            "_return_claim_refusal_to_queue",
            "_stamp_admission",
            "_steering_brief",
            "_stored_field_value",
            "_write_pointer",
            "_writeback_pointer",
            "carried_options",
            "move_queued_run",
        ),
        _launching,
    ),
    **dict.fromkeys(
        (
            "_fair_use_weights",
            "_fair_use_weights_configured",
            "awaits_a_lane_slot",
            "fair_share_admission_order",
            "priorities_first",
            "queued_placements",
            "weighted_project_order",
        ),
        _ordering,
    ),
    **dict.fromkeys(
        (
            "_dispatch_nonrun_wait_state",
            "_gate_state",
            "_ledger_run_rows",
            "_manifest_status",
            "_needs_own_section_verdict",
            "_persisted_plan_states",
            "_plan_state_and_text",
            "_plan_tree_fingerprint",
            "_project_of_docs_dir",
            "_readiness_blocker_names",
            "_readiness_file_identities",
            "_readiness_rows",
            "_ref_state",
            "_request_state",
            "_section_readiness_state",
            "_section_ref",
            "_section_run_unlanded",
            "_wait_state",
            "dispatch_wait_blockers",
            "dispatch_waits",
            "plan_read_scopes",
            "project_read",
            "readiness_pass",
            "record_section_hold",
            "section_hold",
            "section_hold_verdict",
            "unmet_waits",
            "wait_ref_kind",
        ),
        _readiness,
    ),
    **dict.fromkeys(
        (
            "_advance_waiting_requests",
            "_dead_sections",
            "_discarded",
            "_plan_html",
            "_record_release_failure",
            "_rederive_waits",
            "_release_section_key",
            "_release_tick",
            "_release_waiters",
            "_resolve_and_move",
            "advance_waiting_requests",
            "queue_requests",
            "resolve_waits",
        ),
        _release,
    ),
    **dict.fromkeys(
        (
            "_PLAN_REVIEW_PASS",
            "_PlanReviewRecord",
            "_PointerNotHoldableError",
            "_apply_section_rederivation",
            "_build_plan_review_record",
            "_contract_is_current",
            "_dispatch_gate_owed",
            "_hold_for_plan_review",
            "_hold_pointer",
            "_move_pointer",
            "_plan_review_owed",
            "_plan_review_record",
            "_plan_text_digest",
            "_record_rederivation_refusal",
            "_release_plan_review_holds",
            "_section_rederivation",
            "_stored_review_key",
            "plan_review_pass",
            "record_review_contract",
            "review_contract",
        ),
        _reviews,
    ),
    **dict.fromkeys(
        ("admit_once",),
        _selection,
    ),
    **dict.fromkeys(
        (
            "_admit_code_stamp",
            "_admit_lock",
            "_admit_lock_path",
            "_admitting_pass_identity",
            "_checkout_code_is_trusted",
            "_now",
            "_pause",
            "_record_stage_timings",
            "_reexec_into_current_code",
            "_source_root",
            "_utc_now",
            "admit_until_stopped",
        ),
        _service,
    ),
    **dict.fromkeys(
        (
            "ABANDONED",
            "ADMISSION_HISTORY_KEY",
            "ADMISSION_RETRY_BACKOFF_CAP_SECONDS",
            "ADMISSION_RETRY_BACKOFF_SECONDS",
            "ADMISSION_VERDICT_LOG",
            "ADMISSION_VERDICT_LOG_MAX_BYTES",
            "ADMIT_LOCK_NAME",
            "AWAITING_UPSTREAM_PHASE",
            "Any",
            "BLOCKED_UPSTREAM_PHASE",
            "BudgetHold",
            "Callable",
            "Collection",
            "CompetenceLimit",
            "ContextVar",
            "CrewError",
            "DEAD_RUN_PHASES",
            "DEFAULT_ADMIT_INTERVAL_SECONDS",
            "DISPATCH_REFUSAL_REMEDIES",
            "GATE_REF_PREFIX",
            "HELD_PHASE",
            "HOLD_VERDICTS",
            "Iterable",
            "Iterator",
            "LANDING_STOP_SECONDS",
            "LANDING_THREAD_NAME",
            "LAUNCH_FAILED_PHASE",
            "LaneHeld",
            "LanePaused",
            "MISSING",
            "Mapping",
            "MountView",
            "NOT_ADMISSIBLE_PHASES",
            "PENDING",
            "PLAN_REVIEW_HELD_REASON",
            "Path",
            "PlanReviewMissingError",
            "QUEUED_LIFE_KEYS",
            "QUEUED_PHASE",
            "READINESS_REF_PREFIX",
            "RECORDED_HOLD_FIELD",
            "RELEASE_FAILURE_KEY",
            "RELEASE_TICK_SECONDS",
            "REQUEST_REF_PREFIX",
            "REVIEW_CONTRACT_KEY",
            "REVIEW_ROLE",
            "RUN_DELIVERED_STATUSES",
            "RUN_FAILED_STATUSES",
            "SATISFIED",
            "SECTION_HOLD_KINDS",
            "STALL_AFTER_SECONDS",
            "STALL_VERDICT",
            "ScopeConflict",
            "Sequence",
            "TaskNode",
            "UTC",
            "UnreconciledRuns",
            "WAITING_UPSTREAM_PHASES",
            "WAIT_UNRESOLVABLE_PREFIX",
            "_BOUNDED_READ",
            "_CAPACITY_REFUSAL_REMEDY",
            "_CARRIED_OPTION_KEYS",
            "_LANE_GATE_WAITING_STATES",
            "_MOUNT_UNSET",
            "_NODE_CONTRACT_REFUSAL",
            "_NON_BINDING_HOLD_VERDICTS",
            "_PASS_PLAN_STATES",
            "_READINESS_PASS_ACTIVE",
            "_RELEASE_TICK_LAST",
            "_RETRY_HOLD_VERDICTS",
            "_SECTION_RUNS_CACHE_KEY",
            "_TRUSTED_CODE_STAMPS",
            "_UNLOADED_CACHE_KEY",
            "_dispatch_fleet_gate",
            "_dispatch_lane_allowance",
            "_dispatch_lane_gate",
            "annotations",
            "contextlib",
            "contextmanager",
            "copy",
            "datetime",
            "fcntl",
            "fields",
            "hashlib",
            "json",
            "normalize_section",
            "nullcontext",
            "os",
            "parse_utc",
            "plan_review_units",
            "queue_order",
            "runs",
            "subprocess",
            "sys",
            "threading",
            "time",
            "timedelta",
        ),
        _shared,
    ),
    **dict.fromkeys(
        (
            "_admissible_age_seconds",
            "_lane_has_room",
            "_last_stage_seconds",
            "_last_verdict_of",
            "_pass_verdict_record",
            "_publish_pass_verdicts",
            "_record_gated_verdicts",
            "_record_pass_verdicts",
            "_record_readiness_verdicts",
            "_record_stalls",
            "_stall_stage",
            "_verdict_row",
        ),
        _verdicts,
    ),
    **dict.fromkeys(
        (
            "_docs_for_project",
            "_mount_view",
            "_own_readiness_ref",
            "_run_record_held",
            "_section_status",
            "_validated_batch_waits",
            "derive_wait_refs",
            "section_wait_refs",
            "section_waits_for",
            "unresolvable_wait_obligations",
            "wait_project_for_run",
            "wait_project_lookup",
            "wait_projects_for",
        ),
        _wait_refs,
    ),
}

_BINDINGS = {
    name: tuple(module for module in _HOMES if name in vars(module))
    for name in _EXPORTS
}

__all__ = [name for name in sorted(_EXPORTS) if not name.startswith("_")]


def __getattr__(name):
    owner = _EXPORTS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(owner, name)


def __dir__():
    return sorted(set(globals()) | _EXPORTS.keys())


class _AdmissionFacade(_ModuleType):
    """Forward replacement and deletion through the legacy patch namespace."""

    def __setattr__(self, name, value):
        owner = _EXPORTS.get(name)
        if owner is None:
            super().__setattr__(name, value)
            return
        for module in _BINDINGS[name]:
            setattr(module, name, value)

    def __delattr__(self, name):
        owner = _EXPORTS.get(name)
        if owner is None:
            super().__delattr__(name)
            return
        for module in _BINDINGS[name]:
            if name in vars(module):
                delattr(module, name)


_sys.modules[__name__].__class__ = _AdmissionFacade
