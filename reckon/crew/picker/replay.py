"""Replay recorded dispatch contracts against current lane conditions."""

import statistics
import time
from functools import partial
from pathlib import Path
from typing import Any

from reckon import ledger
from reckon._timestamps import parse_utc
from reckon.crew.node import TaskNode
from reckon.resources import resource_scan_scope

from . import pick, snapshot
from .types import PickRequest

# Serving statuses that mean a picked model is not actually being served.
REFUSED_STATUSES = frozenset({"refused", "logged-out"})


def replay(
    project: str, count: int, config: dict[str, Any], *, repo: Path
) -> dict[str, Any]:
    records = ledger.runs(project, root=repo)
    # Promotion order differs from dispatch order when a long run finishes late.
    eligible = [
        row
        for row in records
        if parse_utc(str(row.get("dispatched_at") or "")) is not None
    ]
    recent = sorted(
        eligible, key=lambda row: parse_utc(row["dispatched_at"]), reverse=True
    )[:count]
    if len(recent) < count:
        raise ValueError(
            f"Replay requested {count} dispatches but the ledger has only {len(recent)}"
        )
    rows = []
    availability_cache = {}
    snapshot_started = time.perf_counter()
    budget_snapshot = snapshot.budget_view(project, config, repo, records)
    shared_inputs = snapshot.routing.shared_verdict_inputs(project, repo)
    snapshot_latency_ms = (time.perf_counter() - snapshot_started) * 1000
    snapshotter = partial(
        snapshot.candidates,
        availability_cache=availability_cache,
        budget_snapshot=budget_snapshot,
        verdict_inputs=shared_inputs,
    )
    # One docs-tree scan serves every row's plan lookup in this replay.
    with resource_scan_scope():
        for record in recent:
            definition = record.get("node_definition") or {}
            node = TaskNode(
                id=str(record.get("node") or record["run_id"]),
                goal=str(definition.get("goal") or ""),
                plan=str(record.get("plan") or ""),
                section=str(record.get("section") or ""),
                role=str(record["role"]),
                spec_level=str(record["spec_level"]),
                done_when=str(definition.get("done_when") or ""),
                estimated_hours=definition.get("estimated_hours"),
            )
            request = PickRequest(
                project=project,
                node=node,
                capability=definition.get("capability") or {},
                estimated_context=int(definition.get("estimated_context") or 0),
                comment=str(definition.get("comment") or ""),
                session=str(record.get("session") or ""),
            )
            selection = pick(
                request,
                config,
                repo=repo,
                records=records,
                snapshotter=snapshotter,
                verdict_inputs=shared_inputs,
                budget_snapshot=budget_snapshot,
            )
            rows.append(
                {
                    "run_id": record["run_id"],
                    "dispatched_at": record["dispatched_at"],
                    "actual_backend": record["backend"],
                    "actual_model": (record.get("agent") or {}).get("model"),
                    "outcome": record.get("gate"),
                    "node": node.as_dict(),
                    "selection": selection.as_dict(),
                }
            )
    return {
        "project": project,
        "conditions": "live at replay, not historical lane state; one dated budget snapshot, one project read and serving observations shared within this cohort",
        "budget_snapshot": budget_snapshot,
        "serving_observations": list(availability_cache.values()),
        "rows": rows,
        "summary": _summary(rows, availability_cache, count, snapshot_latency_ms),
    }


def _summary(
    rows: list[dict[str, Any]],
    observations: dict[tuple[str, str | None], dict[str, Any]],
    count: int,
    snapshot_latency_ms: float,
) -> dict[str, Any]:
    """Summarise one replay, reading each figure from the rows it describes."""

    jev_latencies = [
        row["selection"]["jev_latency_ms"]
        for row in rows
        if row["selection"]["jev_latency_ms"] > 0
    ]

    def picked_refused(row: dict[str, Any]) -> bool:
        selection = row["selection"]
        observation = observations.get((selection["backend"], selection["model"]))
        return bool(observation) and observation.get("status") in REFUSED_STATUSES

    return {
        "count": len(rows),
        "budget_snapshot_latency_ms": round(snapshot_latency_ms, 3),
        "agreement_rate": sum(
            row["actual_backend"] == row["selection"]["backend"] for row in rows
        )
        / count,
        "refused_model_picks": sum(picked_refused(row) for row in rows),
        "fallback_rate": sum(
            row["selection"]["fallback_reason"] is not None for row in rows
        )
        / count,
        "no_selection_count": sum(row["selection"]["backend"] is None for row in rows),
        "jev_calls": len(jev_latencies),
        "median_latency_ms": statistics.median(
            row["selection"]["latency_ms"] for row in rows
        ),
        "max_latency_ms": max(
            (row["selection"]["latency_ms"] for row in rows), default=None
        ),
        "median_jev_latency_ms": (
            statistics.median(jev_latencies) if jev_latencies else None
        ),
        "cost_usd": sum(row["selection"]["usage"].get("cost", 0) or 0 for row in rows),
    }
