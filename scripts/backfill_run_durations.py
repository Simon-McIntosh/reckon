#!/usr/bin/env python3
"""Backfill the duration split that completed run streams can support.

    python scripts/backfill_run_durations.py --project <project> [--root <path>]

The stream is interpreted by :func:`reckon._backends.observe_stream`, the same
span computation used while a run is live.  A retained stream that lacks the
timestamps or receipt spans needed for a model/machine split is marked
underivable rather than filled with a plausible-looking zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reckon import capabilities, ledger  # noqa: E402
from reckon._backends import BackendError, observe_stream  # noqa: E402
from reckon.crew.rollout import read_rollout_receipt  # noqa: E402


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _elapsed_seconds(row: Mapping[str, Any]) -> float | None:
    for key in ("wall_seconds", "worker_seconds"):
        value = row.get(key)
        if _numeric(value):
            return float(value)
    return None


def _command_for_stream(row: Mapping[str, Any], lines: list[str]) -> str:
    """Choose the harness command from durable routing data or stream shape."""
    agent = row.get("agent")
    candidates = (
        row.get("backend"),
        agent.get("backend") if isinstance(agent, Mapping) else None,
    )
    for candidate in candidates:
        name = str(candidate or "").lower()
        if name.startswith("codex"):
            return "codex"
        if name.startswith("clive"):
            return "clive"
        if name.startswith("claude"):
            return "claude"
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, Mapping):
            continue
        kind = str(event.get("type") or "")
        if kind.startswith(("thread.", "turn.", "item.")):
            return "codex"
        if kind in {"assistant", "result", "system", "user"}:
            return "claude"
    raise BackendError("stream and ledger row name no supported backend dialect")


def _codex_session_id(row: Mapping[str, Any], lines: list[str]) -> str:
    """Return the durable session id or the thread id recorded by old streams."""
    recorded = str(row.get("session_id") or "").strip()
    if recorded:
        return recorded
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, Mapping) and event.get("type") == "thread.started":
            return str(event.get("thread_id") or "").strip()
    return ""


def _rollout_receipt(
    row: Mapping[str, Any], command: str, lines: list[str]
) -> object | None:
    """Read a Codex receipt only when it contains both sides of the span."""
    if command != "codex":
        return None
    session_id = _codex_session_id(row, lines)
    if not session_id:
        return None
    agent = row.get("agent")
    model = agent.get("model") if isinstance(agent, Mapping) else None
    receipt = read_rollout_receipt(
        session_id, model_identifier=str(model) if model else None
    )
    if _numeric(getattr(receipt, "generation_seconds", None)) and _numeric(
        getattr(receipt, "machine_seconds", None)
    ):
        return receipt
    return None


def _stream_path(
    row: Mapping[str, Any], streams_root: str | Path | None
) -> Path | None:
    if streams_root is not None:
        candidate = Path(streams_root) / str(row.get("run_id") or "") / "stream.jsonl"
        return candidate if candidate.is_file() else None
    return capabilities.run_stream_path(row)


def backfill_run_durations(
    project: str,
    *,
    root: str | Path | None = None,
    streams_root: str | Path | None = None,
) -> dict[str, Any]:
    """Fill duration figures on old rows and explicitly mark every real gap."""
    data, version = ledger.load(project, root=root)
    counts = {
        "project": project,
        "rows_processed": 0,
        "rows_measured": 0,
        "rows_underivable": 0,
        "streams_found": 0,
        "streams_missing": 0,
        "ledger_version": version,
    }
    for row in data["runs"]:
        state = ledger.duration_measurement_state(row)
        marker = row.get("duration_measurement")
        retry_legacy_codex_join = (
            state == "underivable"
            and isinstance(marker, Mapping)
            and marker.get("reason") == "stream_has_no_model_span"
            and str(row.get("backend") or "").startswith("codex")
            and not str(row.get("session_id") or "").strip()
        )
        if state != "missing" and not retry_legacy_codex_join:
            continue
        counts["rows_processed"] += 1
        elapsed = _elapsed_seconds(row)
        path = _stream_path(row, streams_root)
        if path is None:
            if elapsed is not None:
                row["wall_seconds"] = elapsed
            row["duration_measurement"] = ledger.underivable_duration("stream_missing")
            counts["streams_missing"] += 1
            counts["rows_underivable"] += 1
            continue
        counts["streams_found"] += 1
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            if elapsed is not None:
                row["wall_seconds"] = elapsed
            row["duration_measurement"] = ledger.underivable_duration(
                "stream_unreadable", detail=f"{type(exc).__name__}: {exc}"
            )
            counts["rows_underivable"] += 1
            continue
        try:
            command = _command_for_stream(row, lines)
            observation = observe_stream(
                backend_name=str(row.get("backend") or command),
                backend={"command": command},
                lines=lines,
                elapsed_seconds=elapsed,
                receipt=_rollout_receipt(row, command, lines),
            )
        except BackendError as exc:
            if elapsed is not None:
                row["wall_seconds"] = elapsed
            row["duration_measurement"] = ledger.underivable_duration(
                "stream_dialect_unknown", detail=str(exc)
            )
            counts["rows_underivable"] += 1
            continue
        generation = observation.throughput.get("generation_seconds")
        machine = observation.throughput.get("machine_seconds")
        if not (_numeric(generation) and _numeric(machine)):
            observed_elapsed = observation.throughput.get("elapsed_seconds")
            wall = observed_elapsed if _numeric(observed_elapsed) else elapsed
            if wall is not None:
                row["wall_seconds"] = wall
            row["duration_measurement"] = ledger.underivable_duration(
                "stream_has_no_model_span",
                detail=str(observation.throughput.get("detail") or ""),
            )
            counts["rows_underivable"] += 1
            continue
        wall = round(float(generation) + float(machine), 3)
        throughput = dict(row.get("throughput") or {})
        throughput.update(
            {
                "elapsed_seconds": wall,
                "generation_seconds": float(generation),
                "machine_seconds": float(machine),
            }
        )
        rate = observation.throughput.get("tokens_per_second")
        if _numeric(rate):
            throughput["tokens_per_second"] = rate
        row["wall_seconds"] = wall
        row["throughput"] = throughput
        row.pop("duration_measurement", None)
        counts["rows_measured"] += 1

    if counts["rows_processed"]:
        counts["ledger_version"] = ledger.write(project, data, version, root=root)
    return counts


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backfill_run_durations",
        description=(
            "Fill run wall, generation, and machine durations from retained streams, "
            "recording an explicit reason for every underivable split."
        ),
    )
    parser.add_argument("--project", required=True, help="project whose ledger to fill")
    parser.add_argument("--root", default=None, help="checkout root holding the ledger")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(list(argv) if argv is not None else sys.argv[1:])
    result = backfill_run_durations(args.project, root=args.root)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
