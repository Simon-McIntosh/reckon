"""Shared admission vocabulary, imported dependencies and pass state."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import MISSING, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew import queue_order, runs
from reckon.crew.dispatch_admission import (
    _LANE_GATE_WAITING_STATES,
    LaneHeld,
    LanePaused,
    _dispatch_fleet_gate,
    _dispatch_lane_gate,
)
from reckon.crew.dispatch_plan import _dispatch_lane_allowance
from reckon.crew.node import (
    BudgetHold,
    CompetenceLimit,
    CrewError,
    PlanReviewMissingError,
    ScopeConflict,
    TaskNode,
    UnreconciledRuns,
)
from reckon.crew.recovery_vocabulary import REVIEW_ROLE
from reckon.crew.refusals import DISPATCH_REFUSAL_REMEDIES
from reckon.crew.routing import plan_review_units as plan_review_units  # noqa: PLC0414
from reckon.ledger import normalize_section

__all__ = [
    "ABANDONED",
    "ADMISSION_HISTORY_KEY",
    "ADMISSION_RETRY_BACKOFF_CAP_SECONDS",
    "ADMISSION_RETRY_BACKOFF_SECONDS",
    "ADMISSION_VERDICT_LOG",
    "ADMISSION_VERDICT_LOG_MAX_BYTES",
    "ADMIT_LOCK_NAME",
    "AWAITING_UPSTREAM_PHASE",
    "BLOCKED_UPSTREAM_PHASE",
    "DEAD_RUN_PHASES",
    "DEFAULT_ADMIT_INTERVAL_SECONDS",
    "DISPATCH_REFUSAL_REMEDIES",
    "GATE_REF_PREFIX",
    "HELD_PHASE",
    "HOLD_VERDICTS",
    "LANDING_STOP_SECONDS",
    "LANDING_THREAD_NAME",
    "LAUNCH_FAILED_PHASE",
    "MISSING",
    "NOT_ADMISSIBLE_PHASES",
    "PENDING",
    "PLAN_REVIEW_HELD_REASON",
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
    "UTC",
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
    "Any",
    "BudgetHold",
    "Callable",
    "Collection",
    "CompetenceLimit",
    "ContextVar",
    "CrewError",
    "Iterable",
    "Iterator",
    "LaneHeld",
    "LanePaused",
    "Mapping",
    "MountView",
    "Path",
    "PlanReviewMissingError",
    "ScopeConflict",
    "Sequence",
    "TaskNode",
    "UnreconciledRuns",
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
]

ADMIT_LOCK_NAME = "admit.lock"

DEFAULT_ADMIT_INTERVAL_SECONDS = 5.0

#: The phase a request carries while it waits for a lane slot. The admission
#: pass admits from this phase, and every reader that reports where a request
#: stands reads its place through :func:`queued_placements`.
QUEUED_PHASE = "queued"

HELD_PHASE = "held"

#: The reason an admission pass records on a request it holds because one of
#: the plan units the request reads carries no stored review. A hold written by
#: a coordinator keeps its own reason and its own life: only a hold carrying
#: this reason and the ``automatic`` mark is released by the pass that finds
#: the review stored.
PLAN_REVIEW_HELD_REASON = "plan-review-owed"

#: The pointer key holding the plan digests a request was queued against: its
#: own section's text and the document text it reads as its contract. Admission
#: compares the plan against these rather than against the plan's whole debt, so
#: an edit elsewhere in the plan holds no request that did not read it.
REVIEW_CONTRACT_KEY = "review_contract"

LAUNCH_FAILED_PHASE = "launch-failed"

#: The request-phase vocabulary, one place every reader of a request's phase
#: takes its words from. ``awaiting-upstream`` is a request whose section
#: declares refs that have not landed; ``blocked-upstream`` is the same request
#: once one of those refs is closed as abandoned or failed, which no admission
#: pass will launch: the wait is not a slot hold and no lane will clear it.
AWAITING_UPSTREAM_PHASE = "awaiting-upstream"

BLOCKED_UPSTREAM_PHASE = "blocked-upstream"

#: The two phases a request carries while it waits on refs rather than on a
#: lane slot. Both are read here rather than spelled out at each filter.
WAITING_UPSTREAM_PHASES = (AWAITING_UPSTREAM_PHASE, BLOCKED_UPSTREAM_PHASE)

#: A run whose phase is one of these will not deliver the section it names, so
#: a ref waiting on that section is blocked rather than pending.
DEAD_RUN_PHASES = frozenset({"failed"})

# A queued request whose last attempt failed for a reason other than a lane
# hold waits this long before it is attempted again, doubling per consecutive
# failure up to the cap. The back-off is bounded so a request that can never
# launch is still retried eventually rather than parked forever.
ADMISSION_RETRY_BACKOFF_SECONDS = 30.0

ADMISSION_RETRY_BACKOFF_CAP_SECONDS = 600.0

#: One JSON line per admission pass whose lane had a queued pointer, appended
#: under the crew home beside ``pick-timings.jsonl``. A pass that holds a
#: request for its session's allowance, its lane's gate, its retry back-off or
#: an earlier request otherwise records nothing, so the log is what names why a
#: request waited. The log is trimmed to this many bytes on every append, so a
#: service that runs for days cannot grow it without limit.
ADMISSION_VERDICT_LOG = "admission-verdicts.jsonl"

ADMISSION_VERDICT_LOG_MAX_BYTES = 1_048_576

#: The verdict a pass records for a request that has stayed admissible, on a
#: lane with room, for longer than :data:`STALL_AFTER_SECONDS`. It is recorded
#: once per request and stands until a later verdict for the same request
#: replaces it, so a reader of the last verdict tells a request that was stalled
#: from one that has since launched.
STALL_VERDICT = "stalled"

#: How long a request may stay admissible and unlaunched while its lane has room
#: before the pass records a stall verdict. Ten minutes is the point at which an
#: idle lane is indistinguishable from a halted system unless the queue says
#: which stage held the work.
STALL_AFTER_SECONDS = 600.0

#: The verdicts that leave a request where it is on purpose. A hold says the
#: pass decided against advancing this request and named why, so nothing
#: admissible is being left undone and the wait is not a stall. The stall
#: verdict belongs only to a request the lane could have advanced and did not.
HOLD_VERDICTS = frozenset(
    {
        "held-for-allowance",
        "held-by-gate",
        "held-on-claim",
        "held-until-changed",
        "held-on-retry-backoff",
        "held-on-plan-review",
        "held-behind-earlier-request",
    }
)

#: The verdicts that settle a request for the sessions that follow. A refusal
#: before launch is the pass's decision about that request rather than a wait it
#: repeats: no stall row belongs beside it in the same pass, and a later pass
#: reads it as answered rather than as a request still waiting.
TERMINAL_VERDICTS = frozenset({"refused-before-launch"})

#: The phases in which a request is not admissible at all, because the work it
#: waits on has not been released. A wait here is the queue working as designed.
NOT_ADMISSIBLE_PHASES = frozenset({"awaiting-upstream", "blocked-upstream"})

RECORDED_HOLD_FIELD = "pass_hold"

_RETRY_HOLD_VERDICTS = frozenset({"held-on-retry-backoff", "held-until-changed"})

# The dispatch options a rebind copies from the request it moves. The waivers a
# lane change carries, never adds: the plan-review and directory-claim overrides
# the original dispatch recorded, and the free-text comment beside them. A
# rebind copies exactly these, so a run moved to another lane is not refused by
# a gate the original request had already answered — and a request that carried
# no waiver arrives carrying none.
_CARRIED_OPTION_KEYS = ("allow_unreviewed_plan", "accept_directory_claim", "comment")

#: The holds after which an earlier request cannot launch in this pass, so it
#: fences nothing behind it. Each names a reason for waiting that lies outside
#: the queue itself: a live run's claim, the lane's gate, a failed attempt's
#: back-off, and a plan whose text has moved since the request was queued.
_NON_BINDING_HOLD_VERDICTS = frozenset(
    {
        "held-on-claim",
        "held-by-gate",
        "held-on-retry-backoff",
        "held-on-plan-review",
    }
)

_TRUSTED_CODE_STAMPS: dict[tuple[str, str], bool] = {}

#: The remedy carried by a refusal a full lane gives. That refusal is cured by
#: an occupying run finishing, which moves nothing the dependency guard
#: fingerprints, so holding it until a change would strand it for as long as
#: the lane stayed full; it keeps the bounded back-off instead.
_CAPACITY_REFUSAL_REMEDY = DISPATCH_REFUSAL_REMEDIES["D09"]

_NODE_CONTRACT_REFUSAL = "node is not dispatchable"

# The three answers a wait ref resolves to. ``satisfied`` clears the ref;
# ``abandoned`` blocks every request waiting on it, because nothing will ever
# deliver it; ``pending`` leaves the request where it is.
SATISFIED = "satisfied"

ABANDONED = "abandoned"

PENDING = "pending"

#: The two refs a section may name beside another section: a gate, and another
#: queued request by its run id. The prefixes are what tell the three apart, so
#: a gate id spelled like a section id is still read as a gate.
GATE_REF_PREFIX = "gate:"

REQUEST_REF_PREFIX = "run:"

#: The ref a node's own section's readiness is waited on under. It names no
#: upstream: it is the plan's hold on the section the request would build, and
#: the blockers behind it are what a reader has to settle.
READINESS_REF_PREFIX = "readiness:"

#: The kinds of authored hold a plan puts on its own section. Each one is
#: reported with the id its plan gives it, so a coordinator knows what to
#: settle rather than going looking for an upstream that does not exist.
SECTION_HOLD_KINDS = ("gate", "decision")

#: The statuses a run's own manifest records when it delivered, and when it
#: ended without delivering. They are the run vocabulary and not the plan's: a
#: plan reads ``shipped`` or ``done``, while a manifest reads ``complete``.
#:
#: ``blocked`` is deliberately not among the failed ones. A blocked manifest is
#: a worker that stopped and asked for help, and the run behind it can still be
#: resumed or repaired to deliver, so the request waiting on it keeps waiting
#: rather than being closed as upstream-abandoned. Only ``failed`` — and a
#: discarded request — means nothing will ever deliver.
RUN_DELIVERED_STATUSES = ("complete",)

RUN_FAILED_STATUSES = ("failed", "abandoned")

#: The obligation a wait carries when no project holds the run it names.
#:
#: A request waiting on a run whose landing can never be read is not waiting
#: upstream: no pass will ever release it. It carries its own kind rather than
#: the plain waiting reading, so a reader is told what to fix instead of being
#: shown a wait that never ends.
WAIT_UNRESOLVABLE_PREFIX = "wait-unresolvable:"

#: A caller that has not read the mount registry yet, distinct from a read
#: that failed and from a read that named no project.
_MOUNT_UNSET = object()

#: The mounted trees keyed by project name, and why they could not be read.
#: ``None`` roots with a reason is the registry being unreadable, which every
#: wait of a pass reports; ``{}`` with no reason is a registry that names no
#: project, which is a verdict about the run.
MountView = tuple[Mapping[str, Path] | None, str]

#: Whether the caller is inside an admission pass, which builds one roadmap per
#: project and shares it across the pass's requests.
_READINESS_PASS_ACTIVE: ContextVar[bool] = ContextVar(
    "readiness_pass_active", default=False
)

#: Each docs tree's persisted plan parses, read once for the length of a pass.
_PASS_PLAN_STATES: ContextVar[dict[tuple[str, str], Mapping[str, Any]] | None] = (
    ContextVar("pass_plan_states", default=None)
)

#: Whether a request's readiness may be read from its own plan alone. Queuing
#: holds a batch against one project-wide view so its requests are judged alike.
_BOUNDED_READ: ContextVar[bool] = ContextVar("bounded_readiness_read", default=True)

#: The readiness cache key under which one pass keeps the project's ledger rows.
#: A pass resolves every waiting request against the same history, so reading it
#: once per project rather than once per wait is what keeps a release tick from
#: paying a ledger read per request.
_SECTION_RUNS_CACHE_KEY = "section-runs"

#: The readiness cache key under which one pass keeps the drain's answer for a
#: section. The derivation reads the project's ledger on its own, so without a
#: per-pass answer every request waiting on the section would pay for it again.
_UNLOADED_CACHE_KEY = "section-unloaded"

#: The field a release that raised leaves on the request it could not move, so
#: the failure reaches the request's own record rather than only the caller that
#: triggered the release.
RELEASE_FAILURE_KEY = "release_failure"

#: How often the admission service runs the release step on its own tick. A
#: release reads readiness and moves waiting requests and launches nothing, so
#: it is paced by its own clock rather than by the pass interval: a pass holding
#: launches or landings must not delay a section's waiters.
RELEASE_TICK_SECONDS = 30.0

#: When the release step last ran, on the monotonic clock. Process-local: it
#: paces this service's ticks, and a restarted service releases at once.
_RELEASE_TICK_LAST = {"at": 0.0}

#: The fields a request carries while it waits, and which describe its queued
#: life rather than the run a launch produced. A launched pointer that kept
#: them at the top level reads to every consumer of a live run as a launch that
#: failed, so admission moves them under :data:`ADMISSION_HISTORY_KEY`.
QUEUED_LIFE_KEYS = (
    "failure",
    "failure_at",
    "failure_attempts",
    "failure_reported_at",
    "failure_plan_commit",
    "failure_guard",
    "retry_at",
)

#: Where a launched pointer keeps the queued-life facts of the request it was
#: admitted from. One entry per admitted attempt, oldest first, so a request
#: that was refused, re-queued and launched again carries every refusal it
#: survived rather than only the last one.
ADMISSION_HISTORY_KEY = "admission_history"

#: The thread name the landing action runs under, so a reader of the process
#: can tell the landing worker from the admission pass it runs beside.
LANDING_THREAD_NAME = "reckon-landing"

#: How long a stopping service waits for an in-flight landing action to finish
#: before leaving its daemon thread to the process exit.
LANDING_STOP_SECONDS = 1.0
