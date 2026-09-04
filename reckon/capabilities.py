"""Derived worker-capability figures rebuilt from committed run ledgers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import fmean, median
from typing import Any

from reckon import _plan_html, ledger
from reckon._store import _config_home, _mounts_path
from reckon.calibration import agent_configuration_key

_ORIENTATION_MINIMUM_SAMPLES = 2
_RESUMED_INPUT_OUTLIER_LIMIT = 60_000_000
_WRITE_TOOL_NAMES = frozenset(
    {"applypatch", "edit", "multiedit", "notebookedit", "write"}
)


def capabilities_path() -> Path:
    """Return the disposable cache path under reckon's config home."""

    return _config_home() / "cache" / "capabilities.json"


def _mounted_docs() -> dict[str, Path]:
    path = _mounts_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(project): Path(value).expanduser().resolve()
        for project, value in raw.items()
        if Path(value).expanduser().resolve().is_dir()
    }


def _plan_estimates(docs_dir: Path) -> dict[str, float]:
    estimates: dict[str, float] = {}
    root = docs_dir / "plans"
    if not root.is_dir():
        return estimates
    for path in sorted(root.glob("*.html")):
        record = _plan_html.parse_meta(path)
        try:
            hours = float(record.get("effort_hours"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(hours) and hours > 0:
            estimates[str(record.get("slug") or path.stem)] = hours
    return estimates


def _descriptive_changed_lines(run: Mapping[str, Any]) -> dict[str, int] | None:
    """Copy scoped line counts for display without making them a score."""

    value = run.get("changed_lines")
    if not isinstance(value, Mapping):
        return None
    fields: dict[str, int] = {}
    for name in ("added", "removed", "files"):
        raw = value.get(name)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            fields[name] = raw
    return fields or None


def _quantile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "values": [round(value, 6) for value in ordered],
        "mean": round(fmean(ordered), 6),
        "median": round(median(ordered), 6),
        "minimum": round(ordered[0], 6),
        "maximum": round(ordered[-1], 6),
        "p10": round(_quantile(ordered, 0.1), 6),
        "p90": round(_quantile(ordered, 0.9), 6),
    }


def _success_curve(
    observations: Sequence[dict[str, Any]], bin_width_hours: float
) -> list[dict[str, Any]]:
    buckets: dict[float, list[bool]] = defaultdict(list)
    for observation in observations:
        hours = float(observation["estimated_hours"])
        boundary = math.ceil(hours / bin_width_hours) * bin_width_hours
        buckets[round(boundary, 6)].append(bool(observation["success"]))
    curve: list[dict[str, Any]] = []
    successes = samples = 0
    for boundary in sorted(buckets):
        outcomes = buckets[boundary]
        successes += sum(outcomes)
        samples += len(outcomes)
        curve.append(
            {
                "estimated_hours": boundary,
                "samples": samples,
                "successes": successes,
                "success_rate": round(successes / samples, 6),
            }
        )
    return curve


def _configuration_from_key(key: str) -> dict[str, Any]:
    """Decode a canonical agent key without trusting it to remain JSON."""
    try:
        configuration = json.loads(key)
    except json.JSONDecodeError:
        configuration = {"key": key}
    if not isinstance(configuration, Mapping):
        configuration = {"key": key}
    return dict(configuration)


def _outcome_exclusion_reason(run: Mapping[str, Any]) -> str | None:
    """Apply the ledger worker-pass eligibility rule to one outcome."""

    gate = str(run.get("gate") or "")
    if gate == "passed":
        return None
    if gate == "failed":
        classification = str(run.get("failure_classification") or "")
        if classification == "work-rejected":
            return None
        if classification in ledger.FAILURE_CLASSIFICATIONS:
            return classification
        return "unclassified_failure"
    return "gate_not_run"


def _measured_number(value: Any) -> float | None:
    """Return a finite non-negative measurement, without treating bool as one."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    measured = float(value)
    return measured if math.isfinite(measured) and measured >= 0 else None


def _input_tokens(run: Mapping[str, Any]) -> float | None:
    """Read the charged worker input carried by one promoted run."""

    direct = _measured_number(run.get("input_tokens"))
    if direct is not None:
        return direct
    budget = run.get("budget")
    if not isinstance(budget, Mapping):
        return None
    tokens = budget.get("tokens")
    if not isinstance(tokens, Mapping):
        return None
    return _charged_input_from_usage(tokens)


def _coordinator_input_tokens(run: Mapping[str, Any]) -> float | None:
    """Read measured authoring input without turning an unknown value into zero."""

    definition = run.get("node_definition")
    coordinator = (
        definition.get("coordinator") if isinstance(definition, Mapping) else None
    )
    if not isinstance(coordinator, Mapping):
        coordinator = run.get("coordinator")
    if not isinstance(coordinator, Mapping):
        return None
    authoring_turn = coordinator.get("authoring_turn")
    if not isinstance(authoring_turn, Mapping):
        return None
    tokens = authoring_turn.get("tokens")
    if not isinstance(tokens, Mapping):
        return None
    return _measured_number(tokens.get("input_tokens"))


def _stream_path(run: Mapping[str, Any]) -> Path | None:
    """Resolve the durable worker stream named by a committed run."""

    manifest_path = str(run.get("manifest_path") or "").strip()
    candidates = []
    if manifest_path:
        candidates.append(
            Path(manifest_path).expanduser().resolve().parent / "stream.jsonl"
        )
    run_id = str(run.get("run_id") or "").strip()
    if run_id:
        candidates.append(_config_home() / "crew" / "runs" / run_id / "stream.jsonl")
    return next((path for path in candidates if path.is_file()), None)


def _orientation_stream_paths(run: Mapping[str, Any]) -> tuple[Path, ...]:
    """Return a node's initial and resumed streams in execution order."""

    directories = []
    manifest_path = str(run.get("manifest_path") or "").strip()
    if manifest_path:
        directories.append(Path(manifest_path).expanduser().resolve().parent)
    run_id = str(run.get("run_id") or "").strip()
    if run_id:
        directories.append(_config_home() / "crew" / "runs" / run_id)

    paths: list[Path] = []
    seen: set[Path] = set()
    for directory in directories:
        candidates = [directory / "stream.jsonl"]
        candidates.extend(
            sorted(
                directory.glob("resume-*.jsonl"),
                key=lambda path: (
                    int(path.stem.removeprefix("resume-"))
                    if path.stem.removeprefix("resume-").isdigit()
                    else math.inf
                ),
            )
        )
        for path in candidates:
            if path.is_file() and path not in seen:
                seen.add(path)
                paths.append(path)
    return tuple(paths)


def _charged_input_from_usage(usage: Any) -> float | None:
    """Read one request's full charged input, including cached context."""

    if not isinstance(usage, Mapping):
        return None
    direct = _measured_number(usage.get("input_tokens"))
    cache_parts = [
        _measured_number(usage.get(name))
        for name in ("cache_read_input_tokens", "cache_creation_input_tokens")
    ]
    measured_cache_parts = [value for value in cache_parts if value is not None]
    if measured_cache_parts:
        parts = ([direct] if direct is not None else []) + measured_cache_parts
    else:
        # Streams with ``cached_input_tokens`` report it as the subset of the
        # already-total input count. Adding it again would double-charge cache
        # hits. Streams with separate read/create fields report disjoint parts.
        parts = [direct] if direct is not None else []
    return sum(parts) if parts else None


def _message_usage(event: Mapping[str, Any]) -> Any:
    message = event.get("message")
    return message.get("usage") if isinstance(message, Mapping) else None


def _cumulative_event_input(event: Mapping[str, Any]) -> float | None:
    """Read a cumulative usage snapshot when a stream publishes one."""

    if event.get("type") == "token_count":
        info = event.get("info")
        usage = info.get("total_token_usage") if isinstance(info, Mapping) else None
        return _charged_input_from_usage(usage)
    if event.get("type") in {"turn.completed", "result"}:
        return _charged_input_from_usage(event.get("usage"))
    return None


def _normalised_tool_name(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _tool_write_targets(block: Mapping[str, Any]) -> tuple[str, ...]:
    name = _normalised_tool_name(block.get("name"))
    if not any(name.endswith(candidate) for candidate in _WRITE_TOOL_NAMES):
        return ()
    arguments = block.get("input")
    if not isinstance(arguments, Mapping):
        arguments = block.get("arguments")
    if not isinstance(arguments, Mapping):
        return ()

    targets = [
        str(arguments[key])
        for key in ("file_path", "notebook_path", "path")
        if arguments.get(key)
    ]
    patch = arguments.get("patch")
    if isinstance(patch, str):
        prefixes = ("*** Add File: ", "*** Delete File: ", "*** Update File: ")
        targets.extend(
            line.removeprefix(prefix).strip()
            for line in patch.splitlines()
            for prefix in prefixes
            if line.startswith(prefix)
        )
    return tuple(targets)


def _event_write_targets(event: Mapping[str, Any]) -> tuple[str, ...]:
    item = event.get("item")
    if isinstance(item, Mapping) and item.get("type") == "file_change":
        changes = item.get("changes")
        if isinstance(changes, list):
            return tuple(
                str(change["path"])
                for change in changes
                if isinstance(change, Mapping) and change.get("path")
            )

    message = event.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return ()
    return tuple(
        target
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
        for target in _tool_write_targets(block)
    )


def _target_is_declared(target: str, scopes: Sequence[str]) -> bool:
    normalised = posixpath.normpath(target.strip().replace("\\", "/"))
    for scope in scopes:
        declared = posixpath.normpath(scope)
        if declared == ".":
            if not normalised.startswith("/") and not normalised.startswith("../"):
                return True
            continue
        if (
            normalised == declared
            or normalised.startswith(declared.rstrip("/") + "/")
            or normalised.endswith("/" + declared)
            or "/" + declared.rstrip("/") + "/" in normalised
        ):
            return True
    return False


def _orientation_input_tokens(run: Mapping[str, Any]) -> float | None:
    """Measure charged input consumed before the first declared-path write."""

    direct = _measured_number(run.get("orientation_input_tokens"))
    if direct is not None:
        return direct
    scopes = _write_paths(run)
    if not scopes:
        return None

    prompt_input = 0.0
    prompt_input_measured = False
    cumulative_input: float | None = None
    seen_messages: set[str] = set()
    anonymous_message = 0
    for stream in _orientation_stream_paths(run):
        try:
            with stream.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, Mapping):
                        continue

                    message = event.get("message")
                    if event.get("type") == "assistant" and isinstance(
                        message, Mapping
                    ):
                        message_id = str(message.get("id") or "").strip()
                        if not message_id:
                            anonymous_message += 1
                            message_id = f"anonymous-{anonymous_message}"
                        if message_id not in seen_messages:
                            seen_messages.add(message_id)
                            measured = _charged_input_from_usage(_message_usage(event))
                            if measured is not None:
                                prompt_input += measured
                                prompt_input_measured = True

                    measured_cumulative = _cumulative_event_input(event)
                    if measured_cumulative is not None:
                        cumulative_input = measured_cumulative

                    if any(
                        _target_is_declared(target, scopes)
                        for target in _event_write_targets(event)
                    ):
                        if prompt_input_measured:
                            return prompt_input
                        return cumulative_input
        except OSError:
            continue
    return None


def _tool_steps(run: Mapping[str, Any]) -> float | None:
    """Count completed tool interactions, preferring a ledgered measurement."""

    direct = _measured_number(run.get("tool_steps"))
    if direct is not None:
        return direct
    for block_name in ("throughput", "budget"):
        block = run.get(block_name)
        if isinstance(block, Mapping):
            measured = _measured_number(block.get("tool_steps"))
            if measured is not None:
                return measured

    stream = _stream_path(run)
    if stream is None:
        return None
    completed_items = 0
    tool_use_blocks = 0
    try:
        with stream.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, Mapping):
                    continue
                item = event.get("item")
                if (
                    event.get("type") == "item.completed"
                    and isinstance(item, Mapping)
                    and str(item.get("type") or "")
                    not in {
                        "",
                        "agent_message",
                        "reasoning",
                    }
                ):
                    completed_items += 1
                if event.get("type") != "assistant":
                    continue
                message = event.get("message")
                content = (
                    message.get("content") if isinstance(message, Mapping) else None
                )
                if isinstance(content, list):
                    tool_use_blocks += sum(
                        isinstance(block, Mapping) and block.get("type") == "tool_use"
                        for block in content
                    )
    except OSError:
        return None
    return float(completed_items or tool_use_blocks)


def _write_paths(run: Mapping[str, Any]) -> tuple[str, ...]:
    """Return normalised declared paths from the durable node definition."""

    definition = run.get("node_definition")
    raw_paths = (
        definition.get("write_paths") if isinstance(definition, Mapping) else None
    )
    if not isinstance(raw_paths, list):
        return ()
    normalised = []
    for raw in raw_paths:
        value = str(raw).strip().replace("\\", "/")
        if value:
            normalised.append(posixpath.normpath(value))
    return tuple(sorted(set(normalised)))


def _paths_overlap(first: Sequence[str], second: Sequence[str]) -> bool:
    """Return whether two declared file-or-directory scopes intersect."""

    for left in first:
        for right in second:
            if left == "." or right == ".":
                return True
            if (
                left == right
                or left.startswith(right.rstrip("/") + "/")
                or right.startswith(left.rstrip("/") + "/")
            ):
                return True
    return False


def _redispatched(run: Mapping[str, Any]) -> bool:
    """Read either explicit redispatch lineage or its attempt-number fallback."""

    if str(run.get("attempt_kind") or "") == "redispatch":
        return True
    lineage = run.get("lineage")
    if isinstance(lineage, Mapping) and lineage.get("kind") == "redispatch":
        return True
    try:
        return int(run.get("attempt") or 1) > 1
    except (TypeError, ValueError):
        return False


def _routing_outcome_exclusion(run: Mapping[str, Any]) -> str | None:
    """Name why a committed outcome cannot contribute to routing quality."""

    lineage = run.get("lineage")
    if isinstance(lineage, Mapping) and lineage.get("kind") == "shadow":
        return "shadow"
    exclusion = ledger.measurement_exclusion_reason(run)
    if exclusion:
        return exclusion
    return _outcome_exclusion_reason(run)


def _median_or_none(values: Sequence[float]) -> float | None:
    return round(float(median(values)), 6) if values else None


def _charged_cost(median_input: float | None, rework_rate: float) -> float | None:
    if median_input is None or rework_rate >= 1:
        return None
    return round(median_input / (1.0 - rework_rate), 6)


def _changed_line_count(run: Mapping[str, Any]) -> float | None:
    changed = run.get("changed_lines")
    if not isinstance(changed, Mapping):
        return None
    parts = [_measured_number(changed.get(name)) for name in ("added", "removed")]
    measured = [value for value in parts if value is not None]
    total = sum(measured) if measured else 0.0
    return total if total > 0 else None


def _orientation_floor(observations: Sequence[dict[str, Any]]) -> dict[str, Any]:
    pairs = [
        (float(item["changed_lines"]), float(item["input_tokens"]))
        for item in observations
        if item["changed_lines"] is not None
        and item["input_tokens"] is not None
        and float(item["input_tokens"]) <= _RESUMED_INPUT_OUTLIER_LIMIT
    ]
    input_outliers = sum(
        item["changed_lines"] is not None
        and item["input_tokens"] is not None
        and float(item["input_tokens"]) > _RESUMED_INPUT_OUTLIER_LIMIT
        for item in observations
    )
    stream_measurements = [
        float(item["orientation_input_tokens"])
        for item in observations
        if item["orientation_input_tokens"] is not None
    ]
    result: dict[str, Any] = {
        "status": "unknown",
        "grouped_by": ["model", "role", "spec_level"],
        "formula": (
            "input_tokens = intercept_input_tokens + "
            "tokens_per_changed_line * changed_lines"
        ),
        "samples": len(pairs),
        "minimum_samples": _ORIENTATION_MINIMUM_SAMPLES,
        "input_outliers_excluded": input_outliers,
        "intercept_input_tokens": None,
        "tokens_per_changed_line": None,
        "stream_samples": len(stream_measurements),
        "median_stream_input_tokens_before_first_write": _median_or_none(
            stream_measurements
        ),
    }
    if len(pairs) < _ORIENTATION_MINIMUM_SAMPLES:
        result["reason"] = "fewer_than_two_observations"
        return result

    mean_lines = fmean(lines for lines, _input in pairs)
    mean_input = fmean(input_tokens for _lines, input_tokens in pairs)
    denominator = sum((lines - mean_lines) ** 2 for lines, _input in pairs)
    if denominator == 0:
        result["reason"] = "no_changed_line_variation"
        return result
    slope = (
        sum(
            (lines - mean_lines) * (input_tokens - mean_input)
            for lines, input_tokens in pairs
        )
        / denominator
    )
    intercept = mean_input - slope * mean_lines
    result.update(
        {
            "status": "measured",
            "intercept_input_tokens": round(intercept, 6),
            "tokens_per_changed_line": round(slope, 6),
        }
    )
    return result


def derive_routing(
    mounted_docs: Mapping[str, str | Path],
) -> dict[str, Any]:
    """Derive current routing evidence from every supplied committed ledger.

    Rows pool projects only after grouping by the complete worker configuration
    that a coordinator selects: model, effort, specification ownership and role.
    Each metric names its own sample depth so missing stream or coordinator
    measurements remain unknown rather than becoming zero-cost work.
    """

    source_versions: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    excluded: dict[str, int] = defaultdict(int)
    for project, raw_docs in sorted(mounted_docs.items()):
        docs_dir = Path(raw_docs).expanduser().resolve()
        data, version = ledger.load(str(project), root=docs_dir.parent)
        source_versions[str(project)] = version
        for ledger_index, raw_run in enumerate(data["runs"]):
            run = dict(raw_run)
            run["_project"] = str(project)
            run["_ledger_index"] = ledger_index
            records.append(run)

    usable_outcomes: list[dict[str, Any]] = []
    for run in records:
        reason = _routing_outcome_exclusion(run)
        if reason:
            excluded[reason] += 1
            continue
        usable_outcomes.append(run)

    later_paths: dict[tuple[str, str], list[tuple[int, tuple[str, ...]]]] = defaultdict(
        list
    )
    for run in usable_outcomes:
        plan = str(run.get("plan") or "").strip()
        paths = _write_paths(run)
        if plan and paths:
            later_paths[(run["_project"], plan)].append((run["_ledger_index"], paths))

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in usable_outcomes:
        agent = run.get("agent")
        model = (
            str(agent.get("model") or "").strip() if isinstance(agent, Mapping) else ""
        )
        effort = (
            str(agent.get("effort") or "").strip() if isinstance(agent, Mapping) else ""
        )
        spec_level = str(run.get("spec_level") or "").strip()
        role = str(run.get("role") or "").strip()
        missing = [
            name
            for name, value in (
                ("model", model),
                ("effort", effort),
                ("spec_level", spec_level),
                ("role", role),
            )
            if not value
        ]
        if missing:
            excluded["missing_" + "_and_".join(missing)] += 1
            continue
        own_paths = _write_paths(run)
        plan = str(run.get("plan") or "").strip()
        reworked = bool(own_paths and plan) and any(
            index > run["_ledger_index"] and _paths_overlap(own_paths, paths)
            for index, paths in later_paths.get((run["_project"], plan), ())
        )
        grouped[(model, effort, spec_level, role)].append(
            {
                "passed": str(run.get("gate") or "") == "passed",
                "reworked": reworked,
                "redispatched": _redispatched(run),
                "tool_steps": _tool_steps(run),
                "input_tokens": _input_tokens(run),
                "coordinator_input_tokens": _coordinator_input_tokens(run),
                "changed_lines": _changed_line_count(run),
                "orientation_input_tokens": _orientation_input_tokens(run),
            }
        )

    orientation_observations: dict[tuple[str, str, str], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for (model, _effort, spec_level, role), observations in grouped.items():
        orientation_observations[(model, role, spec_level)].extend(observations)
    orientation_floors = {
        key: _orientation_floor(observations)
        for key, observations in orientation_observations.items()
    }

    rows: list[dict[str, Any]] = []
    for (model, effort, spec_level, role), observations in sorted(grouped.items()):
        samples = len(observations)
        passed = sum(bool(item["passed"]) for item in observations)
        reworked = sum(bool(item["reworked"]) for item in observations)
        redispatched = sum(bool(item["redispatched"]) for item in observations)
        rework_rate = reworked / samples
        tool_steps = [
            float(item["tool_steps"])
            for item in observations
            if item["tool_steps"] is not None
        ]
        worker_inputs = [
            float(item["input_tokens"])
            for item in observations
            if item["input_tokens"] is not None
        ]
        coordinator_inputs = [
            float(item["coordinator_input_tokens"])
            for item in observations
            if item["coordinator_input_tokens"] is not None
        ]
        combined_inputs = [
            float(item["input_tokens"]) + float(item["coordinator_input_tokens"])
            for item in observations
            if item["input_tokens"] is not None
            and item["coordinator_input_tokens"] is not None
        ]
        median_worker_input = _median_or_none(worker_inputs)
        median_combined_input = _median_or_none(combined_inputs)
        rows.append(
            {
                "model": model,
                "effort": effort,
                "spec_level": spec_level,
                "role": role,
                "samples": samples,
                "passed": passed,
                "pass_rate": round(passed / samples, 6),
                "reworked": reworked,
                "rework_rate": round(rework_rate, 6),
                "redispatched": redispatched,
                "redispatch_rate": round(redispatched / samples, 6),
                "tool_step_samples": len(tool_steps),
                "median_tool_steps": _median_or_none(tool_steps),
                "input_samples": len(worker_inputs),
                "median_input_tokens": median_worker_input,
                "coordinator_input_samples": len(coordinator_inputs),
                "median_coordinator_input_tokens": _median_or_none(coordinator_inputs),
                "worker_plus_coordinator_samples": len(combined_inputs),
                "median_worker_plus_coordinator_input_tokens": median_combined_input,
                "orientation_floor": orientation_floors[(model, role, spec_level)],
                "per_run_cost": {
                    "label": "immediate spend; a short window can reflect this",
                    "short_window_can_reflect": True,
                    "worker_only_input_tokens": median_worker_input,
                    "worker_plus_coordinator_input_tokens": median_combined_input,
                },
                "rework_charged_cost_per_durable_node": {
                    "label": (
                        "back-loaded spend; a short window cannot yet reflect "
                        "rework that has not surfaced"
                    ),
                    "short_window_can_reflect": False,
                    "formula": "median input / (1 - rework rate)",
                    "worker_only_input_tokens": _charged_cost(
                        median_worker_input, rework_rate
                    ),
                    "worker_plus_coordinator_input_tokens": _charged_cost(
                        median_combined_input, rework_rate
                    ),
                },
            }
        )

    derived = {
        "source": "committed_run_ledgers_and_durable_worker_streams",
        "projects": sorted(source_versions),
        "ledger_versions": source_versions,
        "rows": rows,
        "excluded": dict(sorted(excluded.items())),
    }
    canonical = json.dumps(derived, sort_keys=True, separators=(",", ":"))
    return {**derived, "source_digest": hashlib.sha256(canonical.encode()).hexdigest()}


def routing_surface(
    project: str,
    *,
    checkout_path: str | Path | None = None,
) -> dict[str, Any]:
    """Derive routing across all mounts, remapping one project to its worktree."""

    mounts: dict[str, str | Path] = _mounted_docs()
    if checkout_path is not None:
        checkout = Path(checkout_path).expanduser().resolve()
        docs_dir = checkout if checkout.name == "docs" else checkout / "docs"
        if not docs_dir.is_dir():
            raise ValueError(f"checkout {checkout} has no readable docs directory")
        mounts[str(project)] = docs_dir
    return derive_routing(mounts)


def derive_capabilities(
    mounted_docs: Mapping[str, str | Path],
    *,
    success_threshold: float = 0.8,
    bin_width_hours: float = 1.0,
) -> dict[str, Any]:
    """Compute capability figures solely from mounted plans and ledgers."""

    if not 0 < success_threshold <= 1:
        raise ValueError("success_threshold must be greater than zero and at most one")
    if not math.isfinite(bin_width_hours) or bin_width_hours <= 0:
        raise ValueError("bin_width_hours must be positive")

    observations_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exclusions_by_agent: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    shadow_observations: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    excluded = {
        "scope_changed": 0,
        "stalled": 0,
        "unusable_completion": 0,
        "invalid": 0,
    }
    source_versions: dict[str, int] = {}
    for project, raw_docs in sorted(mounted_docs.items()):
        docs_dir = Path(raw_docs).expanduser().resolve()
        estimates = _plan_estimates(docs_dir)
        data, version = ledger.load(str(project), root=docs_dir.parent)
        source_versions[str(project)] = version
        runs_by_id = {
            str(run.get("run_id") or ""): run
            for run in data["runs"]
            if run.get("run_id")
        }
        for run in data["runs"]:
            agent_key = agent_configuration_key(run)
            exclusion = ledger.measurement_exclusion_reason(run)
            if exclusion:
                excluded[exclusion] += 1
                if agent_key:
                    exclusions_by_agent[agent_key][exclusion] += 1
                continue
            plan = str(run.get("plan") or "")
            try:
                estimated_hours = float(estimates[plan])
                actual_hours = float(run.get("worker_seconds")) / 3600.0
            except (KeyError, TypeError, ValueError):
                excluded["invalid"] += 1
                continue
            if not agent_key or not math.isfinite(actual_hours) or actual_hours <= 0:
                excluded["invalid"] += 1
                continue
            observation = {
                "project": str(project),
                "run_id": str(run.get("run_id") or ""),
                "plan": plan,
                "estimated_hours": estimated_hours,
                "actual_hours": round(actual_hours, 6),
                "success": str(run.get("gate") or "") == "passed",
                "gate": str(run.get("gate") or ""),
                "tests_added": run.get("tests_added"),
                "changed_lines": _descriptive_changed_lines(run),
                "spec_level": str(run.get("spec_level") or "") or None,
            }
            lineage = run.get("lineage")
            if isinstance(lineage, Mapping) and lineage.get("kind") == "shadow":
                primary_run_id = str(lineage.get("primary_run_id") or "")
                primary = runs_by_id.get(primary_run_id) or {}
                observation.update(
                    {
                        "primary_run_id": primary_run_id,
                        "primary_gate": str(primary.get("gate") or "") or None,
                        "primary_success": (
                            str(primary.get("gate") or "") == "passed"
                            if primary
                            else None
                        ),
                        # The sole control predicate: a confounded pair must
                        # never be pooled with a controlled one when this
                        # slice is read for a qualification verdict.
                        "controlled": ledger.shadow_controlled(lineage),
                    }
                )
                shadow_observations[
                    (agent_key, str(run.get("spec_level") or ""))
                ].append(observation)
                continue
            exclusion = _outcome_exclusion_reason(run)
            if exclusion:
                excluded[exclusion] = excluded.get(exclusion, 0) + 1
                exclusions_by_agent[agent_key][exclusion] += 1
                continue
            observations_by_agent[agent_key].append(observation)

    configurations = []
    for key in sorted(set(observations_by_agent) | set(exclusions_by_agent)):
        observations = sorted(
            observations_by_agent[key],
            key=lambda item: (item["estimated_hours"], item["project"], item["run_id"]),
        )
        # Speed direction is neutral estimated hours per actual worker-hour:
        # above one is faster and below one is slower.
        speed_values = [
            item["estimated_hours"] / item["actual_hours"] for item in observations
        ]
        curve = _success_curve(observations, bin_width_hours)
        passing_sizes = [
            point["estimated_hours"]
            for point in curve
            if point["success_rate"] >= success_threshold
        ]
        configurations.append(
            {
                "key": key,
                "configuration": _configuration_from_key(key),
                "runs": len(observations),
                "success_threshold": success_threshold,
                "success_by_estimated_hours": curve,
                "competence_horizon_hours": max(passing_sizes)
                if passing_sizes
                else None,
                "speed": _distribution(speed_values) if speed_values else None,
                "excluded": dict(sorted(exclusions_by_agent[key].items())),
                "observations": observations,
            }
        )

    shadow_slices = []
    for (key, spec_level), rows in sorted(shadow_observations.items()):
        observations = sorted(
            rows,
            key=lambda item: (item["project"], item["run_id"]),
        )
        controlled = [item for item in observations if item["controlled"]]
        # Qualification depth is distinct primaries covered, not shadow rows —
        # two shadows of the same primary demonstrate the harness can produce
        # a controlled pair, not that a second node was qualified.
        qualification_depth = len(
            {
                item["primary_run_id"]
                for item in controlled
                if item.get("primary_run_id")
            }
        )
        shadow_slices.append(
            {
                "key": key,
                "configuration": _configuration_from_key(key),
                "spec_level": spec_level or None,
                "runs": len(observations),
                "controlled_runs": len(controlled),
                "qualification_depth": qualification_depth,
                "observations": observations,
            }
        )

    derived = {
        "source": "committed_run_ledgers",
        "projects": sorted(source_versions),
        "ledger_versions": source_versions,
        "success_threshold": success_threshold,
        "bin_width_hours": bin_width_hours,
        "excluded": excluded,
        "configurations": configurations,
        "shadow_slices": shadow_slices,
        "routing": derive_routing(mounted_docs),
    }
    canonical = json.dumps(derived, sort_keys=True, separators=(",", ":"))
    return {**derived, "source_digest": hashlib.sha256(canonical.encode()).hexdigest()}


def rebuild_capabilities(
    *,
    mounted_docs: Mapping[str, str | Path] | None = None,
    success_threshold: float = 0.8,
    bin_width_hours: float = 1.0,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Rebuild and atomically replace the disposable capabilities cache."""

    record = derive_capabilities(
        mounted_docs if mounted_docs is not None else _mounted_docs(),
        success_threshold=success_threshold,
        bin_width_hours=bin_width_hours,
    )
    target = Path(path) if path is not None else capabilities_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, target)
    return record


def load_capabilities(path: str | Path | None = None) -> dict[str, Any]:
    """Read the disposable cache without scanning ledgers or rebuilding it."""

    target = Path(path) if path is not None else capabilities_path()
    if not target.is_file():
        return {
            "source": "committed_run_ledgers",
            "ledger_versions": {},
            "configurations": [],
            "routing": {"rows": []},
            "cache_status": "missing",
        }
    data = json.loads(target.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"capabilities cache {target} does not hold an object")
    return data


def inspect_capabilities(
    *,
    mounted_docs: Mapping[str, str | Path] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Report cache freshness against current ledger versions.

    This is intentionally an explicit inspection operation.  Dispatch only
    reads the cache, so a cold cache never turns first dispatch into an
    all-repository synchronous scan.
    """

    mounts = mounted_docs if mounted_docs is not None else _mounted_docs()
    cached = load_capabilities(path)
    cached_versions = cached.get("ledger_versions")
    if not isinstance(cached_versions, Mapping):
        cached_versions = {}
    routing = cached.get("routing")
    routing_rows = routing.get("rows") if isinstance(routing, Mapping) else None
    current_versions: dict[str, int] = {}
    for project, raw_docs in sorted(mounts.items()):
        docs_dir = Path(raw_docs).expanduser().resolve()
        _data, version = ledger.load(str(project), root=docs_dir.parent)
        current_versions[str(project)] = version
    changed = sorted(
        project
        for project in set(current_versions) | set(cached_versions)
        if current_versions.get(project) != cached_versions.get(project)
    )
    return {
        "path": str(Path(path) if path is not None else capabilities_path()),
        "exists": (Path(path) if path is not None else capabilities_path()).is_file(),
        "stale": bool(changed),
        "changed_projects": changed,
        "cached_ledger_versions": dict(cached_versions),
        "current_ledger_versions": current_versions,
        "configurations": len(cached.get("configurations") or []),
        "routing_rows": len(routing_rows) if isinstance(routing_rows, list) else 0,
    }


def project_cache_status(
    cache: Mapping[str, Any], project: str, *, root: str | Path
) -> str:
    """Check one project's cache key without scanning any other repository."""

    versions = cache.get("ledger_versions")
    if not isinstance(versions, Mapping) or project not in versions:
        return "untracked"
    _data, current = ledger.load(project, root=root)
    return "fresh" if versions.get(project) == current else "stale"
