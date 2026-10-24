"""The durable half of a run: a committed ledger owned by the repository.

Run state has two natures that must not share a home. While a worker is in
flight its record changes every few seconds and is worthless once the run ends,
so it lives in a pointer under reckon's config home that is never committed.
Once the run completes, the record is durable evidence of how a plan was
implemented — and plans are repo-local and committed, so their implementation
record is too.

The state directory ``<config-home>/state/<project>`` is a symlink into
``<repo>/docs/state/<project>``. Each completed run owns a file under ``runs/``;
exclusive creation keeps concurrent promotions independent and refuses a
duplicate without rewriting history. Roster and hold edits use version-paired
writes to their aggregate ledger.

Two rules shape everything here.

**Nothing transient is committed, and nothing durable is only cached.** The
ledger holds the roster, completed runs and budget holds; it never holds a pid,
a worktree path or a phase. Holds sit beside runs because they have no worker,
worktree, commit or gate, and therefore must not enter worker-effort
measurements. :func:`append_run` refuses a second record for a run id
because a double promotion double-counts the very measurements
the calibration readers consume.

**A measurement is only falsifiable if it is captured at the moment it is
knowable.** Wall-clock, the agent configuration that ran the node, the scoped
diff and whether the scope was widened mid-flight cannot be reconstructed after
the worktree is gone, so they are fields of the record rather than something a
later reader derives. A scope-changed run measures neither the estimate nor the
worker, so :func:`effort_report` excludes it and says how many it excluded
rather than averaging it in silently.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

# Reached through the module rather than by importing its names: a reload
# rebinds the store's classes in place, and a captured exception class would
# then no longer match the one its own function raises.
from reckon import _store, file_memo
from reckon._plan_html import SECTION_NUMBER_PATTERN
from reckon._timestamps import parse_utc

# The slug the ledger occupies in a project's state directory. It sits beside
# index.json rather than inside it because project config and implementation
# history have different write cadences and different readers.
LEDGER_SLUG = "crew"

# A gate verdict is one of three so the field stays machine-readable for the
# calibration loops that read it. "not-run" is a real answer: a gate whose
# evidence could not be produced is a recorded negative, not a silent pass.
GATE_VERDICTS = ("passed", "failed", "not-run")

# Substitutions attributable only to the backend swap a shadow exists to
# measure. Any other key in a lineage's "substituted" map means something
# besides the candidate backend changed, which confounds the comparison.
_SHADOW_BACKEND_ONLY_KEYS = frozenset({"backend", "launch", "model"})

FAILURE_CLASSIFICATIONS = (
    "work-rejected",
    "correct-refusal",
    "malformed-node",
    "infrastructure-failure",
    "pre-existing-failure",
    "negative-result",
)

USABLE_COMPLETION_SOURCES = frozenset({"terminal_event", "stream_mtime", "provided"})

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_MAX_RETRY_DELAY_SECONDS = 0.05

# Backends launched against hardware this operation owns outright rather than
# a metered API. The harness still emits a total_cost_usd figure computed for
# whatever model name it was told to speak, but no per-token price was ever
# charged for it, so recording that figure as spend inverts any comparison
# that ranks lanes by cost — the free lane reads as the dearest one. See
# flight.yaml's local_backend documentation for the concrete host mapping.
UNMETERED_BACKENDS = frozenset({"clive", "clive-glm"})

# The suffix that marks a catalogue-declared budget group as billed a flat
# subscription rather than metered per token. The flight schema fixes no
# separate billing key, so the group name carries it: the catalogue declares
# ``claude-sub`` and ``codex-sub`` on the backends that draw on them, and a
# group whose name carries this suffix is subscription-billed, so its lanes'
# harness cost is not spend.
SUBSCRIPTION_GROUP_SUFFIX = "-sub"

# Declared schema for completed ledger rows; tests ensure every key a promoted row
# writes is declared here before merging new measurements.
RECORD_FIELDS = (
    "run_id",
    "plan",
    "section",
    "node",
    "node_definition",
    "role",
    "spec_level",
    "execution_fit",
    "attempt_kind",
    "attempt",
    "member",
    "backend",
    # The lane and model key the backend serves, resolved through the catalogue's
    # one name resolver and written beside ``backend`` so a row stays attributable
    # after a backend name retires. Null when the name resolves to no pair.
    "lane",
    "model_key",
    "local",
    "agent",
    "dispatched_at",
    "completed_at",
    "completed_at_source",
    "wall_seconds",
    "duration_measurement",
    "worker_seconds",
    "worker_seconds_source",
    "stalled",
    "time_budget",
    "base_sha",
    "commits",
    "no_commit",
    "commit_resolution",
    "changed_lines",
    "tests_added",
    "gate",
    "gate_check",
    "suite_delta",
    "failure_classification",
    "outcome",
    # The verb a coordinator recorded for why this run's pointer outlived its
    # session, carried onto the committed row because promotion deletes the
    # pointer, which is the only other copy. Null on a row whose run carried no
    # disposition, so a reader can tell a run that was disposed of deliberately
    # from one whose field was never written.
    "disposition",
    "worktree_retention",
    "manifest_path",
    "scope_changed",
    "scope_acceptances",
    # The crew session that dispatched the run, distinct from ``session_id``
    # (the worker's own harness session).
    "session",
    "session_id",
    "session_harness",
    "session_model",
    "budget",
    "lane_receipt",
    "lineage",
    "shadow_controlled",
    "shadow_patch",
    "boundary_waiver",
    "domain_waiver",
    "resume_waiver",
    "watch_override",
    "review",
    "clone_matches",
    "review_tier",
    # What promoted the run: "review-acceptance" when a clean stored review
    # accepted it without a coordinator, "review-self" when a review run
    # promoted itself once its stored record parsed, null on every row a
    # coordinator promoted. Always present so a reader can tell an
    # autonomously promoted row is distinguishable from one whose value was
    # never written.
    "promoted_by",
    "picker_selection",
    "route_mode",
    "unreconciled_override",
    "orchestrator_lane_override",
    # A brief run's authority in place of a plan section: the digest and stored
    # path of the brief it read, the plan an unplanned implement landing changed,
    # and the reason it changed none. All three ride every row, null when the run
    # needed neither, so a reader can tell an unrecorded value from one never
    # asked for.
    "brief",
    "plan_link",
    "unplanned_reason",
)


class LedgerError(Exception):
    """A ledger read or write cannot proceed, and the message says why."""


class SuiteDeltaError(LedgerError):
    """An armed promotion lacks evidence or adds suite failures."""

    def __init__(
        self,
        detail: str,
        *,
        missing_fields: Iterable[str] = (),
        added_failure_ids: Iterable[str] = (),
    ) -> None:
        self.missing_fields = [str(field) for field in missing_fields]
        self.added_failure_ids = sorted(
            {str(test_id) for test_id in added_failure_ids if str(test_id).strip()}
        )
        super().__init__(detail)


@lru_cache(maxsize=8)
def _identity_catalogue(path: str, stamp: tuple | None) -> Mapping[str, Any]:
    """Cache only while the catalogue's file identity remains unchanged."""
    from reckon import flight

    return flight.read_layer_file(path)


def resolve_name(
    name: str, *, lane: str | None = None
) -> tuple[str | None, str | None]:
    """Resolve a catalogue lane, model key, model id or alias to one pair.

    Undeclared names stay unknown. A bare model shared by lanes is refused;
    callers reading a record that already names a lane can constrain the match.
    Historical aliases remain valid even when their model is no longer offered.
    """
    from reckon import flight

    name = str(name or "").strip()
    if not name:
        return None, None
    path = flight.model_catalogue_path()
    catalogue = _identity_catalogue(str(path), _file_identity(path))
    lanes = catalogue.get("lanes") or {}
    aliases = catalogue.get("aliases") or {}
    matches: set[tuple[str, str]] = set()
    if name in aliases:
        entry = aliases[name]
        matches.add((entry["lane"], entry["model_key"]))
    elif name in lanes:
        matches.add((name, lanes[name]["default_model"]))
    else:
        for lane_name, declaration in lanes.items():
            for key, settings in (declaration.get("models") or {}).items():
                if name in (key, settings.get("model"), settings.get("alias")):
                    matches.add((lane_name, key))
        for entry in aliases.values():
            if name == entry["model_key"]:
                matches.add((entry["lane"], entry["model_key"]))
    if lane:
        matches = {pair for pair in matches if pair[0] == lane}
    if len(matches) > 1:
        raise ValueError(
            f"ambiguous routing name {name!r}; declared by lanes "
            + ", ".join(sorted(pair[0] for pair in matches))
        )
    return next(iter(matches), (None, None))


def lanes_declaring_model(model: str) -> list[str]:
    """Every catalogue lane that declares ``model`` as a key, id or alias.

    A bare name a single lane declares resolves directly; a name two lanes
    declare makes :func:`resolve_name` raise, so each lane is asked in turn.
    Used to name the declaring lane(s) when a model is refused for being
    outside the lane it was asked for.
    """
    from reckon import flight

    catalogue = flight.read_layer_file(str(flight.model_catalogue_path()))
    declaring: list[str] = []
    for lane_name in catalogue.get("lanes") or {}:
        try:
            match_lane, _key = resolve_name(model, lane=str(lane_name))
        except ValueError:
            continue
        if match_lane is not None:
            declaring.append(str(lane_name))
    return declaring


def _undeclared_model_refusal(model: str, lane: str | None) -> str:
    """The one message both ``--model`` and a picker route refuse a name with.

    A lane was named and the model is declared elsewhere: the refusal names the
    model, the lane it was asked for and the lane that declares it. No lane was
    named: the message names only the model. Either way the same pair produces
    the same text whichever route reached the resolver.
    """
    if lane is not None:
        declaring = [name for name in lanes_declaring_model(model) if name != lane]
        if declaring:
            label = "lane" if len(declaring) == 1 else "lanes"
            names = ", ".join(repr(name) for name in sorted(declaring))
            return (
                f"model {model!r} is declared by {label} {names}, "
                f"not by the {lane!r} lane it was asked for"
            )
        return (
            f"model {model!r} is not declared by the {lane!r} lane it was "
            "asked for, and names no model, model id or alias the catalogue "
            "declares"
        )
    return f"{model!r} names no model, model id or alias the catalogue declares"


def backend_serving_model(
    config: Mapping[str, Any], *, lane: str | None, model: str
) -> tuple[str, str, str]:
    """Resolve a lane and a model name to the configured backend that serves it.

    The pair comes from :func:`resolve_name` over the model catalogue, so a
    model key, a model id and an alias all reach the same ``(lane, model_key)``
    and the configured backend whose entry carries that pair is returned with
    it. A lane's own backend serves its other declared models when no separate
    entry carries the pair. A name no lane declares, a lane that does not
    declare the name and a pair no configured backend serves are refused, so
    ``--model`` and a picker route refuse an undeclared pair identically rather
    than each spelling its own. Returns ``(backend_name, lane, model_key)``.
    """
    resolved_lane, key = resolve_name(model, lane=lane)
    if resolved_lane is None:
        raise ValueError(_undeclared_model_refusal(model, lane))
    for name, entry in (config.get("backends") or {}).items():
        if not isinstance(entry, Mapping):
            continue
        if (
            str(entry.get("lane")) == resolved_lane
            and str(entry.get("model_key")) == key
        ):
            return str(name), resolved_lane, key
    declared_lane = (config.get("lanes") or {}).get(resolved_lane) or {}
    lane_backend = (config.get("backends") or {}).get(resolved_lane)
    if (
        isinstance(declared_lane, Mapping)
        and key in (declared_lane.get("models") or {})
        and isinstance(lane_backend, Mapping)
        and lane_backend.get("lane") == resolved_lane
    ):
        return resolved_lane, resolved_lane, key
    raise ValueError(
        f"model {model!r} resolves to lane {resolved_lane!r} model key {key!r}, "
        "which no configured backend serves"
    )


def normalize_identity(
    record: Mapping[str, Any], *, lane: str | None = None
) -> dict[str, Any]:
    """Add read-time identity fields without changing stored name vocabulary.

    A recorded pair is authoritative. An explicitly recorded model narrows the
    backend's lane; an unknown model never inherits today's lane default.
    Agent and reviewer blocks carry the same identity alongside their raw fields.
    """
    result = dict(record)
    source = next(
        (
            record[key]
            for key in ("agent", "reviewer")
            if isinstance(record.get(key), Mapping)
        ),
        {},
    )
    name = str(
        record.get("backend") or source.get("backend") or record.get("lane") or ""
    )
    lane = record.get("lane") or lane or source.get("lane")
    key = record.get("model_key")
    resolved_lane, resolved_key = resolve_name(name, lane=lane)
    lane = lane or resolved_lane
    model = key or record.get("model") or source.get("model_key") or source.get("model")
    if not model and not name:
        model = record.get("alias") or source.get("alias")
    if not (lane and key):
        if model:
            model_lane, key = resolve_name(str(model), lane=lane)
            # A model-specific name preserves its historical key even when
            # the recorded model id has retired from the catalogue.
            if key is None and name != resolved_lane:
                key = resolved_key
            lane = lane or model_lane
        else:
            key = resolved_key
    if lane:
        result.update(lane=lane, model_key=key)
    for nested in ("agent", "reviewer", "review"):
        if isinstance(record.get(nested), Mapping):
            result[nested] = normalize_identity(
                record[nested], lane=lane if nested == "agent" else None
            )
    return result


def normalize_section(value: Any) -> str:
    """Return the canonical spelling for a numbered plan section."""
    section = re.sub(r"\s+", " ", str(value or "").strip())
    match = re.fullmatch(
        rf"(?:§\s*|#?s(?:ection)?\s*)?({SECTION_NUMBER_PATTERN})",
        section,
        re.IGNORECASE,
    )
    return f"§{match.group(1)}" if match else section


def shadow_controlled(lineage: Any, *, contaminated: Any = None) -> bool:
    """Return whether a shadow's recorded lineage reproduced its primary's configuration.

    The sole definition of the control predicate: read ``lineage.configuration``
    (written once, at shadow dispatch, by comparing the resolved primary and
    shadow agent settings) and check that every key it marks "substituted" is
    the candidate backend, its launch mode, or its model — the differences a
    backend swap necessarily produces. A substituted effort, sandbox, or time
    budget means the pair also varied on a dimension the shadow ladder is not
    measuring, so the comparison is confounded. Lineage carrying no recorded
    configuration cannot be verified either way and is not controlled.

    A row whose stream read the primary's landed answer or its records is not
    controlled whatever the configuration: a matched configuration compares
    nothing when the shadow copied the answer instead of deriving it.
    ``contaminated`` carries that verdict (the run row's ``shadow_contaminated``,
    written by promotion's scan) and makes the pair uncontrolled whenever it
    holds a reason.
    """
    if contaminated:
        return False
    if not isinstance(lineage, Mapping) or lineage.get("kind") != "shadow":
        return False
    configuration = lineage.get("configuration")
    if not isinstance(configuration, Mapping):
        return False
    substituted = configuration.get("substituted")
    if not isinstance(substituted, Mapping) or not substituted:
        return False
    return all(str(key) in _SHADOW_BACKEND_ONLY_KEYS for key in substituted)


def gate_check_missing_fields(gate_check: Any) -> list[str]:
    """Name which required fields a gate-check mapping is missing, if any."""
    check = gate_check if isinstance(gate_check, Mapping) else {}
    missing: list[str] = []
    if not str(check.get("command") or "").strip():
        missing.append("command")
    exit_status = check.get("exit_status")
    if (
        exit_status is None
        or isinstance(exit_status, bool)
        or not isinstance(exit_status, int)
    ):
        missing.append("exit_status")
    if (
        not str(check.get("log_path") or "").strip()
        and not str(check.get("log_digest") or "").strip()
    ):
        missing.append("log path or digest")
    return missing


def _normalized_gate_check(gate_check: Any) -> dict[str, Any] | None:
    """Return the stored shape of a gate check, or None when nothing was given."""
    if not isinstance(gate_check, Mapping):
        return None
    command = str(gate_check.get("command") or "").strip()
    exit_status = gate_check.get("exit_status")
    log_path = str(gate_check.get("log_path") or "").strip()
    log_digest = str(gate_check.get("log_digest") or "").strip()
    if not command and exit_status is None and not log_path and not log_digest:
        return None
    return {
        "command": command,
        "exit_status": exit_status,
        "log_path": log_path or None,
        "log_digest": log_digest or None,
    }


def suite_observation_missing_fields(
    observation: Any,
    *,
    name: str,
) -> list[str]:
    """Name absent evidence in one parsed suite observation."""
    if not isinstance(observation, Mapping):
        return [name]
    missing: list[str] = []
    missing.extend(
        f"{name}.{field}"
        for field in ("revision", "command")
        if not str(observation.get(field) or "").strip()
    )
    exit_status = observation.get("exit_status")
    if isinstance(exit_status, bool) or not isinstance(exit_status, int):
        missing.append(f"{name}.exit_status")
    if observation.get("completed") is not True:
        missing.append(f"{name}.completed")
    failure_count = observation.get("failure_count")
    failure_ids = observation.get("failure_ids")
    if (
        isinstance(failure_count, bool)
        or not isinstance(failure_count, int)
        or failure_count < 0
    ):
        missing.append(f"{name}.failure_count")
    if not isinstance(failure_ids, list) or any(
        not isinstance(test_id, str) or not test_id.strip() for test_id in failure_ids
    ):
        missing.append(f"{name}.failure_ids")
    elif isinstance(failure_count, int) and failure_count != len(failure_ids):
        missing.append(f"{name}.failure_count_matches_failure_ids")
    if (
        not str(observation.get("log_path") or "").strip()
        and not str(observation.get("log_digest") or "").strip()
    ):
        missing.append(f"{name}.log_path_or_log_digest")
    return missing


def normalized_suite_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable ledger shape of a validated suite observation."""
    return {
        "revision": str(observation.get("revision") or "").strip(),
        "command": str(observation.get("command") or "").strip(),
        "exit_status": int(observation["exit_status"]),
        "log_path": str(observation.get("log_path") or "").strip() or None,
        "log_digest": str(observation.get("log_digest") or "").strip() or None,
        "completed": True,
        "failure_count": int(observation["failure_count"]),
        "failure_ids": sorted(
            {str(item).strip() for item in observation["failure_ids"]}
        ),
    }


def added_failure_ids(
    baseline: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> list[str]:
    """Return failure ids present in ``after`` but absent from ``baseline``.

    This is the exact set-difference arithmetic the suite-delta promotion gate
    already computes to separate new failures from pre-existing ones. A per-
    failure attribution reuses it here rather than defining a second, looser
    notion of "new".
    """
    baseline_ids = (
        set(baseline.get("failure_ids") or [])
        if isinstance(baseline, Mapping)
        else set()
    )
    after_ids = (
        set(after.get("failure_ids") or []) if isinstance(after, Mapping) else set()
    )
    return sorted(after_ids - baseline_ids)


def failure_attribution_missing_fields(
    attribution: Any,
    after: Mapping[str, Any] | None,
    *,
    name: str = "failure_attribution",
) -> list[str]:
    """Name attribution entries that cannot be trusted as evidence.

    An attributed failure id absent from the after observation cannot be a
    real failure the worker saw, and an entry with no candidate commit is
    prose rather than an attribution.
    """
    if attribution is None:
        return []
    if not isinstance(attribution, Mapping):
        return [name]
    after_ids = (
        set(after.get("failure_ids") or []) if isinstance(after, Mapping) else set()
    )
    missing: list[str] = []
    for failure_id, commit in attribution.items():
        label = f"{name}[{failure_id}]"
        if str(failure_id) not in after_ids:
            missing.append(f"{label} not in after_suite.failure_ids")
        elif not str(commit or "").strip():
            missing.append(f"{label} has no candidate commit")
    return missing


def new_failure_attribution(
    baseline: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    attribution: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return each newly added failure's candidate commit.

    "New" is exactly :func:`added_failure_ids` — an entry naming a
    pre-existing failure (one also present in ``baseline``) is dropped rather
    than treated as an attribution, so a worker cannot attribute a failure it
    did not introduce.
    """
    added = set(added_failure_ids(baseline, after))
    mapping = attribution if isinstance(attribution, Mapping) else {}
    return {
        str(failure_id): str(commit).strip()
        for failure_id, commit in mapping.items()
        if str(failure_id) in added and str(commit or "").strip()
    }


# ── Routing evidence (recorded at promotion) ────────────────────────────────
#
# A routing measure that only read the worker manifest decays as run
# directories are cleaned, and a later touch of paths the earlier manifest
# already named is a different event from a touch of paths it never did — yet
# both read alike on a ledger row carrying only a manifest path and free-text
# outcome. The three facts below are therefore captured onto each promoted row
# at the moment the evidence exists, so a later measure does not need the file
# that produced them.


def _path_like_tokens(text: str) -> list[str]:
    """Return the repository-path-shaped words inside one prose entry.

    An outside-scope entry is free text a worker wrote, so a path it names may be bare
    (``reckon/crew/promotion.py``) or embedded in prose (``"workers.py is
    outside the fence"``). A token counts as a path when it carries a slash or
    ends in a file suffix — and is excluded when it is an option (``--flag``)
    or a bare word. The host is a POSIX checkout, so the slash rule is enough
    for repository-relative paths, which is the frame the later row's changed
    paths are recorded in.
    """
    tokens: list[str] = []
    for token in re.split(r"[\s,]+", text):
        candidate = token.strip().strip("\"'`()[]{}*<>")
        if not candidate or candidate.startswith("-") or "://" in candidate:
            continue
        if "/" in candidate or re.fullmatch(r"[\w./-]+\.[A-Za-z0-9]{1,8}", candidate):
            tokens.append(candidate)
    return tokens


def outside_scope_paths_from_entries(entries: Any) -> list[str]:
    """Extract the repository paths a manifest's ``outside_scope`` names.

    A following run touching the paths an earlier manifest already named is the
    continuation of that node's stated followup — a different routing event
    from touching paths it never named. The extraction treats the source as
    the prose it is: a structured entry may be a mapping with its own ``path``
    or ``paths`` keys, and a textual entry is split into path-shaped tokens.
    Paths keep first-appearance order and duplicates are dropped, so the list
    stays comparable against a later row's ``changed_lines`` paths.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for entry in entries or ():
        candidates: list[str]
        if isinstance(entry, Mapping):
            candidates = [str(entry["path"])] if entry.get("path") else []
            nested_paths = entry.get("paths")
            if isinstance(nested_paths, str):
                candidates.extend(_path_like_tokens(nested_paths))
            elif isinstance(nested_paths, list):
                for nested in nested_paths:
                    if isinstance(nested, Mapping):
                        if str(nested.get("path") or "").strip():
                            candidates.append(str(nested["path"]).strip())
                    elif str(nested or "").strip():
                        candidates.extend(_path_like_tokens(str(nested)))
        else:
            candidates = _path_like_tokens(str(entry))
        for candidate in candidates:
            cleaned = str(candidate).strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            paths.append(cleaned)
    return paths


def _run_tip(record: Mapping[str, Any]) -> str:
    """Return the last cited commit of a promoted run, or empty."""
    commits = record.get("commits")
    if isinstance(commits, list):
        for commit in reversed(commits):
            if str(commit or "").strip():
                return str(commit).strip()
    return ""


def predecessor_run_id(
    *,
    supplied: Any,
    base_sha: str,
    runs: Iterable[Mapping[str, Any]] | None,
    exclude_run_id: str = "",
) -> str | None:
    """Return the run id this run continues, or None when none is evident.

    A coordinator-supplied value is authoritative when present — it names a
    predecessor the dispatcher already knows. Otherwise the link is derived
    from the repository: a run whose base commit is the promoted tip of exactly
    one earlier run is that run's continuation. A base matching several runs,
    or no run, names no predecessor, and the caller omits the field rather
    than writing null — a run without a predecessor must not read like one
    whose linkage was measured and found empty.
    """
    supplied_run = str(supplied or "").strip()
    if supplied_run:
        return supplied_run
    base = str(base_sha or "").strip()
    if not base:
        return None
    matches = {
        str(item.get("run_id") or "")
        for item in runs or ()
        if str(item.get("run_id") or "")
        and str(item.get("run_id") or "") != exclude_run_id
        and _run_tip(item) == base
    }
    if len(matches) == 1:
        return matches.pop()
    return None


# A correction signal is phrasing a worker uses to state that a factual premise
# of its brief is wrong. The list is the vocabulary, and it is deliberately
# small so a normal report almost never trips it; the count is of statements,
# not of marker hits, so a correction spanning words stays one correction.
_CORRECTION_SIGNAL = re.compile(
    r"disput(?:e|es|ed|ing) +the +(?:premise|brief|spec)"
    r"|contrary to the (?:brief|spec|brief's premise)"
    r"|contradicts the (?:brief|spec)"
    r"|the (?:stated )?premise .{0,60}?(?:is|was) +(?:incorrect|wrong|false|"
    r"untrue|not the case|no longer (?:true|the case))",
    re.IGNORECASE,
)


def stated_correction_count(report_text: Any) -> int | str:
    """Count corrections the worker's report states against the brief's premises.

    The worker's own report is the only evidence that a premise was corrected,
    so the count comes from the report text — never from a later reader's
    opinion of the work. A report that can be read is scanned and the number of
    distinct statements carrying a correction signal is returned, zero when the
    worker made none. A report that cannot be read returns the string
    ``"unknown"``, because a measured zero and an unmeasured report are
    different values and merging them is what makes a dispute invisible.
    """
    text = str(report_text or "")
    if not text.strip():
        return "unknown"
    from reckon.crew.reports import parse_manifest

    try:
        manifest = parse_manifest(text)
    except (KeyError, ValueError):
        return "unknown"
    structured = manifest.get("premise_corrections")
    if structured is not None:
        if isinstance(structured, list):
            items = structured
        else:
            items = [
                part.strip()
                for part in re.split(r"[\n,]+", str(structured))
                if part.strip()
            ]
        return sum(1 for item in items if str(item or "").strip())
    return sum(1 for line in text.splitlines() if _CORRECTION_SIGNAL.search(line))


def evidence_records_for_plan(
    project: str,
    plan: str,
    root: str | Path | None = None,
) -> list[Path]:
    """Return typed evidence resources whose back-link names ``plan``.

    Both live evidence and frozen evidence are authoritative at closure. A
    project-qualified reference only matches when its qualifier names the
    project being written.
    """
    from reckon import _plan_html
    from reckon._schema import parse_plan_ref

    docs_dir = _store._docs_dir_for_project(project, root)
    if docs_dir is None:
        return []
    evidence_dir = docs_dir / "evidence"
    candidates = [
        *evidence_dir.glob("*.html"),
        *evidence_dir.glob("archive/*.html"),
    ]
    matches: list[Path] = []
    for path in sorted(candidates):
        record = _plan_html.parse_plan(path)
        if record.get("type") != "evidence":
            continue
        for raw_ref in record.get("evidence_for") or []:
            ref = parse_plan_ref(raw_ref)
            if ref is not None and not ref.is_external(project) and ref.slug == plan:
                matches.append(path)
                break
    return matches


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _retry_backoff(attempt: int) -> None:
    """Yield to competing ledger writers with a small jittered delay."""
    ceiling = min(_MAX_RETRY_DELAY_SECONDS, 0.002 * (2**attempt))
    time.sleep(random.uniform(ceiling / 2, ceiling))  # noqa: S311 - scheduling jitter


# ── Location and raw access ─────────────────────────────────────────────────


def ledger_path(project: str, root: str | Path | None = None) -> Path:
    """Return the ledger file for a project.

    Without ``root`` this resolves through the config-home state directory,
    which is symlinked into the registered checkout — the same route
    ``index.json`` takes. With ``root`` (a checkout's repo root) it resolves
    under that checkout, which is what lets a worker inside a worktree read and
    write its own ledger instead of the registered main one.
    """
    if not _SAFE_ID.fullmatch(str(project)):
        raise LedgerError(f"project {project!r} must match {_SAFE_ID.pattern}")
    return _store.state_path(project, LEDGER_SLUG, root)


def _run_ledger_root(project: str, root: str | Path | None) -> Path | None:
    """Resolve the checkout that owns a promoted run's project.

    A run may implement one project's plan in a different repository. Its
    pointer therefore names the code checkout, which is not necessarily where
    the project's durable state belongs. A checkout proves local ownership by
    carrying that project's index; otherwise the mount registry is the
    authority. Missing registry data fails closed once a registry exists,
    rather than creating another project's state beneath the code checkout.
    """
    if root is None:
        return None
    requested = Path(root).expanduser().resolve()
    local_index = _store.state_path(project, "index", requested)
    if local_index.is_file():
        return requested

    mounts_path = _store._mounts_path()
    if not mounts_path.is_file():
        return requested

    from reckon import flight

    try:
        mounted_docs = flight.mounted_project_docs().get(project)
    except flight.FlightConfigError as exc:
        raise LedgerError(
            f"cannot resolve the ledger checkout for project {project!r}: "
            f"the promoting checkout is {requested}, and mount registry "
            f"{mounts_path} is unreadable: {exc}"
        ) from exc
    if mounted_docs is not None and mounted_docs.is_dir():
        return mounted_docs.parent.resolve()

    requested_ledger = ledger_path(project, requested)
    raise LedgerError(
        f"cannot resolve the ledger checkout for project {project!r}: "
        f"the promoting checkout would write {requested_ledger}, but mount "
        f"registry {mounts_path} names no readable checkout for that project"
    )


def serialize_run(record: Mapping[str, Any]) -> str:
    """Encode a complete run identically for comparison, export and promotion."""
    return json.dumps(dict(record), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def run_path(project: str, run_id: str, root: str | Path | None = None) -> Path:
    """Resolve a run beside the project's aggregate ledger."""
    if not _SAFE_ID.fullmatch(str(run_id)):
        raise LedgerError(f"run id {run_id!r} must match {_SAFE_ID.pattern}")
    return ledger_path(project, root).parent / "runs" / f"{run_id}.json"


def write_run_file(
    project: str,
    row: Mapping[str, Any],
    root: str | Path | None = None,
    *,
    fsync: bool = True,
    mode: int | None = 0o600,
) -> Path:
    """Persist one run where it is held: its own file under ``runs/``.

    The record goes through :func:`serialize_run`, the one encoder for a run, so
    the file cannot drift from a copy of the same row the aggregate holds, which
    :func:`load` refuses to read as the same history. Every writer that amends a
    row that owns a file calls this rather than encoding the row itself.

    ``mode`` is the permission bits the file must carry. A caller amending a
    file it did not create passes ``None``, leaving the bits the file already
    has, which is what a rewrite that only re-encodes an existing row wants.
    """
    target = run_path(project, str(row["run_id"]), root)
    _store.write_atomically(
        target,
        lambda handle: handle.write(serialize_run(row)),
        mode=mode,
        fsync=fsync,
    )
    return target


def _run_files(project: str, root: str | Path | None = None) -> list[Path]:
    return sorted((ledger_path(project, root).parent / "runs").glob("*.json"))


def _folded_run_ids(
    incoming_runs: Sequence[Any],
    aggregate_runs: Sequence[Any],
    project: str,
    root: str | Path | None,
) -> list[str]:
    """Run ids the incoming rows carry that only their own file holds today.

    A row is the same record whether it is read from the aggregate or from the
    file under ``runs/``, so an incoming write that carries a row the aggregate
    does not hold while that row's file exists is about to copy a per-run record
    into the aggregate. The ids are returned sorted so a refusal names them
    stably, and a row the aggregate already holds is never counted: a write
    that keeps a row and edits it is the ordinary shape, not a fold, and
    :func:`_keep_run_files_identical` re-encodes the file to match it.
    """
    incoming = {
        str(row["run_id"])
        for row in incoming_runs
        if isinstance(row, Mapping) and row.get("run_id")
    }
    held = {
        str(row["run_id"])
        for row in aggregate_runs
        if isinstance(row, Mapping) and row.get("run_id")
    }
    on_disk = {path.stem for path in _run_files(project, root)}
    return sorted((incoming - held) & on_disk)


def _load_aggregate(
    project: str, root: str | Path | None
) -> tuple[dict[str, Any], int]:
    """Read the aggregate collections and their current version.

    An absent ledger is the ordinary state of a project that has run no workers
    yet, so it reads as an empty roster at version 0 rather than an error. A
    present ledger must carry list-valued collections: treating a malformed
    collection as empty would turn damaged history into a fresh start.
    """
    path = ledger_path(project, root)
    data, version = _store._load_json_envelope(path)
    if not path.exists():
        return {"members": [], "runs": [], "holds": []}, version
    members = data.get("members")
    runs = data.get("runs", [])
    holds = data.get("holds")
    malformed = [
        name
        for name, value in (("members", members), ("runs", runs), ("holds", holds))
        if not isinstance(value, list)
    ]
    if malformed:
        raise LedgerError(
            f"ledger for {project!r} at {path} is malformed: "
            f"{', '.join(malformed)} must be list-valued; refusing to read "
            "damaged history as an empty project"
        )
    return (
        {
            "members": list(members),
            "runs": [
                normalize_outside_scope_paths(row) if isinstance(row, Mapping) else row
                for row in runs
            ],
            "holds": list(holds),
        },
        version,
    )


def load_aggregate(
    project: str, root: str | Path | None = None
) -> tuple[dict[str, Any], int]:
    """Read the aggregate's own collections, never the union :func:`load` merges.

    A caller that intends to write the ledger reads through here: the union
    :func:`load` returns is the reader's view, and its run rows include every
    run that already owns a file under ``runs/``. Handing those rows back to
    :func:`write` would copy each of them into the tracked aggregate beside its
    own file, which :func:`write` refuses.

    A caller that has to decide from the whole history — an idle member's last
    dispatch, a row to patch — still reads :func:`load`, and passes the rows
    this returns, patched in place where the row is one of them, to the writer.
    """
    return _load_aggregate(project, root)


def run_ids(project: str, root: str | Path | None = None) -> set[str]:
    """Read index membership without decoding the individual run payloads."""
    data, _version = _load_aggregate(project, root)
    return {
        str(record["run_id"])
        for record in data["runs"]
        if isinstance(record, Mapping) and record.get("run_id")
    } | {path.stem for path in _run_files(project, root)}


# The index stores file-derived values only. Every access checks file metadata,
# including ctime, so edits with a preserved mtime still invalidate the row.
_RUN_INDEX_VERSION = 4

# The metadata key an index uses to carry the resolved ledger path it indexes.
# It is written when the index is built, so a sweep can identify an index whose
# checkout has since been removed without reading the ledger it once served.
LEDGER_INDEX_PATH_KEY = "ledger_path"


def _file_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino


def _run_snapshot(project: str, root: str | Path | None) -> list[tuple[Path, Any]]:
    paths = _run_files(project, root)
    # Metadata lives remotely on a shared filesystem. Bound concurrency while
    # checking every file: directory mtime cannot detect an in-place edit.
    if len(paths) > 32:
        with ThreadPoolExecutor(max_workers=8) as pool:
            stamps = list(pool.map(_file_identity, paths))
    else:
        stamps = [_file_identity(path) for path in paths]
    return list(zip(paths, stamps, strict=True))


def input_stamp(project: str, root: str | Path | None = None) -> str | None:
    """Content-change key for aggregate, additions, removals and in-place edits."""
    aggregate = ledger_path(project, root).resolve()
    snapshot = _run_snapshot(project, root)
    aggregate_identity = _file_identity(aggregate)
    if aggregate_identity is None:
        # An absent aggregate is a valid empty roster; unreadable metadata is
        # not evidence of absence and must forfeit caching.
        try:
            aggregate.stat()
        except FileNotFoundError:
            pass
        except OSError:
            return None
        else:
            return None
    if any(stamp is None for _, stamp in snapshot):
        return None
    value = [
        str(aggregate),
        aggregate_identity,
        [(path.name, stamp) for path, stamp in snapshot],
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def _default_cache_root() -> Path:
    """The user cache root: the pick cache resolved with no configured home.

    ``client`` is the one kind :func:`_store.cache_root` resolves without
    consulting the configuration home, so the directory above it is the reckon
    user cache root — ``$XDG_CACHE_HOME/reckon`` or ``~/.cache/reckon``.
    Asking ``_store`` for it keeps one owner for that rule instead of a second
    copy of it here.
    """
    return _store.cache_root("client").parent


def _index_directory() -> Path:
    """The directory a run index is published into.

    The configured pick cache sits inside the configuration home whenever that
    home is named, and the fleet mounts the configuration home read-only around
    worker launches. An index has to be creatable to be read: a process that
    cannot open one there pays a full scan of every run file on each read,
    because no index exists for the last-good fallback to serve. So an index
    whose configured directory exists and is not writable is published to the
    user cache directory instead. Both spellings hash the ledger path into the
    filename, so a reader outside the fence finds the index built inside it.
    """
    configured = _store.cache_root("pick-input")
    if configured.is_dir() and not os.access(configured, os.W_OK):
        return _default_cache_root()
    return configured


def _run_index_path(project: str, root: str | Path | None) -> Path:
    source = str(ledger_path(project, root).resolve())
    identity = hashlib.sha256(source.encode()).hexdigest()
    return _index_directory() / f"ledger-{identity}.sqlite"


def recorded_ledger_path(index: str | Path) -> str | None:
    """The ledger path an index records, or ``None`` when it records none.

    Read without creating an index: a cache file that is absent, unreadable,
    corrupt or written by an older schema has no recorded path, which is a
    distinct state from one whose recorded checkout is gone.
    """
    try:
        connection = sqlite3.connect(
            f"file:{Path(index)}?mode=ro", uri=True, timeout=0.2
        )
        with contextlib.closing(connection):
            row = connection.execute(
                "SELECT payload FROM metadata WHERE name=?", (LEDGER_INDEX_PATH_KEY,)
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return str(row[0]) if row and row[0] else None


def _index_stamp_members(
    project: str, root: str | Path | None = None
) -> dict[str, Any]:
    """Every member of the ledger index stamp, keyed by member name.

    One owner for the member set. :func:`index_stamp` renders these members as
    the ordered list its positional callers read, and :func:`index_state_stamp`
    returns the same members keyed by name. A member added here reaches both, so
    a member added to the stamp can no longer be invisible to a caller that
    compares by name. The insertion order of this mapping is the order
    :func:`index_stamp` returns.
    """
    aggregate = ledger_path(project, root)
    return {
        "aggregate": str(aggregate.resolve()),
        "version": _RUN_INDEX_VERSION,
        "aggregate_identity": _file_identity(aggregate),
        "runs_identity": _file_identity(aggregate.parent / "runs"),
        "index_identity": _file_identity(_run_index_path(project, root)),
    }


# The index file's own identity: a read refreshes that file, so its identity
# moves across every refresh without the ledger behind it changing. A caller
# keyed on the ledger's state omits this member by name.
_INDEX_IDENTITY_MEMBER = "index_identity"


def index_stamp(project: str, root: str | Path | None = None) -> list[Any]:
    """Constant-cost key for the last indexed content and atomic ledger writes.

    The index changes when a reader observes an in-place edit. Aggregate writes
    and atomic per-run replacements also move the source or directory stamp,
    so a profile cache notices them before another reader refreshes the index.
    """
    return list(_index_stamp_members(project, root).values())


def index_state_stamp(project: str, root: str | Path | None = None) -> dict[str, Any]:
    """The ledger index members a read is a reading of, keyed by member name.

    The same member set :func:`index_stamp` renders, derived from the one owner
    of that set, without the index file's own identity: a read refreshes that
    file, so its identity moves across every refresh while the ledger behind it
    is unchanged, and a caller keyed on the state of the ledger must not move
    with it.

    Members are keyed by name so a caller compares the state it means by name.
    The index identity is omitted by name, so a member added to the set ahead of
    it cannot displace that omission, and a member added to the set reaches this
    record without a second list to keep in step.
    """
    members = _index_stamp_members(project, root)
    members.pop(_INDEX_IDENTITY_MEMBER, None)
    return members


# Persist exactly the history fields consumed by picker outcomes, return times,
# attempts and budget readers. Full records remain available to other callers.
_PICKER_FIELDS = (
    "run_id",
    "node",
    "plan",
    "section",
    "backend",
    "agent",
    "member",
    "role",
    "spec_level",
    "gate",
    "failure_classification",
    "completed_at",
    "completed_at_source",
    "dispatched_at",
    "observed_at",
    "terminal_at",
    "started_at",
    "created_at",
    "time_budget",
    "wall_seconds",
    "budget",
    "throughput",
    "lane_receipt",
)


def picker_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project a full record onto the inputs the picker consumes."""
    return {key: record[key] for key in _PICKER_FIELDS if key in record}


def picker_runs(project: str, root: str | Path | None = None) -> list[dict[str, Any]]:
    """Read projected history without decoding retained full payloads."""
    data, _version = load(project, root, True)
    return data["runs"]


# One sweep reads a per-run document through several readers — the ledger's own
# :func:`_read_run` and the held-worktree scan in
# :mod:`reckon.crew.obligations` both decode every run file — so the decode is
# held in :mod:`reckon.file_memo`, the repository's one memo for a parse keyed
# on a file's stat identity. It is keyed on the file rather than on anything
# the sweep knows, so it survives between two sweeps of the same process, and a
# document that cannot be read is parsed uncached and so reads as absent exactly
# as it always did.
_RUN_DOCUMENT_KIND = "ledger-run"


def read_document(source: Path) -> Any:
    """Decode one JSON document, reusing the parse while its file is unchanged."""
    return file_memo.memoized(
        _RUN_DOCUMENT_KIND,
        source,
        lambda: json.loads(source.read_text(encoding="utf-8")),
    )


def _read_run(source: Path) -> dict[str, Any]:
    try:
        record = read_document(source)
    except (OSError, ValueError) as exc:
        raise LedgerError(
            f"cannot read run {source.stem!r} at {source}: {exc}"
        ) from exc
    if not isinstance(record, dict) or record.get("run_id") != source.stem:
        raise LedgerError(f"run {source.stem!r} at {source} must hold its own run_id")
    # The row's outside-scope key is mapped on the way in, so a document written
    # before the key was renamed reaches every reader under the current one and
    # the retired spelling is never carried into a later write.
    return normalize_outside_scope_paths(record)


def _index_payload(
    connection: sqlite3.Connection,
    meta: Mapping[str, str],
    *,
    headers_only: bool,
    picker_only: bool,
) -> tuple[dict[str, Any], int]:
    """Decode one index's stored payloads into the union :func:`load` returns.

    Shared by the refreshing read and the last-good read, so a mtime-moved
    index is decoded by exactly the same rules as a freshly refreshed one.
    """
    data = json.loads(meta["header"])
    if not isinstance(data, dict) or not all(
        isinstance(data.get(name), list) for name in ("members", "holds")
    ):
        raise ValueError("invalid index header")
    version = int(meta["version"])
    if headers_only:
        data["runs"] = [
            {"run_id": row[0]}
            for row in connection.execute(
                "SELECT run_id FROM aggregate_rows WHERE run_id IS NOT NULL "
                "UNION ALL SELECT name FROM records WHERE name NOT IN "
                "(SELECT run_id FROM aggregate_rows WHERE run_id IS NOT NULL)"
            )
        ]
        return data, version
    column = "picker" if picker_only else "payload"
    data["runs"] = [
        normalize_outside_scope_paths(json.loads(row[0]))
        for row in connection.execute(
            f"SELECT {column} FROM aggregate_rows ORDER BY ordinal"  # noqa: S608 - fixed column names
        )
    ]
    extra = [
        normalize_outside_scope_paths(json.loads(row[0]))
        for row in connection.execute(
            f"SELECT {column} FROM records WHERE name NOT IN "  # noqa: S608 - fixed column names
            "(SELECT run_id FROM aggregate_rows WHERE run_id IS NOT NULL) ORDER BY name"
        )
    ]
    if extra:
        data["runs"].extend(extra)
        data["runs"].sort(key=promotion_order_key)
    return data, version


def _last_good_index_data(
    project: str,
    root: str | Path | None,
    *,
    headers_only: bool,
    picker_only: bool,
) -> tuple[dict[str, Any], int, float] | None:
    """Read the last published index without refreshing it, and its age.

    A refresh the writer refused — a concurrent edit, a locked index, an
    unwritable cache directory — must not make every reader pay a full scan of
    the aggregate. The index the last successful refresh published is a
    complete, self-consistent snapshot of the history as of that refresh, so it
    is read here instead, together with the number of seconds since it was
    written. Absent or unreadable or malformed, it reads as ``None`` and the
    caller keeps its existing fallback.

    A snapshot is last-good only while the aggregate it was built from is still
    the one on disk. Once the aggregate has been replaced, the stamp it recorded
    names a revision the ledger has moved past, and serving the snapshot would
    hand the caller history the aggregate contradicts — the empty view a
    promotion's own read publishes before it writes, held by a later reader
    whose refresh was refused under a peer's lock. The aggregate's identity is a
    stat rather than a parse, so a moved aggregate is detected here and the
    caller falls through to the aggregate itself; an unchanged one still avoids
    the scan.
    """
    path = _run_index_path(project, root)
    if not path.is_file():
        return None
    try:
        with contextlib.closing(
            sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
        ) as connection:
            meta = dict(connection.execute("SELECT name, payload FROM metadata"))
            if meta.get("stamp") != json.dumps(
                _file_identity(ledger_path(project, root))
            ):
                return None
            refreshed_at = float(meta["refreshed_at"])
            data, version = _index_payload(
                connection,
                meta,
                headers_only=headers_only,
                picker_only=picker_only,
            )
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return None
    return data, version, max(0.0, time.time() - refreshed_at)


# A derivation reads the ledger from several of its steps, and each read rebuilt
# the run index and decoded it again — the profile showed the index built twice
# inside one pass, for milliseconds-apart reads over files that had not moved.
# The first read in a region builds it and the rest are served the same result.
# Outside a region each read keeps the behaviour it has now.
_INDEX_REGION: ContextVar[
    dict[tuple[str, str, bool, bool], tuple[dict[str, Any], int]] | None
] = ContextVar("ledger_index_region", default=None)


@contextlib.contextmanager
def index_reuse() -> Iterable[
    dict[tuple[str, str, bool, bool], tuple[dict[str, Any], int]]
]:
    """Build the run index once for every reader inside this region.

    A nested region joins the cache already open rather than starting a fresh
    one, so a read made while slicing a session's payload is served the index
    the derivation built.
    """
    active = _INDEX_REGION.get()
    if active is not None:
        yield active
        return
    cache: dict[tuple[str, str, bool, bool], tuple[dict[str, Any], int]] = {}
    token = _INDEX_REGION.set(cache)
    try:
        yield cache
    finally:
        _INDEX_REGION.reset(token)


def _indexed_data(
    project: str,
    root: str | Path | None,
    *,
    headers_only: bool = False,
    picker_only: bool = False,
) -> tuple[dict[str, Any], int]:
    reuse = _INDEX_REGION.get()
    index_key = (
        str(project),
        str(root) if root is not None else "",
        headers_only,
        picker_only,
    )
    if reuse is not None and index_key in reuse:
        return reuse[index_key]
    aggregate = ledger_path(project, root)
    aggregate_stamp = json.dumps(_file_identity(aggregate))
    snapshot = _run_snapshot(project, root)
    path = _run_index_path(project, root)
    index_source = str(ledger_path(project, root).resolve())
    path.parent.mkdir(parents=True, exist_ok=True)

    def update(connection: sqlite3.Connection) -> tuple[dict[str, Any], int]:
        # The transaction publishes stamps and payloads together. Aggregate and
        # split records remain independent sources, including their conflict check.
        with connection:
            # The context manager commits but does not begin a transaction.
            # Include schema writes so a cold read pays for one durable commit.
            connection.execute("BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != _RUN_INDEX_VERSION:
                for table in ("records", "aggregate_rows", "metadata"):
                    connection.execute(f"DROP TABLE IF EXISTS {table}")
                connection.execute(f"PRAGMA user_version={_RUN_INDEX_VERSION}")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS records "
                "(name TEXT PRIMARY KEY, stamp TEXT, payload TEXT, picker TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS aggregate_rows "
                "(ordinal INTEGER PRIMARY KEY, run_id TEXT, payload TEXT, picker TEXT)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS aggregate_identity ON aggregate_rows(run_id)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata "
                "(name TEXT PRIMARY KEY, payload TEXT)"
            )
            meta = dict(connection.execute("SELECT name, payload FROM metadata"))
            # The index records the ledger path it indexes, so a sweep can tell
            # an index whose checkout is gone from one merely stale: the path is
            # an identity, while a filename hash is opaque. Written here so any
            # index a reader touches carries it, including one built before this
            # row existed.
            if meta.get(LEDGER_INDEX_PATH_KEY) != index_source:
                connection.execute(
                    "INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                    (LEDGER_INDEX_PATH_KEY, index_source),
                )
            aggregate_changed = meta.get("stamp") != aggregate_stamp
            if aggregate_changed:
                data, version = _load_aggregate(project, root)
                connection.execute("DELETE FROM aggregate_rows")
                connection.executemany(
                    "INSERT INTO aggregate_rows VALUES (?, ?, ?, ?)",
                    [
                        (
                            i,
                            str(row["run_id"])
                            if isinstance(row, Mapping) and row.get("run_id")
                            else None,
                            json.dumps(row, sort_keys=True, separators=(",", ":")),
                            json.dumps(
                                picker_record(row) if isinstance(row, Mapping) else row,
                                separators=(",", ":"),
                            ),
                        )
                        for i, row in enumerate(data["runs"])
                    ],
                )
                meta = {
                    "stamp": aggregate_stamp,
                    "header": json.dumps(
                        {"members": data["members"], "holds": data["holds"]}
                    ),
                    "version": str(version),
                }
                connection.executemany(
                    "INSERT OR REPLACE INTO metadata VALUES (?, ?)", meta.items()
                )
            stored = dict(connection.execute("SELECT name, stamp FROM records"))
            changed = []
            for source, stamp in snapshot:
                if stamp is None:
                    raise OSError(f"cannot stat run: {source}")
                encoded = json.dumps(stamp)
                prior = stored.pop(source.stem, None)
                if prior != encoded:
                    changed.append(source.stem)
                    record = _read_run(source)
                    if _file_identity(source) != stamp:
                        # Refuse to publish a cache built across a concurrent edit.
                        # The caller's uncached path still supplies the current read.
                        raise OSError(f"run changed during index read: {source}")
                    connection.execute(
                        "INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)",
                        (
                            source.stem,
                            encoded,
                            json.dumps(record, sort_keys=True, separators=(",", ":")),
                            json.dumps(picker_record(record), separators=(",", ":")),
                        ),
                    )
            connection.executemany(
                "DELETE FROM records WHERE name=?", [(name,) for name in stored]
            )
            conflict = None
            conflict_sql = (
                "SELECT r.name FROM records r JOIN aggregate_rows a ON a.run_id=r.name "
                "WHERE a.ordinal=(SELECT MAX(b.ordinal) FROM aggregate_rows b WHERE b.run_id=r.name) "
                "AND a.payload != r.payload"
            )
            if aggregate_changed:
                conflict = connection.execute(conflict_sql + " LIMIT 1").fetchone()
            else:
                for name in changed:
                    conflict = connection.execute(
                        conflict_sql + " AND r.name=? LIMIT 1", (name,)
                    ).fetchone()
                    if conflict:
                        break
            if conflict:
                source = aggregate.parent / "runs" / f"{conflict[0]}.json"
                raise LedgerError(
                    f"run {conflict[0]!r} differs between {aggregate} and {source}; "
                    "refusing to read conflicting history"
                )
            # The refreshed index carries the time it was published, so a later
            # reader forced back onto this index can report how old its rows are.
            if aggregate_changed or changed:
                connection.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('refreshed_at', ?)",
                    (str(time.time()),),
                )
            return _index_payload(
                connection,
                meta,
                headers_only=headers_only,
                picker_only=picker_only,
            )

    with contextlib.closing(sqlite3.connect(path, timeout=0.2)) as connection:
        try:
            result = update(connection)
        except sqlite3.OperationalError:
            raise
        except (sqlite3.DatabaseError, ValueError, KeyError, TypeError):
            # A corrupt index is a miss. Publish a complete rebuilt database by
            # atomic replacement; authoritative ledger files are never changed.
            descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".sqlite")
            os.close(descriptor)
            try:
                with contextlib.closing(sqlite3.connect(temporary)) as replacement:
                    result = update(replacement)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
    if reuse is not None:
        reuse[index_key] = result
    return result


def indexed_headers(
    project: str, root: str | Path | None = None
) -> tuple[dict[str, Any], int]:
    """Read roster and history identities without decoding complete run payloads."""
    return load(project, root, headers_only=True)


def load(
    project: str,
    root: str | Path | None = None,
    picker_only: bool = False,
    *,
    use_index: bool = True,
    headers_only: bool = False,
) -> tuple[dict[str, Any], int]:
    """Read the union of aggregate and per-run records, refusing disagreements.

    An interrupted export can leave two copies of a run. They count once only
    when their canonical serialisations agree; disagreement refuses the entire
    read so a caller cannot accidentally count an incomplete history.

    A refresh the index refuses — a concurrent edit, a locked or unwritable
    index — falls back to the last good index rather than the aggregate: this
    snapshot is complete as of its own refresh, and returning it with the
    seconds since that refresh keeps a "rows no newer than" figure on the read
    instead of charging every reader a full scan of a multi-megabyte aggregate
    whenever a peer happens to be writing. Only when no good index can be read
    at all does the read open the aggregate.

    ``picker_only`` reads each row as the projection :func:`picker_record`
    retains beside its payload in the index, so a caller that consumes only
    those fields decodes no full record on a warm read. What comes back from
    the aggregate on a cold read is projected the same way.
    """
    if use_index:
        try:
            return _indexed_data(
                project, root, headers_only=headers_only, picker_only=picker_only
            )
        except (OSError, sqlite3.Error):
            # A disposable cache cannot make an otherwise readable ledger fail.
            stale = _last_good_index_data(
                project,
                root,
                headers_only=headers_only,
                picker_only=picker_only,
            )
            if stale is not None:
                data, version, age = stale
                data["index_age_seconds"] = round(age, 3)
                return data, version
    data, version = _load_aggregate(project, root)
    _merge_run_documents(project, root, data, refuse=True)
    if picker_only:
        data["runs"] = [
            picker_record(row) if isinstance(row, Mapping) else row
            for row in data["runs"]
        ]
    return data, version


def promotion_order_key(record: Mapping[str, Any]) -> tuple[str, str]:
    """The position one row takes in the union :func:`load` returns.

    Completion order survives splitting records across either source, and run
    ids break ties so rows without a completion stamp still have one order.
    """
    return (
        str(record.get("completed_at") or record.get("run_id") or ""),
        str(record.get("run_id") or ""),
    )


def _merge_run_documents(
    project: str,
    root: str | Path | None,
    data: dict[str, Any],
    *,
    refuse: bool,
) -> bool:
    """Fold the per-run documents into ``data['runs']``, in promotion order.

    A run that owns a file is held twice, once as an aggregate row and once as
    its own document. The union counts it once, so two copies must serialise
    alike; ``refuse`` is the only difference between the modes. A refusing
    caller — the reader every ordinary read goes through — loses the whole read
    to a document that does not stand for its run or that disagrees with the
    aggregate, so no caller counts an incomplete history. A non-refusing
    caller loses only the document it cannot use and keeps the rest.

    Returns whether the union gained a row, so a caller whose documents changed
    nothing leaves the stored order alone.
    """
    path = ledger_path(project, root)
    by_id = {
        str(record["run_id"]): record
        for record in data["runs"]
        if isinstance(record, Mapping) and record.get("run_id")
    }
    added = False
    for source in _run_files(project, root):
        try:
            record = _read_run(source)
        except LedgerError:
            if refuse:
                raise
            continue
        run_id = record["run_id"]
        if run_id in by_id:
            if refuse and serialize_run(by_id[run_id]) != serialize_run(record):
                raise LedgerError(
                    f"run {run_id!r} differs between {path} and {source}; "
                    "refusing to read conflicting history"
                )
            # A non-refusing caller keeps the aggregate's own row: a document
            # that disagrees with it is the copy a later write failed to keep
            # identical, and the aggregate holds the revision it read.
            continue
        data["runs"].append(record)
        by_id[run_id] = record
        added = True
    if added:
        data["runs"].sort(key=promotion_order_key)
    return added


def run_rows_from_documents(
    project: str, root: str | Path | None = None
) -> list[dict[str, Any]]:
    """The rows the aggregate and the per-run documents hold, merged unrefused.

    This is :func:`load`'s union, ordering and run-id-against-file check with
    its refusals replaced by skips, for a caller that must answer from whatever
    is readable rather than fail outright — a screen that would otherwise drop
    a duty it cannot name. A per-run document that is absent, unreadable, or
    not the run it is named for contributes no row, and a document that
    disagrees with the aggregate's row leaves the aggregate's row standing. An
    aggregate that cannot be read at all contributes its rows to no one, and
    the documents are still read: they are what a run's own writer put there,
    and they are present whatever happened to the aggregate that copies them.
    """
    try:
        data, _version = _load_aggregate(project, root)
    except Exception:  # noqa: BLE001 — a damaged aggregate is not a lost read
        # Every way the aggregate can fail to read lands here, not just the
        # malformed-collection refusal: an envelope a conflict marker or a
        # truncated write has damaged raises the store's own error, which is no
        # more a reason to lose the documents than a malformed list is.
        data = {"members": [], "runs": [], "holds": []}
    _merge_run_documents(project, root, data, refuse=False)
    return list(data["runs"])


def history_version(data: Mapping[str, Any], aggregate_version: int) -> str:
    """A freshness key over the union :func:`load` returns.

    The aggregate envelope version advances only when the aggregate file is
    rewritten, while a promoted run lands in its own file beside it, so a
    reader keying freshness on that version alone cannot tell a newly appended
    run from an unchanged history. Folding the run ids the union already holds
    into the key moves it when a run is appended, without reading the files
    again; the aggregate version still contributes so a roster-only rewrite
    that leaves the run set alone is also visible.
    """
    run_ids = sorted(
        str(record.get("run_id"))
        for record in data.get("runs") or []
        if isinstance(record, Mapping) and record.get("run_id")
    )
    digest = hashlib.sha256("\n".join(run_ids).encode("utf-8")).hexdigest()
    return f"{aggregate_version}.{digest}"


def _roster_ids(entries: Any) -> set[str]:
    """The member ids a roster carries, ignoring rows that name none."""
    return {
        str(entry.get("id"))
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("id")
    }


def dropped_member_ids(stored: Any, incoming: Any) -> list[str]:
    """Member ids the stored roster holds that an incoming write leaves out.

    The comparison is by id rather than by count, because losing a member is
    the guarded fact: a write that drops one member while adding another keeps
    the count identical and still empties the roster of the id it dropped.
    """
    return sorted(_roster_ids(stored) - _roster_ids(incoming))


def _run_store_location() -> str:
    """The run store's path, named in a refusal as the recovery for a loss.

    Resolved lazily and imported inside the call so an ordinary ledger write
    carries no dependency on the store module; only a refusal that must name
    the recovery pays for it.
    """
    from reckon import run_store

    return str(run_store.store_path())


def _restore_run_files(snapshots: Sequence[tuple[Path, str]]) -> None:
    """Put each captured revision back, undoing a partly applied rewrite.

    Called when a later part of the same :func:`write` call fails, so the
    project never holds a per-run file ahead of an aggregate row that was never
    written. The reader then finds the copies agreeing at the revision it read
    before, which is the state it can read, rather than a disagreement no
    reader may read.
    """
    for target, text in snapshots:
        with contextlib.suppress(OSError):
            _store.write_atomically(
                target, lambda handle, text=text: handle.write(text), fsync=False
            )


def _keep_run_files_identical(
    project: str, rows: Sequence[Any], root: str | Path | None
) -> list[tuple[Path, str]]:
    """Rewrite a per-run file so it equals the aggregate row it copies.

    A completed run may be recorded in both the aggregate run list and its own
    file under ``runs/``, and :func:`load` refuses the whole project when the
    two serialisations disagree. A write that edited an aggregate row and left
    the file behind would therefore hand the caller a project its own reader
    cannot read, so every written row that already owns a file is re-encoded by
    :func:`serialize_run` here, keeping the two copies byte-identical.

    The caller runs this before the aggregate envelope is written, and applies
    it as a pair: a per-run file that cannot be rewritten raises here, while
    the aggregate still holds the revision it read, and any file this call did
    rewrite is put back first, so a failed write leaves the project readable
    at its prior revision rather than advanced in one copy and stale in the
    other. The caller restores the returned snapshots in turn if the envelope
    write that follows is refused.

    A row with no file is left alone: writing one is the layout move
    :func:`_split_project_runs` performs explicitly and commits, not a side
    effect an unrelated write should carry. The file already agreeing with the
    row is also left alone, so a write that changes no run still touches no run
    file. Each replaced file yields the revision it held, which is what
    :func:`_restore_run_files` puts back.
    """
    snapshots: list[tuple[Path, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        run_id = row.get("run_id")
        if not run_id or not _SAFE_ID.fullmatch(str(run_id)):
            continue
        target = run_path(project, str(run_id), root)
        encoded = serialize_run(row)
        try:
            if not (target.exists() or target.is_symlink()):
                continue
            prior = target.read_text(encoding="utf-8")
            if prior == encoded:
                continue
            write_run_file(project, row, root, fsync=False, mode=None)
        except OSError as exc:
            _restore_run_files(snapshots)
            raise LedgerError(
                f"cannot keep run {run_id!r} readable for {project!r}: "
                f"{target} could not be made identical to its ledger row, so "
                f"the write was abandoned at the revision the ledger already "
                f"held: {exc}"
            ) from exc
        snapshots.append((target, prior))
    return snapshots


def ledger_lock_path(project: str) -> Path:
    """The lock file one project's ledger write serialises on.

    It lives outside the repository, under the config home's ``locks/``
    directory, so serialising a write never adds a file to the tree it is
    writing and never collides with a project's own tracked content. The
    project name is folded into the filename with a digest of the full name so
    two projects whose names sanitise alike cannot share one lock.

    Named here rather than inline in :func:`_ledger_write_lock` because the
    path is operator-visible: a stuck writer is diagnosed by looking at the
    lock it holds, and a test asserts the write creates nothing inside the
    repository.
    """
    from reckon.crew.runs import crew_home

    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", project).strip("-.") or "project"
    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:16]
    return crew_home() / "locks" / f"ledger-write-{readable[:48]}-{digest}.lock"


@contextlib.contextmanager
def _ledger_write_lock(project: str):
    """Serialise one project's whole ledger write across processes.

    A ledger write is a read-modify-write across more than one file: it reads
    the aggregate at a version, rewrites the per-run files that must follow
    their rows, then writes the aggregate under that version. Without mutual
    exclusion two writers prepared from the same revision interleave — one
    rewrites a run file, the other writes the file and commits, and the first
    then fails its version check and puts back the copy it captured *before*
    the winner wrote, replacing a committed row with a stale one. The reader
    then refuses the whole project, which is the disagreement the ordering of
    these writes exists to prevent. Holding an exclusive lock across the whole
    cycle removes the interleaving rather than compensating for it: the loser
    is refused before it writes anything, and any restore it performs can only
    concern files it wrote itself.

    The lock is advisory and process-scoped (``flock``), which is the scope of
    the hazard: every writer here is a command in a separate process. It is
    released when the file handle closes, so a writer that dies mid-write
    cannot strand the lock.
    """
    path = ledger_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write(
    project: str,
    data: Mapping[str, Any],
    expected_version: int,
    root: str | Path | None = None,
    *,
    allow_member_removal: bool = False,
    commit: bool = False,
) -> int:
    """Write the ledger, refusing a stale expected version.

    Pairing every write with the version it read is what makes two concurrent
    orchestrators safe: the loser is told to re-read rather than silently
    overwriting a record it never saw.

    A write that leaves out a member id the stored roster holds is refused
    unless ``allow_member_removal`` states that the removal is intended. This
    is what stops a write emptying a roster through a call meant for another
    purpose: the caller that genuinely retires a member passes the flag, so
    the removal is stated where it is performed rather than inferred later
    from an absence. An absent ledger has no members, so creating one from
    empty is unaffected.

    A write that cannot bring every per-run file it must change into line with
    its aggregate row is abandoned, and the files it had already changed are
    put back, so a store that refuses one write (a read-only tree, a full
    disk) leaves the project readable at the revision it held rather than
    advanced in one copy and stale in the other.

    The whole read-modify-write is serialised across processes on a lock file
    of its own (:func:`ledger_lock_path`), so the version pairing is not the
    only thing standing between two writers: a writer prepared from a revision
    another has since committed is refused before it writes anything, and the
    files it puts back on a later failure are only ever the ones it wrote.

    The roster check answers to the version check rather than pre-empting it:
    a write prepared against a version the ledger has moved past is refused
    for that reason, and the caller's re-read then faces the guard on a
    current roster. Reporting a membership difference drawn from a stale view
    would aim the caller at the wrong remedy.

    A write that would take the aggregate's own run count below what that
    aggregate holds is refused, naming both counts. The count is the aggregate's
    own rows rather than the union :func:`load` returns: once a project's runs
    live in per-run files, a write of the aggregate's rows is a reduction of the
    union by construction and still the ordinary shape of an edit. A reduction
    of the aggregate's own rows is by construction a lost read rather than an
    edit — the shape that carried one project's ledger from 110 runs and 87
    members down to a single row in one promote commit. The run store keeps
    every promoted run independently of this file, so the refusal names it as
    the recovery. Like the roster check, this one answers to the version
    check: a write prepared against a version the ledger has moved past is
    refused for that reason, and the version conflict is what a concurrent
    writer must act on.

    A write that would fold into the aggregate a run already held as its own
    per-run file is refused, naming how many it would have folded. The two
    copies of a run are one record; an aggregate that also carries the rows
    under ``runs/`` is the export :func:`_split_project_runs` exists to undo, so
    a writer that read :func:`load`'s union and wrote it back would duplicate
    every promoted run in the aggregate and grow the tracked file without bound.
    The union is the reader's view, not a writer's input: a caller either passes
    the aggregate's own rows, or appends one new run through :func:`append_run`,
    which writes that run's file and nothing else.

    ``commit`` states at the call site that the caller is committing the roster
    change it is making: the retirement the write records is committed at once,
    under a subject naming the members it dropped, rather than waiting to ride
    whichever change touches the roster next. The commit is skipped when the
    roster already holds content that is not this write's to record, or when
    the roster's checkout cannot answer for it. The write is then left in the
    tree exactly as an unrequested write would be, and the next named commit
    refuses it on those same grounds, which is what a caller without a
    checkout to commit into would have seen anyway. A committed write drops at least
    one member: a write that changes nothing has nothing to commit, and says so
    rather than leaving an empty commit behind.
    """
    path = ledger_path(project, root)
    # The lock spans the read, the version check, the per-run file writes and
    # the aggregate write, so two writers prepared from one revision cannot
    # interleave: the loser is refused before it writes anything, and a restore
    # it performs can only put back a file it wrote itself. Held across the
    # roster commit too, because that commit is the last step of the same write.
    with _ledger_write_lock(project):
        incoming_members = list(data.get("members", []))
        incoming_runs = list(data.get("runs", []))
        stored, stored_version = load(project, root)
        # The guard below measures the incoming rows against the aggregate's own
        # rows, never against the union load() returns: the union is the reader's
        # view, and the per-run files it folds in are rows this write must leave
        # where they are.
        aggregate, _aggregate_version = _load_aggregate(project, root)
        current_view = expected_version == stored_version
        dropped = (
            dropped_member_ids(stored["members"], incoming_members)
            if current_view
            else []
        )
        if dropped and not allow_member_removal:
            raise LedgerError(
                f"refusing to write the ledger for {project!r}: this write drops "
                f"roster member(s) {', '.join(dropped)} that the stored roster "
                f"holds, taking the member count from {len(stored['members'])} to "
                f"{len(incoming_members)}, which is how a roster empties itself "
                "without anyone asking; pass allow_member_removal=True when the "
                "removal is intended"
            )
        if current_view and len(incoming_runs) < len(aggregate["runs"]):
            raise LedgerError(
                f"refusing to write the ledger for {project!r}: this write takes "
                f"the committed run count from {len(aggregate['runs'])} to "
                f"{len(incoming_runs)}, and a promotion only ever appends, so a "
                f"reduction is by construction a lost read rather than an edit; "
                f"the run store at {_run_store_location()} holds every promoted "
                "run independently of this file and is the recovery for a ledger "
                "that has already lost rows"
            )
        folded = _folded_run_ids(incoming_runs, aggregate["runs"], project, root)
        if current_view and folded:
            raise LedgerError(
                f"refusing to write the ledger for {project!r}: this write would "
                f"fold {len(folded)} run row(s) already held as per-run files "
                f"into the aggregate ({', '.join(folded[:5])}"
                f"{', …' if len(folded) > 5 else ''}), which copies each of them "
                "into the tracked aggregate beside its own file; pass the "
                "aggregate's own rows, or append one new run with append_run, "
                "which writes only that run's file"
            )
        if commit and not dropped:
            raise LedgerError(
                f"nothing to commit for {project!r}: commit=True names the roster "
                "rows a write dropped, and this write dropped none"
            )
        if not current_view:
            # Refused here, under the lock and before any per-run file is
            # touched, rather than left to the envelope's own version check:
            # this writer returns its conflict having written nothing, so a
            # winner's committed rows cannot be overwritten by a copy this
            # writer captured before the winner wrote.
            raise LedgerError(
                f"ledger for {project!r} moved from version {expected_version} to "
                f"{stored_version} while this write was being prepared; "
                "re-read and retry"
            )
        obstruction = None
        if commit:
            try:
                obstruction = _roster_obstruction(
                    project, ", ".join(dropped), path, stored["members"]
                )
            except LedgerError as exc:
                obstruction = str(exc)
        payload = {
            "members": sorted(
                (dict(member) for member in incoming_members),
                key=lambda member: str(member.get("id", "")),
            ),
            "runs": list(data.get("runs", [])),
            "holds": list(data.get("holds", [])),
        }
        # The per-run files are made consistent with the rows they copy before
        # the aggregate envelope advances, never after. If one of them cannot
        # be rewritten, the write is abandoned while the aggregate still holds
        # the revision it read, so the reader finds the two copies agreeing
        # rather than a row the file cannot follow. The reverse order left the
        # aggregate ahead of its files when a store refused a single write,
        # which is the disagreement load() refuses the whole project for.
        snapshots = _keep_run_files_identical(project, payload["runs"], root)
        try:
            version = _store._write_json_envelope(
                path, project, LEDGER_SLUG, payload, expected_version
            )
        except _store.VersionConflict as exc:
            _restore_run_files(snapshots)
            raise LedgerError(
                f"ledger for {project!r} moved from version {exc.expected} to "
                f"{exc.current} while this write was being prepared; re-read and retry"
            ) from exc
        except Exception:
            # An envelope that could not be written leaves the aggregate at the
            # revision it read, so the files already brought forward must go back
            # with it; otherwise this write would hand the reader exactly the
            # disagreement the order above exists to avoid.
            _restore_run_files(snapshots)
            raise
        if commit and obstruction is None:
            _commit_state_write(
                project,
                "chore(roster): retire " + ", ".join(dropped),
                (
                    "Commit the project roster immediately so this roster change "
                    "cannot ride an unrelated later change."
                ),
                path,
                "roster",
            )
    return version


# ── The roster ──────────────────────────────────────────────────────────────


def members(project: str, root: str | Path | None = None) -> list[dict[str, Any]]:
    """Return the project's team roster."""
    return load(project, root)[0]["members"]


def member(
    project: str, member_id: str, root: str | Path | None = None
) -> dict[str, Any] | None:
    """Return one roster member, or None when the project has no such member."""
    for entry in members(project, root):
        if str(entry.get("id")) == str(member_id):
            return entry
    return None


def _state_checkout(
    project: str, path: Path, what: str = "roster"
) -> tuple[Path, Path]:
    """Resolve the checkout and repository-relative path for one state file.

    ``what`` names the write in the refusal, because the same resolution
    serves the roster and the one-time run split, and an operator reading a
    refusal must be able to tell which write could not be committed. Every
    state file committed here resolves through this one implementation, so a
    second writer cannot drift into a different tree.
    """
    resolved_path = path.expanduser().resolve()
    discovery_root = resolved_path.parent
    while not discovery_root.exists() and discovery_root != discovery_root.parent:
        discovery_root = discovery_root.parent
    discovered = subprocess.run(
        [
            "git",
            "-C",
            str(discovery_root),
            "rev-parse",
            "--show-toplevel",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if discovered.returncode != 0:
        raise LedgerError(
            f"cannot commit {what} for {project!r}: {resolved_path} is not in a git checkout"
        )
    checkout = Path(discovered.stdout.strip()).resolve()
    try:
        relative_path = resolved_path.relative_to(checkout)
    except ValueError as exc:
        raise LedgerError(
            f"{what} for {project!r} resolved outside its git checkout: "
            f"{resolved_path} is not beneath {checkout}"
        ) from exc
    return checkout, relative_path


# The roster fields that carry a captured session: the resumable id a run
# reported, the model it was captured under, and the per-model map a member
# accumulates. A capture is written by whatever run last reported one rather
# than by an author registering a member, so these fields are not part of what
# a registration is a claim about.
_SESSION_CAPTURE_FIELDS = frozenset({"session_id", "session_model", "sessions"})


def _registration_content(member: Mapping[str, Any] | None) -> dict[str, Any]:
    """The part of a member row a registration owns, without its capture.

    Two rows with equal registration content are the same membership: a row
    that gained or refreshed a session is not a different member, and a row
    whose harness, role or identity moved. An absent row has no content, so it
    still differs from any row present.
    """
    return {
        key: value
        for key, value in (member or {}).items()
        if key not in _SESSION_CAPTURE_FIELDS
    }


def _roster_obstruction(
    project: str,
    member_id: str,
    path: Path,
    current_members: list[dict[str, Any]],
) -> str | None:
    """Describe roster content a named commit cannot claim, or None when clean.

    A commit names one change, so it can only be made while the roster holds
    nothing else unrecorded by a different author. The comparison is on member
    rows rather than on dirtiness alone: a roster rewritten with the rows it
    already held is not a write anyone would be publishing under someone
    else's subject.

    Content that is no member row at all — the hold history a budget check
    opens, a completed-run row — is the tool's own bookkeeping, so it obstructs
    nothing. Refusing on it made a dispatch that had just recorded a hold on its
    own backend unable to commit the session member it registered next, and left
    the idle reaper's removal uncommitted, so the registration that followed saw
    a removed row as another author's pending write. A member row whose only
    unrecorded difference is its captured session is the same bookkeeping for
    the same reason: the resumable id is written by whichever run reported one,
    not by an author registering a member, so it obstructs no named commit
    either — refusing on it made a capture that nobody had committed block the
    next registration until someone committed the roster by hand.

    Reporting the obstruction rather than raising lets a caller that is
    entitled to proceed without a commit, such as the idle reaper whose
    removal stands whether or not it can be recorded, leave the write in the
    tree exactly as an unrequested write would, and lets the next named commit
    refuse it on the same grounds.
    """
    checkout, relative_path = _state_checkout(project, path)
    status = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            str(relative_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        raise LedgerError(
            f"cannot inspect the roster before committing {member_id!r}: "
            f"{status.stderr.strip() or status.stdout.strip()}"
        )
    if not status.stdout.strip():
        return None

    committed = subprocess.run(
        ["git", "-C", str(checkout), "show", f"HEAD:{relative_path.as_posix()}"],
        capture_output=True,
        text=True,
        check=False,
    )
    committed_members: list[dict[str, Any]] = []
    if committed.returncode == 0:
        try:
            payload = json.loads(committed.stdout)
            committed_members = list(payload.get("data", {}).get("members", []))
        except (AttributeError, TypeError, ValueError):
            committed_members = []

    current_by_id = {str(item.get("id")): item for item in current_members}
    committed_by_id = {str(item.get("id")): item for item in committed_members}
    changed_members = sorted(
        member
        for member in current_by_id.keys() | committed_by_id.keys()
        if _registration_content(current_by_id.get(member))
        != _registration_content(committed_by_id.get(member))
    )
    if changed_members:
        return "uncommitted member registration(s): " + ", ".join(changed_members)
    return None


def _refuse_dirty_roster_commit(
    project: str,
    member_id: str,
    path: Path,
    current_members: list[dict[str, Any]],
) -> None:
    """Refuse a candidate commit when the roster already carries another write."""
    obstruction = _roster_obstruction(project, member_id, path, current_members)
    if obstruction is None:
        return
    raise LedgerError(
        f"cannot commit roster registration {member_id!r}: {obstruction}; "
        "commit or discard that roster write before retrying"
    )


def _merge_in_progress(checkout: Path) -> bool:
    """Whether ``checkout`` has a merge it has not concluded.

    ``MERGE_HEAD`` exists exactly while a merge is unresolved, and it lives in
    the git directory — the per-worktree one where a linked worktree keeps it,
    which is why git resolves the path rather than the checkout's ``.git``
    entry being read here by hand.
    """
    probe = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--git-path", "MERGE_HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return False
    merge_head = Path(probe.stdout.strip())
    if not merge_head.is_absolute():
        merge_head = checkout / merge_head
    return merge_head.exists()


def _state_checkout_paths(
    project: str, paths: Iterable[Path], what: str
) -> tuple[Path, list[Path]]:
    """Resolve a set of state files to one checkout and their relative paths.

    Every path must resolve to the same checkout, because the set shares one
    commit; a set spanning two checkouts is refused rather than split, so a
    caller that meant one commit learns its write cannot be one. Resolution
    runs for every path before any staging, so a refused set leaves nothing
    staged.
    """
    checkout: Path | None = None
    relatives: list[Path] = []
    for path in paths:
        resolved_checkout, relative = _state_checkout(project, path, what)
        if checkout is None:
            checkout = resolved_checkout
        elif resolved_checkout != checkout:
            raise LedgerError(
                f"{what} write for {project!r} spans two checkouts: "
                f"{relative} is in {resolved_checkout}, not {checkout}"
            )
        relatives.append(relative)
    if checkout is None:
        raise LedgerError(f"no {what} path given to commit")
    return checkout, relatives


def _commit_state_write(
    project: str,
    subject: str,
    body: str,
    path: Path | Iterable[Path],
    what: str = "state",
) -> None:
    """Commit the requested state write(s) without sweeping other staged paths.

    ``path`` names one file, as most callers pass, or a set of files that must
    share one commit: a review run's promotion commits its ledger row and its
    review record together, so the record lands in the commit the promotion
    already makes rather than adding one. Every path must resolve to the same
    checkout, and a refused set leaves nothing staged.

    The commit is skipped while a merge is in progress: a commit made there
    would conclude the merge or fail on the conflicted index, and it is the
    session that owns the merge which decides what its commit carries. The
    write stays in the tree, exactly as a caller that asked for no commit
    would leave it, and the skip is stated on stderr.
    """
    if isinstance(path, (str, Path)):
        paths = [Path(path)]
    else:
        paths = [Path(item) for item in path]
    if not paths:
        raise LedgerError(f"no {what} path given to commit")
    checkout, relative_paths = _state_checkout_paths(project, paths, what)
    named = ", ".join(str(relative) for relative in relative_paths)

    if _merge_in_progress(checkout):
        print(
            f"merge in progress in {checkout}: leaving the {what} write at "
            f"{named} uncommitted",
            file=sys.stderr,
        )
        return

    relative_args = [str(relative) for relative in relative_paths]
    staged = subprocess.run(
        ["git", "-C", str(checkout), "add", "--", *relative_args],
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode != 0:
        raise LedgerError(
            f"could not stage {what} write {named}: "
            f"{staged.stderr.strip() or staged.stdout.strip()}"
        )

    committed = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "commit",
            "--only",
            "-m",
            subject,
            "-m",
            body,
            "--",
            *relative_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if committed.returncode != 0:
        subprocess.run(
            ["git", "-C", str(checkout), "reset", "-q", "HEAD", "--", *relative_args],
            capture_output=True,
            text=True,
            check=False,
        )
        raise LedgerError(
            f"could not commit {what} write {named}: "
            f"{committed.stderr.strip() or committed.stdout.strip()}"
        )


def register_member(
    project: str,
    member_id: str,
    *,
    harness: str,
    role: str = "implement",
    session_id: str | None = None,
    root: str | Path | None = None,
    now: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Add or update a roster member, returning the stored entry.

    A member registered with a null session is the normal case: the id it will
    reuse does not exist until its first run reports one, and observation
    persists it under the run's resolved agent configuration then.
    """
    if not _SAFE_ID.fullmatch(str(member_id)):
        raise LedgerError(f"member id {member_id!r} must match {_SAFE_ID.pattern}")
    if not str(harness).strip():
        raise LedgerError("a member must name the harness it dispatches to")
    # The roster write carries the aggregate's own collections: the union
    # :func:`load` merges adds every run that owns a file, and writing those
    # rows back would copy each of them into the tracked aggregate beside its
    # own file, which :func:`write` refuses.
    data, version = _load_aggregate(project, root)
    if commit:
        _refuse_dirty_roster_commit(
            project,
            member_id,
            ledger_path(project, root),
            data["members"],
        )
    existing = next(
        (
            dict(item)
            for item in data["members"]
            if str(item.get("id")) == str(member_id)
        ),
        {},
    )
    entry = {
        "id": str(member_id),
        "harness": str(harness),
        "role": str(role),
        "session_id": str(session_id) if session_id else None,
        "session_model": (
            existing.get("session_model")
            if session_id and str(existing.get("session_id") or "") == str(session_id)
            else None
        ),
        "sessions": dict(existing.get("sessions") or {}),
        "created": next(
            (
                str(existing.get("created"))
                for existing in data["members"]
                if str(existing.get("id")) == str(member_id) and existing.get("created")
            ),
            (now or _utc_now())[:10],
        ),
    }
    data["members"] = [
        existing
        for existing in data["members"]
        if str(existing.get("id")) != str(member_id)
    ] + [entry]
    write(project, data, version, root)
    if commit:
        _commit_state_write(
            project,
            f"chore(roster): register {member_id}",
            (
                "Commit the project roster immediately so this roster change "
                "cannot ride an unrelated later change."
            ),
            ledger_path(project, root),
            "roster",
        )
    return entry


def _index_append(project: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Update the rebuildable index after the authoritative file is written.

    An index failure is returned to promotion without altering the run file or
    failing its append. Index lag exposes missing rows until a reader rebuilds
    the cache from the files.
    """
    from reckon import run_store

    try:
        run_store.append(project, dict(record))
    except Exception as exc:  # noqa: BLE001 — index failure cannot undo the run file
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return {"status": "written"}


def _git_stdout(checkout: Path, *args: str) -> str | None:
    """Run a git query in ``checkout``; ``None`` when git declines to answer."""
    done = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout if done.returncode == 0 else None


def _history_is_truncated(checkout: Path) -> bool:
    """Whether this checkout holds a history git cannot answer a query from.

    A shallow, grafted or partial clone answers a path-history query only from
    the commits it carries, so a path whose commits fall outside a shallow
    boundary reads exactly like a path never recorded. Git can report that its
    own history is incomplete — ``rev-parse --is-shallow-repository`` for a
    shallow clone, a ``refs/replace`` ref or an ``info/grafts`` file for a
    grafted one, and a promisor remote (``extensions.partialClone`` or
    ``remote.<name>.promisor``) for a partial clone — and where it does, an
    empty log is evidence of a truncation, never of an absence.
    """
    shallow = _git_stdout(checkout, "rev-parse", "--is-shallow-repository")
    if shallow is not None and shallow.strip() == "true":
        return True
    replacements = _git_stdout(
        checkout, "for-each-ref", "--format=%(refname)", "refs/replace/"
    )
    if replacements is not None and replacements.strip():
        return True
    grafted = _git_stdout(checkout, "rev-parse", "--git-path", "info/grafts")
    if grafted:
        grafts = Path(grafted.strip())
        if not grafts.is_absolute():
            grafts = checkout / grafts
        if grafts.exists():
            return True
    partial = _git_stdout(
        checkout,
        "config",
        "--get-regexp",
        r"^(extensions\.partialclone|remote\..*\.promisor)$",
    )
    return bool(partial and partial.strip())


def _ledger_ever_tracked(path: Path) -> bool | None:
    """Ask git whether this checkout ever recorded the ledger path.

    A committed ledger removed from the working tree and a project that never
    had one present identically — an absent file — so the file alone cannot
    say which write is safe. Git can: ``True`` means the path appears somewhere
    in the repository's history, so its absence is a deletion to recover;
    ``False`` means git has never recorded it, so the project is genuinely new.
    ``None`` is returned when the question cannot be answered here — the path
    is outside any checkout, the checkout carries no commits, or its history is
    shallow, grafted or partial, so an empty log would report a truncation as
    an absence — and a caller keeps its conservative refusal rather than
    reading silence as a fresh start.
    """
    resolved = path.expanduser().resolve()
    discovery_root = resolved.parent
    while not discovery_root.exists() and discovery_root != discovery_root.parent:
        discovery_root = discovery_root.parent
    toplevel = subprocess.run(
        ["git", "-C", str(discovery_root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if toplevel.returncode != 0:
        return None
    checkout = Path(toplevel.stdout.strip()).resolve()
    try:
        relative_path = resolved.relative_to(checkout)
    except ValueError:
        return None
    if _history_is_truncated(checkout):
        return None
    logged = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "log",
            "--all",
            "--format=%H",
            "--",
            relative_path.as_posix(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if logged.returncode != 0:
        return None
    return bool(logged.stdout.strip())


def append_run(
    project: str,
    record: Mapping[str, Any],
    *,
    root: str | Path | None = None,
    allow_create: bool = False,
) -> dict[str, Any]:
    """Create one run file exclusively, then attempt one index update.

    Existing target paths and top-level aggregate rows refuse duplicate ids.
    A byte scan avoids parsing the aggregate unless it contains this id; other
    run files are never opened. The caller owns the commit. Independent files
    need no retry queue.
    The returned ``version`` is None because no aggregate version is advanced.

    An absent aggregate still needs the history guard: a tracked file missing
    from the working tree can hold unsplit history that needs recovery. A path
    never tracked can accept a run without creating an aggregate; where git
    cannot answer, ``allow_create=True`` explicitly permits initialisation.
    """
    run_id = str(record.get("run_id") or "")
    if not run_id:
        raise LedgerError("a run record must carry a run_id")
    ledger_root = _run_ledger_root(project, root)
    path = ledger_path(project, ledger_root)
    target = run_path(project, run_id, ledger_root)
    refusal = (
        f"run {run_id!r} already exists at {target}; promoting it twice "
        "would double-count its measurements"
    )
    if target.exists() or target.is_symlink():
        raise LedgerError(refusal)
    if (
        not path.exists()
        and not allow_create
        and _ledger_ever_tracked(path) is not False
    ):
        raise LedgerError(
            f"refusing to promote run {run_id!r} for {project!r}: ledger file "
            f"{path} does not exist. The run store at {_run_store_location()} "
            "is the independent authority that holds every promoted run; "
            "recover the ledger or initialise a genuinely new project with "
            "allow_create=True."
        )
    try:
        aggregate_bytes = path.read_bytes()
    except FileNotFoundError:
        aggregate_bytes = b""
    if run_id.encode("utf-8") in aggregate_bytes:
        try:
            envelope = json.loads(aggregate_bytes)
            entries = envelope.get("data", {}).get("runs", [])
        except (ValueError, AttributeError) as exc:
            raise LedgerError(f"cannot check run {run_id!r} in {path}: {exc}") from exc
        if not isinstance(entries, list):
            raise LedgerError(
                f"cannot check run {run_id!r} in {path}: runs must be a list"
            )
        existing = next(
            (
                item
                for item in entries
                if isinstance(item, Mapping) and str(item.get("run_id")) == run_id
            ),
            None,
        )
        if existing is not None:
            raise LedgerError(
                f"run {run_id!r} is already in {path} "
                f"(completed {existing.get('completed_at')!r}); promoting it twice "
                "would double-count its measurements"
            )

    stored_record = dict(record)
    stored_record.pop("store_write", None)
    encoded = serialize_run(stored_record)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
    except FileExistsError as exc:
        raise LedgerError(refusal) from exc
    store_outcome = _index_append(project, stored_record)
    return {
        "path": str(target),
        "version": None,
        "run": stored_record,
        "store": store_outcome,
    }


def _split_project_runs(project: str, docs: Path, *, dry_run: bool) -> dict[str, Any]:
    """Classify and then migrate one project's aggregate run rows.

    Every row is written by :func:`serialize_run`, so the file a split produces
    is the same file a promotion would have produced for that row. A project
    migrates only when each row with an existing file already matches; writing
    the others would leave two disagreeing copies of one run, which
    :func:`load` refuses to read at all. A differing file therefore stops the
    whole project, since the aggregate list must survive as the copy a reader
    can still union.
    """
    root = docs.parent
    path = ledger_path(project, root)
    entry: dict[str, Any] = {
        "rows": 0,
        "missing": 0,
        "identical": 0,
        "differing": 0,
        "differing_files": [],
        "written": 0,
        "commit": None,
        "skipped": None,
    }
    if not path.is_file():
        entry["skipped"] = f"no ledger at {path}"
        return entry
    data, version = _store._load_json_envelope(path)
    if "runs" not in data:
        entry["skipped"] = "the ledger holds no runs key"
        return entry
    rows = data["runs"]
    if not isinstance(rows, list):
        raise LedgerError(
            f"refusing to split {project!r}: runs in {path} must be a list, "
            "and a malformed collection is not an empty history"
        )
    entry["rows"] = len(rows)
    planned: list[tuple[Any, Path]] = []
    # Only the rows of the list are considered. A run file whose id no row
    # names is a promotion that landed while the aggregate still held older
    # rows; it is never opened, so it is neither counted nor disturbed.
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("run_id"):
            raise LedgerError(
                f"refusing to split {project!r}: {path} holds a runs entry "
                "that is not an object with a run_id"
            )
        target = run_path(project, str(row["run_id"]), root)
        planned.append((row, target))
        if target.exists() or target.is_symlink():
            try:
                existing = target.read_text(encoding="utf-8")
            except OSError as exc:
                raise LedgerError(
                    f"cannot read run file {target} while splitting {project!r}: {exc}"
                ) from exc
            if existing == serialize_run(row):
                entry["identical"] += 1
            else:
                entry["differing"] += 1
                entry["differing_files"].append(str(target))
        else:
            entry["missing"] += 1
    if entry["differing"]:
        entry["stopped"] = (
            "a run file already present for a listed row differs from the "
            "serialiser's output for that row"
        )
        return entry
    if dry_run:
        return entry

    for row, target in planned:
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(serialize_run(row), encoding="utf-8")
        entry["written"] += 1

    remaining = {key: value for key, value in data.items() if key != "runs"}
    try:
        _store._write_json_envelope(path, project, LEDGER_SLUG, remaining, version)
    except _store.VersionConflict as exc:
        raise LedgerError(
            f"the ledger for {project!r} moved from version {exc.expected} to "
            f"{exc.current} while the split was being prepared; re-read and retry"
        ) from exc

    checkout, relative_path = _state_checkout(project, path, "run split")
    commit_paths = [relative_path]
    for _row, target in planned:
        try:
            commit_paths.append(target.resolve().relative_to(checkout))
        except ValueError as exc:
            raise LedgerError(
                f"run file {target} resolved outside the checkout {checkout} "
                f"that owns {path}"
            ) from exc
    entry["commit"] = _commit_run_split(
        project, checkout, commit_paths, rows=entry["rows"]
    )
    return entry


def _commit_run_split(
    project: str, checkout: Path, commit_paths: list[Path], *, rows: int
) -> str:
    """Commit one project's split: the trimmed ledger plus the files it wrote.

    Only the named paths enter the commit, so a peer's staged work in the same
    checkout is not swept into a migration commit it never described.
    """
    names = [str(path) for path in commit_paths]
    staged = subprocess.run(
        ["git", "-C", str(checkout), "add", "--", *names],
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode != 0:
        raise LedgerError(
            f"could not stage the run split for {project!r}: "
            f"{staged.stderr.strip() or staged.stdout.strip()}"
        )
    subject = f"chore(ledger): split {project} runs into per-run files"
    body = (
        f"Write the {rows} aggregate run row(s) of {project!r} to "
        "runs/<run_id>.json through the one serialiser and drop the aggregate "
        "list, so every run has a single home and a reader no longer unions "
        "two copies of one history."
    )
    committed = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "commit",
            "--only",
            "-m",
            subject,
            "-m",
            body,
            "--",
            *names,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if committed.returncode != 0:
        raise LedgerError(
            f"could not commit the run split for {project!r} in {checkout}: "
            f"{committed.stderr.strip() or committed.stdout.strip()}"
        )
    return (
        committed.stdout.strip().splitlines()[0]
        if committed.stdout.strip()
        else subject
    )


def split_runs(*, dry_run: bool = False) -> dict[str, Any]:
    """Move every mounted project's aggregate run rows into their own files.

    The one-time migration from the aggregate ``runs`` list to one file per
    run, over every project in the mount registry: a project whose ledger
    exists and holds a ``runs`` key — including an empty list — is migrated,
    and any other is reported as skipped. Each project is written and
    committed on its own, so one project's refusal never blocks another's.

    ``dry_run`` reports what each project would do and writes nothing, which
    is how an operator sees the differing files before a real migration
    refuses on them.
    """
    from reckon import flight

    try:
        docs_by_project = flight.mounted_project_docs()
    except flight.FlightConfigError as exc:
        raise LedgerError(
            f"cannot split runs without the mount registry: {exc}"
        ) from exc
    projects = {
        project: _split_project_runs(project, Path(docs), dry_run=dry_run)
        for project, docs in sorted(docs_by_project.items())
    }
    return {"dry_run": dry_run, "projects": projects}


def index_lag(
    project: str,
    root: str | Path | None = None,
) -> int | str:
    """Count run filenames absent from the index without rebuilding that index."""
    from reckon import run_store

    indexed = run_store.indexed_run_ids(project)
    if indexed is None:
        return "no index"
    paths = _run_files(project, _run_ledger_root(project, root))
    return len({path.stem for path in paths} - indexed)


def runs(
    project: str,
    root: str | Path | None = None,
    *,
    plan: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return selected completed runs in promotion order.

    ``since`` is an inclusive completion-time boundary. Records without a
    usable completion timestamp cannot satisfy that filter. ``limit`` selects
    the most recently promoted matching records while preserving their stored
    order.

    Stored values are preserved, with catalogue-derived lane and model key
    added at read time. Use read_records for stamp-derived duration rather
    than the stored wall_seconds carried here.
    """
    records, _version = read_records(
        project,
        root,
        plan=plan,
        since=since,
        limit=limit,
        with_figures=False,
    )
    return records


def read_records(
    project: str,
    root: str | Path | None = None,
    *,
    plan: str | None = None,
    since: str | None = None,
    limit: int | None = None,
    with_figures: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """Read selected completed runs and their ledger version once.

    ``with_figures`` adds the two questions a reader puts to a completed node:
    ``wall_seconds``, the span between that row's own ``dispatched_at`` and
    ``completed_at`` stamps, and ``width_at_start``, the number of runs whose
    own measured span covers the row's dispatch instant. The stored field
    carrying the first name is never passed through — on nearly half the ledger
    it is a different quantity from the span — so a row whose stored value
    disagrees reports the stamp-derived one, and a figure the stamps cannot
    support is ``None`` rather than zero. Width is counted over every committed
    row before the selection below narrows it, because how wide the fleet was at
    a dispatch instant is a property of that moment rather than of the rows
    being selected.

    Disabling figures preserves stored measurements in promotion order.
    Both paths add catalogue identity fields without rewriting committed rows.
    """
    data, version = load(project, root)
    if with_figures:
        from reckon.crew import summary

        records = summary.run_rows(project, root=root)
    else:
        records = list(data["runs"])
    selected_plan = str(plan or "").strip()
    if selected_plan:
        records = [
            record
            for record in records
            if str(record.get("plan") or "") == selected_plan
        ]
    if since is not None:
        boundary = _parse_timestamp(since, "since")
        filtered: list[dict[str, Any]] = []
        for record in records:
            completed_at = record.get("completed_at")
            if not completed_at:
                continue
            try:
                completion = _parse_timestamp(completed_at, "completed_at")
            except LedgerError:
                continue
            if completion >= boundary:
                filtered.append(record)
        records = filtered
    if limit is not None:
        if isinstance(limit, bool) or limit < 1:
            raise LedgerError("record limit must be a positive integer")
        records = records[-limit:]
    return [normalize_identity(record) for record in records], version


def review_block(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The review block a row's readers should see, or ``None``.

    A row's recorded review fields state what a promotion believed when it
    landed, and that belief is itself evidence, so a misattributed row is never
    rewritten in place. It carries an appended ``review_correction`` beside
    them instead. Every reader of a row's review therefore reads through here:
    a corrected row reads as a run with no review, so it drops out of review
    totals and per-dimension medians rather than contributing another round's
    verdict. A row that carries no correction returns its recorded block, and
    a row that carries no block at all returns ``None`` either way, because an
    absent review is not a score of zero.
    """
    if row.get("review_correction"):
        return None
    block = row.get("review")
    return block if isinstance(block, Mapping) else None


def review_scores(
    records: Iterable[Mapping[str, Any]],
    *,
    plan: str | None = None,
    node: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return per-dimension scores from ledger rows that carry a review.

    Filters bind the row's own run attributes, so a question like "which
    dimension scored low on the work this plan produced" is answerable from
    the committed ledger alone rather than by reading files by hand. A row
    promoted without a review is omitted — an absent review is not a score of
    zero, and nothing here turns it into one: a reviewed run whose dimensions
    genuinely measure zero carries a block with ``total`` 0 and shows up in
    the result, an unreviewed run does not.
    """
    selected: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if plan is not None and str(record.get("plan") or "") != str(plan):
            continue
        if node is not None and str(record.get("node") or "") != str(node):
            continue
        if run_id is not None and str(record.get("run_id") or "") != str(run_id):
            continue
        block = review_block(record)
        if not isinstance(block, Mapping):
            continue
        selected.append(
            {
                "run_id": str(record.get("run_id") or ""),
                "plan": str(record.get("plan") or ""),
                "node": str(record.get("node") or ""),
                "status": str(block.get("status") or "unparsed"),
                "scores": dict(block.get("scores") or {}),
                "absent": list(block.get("absent") or []),
                "total": block.get("total"),
            }
        )
    return selected


def _resume_stream_order(path: Path) -> tuple[int, str]:
    """Order numbered resume streams by turn rather than filename text."""
    match = re.fullmatch(r"resume-(\d+)\.jsonl", path.name)
    return (int(match.group(1)), path.name) if match else (sys.maxsize, path.name)


def _run_streams(run_id: str, streams_root: Path) -> list[Path]:
    """Return every surviving stream for one run in stable path order."""

    from reckon.crew import metering

    return metering.run_streams(streams_root / run_id / "stream.jsonl")


_EXPLICIT_ZONE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2}(?::\d{2})?)$")


def _parsed_stamp(value: Any) -> datetime | None:
    """Parse one ledger stamp, reading a value without a zone as UTC.

    A value padded with whitespace or suffixed with a lowercase zone marker is
    not a stamp this ledger writes, so it is refused rather than read.
    """

    text = str(value)
    if not text or text != text.strip() or text.endswith("z"):
        return None
    return parse_utc(text)


def _zoned_stamp(value: Any) -> datetime | None:
    """Parse a stamp that must state its zone, refusing one that does not."""

    if not isinstance(value, str) or not value.strip():
        return None
    if value != value.strip() or not _EXPLICIT_ZONE.search(value):
        return None
    return parse_utc(value)


def _event_completion(paths: Iterable[Path]) -> str | None:
    """Return the newest aware event timestamp across surviving streams."""

    timestamps: list[tuple[datetime, str]] = []
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                timestamp = event.get("timestamp")
                if not isinstance(timestamp, str) or not timestamp.strip():
                    continue
                parsed = _zoned_stamp(timestamp)
                if parsed is not None:
                    timestamps.append((parsed, timestamp))
    return max(timestamps, key=lambda item: item[0])[1] if timestamps else None


def _stream_completion(paths: list[Path]) -> tuple[str, str]:
    """Resolve completion from event timestamps, then the newest stream mtime."""

    event_time = _event_completion(paths)
    if event_time:
        return event_time, "terminal_event"
    newest = max(path.stat().st_mtime for path in paths)
    completed = (
        datetime.fromtimestamp(newest, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return completed, "stream_mtime"


def _worker_seconds(dispatched_at: Any, completed_at: Any) -> int | None:
    """Return elapsed seconds, treating timestamps without an offset as UTC."""

    start = _parsed_stamp(dispatched_at)
    finish = _parsed_stamp(completed_at)
    if start is None or finish is None:
        return None
    return max(0, int((finish - start).total_seconds()))


def _persist_repaired_rows(
    project: str,
    aggregate: dict[str, Any],
    aggregate_version: int,
    repaired: Sequence[Mapping[str, Any]],
    changed_ids: set[str],
    root: str | Path | None,
) -> int:
    """Persist repaired rows where each is held, returning the aggregate version.

    Rows are decided from the union :func:`load` returns, so every run is
    measured, but only the aggregate's own rows may go back to the writer: an
    aggregate-held row is patched in place, and a row that owns a file under
    ``runs/`` is written through that file. Handing the whole history to
    :func:`write` would copy each per-run row into the tracked aggregate, which
    :func:`write` refuses.
    """
    held = {
        str(row["run_id"]): row
        for row in aggregate["runs"]
        if isinstance(row, Mapping) and row.get("run_id")
    }
    aggregate_changed = False
    for record in repaired:
        run_id = str(record.get("run_id") or "")
        if run_id not in changed_ids:
            continue
        home = held.get(run_id)
        if home is None:
            write_run_file(project, record, root)
            continue
        home.clear()
        home.update(record)
        aggregate_changed = True
    if not aggregate_changed:
        return aggregate_version
    return write(project, aggregate, aggregate_version, root)


def repair_completion(
    project: str,
    *,
    root: str | Path | None = None,
    write_changes: bool = False,
    streams_root: str | Path | None = None,
) -> dict[str, Any]:
    """Re-derive historical completion measurements from surviving streams.

    Reporting is the default.  Persistence requires ``write_changes=True``;
    records with no surviving stream are reported as unusable and never
    rewritten, because promotion time is not a worker completion measurement.
    """

    data, version = load(project, root)
    stream_base = (
        Path(streams_root)
        if streams_root is not None
        else _store._config_home() / "crew" / "runs"
    )
    repaired_runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    changes = 0
    unusable = 0
    unchanged = 0
    for raw_record in data["runs"]:
        record = dict(raw_record)
        run_id = str(record.get("run_id") or "")
        paths = _run_streams(run_id, stream_base)
        if not paths:
            unusable += 1
            rows.append(
                {
                    "run_id": run_id,
                    "action": "unusable",
                    "calibration_usable": False,
                    "completion_source": None,
                    "detail": "no surviving stream file; record left unchanged",
                }
            )
            repaired_runs.append(record)
            continue

        completed_at, source = _stream_completion(paths)
        seconds = _worker_seconds(record.get("dispatched_at"), completed_at)
        replacement = {
            "completed_at": completed_at,
            "completed_at_source": source,
            "worker_seconds": seconds,
        }
        changed_fields = {
            field: {"before": record.get(field), "after": value}
            for field, value in replacement.items()
            if record.get(field) != value
        }
        action = "updated" if changed_fields else "unchanged"
        if changed_fields:
            changes += 1
            record.update(replacement)
        else:
            unchanged += 1
        rows.append(
            {
                "run_id": run_id,
                "action": action,
                "calibration_usable": True,
                "completion_source": source,
                "completed_at": completed_at,
                "worker_seconds": seconds,
                "changed_fields": changed_fields,
                "stream_files": [str(path) for path in paths],
            }
        )
        repaired_runs.append(record)

    new_version = version
    written = False
    if write_changes and changes:
        aggregate, aggregate_version = _load_aggregate(project, root)
        new_version = _persist_repaired_rows(
            project,
            aggregate,
            aggregate_version,
            repaired_runs,
            {str(row["run_id"]) for row in rows if row["action"] == "updated"},
            root,
        )
        written = True
    return {
        "project": project,
        "path": str(ledger_path(project, root)),
        "write_requested": bool(write_changes),
        "written": written,
        "version_before": version,
        "version_after": new_version,
        "records": len(rows),
        "updated": changes,
        "unchanged": unchanged,
        "unusable": unusable,
        "rows": rows,
    }


# ── Budget holds ────────────────────────────────────────────────────────────


def _parse_timestamp(value: Any, label: str) -> datetime:
    """Parse one labelled ledger timestamp as an aware instant.

    Retained rather than routed through ``reckon._timestamps.parse_utc``: this
    returns the moment in the zone the stamp was written in, and the shared
    parser normalises every zone to UTC, which would change the value a stamp
    carrying an offset of its own resolves to.
    """
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"{label} {value!r} is not ISO-8601") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_utc(value: Any) -> datetime:
    """Parse one hold-check timestamp and normalise it to an aware instant."""
    return _parse_timestamp(value, "hold check time")


def _hold_id(
    backend: str, checked_at: str, existing: Iterable[Mapping[str, Any]]
) -> str:
    """Return a readable id that remains unique within one ledger."""
    safe_backend = re.sub(r"[^A-Za-z0-9._-]+", "-", backend).strip("-") or "backend"
    stamp = re.sub(r"[^0-9]", "", checked_at)
    base = f"hold-{safe_backend}-{stamp}"
    used = {str(item.get("hold_id") or "") for item in existing}
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


def record_hold_checks(
    project: str,
    checks: Iterable[Mapping[str, Any]],
    *,
    checked_at: str,
    root: str | Path | None = None,
    attempts: int = 12,
) -> dict[str, Any]:
    """Open, deduplicate or close budget holds from one pre-flight.

    At most one hold is open per backend. Rechecking a backend while it remains
    held therefore preserves one record for the continuous hold window. The
    first later clear check closes that record and measures actual wall-clock
    elapsed time; the reported reset remains separate because a scheduled
    resumption can fire early or late.

    A check that changes a hold commits the aggregate it rewrote at once, under
    a subject naming the transition, so the shared file is not left dirty for
    whichever session next notices it. The commit is skipped, with the write
    left in the tree and the skip stated on stderr, while a merge is in
    progress. A checkout that cannot answer for the write is skipped the same
    way, so a hold check against a state directory outside git still records
    the transition.
    """
    moment = _parse_utc(checked_at)
    prepared = [dict(check) for check in checks]
    last: LedgerError | None = None
    for _attempt in range(max(1, attempts)):
        # The aggregate's own rows, never :func:`load`'s union: a hold is a fact
        # about the aggregate alone, and writing the union back would carry every
        # per-run file's row into it. The writer refuses that fold, and reading
        # the aggregate here is also what leaves its run rows byte-identical
        # across a hold transition.
        data, version = _load_aggregate(project, root)
        history = [dict(item) for item in data["holds"]]
        changed = False
        outcomes: list[dict[str, Any]] = []
        for check in prepared:
            backend = str(check.get("backend") or "").strip()
            if not backend:
                raise LedgerError("a hold check must name its backend")
            open_hold = next(
                (
                    item
                    for item in reversed(history)
                    if str(item.get("backend") or "") == backend
                    and not item.get("closed_at")
                ),
                None,
            )
            if bool(check.get("held")):
                if open_hold is not None:
                    outcomes.append({"action": "unchanged", "hold": dict(open_hold)})
                    continue
                state = check.get("state") or {}
                record = {
                    "hold_id": _hold_id(backend, checked_at, history),
                    "backend": backend,
                    "purpose": str(check.get("purpose") or "dispatch"),
                    "opened_at": checked_at,
                    "closed_at": None,
                    "held_seconds": None,
                    "utilisation_pct": state.get("utilisation_pct"),
                    "resets_at": state.get("resets_at"),
                    "effective_ceiling_pct": check.get("effective_ceiling_pct"),
                    "reason": str(check.get("reason") or ""),
                    "closed_by_purpose": None,
                    "resumption_fired": False,
                }
                history.append(record)
                outcomes.append({"action": "opened", "hold": dict(record)})
                changed = True
                continue
            if open_hold is None:
                continue
            opened = _parse_utc(open_hold.get("opened_at"))
            open_hold["closed_at"] = checked_at
            open_hold["held_seconds"] = max(0, int((moment - opened).total_seconds()))
            open_hold["closed_by_purpose"] = str(check.get("purpose") or "dispatch")
            open_hold["resumption_fired"] = bool(check.get("resumption_fired"))
            outcomes.append({"action": "closed", "hold": dict(open_hold)})
            changed = True

        if not changed:
            return {
                "path": str(ledger_path(project, root)),
                "version": version,
                "outcomes": outcomes,
            }
        data["holds"] = history
        try:
            new_version = write(project, data, version, root)
        except LedgerError as exc:
            last = exc
            _retry_backoff(_attempt)
            continue
        path = ledger_path(project, root)
        transitions = ", ".join(
            f"{'close' if item['action'] == 'closed' else 'open'} "
            f"{item['hold'].get('hold_id')}"
            for item in outcomes
            if item["action"] in {"opened", "closed"}
        )
        try:
            _commit_state_write(
                project,
                f"chore(holds): {transitions}",
                (
                    "Commit the hold transition immediately so this hold record "
                    "cannot ride an unrelated later change."
                ),
                path,
                "hold",
            )
        except LedgerError as exc:
            print(f"could not commit hold write {path}: {exc}", file=sys.stderr)
        return {
            "path": str(path),
            "version": new_version,
            "outcomes": outcomes,
        }
    raise LedgerError(
        f"ledger for {project!r} was rewritten on every hold-check attempt — {last}"
        if last
        else f"ledger for {project!r} could not record its hold check"
    )


def holds(project: str, root: str | Path | None = None) -> list[dict[str, Any]]:
    """Return every budget hold, in opening order."""
    return load(project, root)[0]["holds"]


# ── Measured effort ─────────────────────────────────────────────────────────


def declared_efforts(project: str, root: str | Path | None = None) -> dict[str, str]:
    """Map each live plan slug to the effort letter it declares.

    Read from the plans themselves rather than stored beside the runs, so the
    claim and the measurement cannot drift apart: re-sizing a plan changes what
    the next report compares against, with no second copy to update.
    """
    from reckon import _plan_html
    from reckon._store import _docs_dir_for_project
    from reckon.resources import iter_resources

    docs_dir = _docs_dir_for_project(project, root)
    if docs_dir is None:
        return {}
    efforts: dict[str, str] = {}
    for resource in iter_resources(
        docs_dir, project, include_archived=False, ignore_invalid=True
    ):
        if resource.type != "plan":
            continue
        effort = str(_plan_html.parse_meta(resource.path).get("effort") or "").strip()
        if effort:
            efforts[resource.slug] = effort
    return efforts


def _minutes(seconds: Any) -> float | None:
    try:
        return round(float(seconds) / 60.0, 1)
    except (TypeError, ValueError):
        return None


def effort_report(
    project: str,
    *,
    root: str | Path | None = None,
    declared: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Report measured worker-time per plan against that plan's declared effort.

    Effort is otherwise an asserted letter with no external referent. This makes
    it falsifiable by putting the measurement beside the claim and showing the
    spread, which is the quantity a single mean hides. Weighting and the unit
    migration belong to ``effort-calibration``; this only reports what was
    measured.
    """
    if declared is None:
        declared = declared_efforts(project, root)
    letters = {str(k): str(v) for k, v in declared.items()}
    by_plan: dict[str, dict[str, Any]] = {}
    excluded_scope_changed = 0
    excluded_stalled = 0
    excluded_unusable_completion = 0
    for record in runs(project, root):
        plan = str(record.get("plan") or "")
        row = by_plan.setdefault(
            plan,
            {
                "plan": plan,
                "declared_effort": letters.get(plan, ""),
                "runs": 0,
                "excluded_scope_changed": 0,
                "excluded_stalled": 0,
                "excluded_unusable_completion": 0,
                "excluded_contaminated": 0,
                "measured_minutes": 0.0,
                "durations": [],
            },
        )
        exclusion = measurement_exclusion_reason(record)
        if exclusion:
            row[f"excluded_{exclusion}"] += 1
            if exclusion == "scope_changed":
                excluded_scope_changed += 1
            elif exclusion == "stalled":
                excluded_stalled += 1
            else:
                excluded_unusable_completion += 1
            continue
        minutes = _minutes(record.get("worker_seconds"))
        if minutes is None:
            continue
        row["runs"] += 1
        row["durations"].append(minutes)

    plans = []
    for plan in sorted(by_plan):
        row = by_plan.pop(plan)
        durations = sorted(row.pop("durations"))
        row["measured_minutes"] = round(sum(durations), 1)
        row["mean_minutes"] = (
            round(sum(durations) / len(durations), 1) if durations else None
        )
        row["min_minutes"] = durations[0] if durations else None
        row["max_minutes"] = durations[-1] if durations else None
        row["spread_minutes"] = (
            round(durations[-1] - durations[0], 1) if durations else None
        )
        plans.append(row)

    by_effort: dict[str, dict[str, Any]] = {}
    for row in plans:
        letter = row["declared_effort"] or "unknown"
        bucket = by_effort.setdefault(
            letter,
            {"effort": letter, "plans": 0, "runs": 0, "minutes": [], "means": []},
        )
        bucket["plans"] += 1
        bucket["runs"] += row["runs"]
        if row["mean_minutes"] is not None:
            bucket["means"].append(row["mean_minutes"])
        if row["min_minutes"] is not None:
            bucket["minutes"].extend([row["min_minutes"], row["max_minutes"]])
    buckets = []
    for letter in sorted(by_effort):
        bucket = by_effort[letter]
        minutes = sorted(bucket.pop("minutes"))
        means = bucket.pop("means")
        bucket["mean_minutes"] = round(sum(means) / len(means), 1) if means else None
        bucket["spread_minutes"] = (
            round(minutes[-1] - minutes[0], 1) if minutes else None
        )
        buckets.append(bucket)

    return {
        "plans": plans,
        "by_effort": buckets,
        "excluded": {
            "scope_changed": excluded_scope_changed,
            "stalled": excluded_stalled,
            "unusable_completion": excluded_unusable_completion,
        },
        "excluded_scope_changed": excluded_scope_changed,
        "excluded_stalled": excluded_stalled,
        "excluded_unusable_completion": excluded_unusable_completion,
        "note": (
            "A run whose scope was widened mid-flight measures neither the "
            "estimate nor the worker, a stalled run measures idle wall time, "
            "and a run completed at promotion time has no surviving stream "
            "boundary. All are excluded from the "
            "measured columns and counted separately."
        ),
    }


def summary(
    project: str,
    *,
    root: str | Path | None = None,
    declared: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Summarise runs and holds without mixing their measurements."""
    data, version = load(project, root)
    records = data["runs"]
    hold_records = data["holds"]
    gates: dict[str, dict[str, int]] = {"live": {}, "shadow": {}}
    worker_gate = {
        "passed": 0,
        "work_rejected": 0,
        "pass_rate": None,
        "excluded": dict.fromkeys(FAILURE_CLASSIFICATIONS[1:], 0),
        "unclassified": 0,
    }
    suite_deltas = {"clean": 0, "refused": 0, "waived": 0, "unchanged": 0}
    run_kinds = {"live": 0, "shadow": 0}
    for record in records:
        lineage = record.get("lineage")
        kind = (
            "shadow"
            if isinstance(lineage, Mapping) and lineage.get("kind") == "shadow"
            else "live"
        )
        run_kinds[kind] += 1
        verdict = str(record.get("gate") or "unknown")
        gates[kind][verdict] = gates[kind].get(verdict, 0) + 1
        suite_delta = record.get("suite_delta")
        if kind == "live" and isinstance(suite_delta, Mapping):
            status = str(suite_delta.get("status") or "")
            if status in suite_deltas:
                suite_deltas[status] += 1
        if kind != "live":
            continue
        if verdict == "passed":
            worker_gate["passed"] += 1
        elif verdict == "failed":
            classification = str(record.get("failure_classification") or "")
            if classification == "work-rejected":
                worker_gate["work_rejected"] += 1
            elif classification in worker_gate["excluded"]:
                worker_gate["excluded"][classification] += 1
            else:
                worker_gate["unclassified"] += 1
    denominator = worker_gate["passed"] + worker_gate["work_rejected"]
    worker_gate["pass_rate"] = (
        round(worker_gate["passed"] / denominator, 4) if denominator else None
    )
    for classification, count in worker_gate["excluded"].items():
        worker_gate[f"excluded_{classification.replace('-', '_')}"] = count
    sessions = sum(
        1
        for entry in data["members"]
        if entry.get("session_id") or entry.get("sessions")
    )
    from reckon.crew.runs import list_live

    suite_deltas["refused"] += sum(
        1
        for pointer in list_live(project=project)
        if isinstance(pointer.get("suite_delta_refusal"), Mapping)
    )
    return {
        "version": version,
        "path": str(ledger_path(project, root)),
        "members": len(data["members"]),
        "members_with_session": sessions,
        "runs": len(records),
        "run_kinds": run_kinds,
        "holds": len(hold_records),
        "open_holds": sum(1 for record in hold_records if not record.get("closed_at")),
        "total_held_seconds": sum(
            int(record.get("held_seconds") or 0) for record in hold_records
        ),
        "gates": {kind: dict(sorted(counts.items())) for kind, counts in gates.items()},
        "worker_gate": worker_gate,
        "suite_deltas": suite_deltas,
        "plans": sorted({str(record.get("plan") or "") for record in records}),
        "effort": effort_report(project, root=root, declared=declared),
    }


# The run-record composition family was moved out of this module into its own
# concern module. Every name below is re-exported so a caller that does not
# move keeps importing it from here; that module imports its shared constants
# and helpers back from this one at its end, so the re-export sits last here,
# after every definition it reads is bound.

from reckon.run_record import (  # noqa: E402, F401
    _RESOLVED_FLIGHT_CACHE,
    OUTSIDE_SCOPE_PATHS,
    RETIRED_OUTSIDE_SCOPE_PATHS,
    _clone_report,
    _flight_layer_key,
    _label_unmetered_cost,
    _resolved_flight_config,
    backend_budget_group,
    build_record,
    duration_measurement_state,
    is_subscription_backend,
    is_unmetered_backend,
    measured_throughput,
    measurement_exclusion_reason,
    normalize_outside_scope_paths,
    per_run_budget,
    promoted_budget_fallback,
    promoted_pace_row,
    promoted_picker_selection,
    promoted_route_mode,
    underivable_duration,
    unmeasured_clone_report,
)
