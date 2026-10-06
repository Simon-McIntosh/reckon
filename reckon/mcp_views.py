"""Progressive, human-readable response views for Reckon's MCP tools."""

from __future__ import annotations

import base64
import copy
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from reckon import _backends, ledger
from reckon import budget as budget_module
from reckon._plan_html import (
    RECKON_ATTRIBUTE,
    machinery_kind,
    plan_headings,
    section_prose,
    section_record_id,
)
from reckon._timestamps import parse_utc
from reckon.crew import lane_document as lane_document_module
from reckon.crew import rollout as rollout_module
from reckon.crew import staleness as staleness_module
from reckon.doccheck import lifecycle_staleness, modified_age_days
from reckon.evidence import EXECUTABLE_SECTION_ROLES
from reckon.lifecycle import (
    COMPLETED_STATUSES,
    TERMINAL_STATUSES,
    effective_status,
    is_section_scoped,
    unpassed_gate_blockers,
    unresolved_dependencies,
)
from reckon.project_state import _natural_identifier_key

VIEW_NAMES = frozenset(
    {"summary", "detail", "history", "version", "raw", "schema", "section"}
)
RESOURCE_TYPES = frozenset(
    {
        "plan",
        "research",
        "evidence",
        "sprint",
        "milestone",
        "blocker",
        "timeline",
        "project",
        "review",
        "audit",
    }
)
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
RESPONSE_SCHEMA_VERSION = 2
MAX_SELECTOR_LENGTH = 128
MAX_CURSOR_LENGTH = 256
MAX_ERROR_TEXT_LENGTH = 512
MAX_ERROR_COLLECTION_ITEMS = 25
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Quota readings are whole percentages, so a 94% threshold leaves six
# distinguishable percentage points before exhaustion rather than treating
# quantisation noise as a dispatchable margin.
AT_RISK_USED_PERCENT = 94
# Forty-seven minutes is less than one sixth of the shortest reported
# 300-minute window; older evidence cannot support a claim about serving now.
QUOTA_READING_STALE_AFTER = timedelta(minutes=47)

AMPLE_SERVING_STATE = "will_serve"
AT_RISK_SERVING_STATE = "at_risk"
STALE_SERVING_STATE = "stale"
EXHAUSTED_SERVING_STATE = "exhausted"
UNMEASURED = "unmeasured"
BORROWED = "borrowed"

_PROBE_CACHE_LOCK = threading.Lock()
_PROBE_CACHE: dict[tuple[int, str], tuple[object, datetime, dict[str, Any]]] = {}
_ATTEMPT_CACHE_LOCK = threading.RLock()
_ATTEMPT_CACHE: dict[tuple[Any, ...], tuple[object, Any]] = {}
_ATTEMPT_CACHE_LIMIT = 64


def _unmeasured_reason(value: object) -> str | None:
    """Return an explicit receipt gap reason, or ``None`` for a measurement."""

    if isinstance(value, rollout_module.Unmeasured):
        return str(value.value)
    return None


def _receipt_observed_at(run: Mapping[str, Any]) -> str | None:
    """Return the closest durable timestamp to the selected receipt reading.

    The field list is the budget module's, because this stamp dates the same
    reading the pace reader dates: one helper decides which field a run's
    observation and both surfaces read it, so they cannot disagree about a
    record that carries only one of them.
    """

    return budget_module.run_observed_stamp(run)


_EXPLICIT_ZONE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2}(?::\d{2})?)$")


def _stated_day(text: str, *, truncated: bool) -> date | None:
    """The calendar day a stamp states, read as a day rather than an instant.

    A ``truncated`` stamp carries the day at its front and whatever follows; a
    stamp that is not truncated must state nothing but the day. Text that
    states no day returns None, so a comparison against it is not made.
    """

    candidate = text[:10] if truncated else text
    if not truncated and len(candidate) > 10:
        return None
    parsed = parse_utc(candidate)
    return parsed.date() if parsed is not None else None


def _parsed_observation(value: str | None) -> datetime | None:
    """Parse a receipt observation stamp without treating malformed text as fresh.

    The stamp must state a zone: a receipt is only fresh against a moment it
    names, so a value without one is refused. A value that is not text is not a
    receipt stamp at all and is refused before any parsing.
    """

    if not value:
        return None
    if not _EXPLICIT_ZONE.search(value):
        return None
    return parse_utc(value)


def _serving_state(
    used_percent: object,
    observed_at: str | None,
    composed_at: str,
) -> str:
    """State only what a measured quota reading supports at composition time."""

    if not isinstance(used_percent, (int, float)) or isinstance(used_percent, bool):
        return UNMEASURED
    observed = _parsed_observation(observed_at)
    composed = _parsed_observation(composed_at)
    if observed is None or composed is None:
        return UNMEASURED
    if composed - observed > QUOTA_READING_STALE_AFTER:
        return STALE_SERVING_STATE
    if used_percent >= 100:
        return EXHAUSTED_SERVING_STATE
    if used_percent >= AT_RISK_USED_PERCENT:
        return AT_RISK_SERVING_STATE
    return AMPLE_SERVING_STATE


def _observation_age_seconds(observed_at: str | None, composed_at: str) -> int | str:
    observed = _parsed_observation(observed_at)
    composed = _parsed_observation(composed_at)
    if observed is None or composed is None:
        return UNMEASURED
    return max(0, int((composed - observed).total_seconds()))


def _backend_from_run(run: Mapping[str, Any]) -> str:
    """Read the configured backend identity from either durable run shape."""

    backend = str(run.get("backend") or "").strip()
    if backend:
        return backend
    agent = run.get("agent")
    if isinstance(agent, Mapping):
        return str(agent.get("backend") or "").strip()
    return ""


def _latest_backend_runs(
    runs: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Keep the newest session-bearing run for every configured backend."""

    latest: dict[str, Mapping[str, Any]] = {}
    latest_keys: dict[str, tuple[str, str]] = {}
    for run in runs:
        backend = _backend_from_run(run)
        session_id = str(run.get("session_id") or "").strip()
        if not backend or not session_id:
            continue
        ordering = (
            _receipt_observed_at(run) or "",
            str(run.get("run_id") or ""),
        )
        if ordering >= latest_keys.get(backend, ("", "")):
            latest[backend] = run
            latest_keys[backend] = ordering
    return latest


def _quota_readings(receipt: object) -> Mapping[int, object] | object:
    """Read all keyed quota horizons, retaining legacy single-window support."""

    readings = getattr(receipt, "quota_readings", None)
    if readings is not None:
        return readings
    window = getattr(receipt, "quota_window_minutes", None)
    reason = _unmeasured_reason(window)
    if reason is not None:
        return window
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        return rollout_module.Unmeasured.NO_RATE_LIMIT_VALUE
    return {
        window: {
            "window_minutes": window,
            "used_percent": getattr(receipt, "quota_used_percent", None),
            "resets_at": getattr(receipt, "quota_resets_at", None),
        }
    }


def _reading_value(reading: object, key: str) -> object:
    if isinstance(reading, Mapping):
        return reading.get(key)
    return getattr(reading, key, None)


def _measured_or_marker(value: object) -> tuple[object, str | None]:
    reason = _unmeasured_reason(value)
    if reason is not None:
        return "unmeasured", reason
    if value is None:
        return "unmeasured", "no_rate_limit_value"
    return value, None


def _notional_cost_reading(receipt: object) -> tuple[object, str | None]:
    """Serialize the receipt's notional spend and its unmeasured reason.

    The figure is the receipt's own, derived from declared rates in
    ``rollout``; the view never recomputes it.  A marked figure keeps the
    readable absence string in its field and the marker's reason in the
    unmeasured map, so an unpriced lane is distinguishable from a run that
    genuinely cost nothing.  An injected reader that carries no figure
    resolves to the no-model marker rather than fabricating one.
    """
    return _measured_or_marker(
        getattr(
            receipt,
            "notional_cost_usd",
            rollout_module.Unmeasured.NO_MODEL_IDENTIFIER,
        )
    )


def _rate_basis_reading(receipt: object) -> tuple[object, str | None]:
    """Serialize the receipt's rate basis, its date rendered as ISO text."""
    value = getattr(
        receipt, "rate_basis", rollout_module.Unmeasured.NO_MODEL_IDENTIFIER
    )
    reason = _unmeasured_reason(value)
    if reason is not None:
        return "unmeasured", reason
    return {
        "model_identifier": value.model_identifier,
        "input_per_million": value.input_per_million,
        "output_per_million": value.output_per_million,
        "as_of": value.as_of.isoformat(),
    }, None


def _own_lane_figure(readings: object) -> float | None:
    """The figure of the lane's shortest keyed horizon, or ``None``.

    The shortest horizon is the clock that fills first, so it is the lane's
    binding reading. A reading whose keys or figures do not resolve as a
    window and a number yields no figure rather than a guessed one.
    """
    if not isinstance(readings, Mapping) or not readings:
        return None
    keyed: list[tuple[int, float]] = []
    for raw_window, reading in readings.items():
        used = _reading_value(reading, "used_percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        try:
            length = int(raw_window)
        except (TypeError, ValueError):
            continue
        keyed.append((length, float(used)))
    if not keyed:
        return None
    return min(keyed, key=lambda window: window[0])[1]


def _lane_position(observed_at: str | None) -> staleness_module.Reading:
    """Adapt one lane's observation stamp to the staleness reader's input.

    The reader is asked one thing for this lane -- whether its reading is
    inside its shelf life -- and the observation stamp is the whole of that
    decision.  No figure and no serving state are composed here: the lane's own
    figure is reported from its receipt rows, and where a re-query answers, the
    figure and state shown are the probe adapter's, so anything else built at
    this seam would be discarded unread.
    """
    return staleness_module.Reading(
        used_percent=None,
        observed_at=_parsed_observation(observed_at),
    )


def _declared_probe_reading(
    probe: Mapping[str, Any] | None,
    *,
    command: str | None,
    composed_at: str,
) -> staleness_module.Probe:
    """Bind the lane's own probe to the staleness reader's seam.

    The answer is the reading this view already holds for the lane's command:
    one probe read serves every lane that declares it, so a re-query consults
    the probe the allocation read rather than asking it a second time inside
    the composition.  The command is what ties the probe to the lane, so a
    second declared pool on that same command does not disqualify it: the
    figure a re-query reports is the one the lane's own command answered with,
    and refusing it here would leave every lane of a shared command reporting
    only its stale receipt.  A probe that did not answer, and a reading without
    a figure, are no answer at all -- both leave the lane reporting its own
    reading as unresolved.
    """

    def probe_reading() -> staleness_module.Reading | None:
        if command is None or not isinstance(probe, Mapping):
            return None
        if probe.get("status") != "answered":
            return None
        figure = _own_lane_figure(probe.get("quota_windows"))
        if figure is None:
            return None
        observed = str(probe.get("observed_at") or "")
        return staleness_module.Reading(
            used_percent=figure,
            observed_at=_parsed_observation(observed),
            source="probe",
            serving_state=_serving_state(figure, observed, composed_at),
        )

    return probe_reading


def _quota_rows(
    readings: Mapping[int, object] | object,
    observed_at: str | None,
    composed_at: str,
    *,
    source: str,
    serving_state: str | None = None,
    serving_state_reason: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    reason = _unmeasured_reason(readings)
    if reason is not None:
        return [], reason
    if not isinstance(readings, Mapping) or not readings:
        return [], "no_rate_limit_value"

    rows: list[dict[str, Any]] = []
    for raw_window, reading in sorted(readings.items(), key=lambda item: int(item[0])):
        window = _reading_value(reading, "window_minutes")
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            window = raw_window
        used, used_reason = _measured_or_marker(_reading_value(reading, "used_percent"))
        reset, reset_reason = _measured_or_marker(_reading_value(reading, "resets_at"))
        remaining: object = "unmeasured"
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            remaining = max(0, 100 - used)
        row: dict[str, Any] = {
            "window_minutes": int(window),
            "used_percent": used,
            "remaining_percent": remaining,
            "resets_at": reset,
            "observed_at": observed_at or UNMEASURED,
            "age_seconds": _observation_age_seconds(observed_at, composed_at),
            "source": source,
            "serving_state": (
                _serving_state(used, observed_at, composed_at)
                if serving_state is None
                else serving_state
            ),
        }
        reasons = {
            key: value
            for key, value in (
                ("used_percent", used_reason),
                ("remaining_percent", used_reason),
                ("resets_at", reset_reason),
                ("observed_at", None if observed_at else "no_observation_time"),
                ("age_seconds", None if observed_at else "no_observation_time"),
                (
                    "serving_state",
                    serving_state_reason
                    if serving_state_reason is not None
                    else used_reason
                    if used_reason is not None
                    else "no_observation_time"
                    if _parsed_observation(observed_at) is None
                    else None,
                ),
            )
            if value is not None
        }
        if reasons:
            row["unmeasured"] = reasons
        rows.append(row)
    return rows, None


def _run_budget_probe(
    backend_name: str,
    settings: Mapping[str, Any],
    *,
    cache_path: str | Path | None = None,
    now: datetime | None = None,
) -> Mapping[str, Any] | None:
    return _backends.probe_budget(
        backend_name=backend_name, backend=settings, cache_path=cache_path, now=now
    )


def _cached_path_probe_reader(
    cache_path: str | Path | None,
    *,
    now: datetime | None = None,
) -> Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None]:
    """Bind an account-cache path onto the probe reader's two-argument seam.

    The bound moment fixes the cached stamp's age against the same instant the
    view composes, so a stamped figure's age is deterministic under a pinned
    fixture instead of drifting with wall-clock time.
    """

    def probe_reader(
        backend_name: str, settings: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        return _run_budget_probe(backend_name, settings, cache_path=cache_path, now=now)

    return probe_reader


def _owns_account_surface_reading(dialect: _backends.Dialect) -> bool:
    """Whether a dialect reads remaining headroom without the probe exchange.

    The base dialect answers None on both surfaces, so overriding
    ``read_account_surface`` is the declaration that this dialect owns the whole
    read — credential, transport and parse. Inspecting the class is a statement
    about the dialect, not a call into it: declaration never runs the reading.
    """
    return (
        type(dialect).read_account_surface is not _backends.Dialect.read_account_surface
    )


def _declared_probe_command(settings: Mapping[str, Any]) -> tuple[str | None, str]:
    command = str(settings.get("command") or "").strip()
    if not command:
        return None, "backend declares no probe command"
    try:
        dialect = _backends.dialect_for(settings)
        owns_surface = _owns_account_surface_reading(dialect)
        probe = None if owns_surface else dialect.budget_probe(command)
    except (_backends.BackendError, OSError, ValueError) as exc:
        return None, f"dialect declares no quota probe — {exc}"
    if owns_surface:
        return command, "account-surface probe declared"
    if probe is None:
        return None, "dialect declares no quota probe"
    return command, "quota probe declared"


def _cached_probe_reading(
    command: str,
    backend_name: str,
    settings: Mapping[str, Any],
    *,
    reader: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None],
    observed_at: str,
    cache_seconds: float,
) -> dict[str, Any]:
    moment = _parsed_observation(observed_at) or datetime.now(UTC)
    cache_key = (id(reader), command)
    with _PROBE_CACHE_LOCK:
        cached = _PROBE_CACHE.get(cache_key)
        if cached is not None and cached[0] is reader and cache_seconds > 0:
            age = (moment - cached[1]).total_seconds()
            if 0 <= age <= cache_seconds:
                return {**cached[2], "cached": True}

    try:
        answer = reader(backend_name, settings)
    except Exception as exc:  # noqa: BLE001 - a view survives any probe failure
        observation = {
            "status": "unavailable",
            "observed_at": observed_at,
            "detail": f"probe did not answer — {exc}",
            "quota_windows": {},
            "cached": False,
        }
    else:
        answer_map = answer if isinstance(answer, Mapping) else None
        windows = answer_map.get("quota_windows") if answer_map else None
        observed = observed_at
        provenance: dict[str, Any] = {}
        if answer_map is not None and _backends.ACCOUNT_CACHE_STAMP in answer_map:
            # A cache-sourced reading: the block carries no quota windows, so
            # synthesize its single window from the scalar fields, and observe
            # the reading at the copy's own fetch stamp so the copy's age
            # travels beside the figure instead of being dropped.
            provenance = {
                _backends.ACCOUNT_CACHE_STAMP: answer_map[_backends.ACCOUNT_CACHE_STAMP]
            }
            if answer_map.get("fetch_age_seconds") is not None:
                provenance["fetch_age_seconds"] = answer_map["fetch_age_seconds"]
            stamp = _parsed_observation(str(answer_map[_backends.ACCOUNT_CACHE_STAMP]))
            if stamp is not None:
                observed = str(answer_map[_backends.ACCOUNT_CACHE_STAMP])
            if windows is None and answer_map.get("headroom") == "known":
                window_minutes = answer_map.get("rate_limit_period_minutes")
                used = answer_map.get("utilisation_pct")
                resets_at = answer_map.get("resets_at")
                if (
                    isinstance(window_minutes, int)
                    and not isinstance(window_minutes, bool)
                    and window_minutes > 0
                    and isinstance(used, (int, float))
                    and not isinstance(used, bool)
                    and resets_at is not None
                ):
                    windows = {
                        window_minutes: {
                            "window_minutes": window_minutes,
                            "used_percent": used,
                            "resets_at": resets_at,
                        }
                    }
        if isinstance(windows, Mapping) and windows:
            observation = {
                "status": "answered",
                "observed_at": observed,
                "detail": str(answer_map.get("detail") or "quota probe answered")
                if answer_map
                else "quota probe answered",
                "quota_windows": windows,
                "cached": False,
                **provenance,
            }
        else:
            detail = (
                str(answer_map.get("detail") or "probe returned no quota windows")
                if answer_map
                else "probe returned no result"
            )
            observation = {
                "status": "unavailable",
                "observed_at": observed_at,
                "detail": f"probe did not answer — {detail}",
                "quota_windows": {},
                "cached": False,
            }

    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[cache_key] = (reader, moment, observation)
    return observation


def _reset_moment(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return _parsed_observation(str(value)) if isinstance(value, str) else None


def _quota_signature(
    readings: Mapping[int, object] | object,
) -> dict[int, datetime] | None:
    if not isinstance(readings, Mapping) or not readings:
        return None
    signature: dict[int, datetime] = {}
    for raw_window, reading in readings.items():
        window = _reading_value(reading, "window_minutes")
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            window = raw_window
        try:
            window_minutes = int(window)
        except (TypeError, ValueError):
            return None
        reset = _reset_moment(_reading_value(reading, "resets_at"))
        if window_minutes <= 0 or reset is None:
            return None
        signature[window_minutes] = reset
    return signature


def _probe_describes_receipt(
    probe_readings: Mapping[int, object] | object,
    receipt_readings: Mapping[int, object] | object,
) -> bool:
    """Require both window lengths and reset schedules to identify one quota."""

    probe_signature = _quota_signature(probe_readings)
    receipt_signature = _quota_signature(receipt_readings)
    return probe_signature is not None and probe_signature == receipt_signature


def _declared_budget_group(settings: Mapping[str, Any]) -> str | None:
    """Return the declared budget group naming this backend, or ``None``.

    A group names by configuration the backends that draw on the same account
    quota.  Identical probe windows or reset times can never serve this role:
    two backends probed identically prove only that they were probed the same
    way, never that they share a budget.  An unset or empty value leaves the
    backend ungrouped, its singleton membership carrying nothing.
    """
    value = settings.get("budget_group")
    if not isinstance(value, str):
        return None
    pool = value.strip()
    return pool or None


def _command_groups(
    command: str,
    command_by_backend: Mapping[str, str | None],
    group_by_backend: Mapping[str, str | None],
) -> set[str | None]:
    """Declared budget groups among the backends that all run one command."""
    return {
        group_by_backend.get(backend_name)
        for backend_name, declared in command_by_backend.items()
        if declared == command
    }


def _probe_is_lane_owned(
    command: str,
    lane_group: str | None,
    command_by_backend: Mapping[str, str | None],
    group_by_backend: Mapping[str, str | None],
) -> bool:
    """Whether the lane may carry a command's probe reading as its own.

    The account probe is one shared reading wherever the same command runs, so
    carrying it as owned requires every backend that declares that command to
    declare the same budget group as the lane.  Where no group is declared
    anywhere the single-account default stands: an undeclared grouping proves
    nothing about ownership, so no borrowing is asserted against it.
    Any divergence in declared groups — an ungrouped sibling beside a grouped
    lane, or two distinct groups — means the reading belongs to an account
    this lane is not declared to share, and the figure must not be shown as
    the lane's own.
    """
    groups = _command_groups(command, command_by_backend, group_by_backend)
    if len(groups) == 1:
        single = next(iter(groups))
        return single is None or single == lane_group
    return lane_group is not None and groups == {lane_group}


def _lane_document_fields(
    settings: Mapping[str, Any], composition_time: str
) -> dict[str, Any]:
    """Return the declared lane document's reading for one lanes-view row."""
    declared = settings.get("lane_document")
    if not declared:
        return {}
    observed = _parsed_observation(composition_time)
    report = lane_document_module.read_lane_document_file(declared, now=observed)
    answered = not report.get("malformed") and any(
        report.get(name) != lane_document_module.UNKNOWN
        for name in ("state", "headroom", "observed_at", "admission_headroom")
    )
    detail = str(report.get("detail") or "").strip()
    return {
        "lane_document": str(declared),
        "lane_state": report["state"],
        "lane_observed_at": report["observed_at"],
        "lane_age_seconds": report["age_seconds"],
        "lane_stale": report["stale"],
        "lane_detail": detail,
        "headroom": report["headroom"],
        "engine_headroom": report["engine_headroom"],
        "admission_headroom": report["admission_headroom"],
        "router_generation_gate": report["router_generation_gate"],
        "admission_verdict": report["admission_verdict"],
        "admission_reason": report["admission_reason"],
        "lane_probe_status": "answered" if answered else "unavailable",
        "lane_probe_detail": detail or "lane document answered",
    }


def _attach_lane_document(
    lane: dict[str, Any], fields: Mapping[str, Any]
) -> dict[str, Any]:
    """Attach a lane reading and let it answer an otherwise absent probe."""
    if not fields:
        return lane
    lane.update(fields)
    if lane.get("probe_status") == "not_declared":
        lane["probe_status"] = fields["lane_probe_status"]
        lane["probe_detail"] = fields["lane_probe_detail"]
    return lane


def crew_lanes_view(
    config: Mapping[str, Any],
    runs: Iterable[Mapping[str, Any]],
    *,
    receipt_reader: Callable[[str], object] | None = None,
    probe_reader: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None]
    | None = None,
    composed_at: str | None = None,
    account_cache_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compose endpoint availability without selecting or ranking a backend.

    Each lane carries its declared ``budget_group``.  A declared local lane
    document contributes its gate-aware headroom and admission verdict.  An
    otherwise absent probe reports that document's answer instead of hiding
    the reading as ``not_declared``.  An account probe reading is
    adopted as the lane's own only when every backend declaring that probe's
    command declares the same budget group; otherwise the lane keeps its own
    receipt reading, and a lane with no reading of its own renders ``borrowed``
    rather than carrying the shared figure.  The top-level ``budget_groups``
    map groups backends by the declared key alone, never by shared window or
    reset times.
    """

    composition_time = composed_at or datetime.now(UTC).isoformat().replace(
        "+00:00", "Z"
    )
    # One moment serves the whole composition, so a shelf-life comparison and a
    # probe stamp's age resolve against the same instant and cannot disagree.
    composition_moment = _parsed_observation(composition_time) or datetime.now(UTC)
    latest = _latest_backend_runs(runs)
    backend_config = config.get("backends")
    configured = backend_config if isinstance(backend_config, Mapping) else {}
    if probe_reader is not None:
        read_probe = probe_reader
    else:
        # The default probe reader consults the on-disk account cache only when
        # the view is handed its path; an injected reader keeps its own seam.
        # The cached stamp's age resolves against the composition instant so a
        # pinned fixture and a determined fetch age stay in the same frame.
        read_probe = (
            _cached_path_probe_reader(account_cache_path, now=composition_moment)
            if account_cache_path is not None
            else _run_budget_probe
        )
    cache_seconds = float(
        budget_module.policy(config).get("availability_probe_cache_seconds", 0)
    )
    command_by_backend: dict[str, str | None] = {}
    probe_detail_by_backend: dict[str, str] = {}
    probe_by_command: dict[str, dict[str, Any]] = {}
    group_by_backend: dict[str, str | None] = {}
    for backend, settings_value in sorted(
        configured.items(), key=lambda item: str(item[0])
    ):
        backend_name = str(backend)
        settings = settings_value if isinstance(settings_value, Mapping) else {}
        group_by_backend[backend_name] = _declared_budget_group(settings)
        if ledger.is_unmetered_backend(backend_name):
            continue
        command, detail = _declared_probe_command(settings)
        command_by_backend[backend_name] = command
        probe_detail_by_backend[backend_name] = detail
        if command is not None and command not in probe_by_command:
            probe_by_command[command] = _cached_probe_reading(
                command,
                backend_name,
                settings,
                reader=read_probe,
                observed_at=composition_time,
                cache_seconds=cache_seconds,
            )
    budget_groups: dict[str, list[str]] = {}
    for backend_name, group in sorted(group_by_backend.items()):
        if group is not None:
            budget_groups.setdefault(group, []).append(backend_name)
    lanes: list[dict[str, Any]] = []

    for backend, settings_value in sorted(
        configured.items(), key=lambda item: str(item[0])
    ):
        backend_name = str(backend)
        settings = settings_value if isinstance(settings_value, Mapping) else {}
        lane_document_fields = _lane_document_fields(settings, composition_time)
        run = latest.get(backend_name)
        if run is None:
            lanes.append(
                _attach_lane_document(
                    {
                        "backend": backend_name,
                        "alias": settings.get("alias"),
                        "model": settings.get("model"),
                        "budget_group": group_by_backend.get(backend_name),
                        "receipt_state": "unused",
                        "observed_at": "unmeasured",
                        "effective_context_window": "unmeasured",
                        "quota_windows": [],
                        "unmeasured": {
                            "observed_at": "unused",
                            "effective_context_window": "unused",
                            "quota_windows": "unused",
                        },
                        "quota_source": UNMEASURED,
                        "probe_status": (
                            probe_by_command[command_by_backend[backend_name]]["status"]
                            if command_by_backend.get(backend_name) is not None
                            else "not_declared"
                        ),
                    },
                    lane_document_fields,
                )
            )
            continue

        session_id = str(run.get("session_id"))
        if ledger.is_unmetered_backend(backend_name):
            lanes.append(
                _attach_lane_document(
                    {
                        "backend": backend_name,
                        "alias": settings.get("alias"),
                        "model": settings.get("model"),
                        "budget_group": group_by_backend.get(backend_name),
                        "receipt_state": "unmetered",
                        "observed_at": UNMEASURED,
                        "effective_context_window": UNMEASURED,
                        "quota_windows": [],
                        "unmeasured": {
                            "receipt": "unmetered",
                            "observed_at": "unmetered",
                            "effective_context_window": "unmetered",
                            "quota_windows": "unmetered",
                        },
                        "quota_source": UNMEASURED,
                        "probe_status": "not_declared",
                    },
                    lane_document_fields,
                )
            )
            continue

        # The served model passes through from the backend configuration the
        # view already holds, so the notional figure resolves without any new
        # identity lookup.  An injected reader keeps its one-argument seam; only
        # the production reader prices the receipt.
        if receipt_reader is not None:
            receipt = receipt_reader(session_id)
        else:
            receipt = rollout_module.read_rollout_receipt(
                session_id,
                model_identifier=str(settings.get("model") or "").strip() or None,
            )
        observed_at = _receipt_observed_at(run)
        context_value, context_reason = _measured_or_marker(
            getattr(receipt, "model_context_window", None)
        )
        readings = _quota_readings(receipt)
        receipt_reason = _unmeasured_reason(
            getattr(receipt, "model_context_window", None)
        )
        if receipt_reason not in {"missing_rollout", "unreadable_rollout"}:
            receipt_reason = _unmeasured_reason(readings)
        unreadable = receipt_reason in {"missing_rollout", "unreadable_rollout"}
        receipt_observed_at = None if unreadable else observed_at
        selected_readings = readings
        selected_observed_at = receipt_observed_at
        quota_source = "receipt"
        command = command_by_backend.get(backend_name)
        probe = probe_by_command.get(command) if command is not None else None
        lane_group = group_by_backend.get(backend_name)
        borrowed = False
        if probe is None:
            probe_status = "not_declared"
            probe_detail = probe_detail_by_backend.get(
                backend_name, "dialect declares no quota probe"
            )
            probe_cached = False
        elif probe["status"] != "answered":
            probe_status = "unavailable"
            probe_detail = str(probe["detail"])
            probe_cached = bool(probe["cached"])
        elif _probe_describes_receipt(probe["quota_windows"], readings):
            if _probe_is_lane_owned(
                command, lane_group, command_by_backend, group_by_backend
            ):
                probe_status = "answered"
                probe_detail = str(probe["detail"])
                probe_cached = bool(probe["cached"])
                selected_readings = probe["quota_windows"]
                selected_observed_at = str(probe["observed_at"])
                quota_source = "probe"
            else:
                # The probe matched and is shared with a sibling in a different
                # declared pool, so its reading belongs to an account this lane
                # is not declared to draw on.  Keep the lane's own measurement
                # rather than adopting a figure it does not own.
                probe_status = "answered"
                probe_detail = (
                    "probe answered, but its command is shared with a pool this lane "
                    "does not declare, so the probe reading is not adopted"
                )
                probe_cached = bool(probe["cached"])
        else:
            probe_status = "unmatched"
            probe_cached = bool(probe["cached"])
            if not (isinstance(readings, Mapping) and readings) and not (
                _probe_is_lane_owned(
                    command, lane_group, command_by_backend, group_by_backend
                )
            ):
                # The only figure this lane could show is the shared account
                # probe, and the declared pools put that account outside this
                # lane's group: render borrowed rather than carrying a sibling's
                # number as its own.
                probe_detail = (
                    "probe answered, but its command is shared with a pool this lane "
                    "does not declare, and this lane has no reading of its own"
                )
                borrowed = True
            else:
                probe_detail = (
                    "probe answered, but its quota windows do not match this lane's "
                    "receipt"
                )
        # A reading older than its configured shelf life is not reported as a
        # position: the lane's probe is asked for a fresh figure and the fresh
        # figure is what the lane shows.  The probe consulted is the reading
        # this composition already holds for the lane's command, so the answer
        # costs no second probe invocation however many lanes draw on it.
        requeried = False
        requery_failed = False
        if isinstance(selected_readings, Mapping) and selected_readings:
            reported = staleness_module.resolve_configured_reading(
                _lane_position(selected_observed_at),
                probe=_declared_probe_reading(
                    probe,
                    command=command,
                    composed_at=composition_time,
                ),
                config=config,
                now=composition_moment,
            )
            if reported.requeried:
                requeried = True
                if isinstance(probe, Mapping) and (
                    reported.serving_state != staleness_module.SERVING_STATE_UNKNOWN
                ):
                    # The probe's rows are now the lane's rows, so the lane's
                    # probe fields travel with them: a row showing the probe's
                    # fresh figures under the probe's source while still
                    # reporting the probe unmatched would contradict itself,
                    # and the stale row it replaces must not be described
                    # either.
                    probe_status = "answered"
                    probe_detail = str(probe["detail"])
                    selected_readings = probe["quota_windows"]
                    selected_observed_at = str(probe["observed_at"])
                    quota_source = "probe"
                else:
                    # Nothing could answer for this lane.  The old figure is
                    # kept with the age that disqualifies it and a serving
                    # state of unknown, so the failure reads as neither
                    # headroom nor exhaustion.
                    requery_failed = True
        quota_rows, quota_reason = _quota_rows(
            selected_readings,
            selected_observed_at,
            composition_time,
            source=quota_source,
            serving_state=(
                staleness_module.SERVING_STATE_UNKNOWN if requery_failed else None
            ),
            serving_state_reason="requery_did_not_answer" if requery_failed else None,
        )
        if borrowed:
            quota_reason = "shared_command_probe_not_owned"
        notional_cost, notional_reason = _notional_cost_reading(receipt)
        basis, basis_reason = _rate_basis_reading(receipt)
        lane: dict[str, Any] = {
            "backend": backend_name,
            "alias": settings.get("alias"),
            "model": settings.get("model"),
            "budget_group": lane_group,
            "receipt_state": "unreadable" if unreadable else "readable",
            "observed_at": selected_observed_at or UNMEASURED,
            "effective_context_window": context_value,
            "quota_windows": quota_rows,
            "quota_source": (
                quota_source if quota_rows else BORROWED if borrowed else UNMEASURED
            ),
            "probe_status": probe_status,
            "probe_detail": probe_detail,
            "probe_cached": probe_cached,
            "requeried": requeried,
            "notional_cost_usd": notional_cost,
            "rate_basis": basis,
        }
        if quota_source == "probe" and isinstance(probe, Mapping):
            # A probe figure is shown as the lane's own, so the reading's
            # provenance travels with it: the account-cache stamp and its fetch
            # age sit beside the figure the lane now renders.
            fetch_age = probe.get("fetch_age_seconds")
            fetch_stamp = probe.get(_backends.ACCOUNT_CACHE_STAMP)
            if fetch_age is not None:
                lane["probe_fetch_age_seconds"] = fetch_age
            if fetch_stamp is not None:
                lane["probe_fetch_stamp"] = fetch_stamp
        unmeasured = {
            key: value
            for key, value in (
                ("receipt", receipt_reason if unreadable else None),
                (
                    "observed_at",
                    "no_receipt_observation"
                    if unreadable
                    else None
                    if receipt_observed_at
                    else "no_observation_time",
                ),
                ("effective_context_window", context_reason),
                ("quota_windows", quota_reason),
                ("notional_cost_usd", notional_reason),
                ("rate_basis", basis_reason),
            )
            if value is not None
        }
        if unmeasured:
            lane["unmeasured"] = unmeasured
        lanes.append(_attach_lane_document(lane, lane_document_fields))

    return {
        "composed_at": composition_time,
        "lanes": lanes,
        "budget_groups": budget_groups,
    }


def sprint_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise live sprint-member lifecycle state without storing it."""

    rows = [item for item in items if isinstance(item, dict)]
    counts: dict[str, int] = {}
    current_work: list[dict[str, Any]] = []
    implementations: list[float] = []
    for item in rows:
        status = str(item.get("effective_status") or item.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
        impl = float(item.get("impl", 0.0) or 0.0)
        implementations.append(impl)
        if status not in TERMINAL_STATUSES and 0.0 < impl < 1.0:
            current_work.append(
                {
                    key: item[key]
                    for key in ("slug", "title", "effective_status", "impl")
                    if item.get(key) is not None
                }
            )
    return {
        "item_count": len(rows),
        "by_effective_status": counts,
        "mean_impl": round(sum(implementations) / len(rows), 4) if rows else 0.0,
        "current_work": current_work,
    }


def sprint_state_view(roadmap: dict[str, Any]) -> list[dict[str, Any]]:
    """Project roadmap-owned sprint facts for transport to composed clients."""

    fields = (
        "id",
        "ref",
        "derived_state",
        "state_drift",
        "implementation_pct",
        "blocked",
    )
    view = []
    for sprint in roadmap.get("sprints", []):
        if not isinstance(sprint, dict) or not sprint.get("id"):
            continue
        row = {key: sprint[key] for key in fields if key in sprint}
        # Runs in flight are counted; interrupted runs are listed apart because
        # they need a decision rather than patience. Both keys are always
        # present, so a sprint with neither reports an empty list instead of
        # leaving a reader unable to tell an empty list from an omitted key.
        row["in_flight"] = list(sprint.get("in_flight") or [])
        row["interrupted"] = list(sprint.get("interrupted") or [])
        view.append(row)
    return view


def ready_set_view(roadmap: dict[str, Any]) -> dict[str, Any]:
    """Project the canonical roadmap's ready rows for composed clients."""

    readiness_by_slug = {
        str(row.get("slug")): row
        for row in roadmap.get("ready_now", [])
        if isinstance(row, dict) and row.get("slug")
    }
    ready = []
    for summary in roadmap.get("immediate_roadmap", []):
        if not isinstance(summary, dict):
            continue
        row = dict(summary)
        readiness = readiness_by_slug.get(str(row.get("slug")), {})
        for key in (
            "section_readiness",
            "ready_sections",
            "blocked_sections",
            "section_attempts",
        ):
            if key in readiness:
                row[key] = readiness[key]
        ready.append(row)
    return {
        "project": roadmap.get("project"),
        "ready": ready,
        "review": roadmap.get("review"),
        "sprints": sprint_state_view(roadmap),
        "endpoints": roadmap.get("endpoints", []),
    }


#: The row types the metadata index inventories, so an agent read lists the
#: same documents the served surface paints.
_INDEXED_INVENTORY_TYPES = frozenset({"plan", "research", "evidence"})


def index_discovery(
    docs_dir: Path,
    project: str,
    state_root: Path | None,
) -> dict[str, Any]:
    """Build the list-level discovery payload from the persisted metadata index.

    A reader answered through ``discover_plans`` walks every tree it touches and
    parses every plan file, in its own process, cold on each restart. The index
    the served process persists already carries the list-level facts — which
    documents exist and their slug, href, type, title, status, sprint and stamps
    — so an agent read takes those from it and derives only the project's own
    state: the sprints, milestones, blockers and timeline held in the project
    state document. No plan file is opened, and the heavier per-document state
    belongs to a read of that document.
    """

    from reckon import metadata_index

    # This process runs no change watch, so the rows are revalidated by stat on
    # every call: a long-lived reader must see a later edit, and the re-stat
    # re-parses only the files whose identity moved.
    rows = metadata_index.index_rows(docs_dir, project, revalidate=True)
    inventory = [
        _indexed_item(row)
        for row in rows
        if str(row.get("type") or "") in _INDEXED_INVENTORY_TYPES
    ]
    (
        sprints,
        milestones,
        blockers,
        timeline,
        active_sprint_id,
        north_stars,
        resource_versions,
        source_format,
    ) = _project_state_lists(docs_dir, project, state_root)

    # A sprint a plan names but the state document does not carry still lists,
    # matching the derived payload's stub instead of dropping the plan's row.
    known = {
        str(sprint.get("id"))
        for sprint in sprints
        if isinstance(sprint, dict) and sprint.get("id")
    }
    referenced = {str(item.get("sprint")) for item in inventory if item.get("sprint")}
    for sprint_id in sorted(referenced - known):
        sprints.append(
            {
                "id": sprint_id,
                "theme": f"Sprint {sprint_id}",
                "description": "Auto-synthesized from plan inventory",
                "status": "planned",
                "items": [],
            }
        )

    from reckon.serve import _derive_lifecycle

    inventory, sprints = _derive_lifecycle(project, inventory, sprints, blockers)
    return {
        "inventory": inventory,
        "sprints": sprints,
        "milestones": milestones,
        "blockers": blockers,
        "timeline": timeline,
        "active_sprint_id": active_sprint_id,
        "north_stars": north_stars,
        "source_format": source_format,
        "resource_versions": resource_versions,
    }


def _indexed_item(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return one index row in the item shape a discovery reader consumes."""

    artifact_type = str(row.get("type") or "")
    return {
        "slug": row.get("slug"),
        "href": row.get("href"),
        "type": artifact_type,
        "title": row.get("title") or row.get("slug"),
        "status": row.get("status") or ("draft" if artifact_type == "plan" else ""),
        "sprint": row.get("sprint") or None,
        "archived": row.get("archived") or "",
        "created": row.get("created"),
        "edited": row.get("edited"),
    }


def _project_state_lists(
    docs_dir: Path,
    project: str,
    state_root: Path | None,
) -> tuple[list, list, list, list, Any, list, dict, str]:
    """Return the project's own resource lists and their source format."""

    from reckon.project_state import compose_project_state, project_state_mode

    if project_state_mode(docs_dir).format == "distributed":
        composed = compose_project_state(docs_dir, project)
        return (
            list(composed.get("sprints") or []),
            list(composed.get("milestones") or []),
            list(composed.get("blockers") or []),
            list(composed.get("timeline") or []),
            composed.get("active_sprint_id"),
            list(composed.get("north_stars") or []),
            dict(composed.get("resource_versions") or {}),
            "distributed",
        )

    sprints: list = []
    milestones: list = []
    blockers: list = []
    timeline: list = []
    active_sprint_id = None
    north_stars: list = []
    if state_root is not None:
        state_file = state_root / project / "index.json"
        if state_file.is_file():
            try:
                envelope = json.loads(state_file.read_text())
                data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
                sprints = list(data.get("sprints", []))
                milestones = list(data.get("milestones", []))
                blockers = list(data.get("blockers", []))
                timeline = list(data.get("timeline", []))
                active_sprint_id = data.get("active_sprint_id")
                north_stars = list(data.get("north_stars", []))
            except (OSError, json.JSONDecodeError):
                pass
    return (
        sprints,
        milestones,
        blockers,
        timeline,
        active_sprint_id,
        north_stars,
        {},
        "legacy-index",
    )


def compose_review(
    review: dict[str, Any],
    inventory: list[dict[str, Any]],
    sprints: list[dict[str, Any]],
    project: str,
    project_resources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Join a stored review to current project state."""

    from copy import deepcopy

    from reckon._schema import parse_plan_ref

    plans = {
        str(item.get("slug")): item
        for item in inventory
        if isinstance(item, dict)
        and item.get("type", "plan") == "plan"
        and item.get("slug")
    }
    resources: dict[tuple[str, str], dict[str, Any]] = {
        ("plan", slug): item for slug, item in plans.items()
    }
    resource_groups = project_resources or {}
    for kind, rows in (
        ("sprint", sprints),
        ("milestone", resource_groups.get("milestones") or []),
        ("blocker", resource_groups.get("blockers") or []),
    ):
        resources.update(
            {
                (kind, str(item.get("id"))): item
                for item in rows
                if isinstance(item, dict) and item.get("id")
            }
        )
    resources[("project", project)] = {"id": project, "status": "active"}

    def subject_row(subject: dict[str, Any]) -> dict[str, Any] | None:
        kind = str(subject.get("kind") or "")
        subject_id = str(subject.get("id") or "")
        if kind == "plan":
            ref = parse_plan_ref(subject_id)
            if ref is None or ref.is_external(project):
                return None
            subject_id = ref.slug
        return resources.get((kind, subject_id))

    def action_satisfied(verb: str, status: str) -> bool:
        if verb == "close":
            return status in TERMINAL_STATUSES
        if verb == "reopen":
            return bool(status) and status not in TERMINAL_STATUSES
        if verb == "resolve":
            return status in {*TERMINAL_STATUSES, "resolved", "closed"}
        return False

    composed = deepcopy(review)
    reviewed_at = str(composed.get("reviewed_at") or "")
    findings = []
    for finding in composed.get("findings") or []:
        row = dict(finding)
        subject = row.get("subject") if isinstance(row.get("subject"), dict) else {}
        live = subject_row(subject)
        status = str((live or {}).get("status") or "")
        checked_at = str(row.get("checked_at") or "")
        changed_at = str((live or {}).get("modified") or (live or {}).get("last") or "")
        changed_day = _stated_day(changed_at, truncated=True)
        checked_day = _stated_day(checked_at, truncated=False)
        moved = bool(
            changed_at
            and changed_day is not None
            and checked_day is not None
            and changed_day > checked_day
        )
        row.update(
            {
                "subject_found": live is not None,
                "subject_status": status,
                "stale": moved and not bool(row.get("resolved_at")),
                "current": bool(live)
                and not row.get("resolved_at")
                and not moved
                and not action_satisfied(
                    str((row.get("recommended_action") or {}).get("verb") or ""),
                    status,
                ),
            }
        )
        findings.append(row)
    composed["findings"] = findings

    priority = []
    ranked_sprints: list[str] = []
    stored_priority = sorted(
        (row for row in composed.get("priority") or [] if isinstance(row, dict)),
        key=lambda row: (int(row.get("rank", 10**6)), str(row.get("ref") or "")),
    )
    for stored in stored_priority:
        row = dict(stored)
        ref = parse_plan_ref(str(row.get("ref") or ""))
        live = None if ref is None or ref.is_external(project) else plans.get(ref.slug)
        status = str((live or {}).get("status") or "")
        effective = str((live or {}).get("effective_status") or status)
        sprint = (live or {}).get("sprint")
        landed = effective in TERMINAL_STATUSES
        modified = str((live or {}).get("modified") or (live or {}).get("last") or "")
        modified_day = _stated_day(modified, truncated=True)
        reviewed_day = _stated_day(reviewed_at, truncated=True)
        moved = bool(
            modified
            and reviewed_at
            and modified_day is not None
            and reviewed_day is not None
            and modified_day > reviewed_day
        )
        row.update(
            {
                "status": status,
                "effective_status": effective,
                "impl": float((live or {}).get("impl", 0.0) or 0.0),
                "sprint": sprint,
                "landed": landed,
                "stale": moved and not landed,
            }
        )
        if sprint and sprint not in ranked_sprints:
            ranked_sprints.append(str(sprint))
        priority.append(row)
    composed["priority"] = priority
    open_sprints = sorted(
        (
            str(sprint.get("id"))
            for sprint in sprints
            if isinstance(sprint, dict)
            and sprint.get("id")
            and str(sprint.get("status") or "planned") not in TERMINAL_STATUSES
        ),
        key=_natural_identifier_key,
    )
    composed["sprint_order"] = ranked_sprints + [
        sprint_id for sprint_id in open_sprints if sprint_id not in ranked_sprints
    ]
    return composed


def load_composed_review(
    docs_dir: Path,
    project: str,
    inventory: list[dict[str, Any]],
    sprints: list[dict[str, Any]],
    project_resources: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, int | None]:
    """Read and compose the optional review singleton through one shared path."""

    from reckon.project_state import ProjectStateError, read_resource, resource_path

    if not resource_path(docs_dir, project, "review", "review").is_file():
        return None, None
    try:
        review, version = read_resource(docs_dir, project, "review", "review")
    except (OSError, ProjectStateError, ValueError):
        return None, None
    return (
        compose_review(review, inventory, sprints, project, project_resources),
        version,
    )


def _run_row(pointer: Mapping[str, Any]) -> dict[str, str]:
    """Project one live pointer's identity and target onto a compact row."""

    node = pointer.get("node")
    node = node if isinstance(node, dict) else {}
    return {
        "run_id": str(pointer.get("run_id") or ""),
        "member": str(pointer.get("member") or ""),
        "section": str(node.get("section") or ""),
        "started_at": str(pointer.get("created_at") or ""),
    }


def _run_target_plan(pointer: Mapping[str, Any]) -> str:
    """Return the plan a live pointer targets, or "" when it names none."""

    node = pointer.get("node")
    node = node if isinstance(node, dict) else {}
    return str(node.get("plan") or "").strip()


def _attempt_inputs(
    project: str, root: str | Path | None, pointers: list[dict[str, Any]] | None
) -> (
    tuple[
        list[Any],
        tuple[str, int, int, int] | None,
        tuple[tuple[str, int, int], ...],
        str | None,
    ]
    | None
):
    """Stamp the ledger index, newest run, and live-pointer names and times."""
    from reckon.crew import runs

    try:
        newest = max(
            (ledger.ledger_path(project, root).parent / "runs").glob("*.json"),
            default=None,
        )
        newest_stamp = None
        if newest is not None:
            info = newest.stat()
            newest_stamp = (
                newest.name,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_size,
            )
        live = []
        for path in sorted(runs.live_dir().glob("*.json")):
            info = path.stat()
            live.append((path.name, info.st_mtime_ns, info.st_size))
        supplied = (
            json.dumps(pointers, sort_keys=True, default=str)
            if pointers is not None
            else None
        )
        return ledger.index_stamp(project, root), newest_stamp, tuple(live), supplied
    except OSError:
        return None


def _cached_attempts(
    key: tuple[Any, ...],
    project: str,
    root: str | Path | None,
    pointers: list[dict[str, Any]] | None,
    compute: Callable[[], Any],
) -> Any:
    """Serialize cache misses so concurrent reads aggregate a snapshot once."""
    with _ATTEMPT_CACHE_LOCK:
        before = _attempt_inputs(project, root, pointers)
        cached = _ATTEMPT_CACHE.get(key)
        if before is not None and cached is not None and cached[0] == before:
            return copy.deepcopy(cached[1])
        value = compute()
        after = _attempt_inputs(project, root, pointers)
        # Reading can refresh the index file itself. Source and live-pointer
        # stamps must remain stable; the post-read index stamp becomes the key.
        if (
            before is not None
            and after is not None
            and before[0][:4] == after[0][:4]
            and before[1:] == after[1:]
        ):
            _ATTEMPT_CACHE[key] = (after, copy.deepcopy(value))
            if len(_ATTEMPT_CACHE) > _ATTEMPT_CACHE_LIMIT:
                _ATTEMPT_CACHE.pop(next(iter(_ATTEMPT_CACHE)))
        return value


def section_attempts_by_plan(
    project: str,
    root: str | Path | None = None,
    pointers: list[dict[str, Any]] | None = None,
    *,
    only_plan: str | None = None,
    only_section: str | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Derive section attempts from distinct executable run ids.

    Committed rows settle an outcome; a live pointer for the same run id adds
    nothing. The legacy ``data-attempts`` attribute is not an input.
    """

    def aggregate() -> dict[str, dict[str, dict[str, Any]]]:
        from reckon.crew import runs

        history, _version = ledger.load(project, root)
        live = (
            pointers
            if pointers is not None
            else runs._list_live_records(project=project)
        )
        return _group_section_attempts(
            project,
            history.get("runs", []),
            live,
            only_plan=only_plan,
            only_section=only_section,
        )

    return _cached_attempts(
        ("group", project, str(root), only_plan, only_section),
        project,
        root,
        pointers,
        aggregate,
    )


def _group_section_attempts(
    project: str,
    history_rows: Iterable[Mapping[str, Any]],
    pointers: list[dict[str, Any]],
    *,
    only_plan: str | None = None,
    only_section: str | None = None,
    settled_run_ids: set[str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Apply one run-id and outcome rule to full or selected history rows."""
    from reckon._plan_html import section_record_id

    wanted_section = section_record_id(only_section) if only_section else None
    observed: dict[str, tuple[str, str, dict[str, Any]]] = {}
    settled_ids = set(settled_run_ids or ())
    for row in history_rows:
        if (
            not isinstance(row, Mapping)
            or row.get("role") not in EXECUTABLE_SECTION_ROLES
        ):
            continue
        run_id = str(row.get("run_id") or "")
        plan = str(row.get("plan") or "")
        section = section_record_id(row.get("section"))
        if run_id:
            settled_ids.add(run_id)
        if (only_plan is not None and plan != only_plan) or (
            wanted_section is not None and section != wanted_section
        ):
            continue
        if not run_id or not plan or not section:
            continue
        gate = str(row.get("gate") or "")
        status = (
            "promoted"
            if gate == "passed"
            else "failed"
            if gate == "failed"
            else "superseded"
        )
        observed[run_id] = (
            plan,
            section,
            {
                "run_id": run_id,
                "status": status,
                **(
                    {"failure_classification": row.get("failure_classification")}
                    if status == "failed"
                    else {}
                ),
            },
        )
    for pointer in pointers:
        if (
            not isinstance(pointer, Mapping)
            or pointer.get("project") != project
            or pointer.get("role") not in EXECUTABLE_SECTION_ROLES
        ):
            continue
        run_id = str(pointer.get("run_id") or "")
        node = pointer.get("node") or {}
        plan = str(node.get("plan") or "") if isinstance(node, Mapping) else ""
        section = (
            section_record_id(node.get("section")) if isinstance(node, Mapping) else ""
        )
        if (
            run_id
            and (
                (only_plan is None and wanted_section is None)
                or run_id not in settled_ids
            )
            and run_id not in observed
            and plan
            and section
            and (only_plan is None or plan == only_plan)
            and (wanted_section is None or section == wanted_section)
        ):
            observed[run_id] = (
                plan,
                section,
                {"run_id": run_id, "status": "in_flight"},
            )
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for plan, section, outcome in (observed[key] for key in sorted(observed)):
        record = grouped.setdefault(plan, {}).setdefault(
            section, {"attempts": 0, "attempt_outcomes": []}
        )
        record["attempts"] += 1
        record["attempt_outcomes"].append(outcome)
    return grouped


def section_attempt_count(
    project: str,
    plan: str,
    section: str,
    root: str | Path | None = None,
) -> int:
    """Count one section from indexed run payloads and raw live pointers.

    Refreshing headers checks the ledger sources and yields all settled run ids.
    The indexed query decodes only rows for this plan; section spelling is then
    normalized in Python, as it is for full plan views.
    """
    from reckon._plan_html import section_record_id

    wanted = section_record_id(section)
    if not project or not plan or not wanted:
        return 0
    return _cached_attempts(
        ("count", project, str(root), plan, wanted),
        project,
        root,
        None,
        lambda: _section_attempt_count_uncached(project, plan, section, wanted, root),
    )


def _section_attempt_count_uncached(
    project: str, plan: str, section: str, wanted: str, root: str | Path | None
) -> int:
    from reckon.crew import runs

    headers, _version = ledger.indexed_headers(project, root)
    header_rows = headers.get("runs", [])
    settled = {
        str(row.get("run_id"))
        for row in header_rows
        if isinstance(row, Mapping) and row.get("run_id")
    }
    roles = tuple(sorted(EXECUTABLE_SECTION_ROLES))
    role_slots = ", ".join("?" for _ in roles)
    query = (
        "SELECT payload FROM aggregate_rows "  # noqa: S608 - role values are bound
        "WHERE json_extract(payload, '$.plan') = ? "
        f"AND json_extract(payload, '$.role') IN ({role_slots}) "
        "UNION ALL SELECT payload FROM records "
        "WHERE name NOT IN (SELECT run_id FROM aggregate_rows WHERE run_id IS NOT NULL) "
        "AND json_extract(payload, '$.plan') = ? "
        f"AND json_extract(payload, '$.role') IN ({role_slots})"
    )
    if any(isinstance(row, Mapping) and set(row) - {"run_id"} for row in header_rows):
        # An unreadable index makes indexed_headers return authoritative full rows.
        rows = [
            row
            for row in header_rows
            if isinstance(row, Mapping)
            and row.get("plan") == plan
            and row.get("role") in EXECUTABLE_SECTION_ROLES
        ]
    else:
        try:
            index_uri = ledger._run_index_path(project, root).as_uri() + "?mode=ro"
            with closing(sqlite3.connect(index_uri, uri=True)) as connection:
                rows = [
                    json.loads(payload)
                    for (payload,) in connection.execute(
                        query, (plan, *roles, plan, *roles)
                    )
                ]
        except (OSError, sqlite3.Error):
            return (
                section_attempts_by_plan(
                    project, root, only_plan=plan, only_section=section
                )
                .get(plan, {})
                .get(wanted, {})
                .get("attempts", 0)
            )
    grouped = _group_section_attempts(
        project,
        rows,
        runs._list_live_records(project=project),
        only_plan=plan,
        only_section=section,
        settled_run_ids=settled,
    )
    return grouped.get(plan, {}).get(wanted, {}).get("attempts", 0)


def with_section_attempts(
    project: str,
    slug: str,
    sections: list[dict[str, Any]],
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Add run-derived attempts to section records for a delivered plan view."""
    from reckon._plan_html import section_record_id

    by_section = section_attempts_by_plan(project, root).get(slug, {})
    enriched = []
    for record in sections:
        details = by_section.get(section_record_id(record["id"]))
        item = {**record, "attempts": details["attempts"] if details else 0}
        if details:
            item["attempt_outcomes"] = details["attempt_outcomes"]
        enriched.append(item)
    return enriched


def _recorded_live_run_classifications(project: str) -> dict[str, dict[str, Any]]:
    """Return the latest watcher event for every run recorded in its stream."""

    from reckon.crew import runs

    path = runs.watch_stream_path(project)
    if not path.is_file():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                event = runs.parse_stream_line(line)
                if event is None or event.get("legacy"):
                    continue
                run_id = str(event.get("run_id") or "")
                classification = str(
                    event.get("recovery_classification") or event.get("to_state") or ""
                )
                if run_id and classification:
                    latest[run_id] = event
    except OSError:
        return {}
    return latest


def partition_live_runs(
    project: str,
    pointers: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    """Split one project's live runs into those in flight and those interrupted.

    The two want opposite actions, so they are never folded into one count. A
    run still in flight is waited on; an interrupted run needs a decision —
    resume where a session survived, redispatch otherwise. Interruption is
    judged by the shared classifier, so the roadmap, the sprint view and
    ``recover`` cannot disagree about which runs stopped involuntarily. Every
    other reading stays in flight, so a run whose liveness cannot be proven is
    still counted as running rather than mistaken for a death.
    """

    from reckon import crew
    from reckon.crew.node import INTERRUPTED_RUN_PHASE
    from reckon.crew.recovery import classify_pointer

    if pointers is None:
        try:
            pointers = crew.list_live()
        except OSError:
            pointers = []

    in_flight: dict[str, list[dict[str, str]]] = {}
    interrupted: dict[str, list[dict[str, str]]] = {}
    recorded = _recorded_live_run_classifications(project)
    for pointer in pointers:
        if not isinstance(pointer, dict) or pointer.get("project") != project:
            continue
        plan = _run_target_plan(pointer)
        if not plan:
            continue
        event = recorded.get(str(pointer.get("run_id") or ""))
        classified = event if event is not None else classify_pointer(pointer)
        classification = str(
            classified.get("recovery_classification")
            or classified.get("classification")
            or classified.get("to_state")
            or ""
        )
        if classification == INTERRUPTED_RUN_PHASE:
            row = _run_row(pointer)
            row["reason"] = str(classified.get("detail") or "")
            row["next_action"] = str(classified.get("next_action") or "")
            interrupted.setdefault(plan, []).append(row)
        else:
            in_flight.setdefault(plan, []).append(_run_row(pointer))
    for grouped in (in_flight, interrupted):
        for runs in grouped.values():
            runs.sort(key=lambda run: run["run_id"])
    return in_flight, interrupted


def in_flight_by_plan(
    project: str,
    pointers: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Group the runs still in flight for one project by their target plan."""

    in_flight, _interrupted = partition_live_runs(project, pointers)
    return in_flight


def interrupted_by_plan(
    project: str,
    pointers: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Group the interrupted runs by plan, each with its reason and next action."""

    _in_flight, interrupted = partition_live_runs(project, pointers)
    return interrupted


class ViewRequestError(ValueError):
    """A stable, agent-readable request error."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class ResourceSelector:
    """Stable typed identity used by every progressive response."""

    project: str
    type: str
    id: str
    archived: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "type": self.type,
            "id": self.id,
            "archived": self.archived,
        }


def compact_size(value: Any) -> int:
    """Return deterministic compact UTF-8 JSON size."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def storage_schema_for(resource_type: str) -> dict[str, Any]:
    """Return the storage contract for one canonical resource type."""

    from reckon._schema import Blocker, Milestone, Sprint, TimelineEntry

    if resource_type in {"plan", "research", "evidence"}:
        from reckon._schema import gen_json_schema

        return gen_json_schema()

    if resource_type in {"sprint", "milestone", "blocker"}:
        model = {
            "sprint": Sprint,
            "milestone": Milestone,
            "blocker": Blocker,
        }[resource_type]
        schema = model.model_json_schema()
        schema["title"] = f"reckon {resource_type.title()}Resource"
        schema["schemaVersion"] = RESPONSE_SCHEMA_VERSION
        properties = schema.setdefault("properties", {})
        properties["type"] = {"const": resource_type, "type": "string"}
        properties["version"] = {"minimum": 0, "type": "integer"}
        schema["required"] = sorted(
            set(schema.get("required") or []) | {"id", "type", "version"}
        )
        return schema

    if resource_type == "timeline":
        return {
            "title": "reckon TimelineResource",
            "schemaVersion": RESPONSE_SCHEMA_VERSION,
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "type", "version", "events"],
            "properties": {
                "id": {"const": "timeline", "type": "string"},
                "type": {"const": "timeline", "type": "string"},
                "version": {"minimum": 0, "type": "integer"},
                "events": {
                    "type": "array",
                    "items": TimelineEntry.model_json_schema(),
                },
            },
        }

    if resource_type == "project":
        return {
            "title": "reckon ProjectResource",
            "schemaVersion": RESPONSE_SCHEMA_VERSION,
            "type": "object",
            "additionalProperties": True,
            "required": ["project", "type", "version"],
            "properties": {
                "project": {"type": "string"},
                "type": {"const": "project", "type": "string"},
                "version": {"minimum": 0, "type": "integer"},
                "owner": {"type": "string"},
                "published": {"type": "string"},
                "scope": {
                    "type": "object",
                    "properties": {
                        "owns": {"type": "array", "items": {"type": "string"}},
                        "excludes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "routes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["work", "project"],
                                "properties": {
                                    "work": {"type": "string"},
                                    "project": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        }
    if resource_type == "review":
        return {
            "title": "reckon ReviewResource",
            "schemaVersion": RESPONSE_SCHEMA_VERSION,
            "type": "object",
            "additionalProperties": True,
            "required": [
                "id",
                "type",
                "version",
                "reviewed_at",
                "reviewed_by",
                "basis",
                "findings",
                "priority",
            ],
        }

    raise ViewRequestError(
        "invalid_resource",
        f"No storage schema exists for resource type {resource_type!r}.",
    )


def normalize_view(view: str | None) -> str:
    """Validate a view name, defaulting typed calls to ``summary``."""

    selected = (view or "summary").strip().lower()
    if selected not in VIEW_NAMES:
        choices = ", ".join(sorted(VIEW_NAMES))
        displayed = repr(view)
        if len(displayed) > 96:
            displayed = displayed[:93] + "..."
        raise ViewRequestError(
            "invalid_view",
            f"Unknown MCP response view {displayed}.",
            f"Choose one of: {choices}.",
        )
    return selected


def authored_plan_text(html_text: str) -> str:
    """Return searchable prose while excluding Reckon's structured collections."""

    return " ".join(prose for _, prose in section_prose(html_text or "") if prose)


def _authored_section_headings(html_text: str) -> list:
    """Locate each authored section's heading record from the shared spans."""
    sections = []
    claimed = set()
    for heading in plan_headings(html_text):
        if (
            heading.level != 2
            or not heading.raw_id
            or heading.identity in claimed
            or heading.machinery
        ):
            continue
        claimed.add(heading.identity)
        sections.append((heading.identity, heading))
    return sections


def _record_text_response(
    selector: ResourceSelector,
    version: int,
    data: dict[str, Any],
    *,
    section: str | None,
    html_text: str | None,
) -> dict[str, Any]:
    """Serve a cumulative evidence record's whole text through the text view.

    A record's fragments are appended after the document rather than into one
    of its sections, so a section read cannot carry the anchors they hold. The
    record is therefore served whole — the composed bytes, or the record's own
    bytes when composition failed — in the ``html``/``text`` fields the text
    view already uses for a plan's authored section.
    """

    if isinstance(section, str) and section.strip():
        raise ViewRequestError(
            "section_not_applicable",
            "An evidence record is served whole, not one section at a time.",
            "Drop the section argument to read the record.",
        )
    text = html_text or ""
    return {
        "resource": selector.as_dict(),
        "version": version,
        "view": "section",
        "section": {
            "id": selector.id,
            "heading": data.get("title") or selector.id,
            "text": " ".join(BeautifulSoup(text, "html.parser").stripped_strings),
            "html": text,
            "declaration": None,
            "record": None,
            "comments": [],
        },
    }


def _authored_heading_html(heading: Tag) -> str:
    """Render a heading without opt-in typed-record attributes."""

    rendered = BeautifulSoup(str(heading), "html.parser").find(True)
    if rendered is None:
        return str(heading)
    if machinery_kind(rendered.attrs) == "section":
        for attribute in tuple(rendered.attrs):
            if (
                attribute == RECKON_ATTRIBUTE
                or attribute
                in {
                    "data-effort-hours",
                    "data-attempts",
                    "data-status",
                    "data-links",
                }
                or attribute.startswith("data-capability-")
            ):
                del rendered.attrs[attribute]
    return str(rendered)


def _section_response(
    selector: ResourceSelector,
    version: int,
    data: dict[str, Any],
    *,
    section: str | None,
    html_text: str | None,
) -> dict[str, Any]:
    """Select one authored section and attach its typed plan context."""

    if selector.type != "plan":
        raise ViewRequestError(
            "invalid_section_view",
            "The section view is available only for plan resources.",
            "Select a plan resource or choose another response view.",
        )
    if not isinstance(section, str) or not section.strip():
        raise ViewRequestError(
            "section_required",
            "view='section' requires a non-empty section identity.",
            "Pass the id of an authored section heading or its wrapping "
            "section element.",
        )
    identity = section.strip()
    if (
        len(identity) > MAX_SELECTOR_LENGTH
        or identity in {".", ".."}
        or not _SAFE_SEGMENT.fullmatch(identity)
    ):
        raise ViewRequestError(
            "invalid_section",
            "section must be one safe heading identity.",
        )
    identity = section_record_id(identity)
    authored = _authored_section_headings(html_text or "")
    selected = next(
        (heading for section_id, heading in authored if section_id == identity),
        None,
    )
    available = [section_id for section_id, _heading in authored]
    if selected is None:
        available_text = ", ".join(available) or "none"
        raise ViewRequestError(
            "section_not_found",
            (
                f"Section {identity!r} was not found in plan {selector.id!r}; "
                f"available sections: {available_text}."
            ),
            "Choose one of the available section identities.",
        )

    source = html_text or ""
    opening, closing = selected.heading_span
    rendered_heading = BeautifulSoup(source[opening:closing], "html.parser").find(True)
    fragments = [_authored_heading_html(rendered_heading)]
    body_source = source[slice(*selected.body_span)]
    body = BeautifulSoup(body_source, "html.parser")
    for element in body.select(f"section[{RECKON_ATTRIBUTE}]"):
        if machinery_kind(element.attrs) is not None:
            element.decompose()
    fragments.extend(
        str(element).strip() for element in body.contents if str(element).strip()
    )
    section_html = "\n".join(fragments)
    section_text = " ".join(
        prose
        for section_id, prose in section_prose(source, keep_landed_cards=True)
        if section_id == identity
    )
    record = next(
        (
            item
            for item in data.get("sections") or []
            if isinstance(item, dict) and item.get("id") == identity
        ),
        None,
    )
    return {
        "resource": selector.as_dict(),
        "version": version,
        "view": "section",
        "section": {
            "id": identity,
            "heading": selected.text,
            "text": section_text,
            "html": section_html,
            "declaration": (data.get("section_declarations") or {}).get(identity),
            "record": record,
            "comments": list((data.get("comments") or {}).get(identity) or []),
        },
    }


def normalize_selector(
    resource: dict[str, Any],
    *,
    fallback_project: str | None = None,
) -> ResourceSelector:
    """Validate the public ``resource`` selector."""

    if not isinstance(resource, dict):
        raise ViewRequestError(
            "invalid_resource",
            "resource must be an object with project, type, and id fields.",
        )
    project = resource.get("project") or fallback_project
    resource_type = str(resource.get("type") or "").strip().lower()
    if resource_type == "doc":
        resource_type = "research"
    resource_id = resource.get("id")
    archived = resource.get("archived", False)
    if not isinstance(project, str) or not project.strip():
        raise ViewRequestError("invalid_resource", "resource.project is required.")
    if resource_type not in RESOURCE_TYPES - {"audit"}:
        raise ViewRequestError(
            "invalid_resource",
            f"Unsupported resource type {resource_type!r}.",
            "Use plan, research, evidence, sprint, milestone, blocker, timeline, or project.",
        )
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ViewRequestError("invalid_resource", "resource.id is required.")
    for label, value in (("project", project.strip()), ("id", resource_id.strip())):
        if (
            len(value) > MAX_SELECTOR_LENGTH
            or value in {".", ".."}
            or not _SAFE_SEGMENT.fullmatch(value)
        ):
            raise ViewRequestError(
                "invalid_resource",
                f"resource.{label} must be one safe path segment.",
            )
    if not isinstance(archived, bool):
        raise ViewRequestError(
            "invalid_resource", "resource.archived must be true or false."
        )
    return ResourceSelector(
        project=project.strip(),
        type=resource_type,
        id=resource_id.strip(),
        archived=archived,
    )


def error_response(
    error: str,
    message: str,
    *,
    selector: ResourceSelector | None = None,
    operation: str = "read",
    hint: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Build the bounded structured error contract."""

    def bounded(value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            if len(value) <= MAX_ERROR_TEXT_LENGTH:
                return value
            return value[: MAX_ERROR_TEXT_LENGTH - 3] + "..."
        if depth >= 4:
            return "<omitted>"
        if isinstance(value, dict):
            return {
                str(key)[:MAX_ERROR_TEXT_LENGTH]: bounded(item, depth + 1)
                for key, item in list(value.items())[:MAX_ERROR_COLLECTION_ITEMS]
            }
        if isinstance(value, (list, tuple)):
            return [
                bounded(item, depth + 1) for item in value[:MAX_ERROR_COLLECTION_ITEMS]
            ]
        return value

    result: dict[str, Any] = {
        "ok": False,
        "error": error,
        "message": message,
        "operation": operation,
    }
    if selector is not None:
        result["resource"] = selector.as_dict()
    result.update(extra)
    if hint:
        result["hint"] = hint
    return bounded(result)


def _cursor(offset: int) -> str:
    raw = json.dumps({"offset": offset}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    if not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_LENGTH:
        raise ViewRequestError(
            "invalid_cursor",
            "The pagination cursor is invalid.",
            "Restart from the first page by omitting cursor.",
        )
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        offset = value["offset"]
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or set(value) != {"offset"}
        ):
            raise ValueError
        return offset
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ViewRequestError(
            "invalid_cursor",
            "The pagination cursor is invalid.",
            "Restart from the first page by omitting cursor.",
        ) from exc


def paginate(
    records: list[Any],
    *,
    cursor: str | None,
    limit: int | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Return one deterministic cursor page."""

    if limit is None:
        page_size = DEFAULT_PAGE_SIZE
    elif not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ViewRequestError("invalid_limit", "limit must be a positive integer.")
    else:
        page_size = min(limit, MAX_PAGE_SIZE)
    offset = _cursor_offset(cursor)
    if offset > len(records):
        raise ViewRequestError(
            "invalid_cursor",
            "The pagination cursor points beyond the available records.",
            "Restart from the first page by omitting cursor.",
        )
    page = records[offset : offset + page_size]
    next_offset = offset + len(page)
    return page, {
        "count": len(page),
        "total": len(records),
        "next_cursor": _cursor(next_offset) if next_offset < len(records) else None,
    }


def _open_decisions(data: dict[str, Any]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for key, value in (data.get("decisions") or {}).items():
        if not isinstance(value, dict) or value.get("choice"):
            continue
        decisions.append(
            {
                "key": key,
                "question": value.get("title", ""),
                "options": list(value.get("choices") or []),
            }
        )
    return decisions


def _followup_record(item: dict[str, Any], *, include_prompts: bool) -> dict[str, Any]:
    result = {
        "id": item.get("id", ""),
        "title": item.get("title", ""),
        "body": item.get("body", ""),
        "recommends_skill": item.get("recommends_skill", ""),
        "capability": item.get("capability") or {},
    }
    if include_prompts:
        result["prompt"] = item.get("prompt", "")
    return result


def _next_action(
    data: dict[str, Any], *, include_prompts: bool
) -> dict[str, Any] | None:
    for item in data.get("followups") or []:
        if isinstance(item, dict) and item.get("status", "open") == "open":
            return _followup_record(item, include_prompts=include_prompts)
    return None


def _relations(data: dict[str, Any]) -> dict[str, list[Any]]:
    relations: dict[str, list[Any]] = {}
    for field in (
        "depends_on",
        "blocks",
        "informs",
        "evidence_for",
        "verifies",
        "supersedes",
    ):
        value = data.get(field)
        if value:
            relations[field] = list(value) if isinstance(value, list) else [value]
    return relations


def _blocking(data: dict[str, Any], deps: list[dict[str, Any]]) -> list[Any]:
    from reckon.roadmap import execution_gates

    explicit = data.get("blocked_by")
    result = list(explicit) if isinstance(explicit, list) else []
    result.extend(unresolved_dependencies(deps))
    # Transition gates hold a closure or a choice, not execution, so the
    # roadmap's execution split governs what counts as blocking here too.
    result.extend(unpassed_gate_blockers(execution_gates(data)))
    return result


def _section_blocking(deps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Report the sections that wait, each beside the targets it waits for.

    A section-scoped row holds one section of the owning plan, so it is
    reported here instead of in the plan-level blocking list, and only while
    its target is unresolved — the same test the plan-level list applies.
    """

    waits: dict[str, list[dict[str, Any]]] = {}
    for dep in deps:
        if not is_section_scoped(dep):
            continue
        if dep.get("found") and dep.get("status") in COMPLETED_STATUSES:
            continue
        target = {
            "ref": dep.get("ref", ""),
            "found": bool(dep.get("found")),
            "status": dep.get("status", ""),
        }
        if dep.get("stage"):
            target["stage"] = dep["stage"]
        waits.setdefault(str(dep.get("source_section")), []).append(target)
    return [
        {"section": section, "ready": False, "waits_on": rows}
        for section, rows in sorted(waits.items())
    ]


def _plan_effort(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the plan estimate and its derived consumption in named units."""

    raw_hours = data.get("effort_hours")
    if raw_hours is None:
        return None
    estimated_hours = float(raw_hours)
    progress = float(data.get("impl", 0.0) or 0.0)
    spent_hours = estimated_hours * progress
    return {
        "estimated_hours": round(estimated_hours, 2),
        "spent_hours": round(spent_hours, 2),
        "remaining_hours": round(estimated_hours - spent_hours, 2),
        "unit": "worker-hours",
    }


def _sprint_capacity(data: dict[str, Any]) -> dict[str, Any]:
    """Return the summed plan estimates for a sprint in named units."""

    capacity = data.get("capacity")
    if isinstance(capacity, dict) and capacity.get("unit") == "worker-hours":
        return capacity
    total_hours = sum(
        float(item.get("effort_hours") or 0.0)
        for item in data.get("items") or []
        if isinstance(item, dict)
    )
    return {"total_hours": round(total_hours, 2), "unit": "worker-hours"}


def _state(
    selector: ResourceSelector,
    data: dict[str, Any],
    blocking: list[Any],
) -> dict[str, Any]:
    resource_type = selector.type
    if resource_type == "plan":
        workflow_status = str(data.get("status") or "draft")
        age_days = modified_age_days(data.get("modified"))
        state = {
            "status": workflow_status,
            "effective_status": effective_status(workflow_status, blocking),
            "progress": float(data.get("impl", 0.0) or 0.0),
            "age_days": age_days,
            "staleness": lifecycle_staleness(
                doc_type="plan",
                status=workflow_status,
                impl=data.get("impl"),
                age_days=age_days,
            ),
            "sprint": data.get("sprint") or None,
            "milestone": data.get("milestone") or None,
            "capability": data.get("capability") or {},
        }
        effort = _plan_effort(data)
        if effort is not None:
            state["effort"] = effort
        return state
    if resource_type == "research":
        reviewed_at = data.get("reviewed_at", "")
        return {
            "reviewed": bool(reviewed_at),
            "reviewed_at": reviewed_at,
            "source_quality": data.get("source_quality", ""),
        }
    if resource_type == "evidence":
        return {
            "verdict": data.get("verdict", ""),
            "recorded_at": data.get("recorded_at", ""),
            "environment": data.get("environment", ""),
        }
    if resource_type == "sprint":
        items = [item for item in data.get("items") or [] if isinstance(item, dict)]
        statuses = [
            str(item.get("effective_status") or item.get("status") or "pending")
            for item in items
        ]
        return {
            "status": data.get("status", "planned"),
            "starts": data.get("starts", ""),
            "ends": data.get("ends", ""),
            "items": len(items),
            "completed": sum(status in {"shipped", "done"} for status in statuses),
            "blocked": sum(status == "blocked" for status in statuses),
            "metrics": data.get("metrics") or sprint_metrics(items),
            "capacity": _sprint_capacity(data),
        }
    if resource_type == "milestone":
        return {
            "status": data.get("status", "planned"),
            "progress": float(data.get("pct", data.get("progress", 0)) or 0),
        }
    if resource_type == "blocker":
        return {
            "status": data.get("status", "open"),
            "owner": data.get("owner", ""),
            "next": data.get("next", ""),
        }
    if resource_type == "timeline":
        return {"events": len(data.get("events") or [])}
    if resource_type == "project":
        summary = data.get("summary") or {}
        return {
            "active_sprint_id": data.get("active_sprint_id"),
            "plans": summary.get("plans", 0),
            "artifacts": summary.get("artifacts", 0),
            "open_followups": summary.get("open_followups", 0),
            "open_questions": summary.get("open_questions", 0),
            "open_decisions": summary.get("open_decisions", 0),
        }
    if resource_type == "review":
        findings = list(data.get("findings") or [])
        priority = list(data.get("priority") or [])
        return {
            "reviewed_at": data.get("reviewed_at", ""),
            "reviewed_by": data.get("reviewed_by", ""),
            "findings": len(findings),
            "current_findings": sum(bool(row.get("current")) for row in findings),
            "priority_rows": len(priority),
            "sprint_order": list(data.get("sprint_order") or []),
        }
    return {}


def _summary(
    selector: ResourceSelector,
    version: int,
    data: dict[str, Any],
    *,
    deps: list[dict[str, Any]],
    include_prompts: bool,
) -> dict[str, Any]:
    if include_prompts:
        raise ViewRequestError(
            "invalid_view_option",
            "Summary responses never include full followup prompts.",
            "Use view='detail' with include_prompts=true.",
        )
    blocking = _blocking(data, deps)
    result = {
        "resource": selector.as_dict(),
        "version": version,
        "view": "summary",
        "title": data.get("title")
        or data.get("theme")
        or data.get("name")
        or selector.id,
        "summary": data.get("summary") or data.get("description") or "",
        "state": _state(selector, data, blocking),
        "blocking": blocking,
        "open_decisions": _open_decisions(data),
        "next": _next_action(data, include_prompts=False),
        "warnings": list(data.get("compatibility_warnings") or []),
    }
    if selector.type == "plan":
        result["section_blocking"] = _section_blocking(deps)
        result["sections"] = list(data.get("sections") or [])
    return result


def _detail(
    selector: ResourceSelector,
    version: int,
    data: dict[str, Any],
    *,
    deps: list[dict[str, Any]],
    include_prompts: bool,
) -> dict[str, Any]:
    result = _summary(selector, version, data, deps=deps, include_prompts=False)
    result["view"] = "detail"
    result["metadata"] = {
        key: data[key]
        for key in (
            "owner",
            "modified",
            "roi",
            "source",
            "source_quality",
            "verdict",
            "environment",
        )
        if data.get(key) not in (None, "")
    }
    result["relations"] = _relations(data)
    result["followups"] = [
        _followup_record(item, include_prompts=include_prompts)
        for item in data.get("followups") or []
        if isinstance(item, dict) and item.get("status", "open") == "open"
    ]
    result["questions"] = [
        item
        for item in data.get("questions") or []
        if isinstance(item, dict) and item.get("status", "open") == "open"
    ]
    if selector.type == "sprint":
        result["items"] = [
            {
                key: item.get(key)
                for key in (
                    "slug",
                    "title",
                    "status",
                    "effective_status",
                    "impl",
                    "capability",
                )
                if item.get(key) is not None
            }
            | ({"effort": _plan_effort(item)} if _plan_effort(item) is not None else {})
            for item in data.get("items") or []
            if isinstance(item, dict)
        ]
    elif selector.type == "timeline":
        result["events"] = list(data.get("events") or [])
    elif selector.type == "review":
        result["findings"] = list(data.get("findings") or [])
        result["priority"] = list(data.get("priority") or [])
        result["sprint_order"] = list(data.get("sprint_order") or [])
    return result


def _history_records(
    selector: ResourceSelector, data: dict[str, Any], *, include_prompts: bool
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for key, item in (data.get("decisions") or {}).items():
        if isinstance(item, dict) and item.get("choice"):
            records.append({"kind": "decision", "key": key, **item})
    for item in data.get("followups") or []:
        if isinstance(item, dict) and item.get("status") == "resolved":
            record = {"kind": "followup", **item}
            if not include_prompts:
                record.pop("prompt", None)
            records.append(record)
    for item in data.get("questions") or []:
        if isinstance(item, dict) and item.get("status") == "resolved":
            records.append({"kind": "question", **item})
    if selector.type == "timeline":
        records.extend(
            {"kind": "timeline", **item}
            for item in data.get("events") or []
            if isinstance(item, dict)
        )
    return records


def _response_schema(
    selector: ResourceSelector,
    view: str,
    *,
    context: str = "resource",
) -> dict[str, Any]:
    pagination = {
        "type": "object",
        "required": ["count", "total", "next_cursor"],
        "properties": {
            "count": {"type": "integer"},
            "total": {"type": "integer"},
            "next_cursor": {"type": ["string", "null"]},
        },
    }
    common: dict[str, Any] = {
        "type": "object",
        "required": ["resource", "version", "view"],
        "properties": {
            "resource": {
                "type": "object",
                "required": ["project", "type", "id", "archived"],
            },
            "version": {"type": "integer"},
            "view": {"const": view},
        },
    }
    if context != "audit":
        common["required"].append("provenance")
        common["properties"]["provenance"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["checkout", "branch", "content_digest"],
            "properties": {
                "checkout": {"type": "string"},
                "branch": {"type": "string"},
                "content_digest": {
                    "type": "string",
                    "pattern": "^sha256:[0-9a-f]{64}$",
                },
            },
        }
    if selector.type == "plan" and view not in {"schema", "version"}:
        common["properties"]["in_flight"] = {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["run_id", "member", "section", "started_at"],
                "properties": {
                    "run_id": {"type": "string"},
                    "member": {"type": "string"},
                    "section": {"type": "string"},
                    "started_at": {"type": "string"},
                },
            },
        }
    if view in {"summary", "detail"}:
        common["required"] += [
            "title",
            "summary",
            "state",
            "blocking",
            "open_decisions",
            "next",
            "warnings",
        ]
        common["properties"].update(
            {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "state": {"type": "object"},
                "blocking": {"type": "array"},
                "open_decisions": {"type": "array"},
                "next": {"type": ["object", "null"]},
                "warnings": {"type": "array"},
            }
        )
        if context == "discovery":
            common["required"] += ["resources", "pagination"]
            common["properties"].update(
                {
                    "resources": {"type": "array"},
                    "pagination": pagination,
                }
            )
            if view == "detail":
                common["required"] += [
                    "source_format",
                    "resource_versions",
                    "milestones",
                    "followups",
                    "questions",
                ]
                common["properties"].update(
                    {
                        "source_format": {"type": ["string", "null"]},
                        "resource_versions": {"type": "object"},
                        "milestones": {"type": "array"},
                        "followups": {"type": "array"},
                        "questions": {"type": "array"},
                    }
                )
        elif context == "audit" and view == "detail":
            common["required"] += ["findings", "pagination", "violations"]
            common["properties"].update(
                {
                    "findings": {"type": "array"},
                    "pagination": pagination,
                    "violations": {"type": "array"},
                }
            )
        elif context == "resource" and view == "detail":
            common["required"] += [
                "metadata",
                "relations",
                "followups",
                "questions",
            ]
            common["properties"].update(
                {
                    "metadata": {"type": "object"},
                    "relations": {"type": "object"},
                    "followups": {"type": "array"},
                    "questions": {"type": "array"},
                    "items": {"type": "array"},
                    "events": {"type": "array"},
                }
            )
    elif view == "history":
        common["required"] += ["records", "pagination"]
        common["properties"].update(
            {
                "records": {"type": "array"},
                "pagination": pagination,
            }
        )
    elif view == "section":
        common["required"].append("section")
        common["properties"]["section"] = {
            "type": "object",
            "required": [
                "id",
                "heading",
                "text",
                "html",
                "declaration",
                "record",
                "comments",
            ],
            "properties": {
                "id": {"type": "string"},
                "heading": {"type": "string"},
                "text": {"type": "string"},
                "html": {"type": "string"},
                "declaration": {"type": ["string", "null"]},
                "record": {"type": ["object", "null"]},
                "comments": {"type": "array"},
            },
        }
    elif view == "version":
        pass
    elif view == "raw":
        common["required"].append("data")
        common["properties"]["data"] = {}
    elif view == "schema":
        common["required"] += [
            "schema_version",
            "response_schema",
            "response_schemas",
        ]
        common["properties"].update(
            {
                "schema_version": {"type": "integer"},
                "response_schema": {"type": "object"},
                "response_schemas": {"type": "object"},
                "storage_schema": {"type": "object"},
                "op_vocab": {"type": "object"},
                "dos_donts": {"type": "object"},
            }
        )
        if context != "audit":
            common["required"] += ["storage_schema", "op_vocab", "dos_donts"]
    return common


def _response_schemas(
    selector: ResourceSelector, *, context: str
) -> dict[str, dict[str, Any]]:
    views = ("summary", "detail", "version", "raw", "schema")
    if context != "audit":
        views = ("summary", "detail", "history", "version", "raw", "schema")
    return {view: _response_schema(selector, view, context=context) for view in views}


def resource_view(
    selector: ResourceSelector,
    version: int,
    data: dict[str, Any],
    *,
    view: str,
    provenance: dict[str, str],
    deps: list[dict[str, Any]] | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    include_prompts: bool = False,
    section: str | None = None,
    html_text: str | None = None,
    storage_schema: dict[str, Any] | None = None,
    op_vocab: dict[str, Any] | None = None,
    dos_donts: dict[str, Any] | None = None,
    response_context: str = "resource",
) -> dict[str, Any]:
    """Transform one canonical resource into the requested response view."""

    selected = normalize_view(view)
    if (
        selector.type == "plan"
        and selected in {"summary", "detail", "section", "raw"}
        and data.get("sections")
    ):
        data = {
            **data,
            "sections": with_section_attempts(
                selector.project,
                selector.id,
                data["sections"],
                provenance.get("checkout"),
            ),
        }
    if selector.type == "review" and selected in {"summary", "detail"}:
        checkout = provenance.get("checkout")
        if checkout:
            discovered = index_discovery(
                Path(checkout) / "docs",
                selector.project,
                Path(checkout) / "docs/state",
            )
            data = compose_review(
                data,
                discovered.get("inventory", []),
                discovered.get("sprints", []),
                selector.project,
                discovered,
            )
    dependencies = deps or []
    result: dict[str, Any]
    if selected == "summary":
        result = _summary(
            selector,
            version,
            data,
            deps=dependencies,
            include_prompts=include_prompts,
        )
    elif selected == "detail":
        result = _detail(
            selector,
            version,
            data,
            deps=dependencies,
            include_prompts=include_prompts,
        )
    elif selected == "history":
        page, pagination = paginate(
            _history_records(selector, data, include_prompts=include_prompts),
            cursor=cursor,
            limit=limit,
        )
        result = {
            "resource": selector.as_dict(),
            "version": version,
            "view": "history",
            "records": page,
            "pagination": pagination,
        }
    elif selected == "section":
        if selector.type == "evidence":
            result = _record_text_response(
                selector,
                version,
                data,
                section=section,
                html_text=html_text,
            )
        else:
            result = _section_response(
                selector,
                version,
                data,
                section=section,
                html_text=html_text,
            )
    elif selected == "version":
        result = {
            "resource": selector.as_dict(),
            "version": version,
            "view": "version",
        }
    elif selected == "raw":
        result = {
            "resource": selector.as_dict(),
            "version": version,
            "view": "raw",
            "data": data,
        }
    else:
        return {
            "resource": selector.as_dict(),
            "version": version,
            "view": "schema",
            "provenance": provenance,
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "response_schema": _response_schema(
                selector, selected, context=response_context
            ),
            "response_schemas": _response_schemas(selector, context=response_context),
            "storage_schema": storage_schema or {},
            "op_vocab": op_vocab or {},
            "dos_donts": dos_donts or {},
        }

    result["provenance"] = provenance
    if (
        selector.type == "plan"
        and not selector.archived
        and selected not in {"version", "schema"}
    ):
        runs = in_flight_by_plan(selector.project).get(selector.id)
        if runs:
            result["in_flight"] = runs
    return result


def discovery_view(
    project: str,
    raw: dict[str, Any],
    *,
    view: str,
    provenance: dict[str, str],
    cursor: str | None,
    limit: int | None,
    include_prompts: bool,
    storage_schema: dict[str, Any] | None = None,
    op_vocab: dict[str, Any] | None = None,
    dos_donts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build progressive project-discovery responses."""

    selected = normalize_view(view)
    selector = ResourceSelector(project=project, type="project", id=project)
    version = int((raw.get("resource_versions") or {}).get("project:project", 0))
    if selected == "raw":
        return {
            "resource": selector.as_dict(),
            "version": version,
            "view": "raw",
            "provenance": provenance,
            "data": raw,
        }
    if selected == "schema":
        return resource_view(
            selector,
            version,
            {},
            view="schema",
            provenance=provenance,
            storage_schema=storage_schema,
            op_vocab=op_vocab,
            dos_donts=dos_donts,
            response_context="discovery",
        )
    if selected == "history":
        page, pagination = paginate(
            list(raw.get("timeline") or []), cursor=cursor, limit=limit
        )
        return {
            "resource": selector.as_dict(),
            "version": version,
            "view": "history",
            "provenance": provenance,
            "records": [{"kind": "timeline", **item} for item in page],
            "pagination": pagination,
        }
    if include_prompts and selected == "summary":
        raise ViewRequestError(
            "invalid_view_option",
            "Summary responses never include full followup prompts.",
            "Use view='detail' with include_prompts=true.",
        )
    plan_resources = [
        {
            key: item.get(key)
            for key in (
                "slug",
                "type",
                "title",
                "status",
                "effective_status",
                "impl",
            )
            if item.get(key) not in (None, "")
        }
        | ({"effort": _plan_effort(item)} if _plan_effort(item) is not None else {})
        for item in raw.get("plans") or []
        if isinstance(item, dict)
    ]
    sprint_resources = [
        {
            "id": sprint.get("id"),
            "type": "sprint",
            "title": sprint.get("theme", ""),
            "status": sprint.get("status", ""),
            "items": len(sprint.get("items") or []),
            "completed": sum(
                (item.get("effective_status") or item.get("status"))
                in {"shipped", "done"}
                for item in sprint.get("items") or []
                if isinstance(item, dict)
            ),
            "blocked": sum(
                (item.get("effective_status") or item.get("status")) == "blocked"
                for item in sprint.get("items") or []
                if isinstance(item, dict)
            ),
            "capacity": _sprint_capacity(sprint),
        }
        for sprint in raw.get("sprints") or []
        if isinstance(sprint, dict)
    ]
    resources = sprint_resources + plan_resources
    page, pagination = paginate(resources, cursor=cursor, limit=limit)
    blockers = [
        {
            key: item.get(key)
            for key in ("id", "status", "summary", "owner", "next")
            if item.get(key) not in (None, "")
        }
        for item in raw.get("blockers") or []
        if isinstance(item, dict) and item.get("status", "open") != "resolved"
    ]
    followups = [
        item
        for item in raw.get("followups") or []
        if isinstance(item, dict) and item.get("status", "open") == "open"
    ]
    # The payload states an open-followup count only when it derived one: a
    # list-level read taken from the index has not opened the documents that
    # hold them, and zero would be a claim it cannot make.
    summary_source = raw.get("summary") or {}
    followup_clause = (
        f" {summary_source['open_followups']} open followups."
        if "open_followups" in summary_source
        else ""
    )
    result: dict[str, Any] = {
        "resource": selector.as_dict(),
        "version": version,
        "view": selected,
        "provenance": provenance,
        "title": project,
        "summary": f"{summary_source.get('plans', 0)} plans.{followup_clause}",
        "state": {
            "active_sprint_id": raw.get("active_sprint_id"),
            "source_format": raw.get("source_format", "legacy-index"),
            **(raw.get("summary") or {}),
        },
        "blocking": blockers,
        "open_decisions": [],
        "next": (
            _followup_record(followups[0], include_prompts=False) if followups else None
        ),
        "warnings": [],
        "resources": page,
        "pagination": pagination,
    }
    if isinstance(raw.get("review"), dict):
        result["review"] = raw["review"]
    if selected == "detail":
        result["source_format"] = raw.get("source_format")
        result["resource_versions"] = raw.get("resource_versions") or {}
        result["milestones"] = list(raw.get("milestones") or [])
        result["followups"] = [
            _followup_record(item, include_prompts=include_prompts)
            for item in followups
        ]
        result["questions"] = list(raw.get("questions") or [])
    return result


def audit_view(
    project: str,
    raw: dict[str, Any],
    *,
    view: str,
    cursor: str | None,
    limit: int | None,
) -> dict[str, Any]:
    """Build progressive audit responses while preserving raw opt-in."""

    selected = normalize_view(view)
    selector = ResourceSelector(project=project, type="audit", id=project)
    if selected == "raw":
        return {
            "resource": selector.as_dict(),
            "version": 0,
            "view": "raw",
            "data": raw,
        }
    if selected == "schema":
        schemas = _response_schemas(selector, context="audit")
        return {
            "resource": selector.as_dict(),
            "version": 0,
            "view": "schema",
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "response_schema": schemas["schema"],
            "response_schemas": schemas,
        }
    findings = list(raw.get("findings") or [])
    counts = raw.get("finding_counts") or {
        "total": len(findings),
        "by_severity": {},
        "by_category": {},
        "by_code": {},
    }
    errors = [
        {
            key: item.get(key)
            for key in ("category", "code", "message", "slug", "path")
            if item.get(key) not in (None, "")
        }
        for item in findings
        if item.get("severity") == "error"
    ]
    summary = {
        "resource": selector.as_dict(),
        "version": 0,
        "view": selected,
        "title": f"{project} audit",
        "summary": (
            f"{raw.get('conformant', 0)}/{raw.get('checked', 0)} conformant; "
            f"{counts.get('total', len(findings))} findings."
        ),
        "state": {
            "checked": raw.get("checked", 0),
            "conformant": raw.get("conformant", 0),
            "finding_counts": counts,
        },
        "blocking": errors,
        "open_decisions": [],
        "next": None,
        "warnings": [],
    }
    if selected == "summary":
        return summary
    if selected == "history":
        raise ViewRequestError(
            "invalid_view",
            "Audit resources do not have a history view.",
            "Use summary, detail, raw, or schema.",
        )
    page, pagination = paginate(findings, cursor=cursor, limit=limit)
    summary["findings"] = page
    summary["pagination"] = pagination
    summary["violations"] = list(raw.get("violations") or [])
    return summary


def _roadmap_finding_counts(findings: list[dict[str, Any]]) -> dict[str, Any]:
    by_severity: dict[str, int] = {}
    for finding in findings:
        severity = str(finding.get("severity") or "unknown")
        by_severity[severity] = by_severity.get(severity, 0) + 1
    return {"total": len(findings), "by_severity": by_severity}


def _pending_plan_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one pending roadmap row into the compact summary shape.

    The row is already computed, so this selects and renames rather than
    re-deriving: ``impl`` is the row's own progress fraction, the blocking ids
    are the ids the row already carries, and the section ids come from the
    row's own declaration projection. Nothing here computes a fact the report
    did not answer, which is what keeps this summary and the detail view from
    disagreeing.
    """

    blocking: set[str] = set()
    for key in (
        "explicit_blockers",
        "held_blockers",
        "gate_blockers",
        "decision_blockers",
    ):
        for blocker in row.get(key) or []:
            if isinstance(blocker, Mapping) and (
                identifier := str(blocker.get("id") or "").strip()
            ):
                blocking.add(identifier)
    for dependency in row.get("depends_on") or []:
        if not isinstance(dependency, Mapping) or dependency.get("satisfied"):
            continue
        reference = str(dependency.get("slug") or dependency.get("ref") or "").strip()
        if reference:
            blocking.add(reference)
    try:
        impl = round(float(row.get("progress_pct") or 0.0) / 100.0, 3)
    except (TypeError, ValueError):
        impl = 0.0
    return {
        "slug": str(row.get("slug") or ""),
        "status": str(row.get("status") or ""),
        "impl": impl,
        "ready": bool(row.get("ready")),
        "blocking": sorted(blocking),
        "implementable_sections": list(row.get("implementable_sections") or []),
    }


def _roadmap_project_summary(raw: dict[str, Any]) -> dict[str, Any]:
    findings = [
        item for item in raw.get("wiring_findings") or [] if isinstance(item, dict)
    ]
    completion = raw.get("completion") or {}
    schedule = raw.get("schedule") or {}
    dependency_ready = len(raw.get("ready_now") or [])
    dependency_blocked = len(raw.get("blocked") or [])
    dependency_deferred = len(raw.get("deferred") or [])
    summary = {
        "project": raw.get("project", ""),
        "completion": {
            key: completion.get(key, 0)
            for key in (
                "plans",
                "completed",
                "pending",
                "lifecycle_completion_pct",
                "implementation_pct",
            )
        },
        "ready": dependency_ready,
        "blocked": dependency_blocked,
        "deferred": dependency_deferred,
        "dependency_readiness": {
            "ready": dependency_ready,
            "blocked": dependency_blocked,
            "deferred": dependency_deferred,
        },
        "schedule_readiness": {
            key: schedule.get(key)
            for key in (
                "configured",
                "configuration_key",
                "window_sprints",
                "horizon_depth",
                "open_sprints",
                "earliest_open_sprint",
                "ready_sprints",
                "ready",
                "deferred",
            )
        },
        "finding_counts": _roadmap_finding_counts(findings),
    }
    sprint_rows = list(raw.get("summary_sprints") or [])
    if sprint_rows:
        summary["summary_sprints"] = sprint_rows
    scope = raw.get("scope")
    if isinstance(scope, Mapping) and str(scope.get("sprint") or "").strip():
        summary["pending_plans"] = [
            _pending_plan_row(row)
            for row in raw.get("pending_work") or []
            if isinstance(row, Mapping)
        ]
    return summary


def roadmap_view(
    raw: dict[str, Any],
    *,
    view: str,
    cursor: str | None,
    limit: int | None,
) -> dict[str, Any]:
    """Build compact, paginated roadmap views while preserving raw opt-in."""

    selected = normalize_view(view)
    if selected not in {"summary", "detail", "raw"}:
        raise ViewRequestError(
            "invalid_view",
            "Roadmaps support summary, detail, or raw views.",
            "Use summary for counts, detail for paginated findings, or raw.",
        )
    project = str(raw.get("project") or "")
    if selected == "raw":
        return {"project": project, "view": "raw", "data": raw}

    if project == "*":
        reports = [item for item in raw.get("projects") or [] if isinstance(item, dict)]
        projects = [_roadmap_project_summary(report) for report in reports]
        result: dict[str, Any] = {
            "project": "*",
            "view": selected,
            "portfolio": raw.get("portfolio") or {},
            "projects": projects,
        }
        if selected == "summary":
            return result
        findings = [
            {"project": report.get("project", ""), **finding}
            for report in reports
            for finding in report.get("wiring_findings") or []
            if isinstance(finding, dict)
        ]
        page, pagination = paginate(findings, cursor=cursor, limit=limit)
        result["findings"] = page
        result["finding_counts"] = _roadmap_finding_counts(findings)
        result["pagination"] = pagination
        return result

    if selected == "summary":
        return {
            "project": project,
            "view": "summary",
            **_roadmap_project_summary(raw),
            "critical_path": raw.get("critical_path") or {},
        }

    findings = [
        item for item in raw.get("wiring_findings") or [] if isinstance(item, dict)
    ]
    page, pagination = paginate(findings, cursor=cursor, limit=limit)
    result = dict(raw)
    result["view"] = "detail"
    result["wiring_findings"] = page
    result["finding_counts"] = _roadmap_finding_counts(findings)
    result["pagination"] = pagination
    return result


def _portfolio_row_view(row: Mapping[str, Any], view: str) -> dict[str, Any]:
    """Project one portfolio row, reducing only the coverage runs in summary.

    A row is already flat and already ranked, so the projection is a selection
    rather than a re-derivation: nothing here computes a figure the report did
    not answer, which is what keeps this view and any other reader of the same
    report from disagreeing.
    """
    rendered = dict(row)
    if view == "summary":
        coverage = row.get("coverage")
        if isinstance(coverage, Mapping):
            rendered["coverage"] = {
                key: value for key, value in coverage.items() if key != "runs"
            }
    return rendered


def portfolio_view(
    report: Mapping[str, Any],
    *,
    view: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Render the cross-project portfolio, one row per mounted project.

    The report arrives already ranked by uncovered critical hours descending
    and already carrying every column, so this view selects and paginates
    rather than deriving: ``summary`` returns the ranked table with each row's
    coverage reduced to its counts, ``detail`` keeps the coverage runs that
    explain which run stands on which path plan, and ``raw`` returns the report
    unchanged for a reader that wants the lossless answer.
    """
    selected = normalize_view(view)
    if selected not in {"summary", "detail", "raw"}:
        raise ViewRequestError(
            "invalid_view",
            "Portfolios support summary, detail, or raw views.",
            "Use summary for the ranked table, detail for coverage runs, or raw.",
        )
    rows = [row for row in report.get("rows") or [] if isinstance(row, Mapping)]
    if selected == "raw":
        return {"project": "*", "view": "raw", "data": dict(report)}
    page, pagination = paginate(list(rows), cursor=cursor, limit=limit)
    return {
        "project": "*",
        "view": selected,
        "columns": list(report.get("columns") or ()),
        # The totals are the report's own, so a paginated page states the fleet
        # figure rather than the page's sum — a caller reading the first page
        # of a large fleet must not mistake a page total for the fleet total.
        "totals": {
            "projects": report.get("projects", len(rows)),
            "live_width": report.get("live_width", 0),
            "unreconciled_runs": report.get("unreconciled_runs", 0),
            "uncovered_critical_hours": report.get("uncovered_critical_hours", 0.0),
        },
        "errors": list(report.get("errors") or []),
        "rows": [_portfolio_row_view(row, selected) for row in page],
        "pagination": pagination,
    }
