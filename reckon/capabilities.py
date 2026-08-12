"""Derived worker-capability figures rebuilt from committed run ledgers."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import fmean, median
from typing import Any

from reckon import _plan_html, ledger
from reckon._store import _config_home, _mounts_path
from reckon.calibration import agent_configuration_key


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
    excluded = {"scope_changed": 0, "unusable_completion": 0, "invalid": 0}
    source_versions: dict[str, int] = {}
    for project, raw_docs in sorted(mounted_docs.items()):
        docs_dir = Path(raw_docs).expanduser().resolve()
        estimates = _plan_estimates(docs_dir)
        data, version = ledger.load(str(project), root=docs_dir.parent)
        source_versions[str(project)] = version
        for run in data["runs"]:
            if run.get("scope_changed"):
                excluded["scope_changed"] += 1
                continue
            if str(run.get("completed_at_source") or "") not in {
                "terminal_event",
                "stream_mtime",
            }:
                excluded["unusable_completion"] += 1
                continue
            plan = str(run.get("plan") or "")
            agent_key = agent_configuration_key(run)
            try:
                estimated_hours = float(estimates[plan])
                actual_hours = float(run.get("worker_seconds")) / 3600.0
            except (KeyError, TypeError, ValueError):
                excluded["invalid"] += 1
                continue
            if not agent_key or not math.isfinite(actual_hours) or actual_hours <= 0:
                excluded["invalid"] += 1
                continue
            observations_by_agent[agent_key].append(
                {
                    "project": str(project),
                    "run_id": str(run.get("run_id") or ""),
                    "plan": plan,
                    "estimated_hours": estimated_hours,
                    "actual_hours": round(actual_hours, 6),
                    "success": str(run.get("gate") or "") == "passed",
                    "gate": str(run.get("gate") or ""),
                    "tests_added": run.get("tests_added"),
                    "changed_lines": _descriptive_changed_lines(run),
                }
            )

    configurations = []
    for key in sorted(observations_by_agent):
        observations = sorted(
            observations_by_agent[key],
            key=lambda item: (item["estimated_hours"], item["project"], item["run_id"]),
        )
        speed_values = [
            item["estimated_hours"] / item["actual_hours"] for item in observations
        ]
        curve = _success_curve(observations, bin_width_hours)
        passing_sizes = [
            point["estimated_hours"]
            for point in curve
            if point["success_rate"] >= success_threshold
        ]
        try:
            configuration = json.loads(key)
        except json.JSONDecodeError:
            configuration = {"key": key}
        if not isinstance(configuration, Mapping):
            configuration = {"key": key}
        configurations.append(
            {
                "key": key,
                "configuration": dict(configuration),
                "runs": len(observations),
                "success_threshold": success_threshold,
                "success_by_estimated_hours": curve,
                "competence_horizon_hours": max(passing_sizes)
                if passing_sizes
                else None,
                "speed": _distribution(speed_values),
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
    """Read the cache, rebuilding it from mounted ledgers when absent."""

    target = Path(path) if path is not None else capabilities_path()
    if not target.is_file():
        return rebuild_capabilities(path=target)
    data = json.loads(target.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"capabilities cache {target} does not hold an object")
    return data
