"""Reconstruct a fixed promotion cohort and replay its recorded service trace.

The executor replays recorded verdicts and service times; it never invokes a
promotion against a mounted repository. This isolates scheduler throughput from
changes in review evidence and repositories since the original requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from reckon._timestamps import parse_utc


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def collect(crew: Path, start: str, end: str, census: Path | None = None) -> dict:
    """Copy retained requests and bounded run records, without writing sources."""
    lower, upper = parse_utc(start), parse_utc(end)
    if lower is None or upper is None or lower >= upper:
        raise ValueError("selection needs an increasing UTC interval")
    sources = read_object(census / "sources.json") if census else {}
    history = list(sources.get("drained") or [])
    drained = crew / "promotion-drained.jsonl"
    if drained.is_file():
        for line in drained.read_text().splitlines():
            try:
                history.append(json.loads(line))
            except ValueError:
                continue
    historical = list((sources.get("requests") or {}).values())
    retained = [
        read_object(path)
        for path in sorted((crew / "promotion-requests").glob("*.json*"))
        if path.is_file()
    ]
    selected = {}
    for request in historical + retained:
        stamp = parse_utc(request.get("requested_at"))
        if stamp is not None and lower <= stamp < upper:
            selected[(request["run_id"], request["request_id"])] = request
    rows = []
    for (run_id, _request_id), request in sorted(selected.items()):
        pointer = read_object(crew / "live" / f"{run_id}.json")
        if not pointer:
            pointer = (sources.get("pointers") or {}).get(run_id, {})
        files = {}
        for name in (
            "dispatch-detail.json",
            "manifest.md",
            "supervisor.json",
            "worker.json",
            "exit.json",
            "end.json",
            "promotion.json",
            "claim.json",
        ):
            for directory in (
                crew / "runs" / run_id,
                crew / "archive" / "runs" / run_id,
            ):
                path = directory / name
                if path.is_file():
                    files[name] = path.read_text()
                    break
        rows.append(
            {
                "request": request,
                "pointer": pointer,
                "run_files": files,
                "ledger": (sources.get("ledgers") or {}).get(run_id, {}),
                "drained": [
                    row
                    for row in history
                    if row.get("run_id") == run_id
                    and row.get("requested_at") == request.get("requested_at")
                ],
            }
        )
    return {
        "header": {
            "selection_rule": f"{start} <= requested_at < {end}",
            "count": len(rows),
            "copied_set_sha256": digest(rows),
            "source_crew": str(crew),
            "source_census": str(census) if census else None,
            "method": "retained requests over historical census requests, keyed by run_id and request_id; bounded run files resolved through archive",
        },
        "rows": rows,
    }


def validate_dataset(data: dict) -> list[dict]:
    """Refuse empty or changed inputs before reporting any throughput."""
    rows = data["rows"]
    header = data["header"]
    if not rows:
        raise ValueError("empty request directory: no rate can be measured")
    if len(rows) != header["count"] or digest(rows) != header["copied_set_sha256"]:
        raise ValueError("copied set does not match its count and digest")
    return rows


def trace_observation(row: dict) -> dict | None:
    """Use only a matching terminal receipt with measured service timestamps."""
    request = row["request"]
    candidates = [request]
    receipt = row["run_files"].get("promotion.json")
    if receipt:
        with suppress(ValueError):
            candidates.append(json.loads(receipt))
    for candidate in candidates:
        if candidate.get("request_id") != request["request_id"]:
            continue
        phase = candidate.get("phase")
        if phase not in {
            "promoted",
            "promotion-refused",
            "promotion-failed",
            "promotion-deferred",
        }:
            continue
        started, finished = (
            parse_utc(candidate.get("started_at")),
            parse_utc(candidate.get("finished_at")),
        )
        if started is not None and finished is not None and finished >= started:
            return {
                "phase": phase,
                "service_seconds": (finished - started).total_seconds(),
            }
    return None


def replay(data: dict, *, scale: float = 1000.0) -> dict:
    """Exercise the real queue with recorded service intervals and verdicts.

    This is a scheduler trace replay, not a new execution of the historical
    promotions. Missing terminal receipts remain unmeasured rather than being
    assigned an invented duration or successful verdict. Record readiness is
    fixed by the trace; it does not probe historical worker PIDs or repositories.
    """
    from reckon.crew import promotion_requests as requests
    from reckon.crew import runs

    rows = validate_dataset(data)
    if scale <= 0:
        raise ValueError("time scale must be positive")
    traces = {row["request"]["request_id"]: trace_observation(row) for row in rows}
    measured = [row for row in rows if traces[row["request"]["request_id"]] is not None]
    if not measured:
        raise ValueError(
            "no matching terminal service receipt: no rate can be measured"
        )
    expected = {
        row["request"]["request_id"]: traces[row["request"]["request_id"]]["phase"]
        for row in measured
    }
    observed = {}
    execution_seconds = 0.0

    def execute(request):
        trace = traces[request["request_id"]]
        time.sleep(trace["service_seconds"] / scale)
        if trace["phase"] == requests.FAILED:
            raise RuntimeError("recorded promotion failure")
        if trace["phase"] == requests.REFUSED:
            return {"ok": False, "reason": "recorded promotion refusal"}
        if trace["phase"] == requests.DEFERRED:
            return None
        return {"ok": True, "record": {"commits": []}}

    with tempfile.TemporaryDirectory(prefix="promotion-replay-") as scratch:
        home = Path(scratch)
        with (
            patch.dict(os.environ, {"RECKON_HOME": str(home)}),
            patch.object(requests, "_review_self_waits", return_value=(False, "")),
        ):
            crew = runs.crew_home()
            # All copied records, including censored ones, remain inspectable in
            # the temporary home. Only observed terminal attempts enter the clock.
            for row in rows:
                request = row["request"]
                run_id = request["run_id"]
                directory = runs.run_dir(run_id)
                directory.mkdir(parents=True, exist_ok=True)
                for name, content in row["run_files"].items():
                    (directory / name).write_text(content)
                runs._write_json(runs.pointer_path(run_id), row["pointer"])
            pending = list(measured)
            service = requests.PromotionService(execute=execute)
            while pending:
                # One queue entry per run; attempts of the same run cannot
                # overwrite one another before their receipts have been read.
                active = {}
                remaining = []
                for row in pending:
                    run_id = row["request"]["run_id"]
                    if run_id in active:
                        remaining.append(row)
                        continue
                    active[run_id] = row["request"]["request_id"]
                    request = {**row["request"], "phase": requests.REQUESTED}
                    runs._write_json(
                        crew / "promotion-requests" / f"{run_id}.json", request
                    )
                began = time.monotonic()
                service.start_refill()
                try:
                    service.tick()
                    deadline = (
                        began
                        + sum(
                            traces[key]["service_seconds"] / scale
                            for key in active.values()
                        )
                        + 60
                    )
                    while active:
                        for run_id, request_id in list(active.items()):
                            receipt = requests.read_promotion(run_id)
                            if receipt.get("request_id") == request_id and receipt.get(
                                "phase"
                            ) in {
                                requests.PROMOTED,
                                requests.REFUSED,
                                requests.FAILED,
                                requests.DEFERRED,
                            }:
                                observed[request_id] = receipt["phase"]
                                del active[run_id]
                        if time.monotonic() > deadline:
                            raise TimeoutError(
                                "replay did not drain within its service bound"
                            )
                        if active:
                            time.sleep(0.005)
                finally:
                    service.stop_refill()
                    service.begin_drain()
                    service.wait_for_in_flight(60)
                execution_seconds += time.monotonic() - began
                pending = remaining
                service = requests.PromotionService(execute=execute)
    counts = Counter(observed.values())
    return {
        "header": data["header"],
        "measurement": "recorded-service scheduler replay; no historical promotion re-executed",
        "time_scale": scale,
        "timed_requests": len(measured),
        "unmeasured_requests": len(rows) - len(measured),
        "verdicts": dict(counts),
        "verdicts_match": observed == expected,
        "elapsed_seconds": execution_seconds,
        "successful_promotions_per_hour": counts[requests.PROMOTED]
        * 3600
        / (execution_seconds * scale),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", type=Path)
    parser.add_argument("--census", type=Path)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--scale", type=float, default=1000.0)
    parser.add_argument("--start", default="2026-11-16T21:00:00Z")
    parser.add_argument("--end", default="2026-11-16T22:30:00Z")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.replay:
        data = json.loads(args.replay.read_text())
        print(json.dumps(data["header"], sort_keys=True), flush=True)
        result = replay(data, scale=args.scale)
        args.output.write_text(json.dumps(result, sort_keys=True))
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0 if result["verdicts_match"] else 1
    if args.collect is None:
        parser.error("supply --collect or --replay")
    data = collect(args.collect, args.start, args.end, args.census)
    args.output.write_text(json.dumps(data, sort_keys=True))
    print(json.dumps(data["header"], sort_keys=True), flush=True)
    return 0 if data["rows"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
