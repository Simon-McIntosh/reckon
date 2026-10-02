"""Replay recorded dispatch contracts against current lane conditions."""

import statistics
from functools import partial
from pathlib import Path
from typing import Any

from reckon import ledger
from reckon._timestamps import parse_utc
from reckon.crew.node import TaskNode

from . import pick, snapshot
from .types import PickRequest


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
    snapshotter = partial(snapshot.candidates, availability_cache=availability_cache)
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
            request, config, repo=repo, records=records, snapshotter=snapshotter
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
        "conditions": "live at replay, not historical lane state; serving observations shared within this cohort",
        "serving_observations": list(availability_cache.values()),
        "rows": rows,
        "summary": {
            "count": len(rows),
            "agreement_rate": sum(
                row["actual_backend"] == row["selection"]["backend"] for row in rows
            )
            / count,
            "refused_model_picks": sum(
                row["selection"]["backend"] == "codex-spark"
                or row["selection"]["model"] == "gpt-5.3-codex-spark"
                for row in rows
            ),
            "fallback_rate": sum(
                row["selection"]["fallback_reason"] is not None for row in rows
            )
            / count,
            "no_selection_count": sum(
                row["selection"]["backend"] is None for row in rows
            ),
            "jev_calls": sum(row["selection"]["jev_latency_ms"] > 0 for row in rows),
            "median_latency_ms": statistics.median(
                row["selection"]["latency_ms"] for row in rows
            ),
            "median_jev_latency_ms": statistics.median(
                row["selection"]["jev_latency_ms"] for row in rows
            ),
            "cost_usd": sum(
                row["selection"]["usage"].get("cost", 0) or 0 for row in rows
            ),
        },
    }
