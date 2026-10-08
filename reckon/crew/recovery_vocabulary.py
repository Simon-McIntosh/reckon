from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from reckon import ledger, review_tiers
from reckon._timestamps import parse_utc
from reckon.capabilities import _charged_input_from_usage
from reckon.crew import lane_document as _lane_document
from reckon.crew import metering, plan_review, quota_weight, runs
from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import review_need
from reckon.crew.host_lease import LEASE_RENEW_SECONDS
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    DEFAULT_WATCH_STALL_WINDOW,
    INTERRUPTED_RUN_PHASE,
    LOG_STALE_AFTER_SECONDS,
    CrewError,
    parse_duration,
)
from reckon.crew.reports import (
    NON_TERMINAL_MANIFEST_STATUSES,
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    manifest_status_is_template,
    parse_manifest,
)
from reckon.crew.routing import _signal_process_group
from reckon.crew.runs import (
    _manifest_freshness,
    _mutate_pointer,
    _process_start_time,
    _project_watch_claim,
    _read_watch_record,
    _stream_quiet_seconds,
    _utc_now,
    _write_watch_record,
    list_live,
    producer_lease_seconds,
    read_pointer,
    update_watch_registration,
    watch_lease_renewed_at,
    watch_lock_path,
)
from reckon.crew.ticker import NEEDS_ACTION, Ticker, _agent_label



# ── Recovery: what an interrupted orchestrator left behind ───────────────────

# What a live pointer can be once nobody is watching it. Worker-reported
# blocked and failed outcomes remain distinct so neither can be mistaken for a
# completed delivery that is eligible for promotion. An unreadable manifest is
# its own outcome: a file exists but no reader can judge it, which is neither a
# delivered record (completed_unpromoted) nor an absence (abandoned). A worker
# whose recorded exit ended the turn with work committed while its manifest
# still reads a working status has delivered something no status word claims, so
# it is its own outcome too: the record needs the verdict word the worker never
# wrote, and the committed work is safe in the tree. Paused is
# the wait that lifts itself: the run is waiting on time or on its own job, and
# nobody has to act, because whoever or whatever lifts the run is not a person.
# The discriminator is exactly that — who lifts it. A stop that needs a person
# or another session stays blocked; a stop whose own job, a window reset or a
# bounded wait ends it is paused. Blocked is alarming because it demands a
# reader; paused must therefore always name what will lift it, so it never
# becomes the bucket a forgotten run sits in.
RECOVERY_CLASSES = (
    "running",
    "waiting",
    "paused",
    "stopped",
    "completed_unpromoted",
    "blocked",
    "failed",
    "unreadable",
    "exited-unfinished",
    "abandoned",
    "lane-event",
)

WAITING_STATUS = "waiting"
# The manifest status vocabulary — TERMINAL_MANIFEST_STATUSES,
# NON_TERMINAL_MANIFEST_STATUSES and manifest_status_is_template — is imported
# from reckon.crew.reports, which owns the single statement of it so the reader
# refusing an unrecognised word names the same set the classifier decides
# against.
WAIT_CONDITION_STATES = frozenset({"pending", "met", "unknown"})
WAIT_PROBE_TIMEOUT_SECONDS = 1.0

# A dispatch writes no manifest of its own, so between launch and the worker's
# first write a live run has no verdict to read. Reporting that gap as unwritten
# the instant it opens makes a working run flicker, so a manifest that carries
# no status is not called unwritten inside this window after the run's dispatch;
# the run keeps whatever its liveness already said. Past the window the word is
# the truthful one: a live run still without a written verdict is unwritten.
LAUNCH_WINDOW_SECONDS = 120
# Workers write manifests by hand rather than atomically, so a reader can catch
# one mid-rewrite: an unparseable, just-modified or shrunk file is not the
# worker's verdict but its absence in transit, and is treated as unchanged. The
# window is short because it only has to cover a single rewrite, not a stall.
MANIFEST_REWRITE_WINDOW_SECONDS = 10

# This is the authoritative answer to "what should the coordinator do now?".
# The older classification remains a lifecycle grouping used by recovery and
# promotion, while this vocabulary names the cause whose remedy differs. A
# lane hold and a worker failure therefore cannot share an instruction even
# though both remain attention-worthy terminal-looking rows.
RECOVERY_VERBS = {
    "running": "observe",
    "queued": "wait",
    "waiting": "wait",
    "paused": "wait",
    "completed_unpromoted": "promote",
    "held": "resume",
    "needs-help": "answer",
    "failed": "redispatch",
    "stalled": "investigate",
    "blocked": "decide",
    "stopped": "inspect",
    "scoring": "review",
    "promotable": "promote",
    "unreadable": "repair",
    "exited-unfinished": "repair",
    "unwritten": "resume",
    "ready": "resume",
    "abandoned": "recover",
    "lane-event": "inspect",
    "refused-at-admission": "resume",
    "launch-failed": "resume",
    "ended-without-manifest": "resume",
    "wait-aged": "investigate",
    INTERRUPTED_RUN_PHASE: "redispatch",
}
RECOVERY_CLASSIFICATIONS = tuple(RECOVERY_VERBS)
ACTIONABLE_RECOVERY_CLASSIFICATIONS = frozenset(
    {
        "held",
        "needs-help",
        "failed",
        "stalled",
        "blocked",
        "stopped",
        "scoring",
        "unreadable",
        "exited-unfinished",
        "unwritten",
        "ready",
        "abandoned",
        "lane-event",
        "refused-at-admission",
        "wait-aged",
        INTERRUPTED_RUN_PHASE,
        # A launch that never reached a model wants the coordinator to repair a
        # command or a PATH, which is work only a person can do; leaving it out
        # of the actionable count is how such a run reads as invisible while it
        # occupies a lane.
        "launch-failed",
    }
)

# Classifications whose remedy decides whether a run's resume path survives.
# Each one reads the resolved session — pointer, then stream, then the promoted
# ledger row — rather than the pointer's own session_id field, which lags it:
# a stopped or abandoned pointer whose stream still names a session resumes
# with every turn intact, so an advice to discard or redispatch it would throw
# away a session that is still there. The blocked and interrupted arms have
# read the resolution since the escape hatch was built; the two disposal arms
# are what this set adds.
RESUMPTION_READING_CLASSIFICATIONS = frozenset(
    {"blocked", INTERRUPTED_RUN_PHASE, "stopped", "abandoned"}
)


REVIEW_NODE_PREFIX = "review-of-"
PLAN_REVIEW_NODE_PREFIX = "plan-review-of-"

# The dispatch role that produces reviews. A run carrying it is the reviewer,
# never the reviewed, so no classification may compose a review of it: the
# composed dispatch names its own source run, so reviewing a review spawns
# another review of the same shape without limit. The promotion boundary keys
# its own exemption on the same fact — a review run is never gated on a review
# of itself.
REVIEW_ROLE = "review"
