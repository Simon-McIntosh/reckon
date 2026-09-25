#!/usr/bin/env python3
"""Sample the local lane document and the live crew runs during a concurrency ramp.

Writes one JSON row per 30 s tick to lane.jsonl, appends newly arrived serving
telemetry rows to requests_sample.jsonl, and every fifth tick scans the live crew
pointers for the stall signature: a live process whose stream has been silent for
over 900 s, or whose last stream line reports corrupt output.
"""

import glob
import json
import os
import subprocess
import time

REPORTS = "/home/ITER/mcintos/.config/reckon/crew/reports/reckon/s21-coord/local-lane-at-width"
LANE = "/home/ITER/mcintos/public/imas-ambix/lane.json"
REQUESTS = "/home/ITER/mcintos/public/imas-ambix/requests.jsonl"
LIVE = "/home/ITER/mcintos/.config/reckon/crew/live"
TICK = 30
TICKS = 110  # ~55 minutes, then stop on its own

LANE_FIELDS = [
    "running", "concurrent_requests", "concurrent_requests_instant",
    "headroom", "headroom_instant", "waiting", "kv_occupancy", "prefix_hit_rate",
    "preemptions", "mean_context", "mean_context_instant", "pool_tokens",
    "sizing_verdict", "sizing_reason", "state", "settling", "volatile",
    "binding_observed", "observed_at", "window_samples", "window_seconds",
    "model_id", "prefill_tok_s", "ttft_s", "throughput_tok_s", "decode_tok_s",
]
GATE_FIELDS = ["in_flight", "width", "effective_width", "waiting", "paused", "reason"]


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def any_prefill_ttft(d, *names):
    for n in names:
        if n in d:
            return d[n]
    return None


def read_lane():
    row = {"ts": now_utc(), "source": "lane.json"}
    try:
        with open(LANE) as fh:
            d = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - a missing/rotating doc is data, not a crash
        row["error"] = repr(exc)
        return row
    for k in LANE_FIELDS:
        row[k] = d.get(k)
    row["lane_present"] = True
    for k, v in d.items():
        if any(t in k.lower() for t in ("prefill", "ttft", "throughput")):
            row["lane_%s" % k] = v
    g = d.get("router_generation_gate") or {}
    for k in GATE_FIELDS:
        row["gate_%s" % k] = g.get(k)
    h = d.get("hicache_host") or {}
    row["hicache_used_fraction"] = h.get("used_fraction")
    return row


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def last_stream_line(path):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            chunk = fh.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None, None
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    return (lines[-1] if lines else None), "corrupt" if any("corrupt" in ln.lower() for ln in lines[-3:]) else None


def scan_runs():
    rows = []
    for p in sorted(glob.glob(os.path.join(LIVE, "*.json"))):
        try:
            with open(p) as fh:
                d = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        if not d.get("local"):
            continue
        pid = d.get("pid")
        stream = d.get("log_path")
        is_alive = bool(pid and alive(pid))
        silence = None
        if stream and os.path.exists(stream):
            silence = round(time.time() - os.path.getmtime(stream), 1)
        last, corrupt = last_stream_line(stream) if stream else (None, None)
        rows.append({
            "run_id": d.get("run_id"),
            "node": (d.get("node") or {}).get("name"),
            "phase": d.get("phase"),
            "pid": pid,
            "process_alive": is_alive,
            "stream_silence_s": silence,
            "stalled_900": bool(is_alive and silence is not None and silence > 900),
            "corrupt_tail": corrupt,
            "last_line": (last or "")[:200],
        })
    return rows


def main():
    lane_rows = os.path.join(REPORTS, "lane.jsonl")
    req_rows = os.path.join(REPORTS, "requests_sample.jsonl")
    stall_rows = os.path.join(REPORTS, "stalls.jsonl")
    try:
        offset = os.path.getsize(REQUESTS)
    except Exception:  # noqa: BLE001
        offset = 0
    for tick in range(TICKS):
        row = read_lane()
        try:
            runs = scan_runs()
            row["local_alive_runs"] = sum(1 for r in runs if r["process_alive"])
            row["local_run_pointers"] = len(runs)
            row["stalled_900"] = sum(1 for r in runs if r["stalled_900"])
        except Exception as exc:  # noqa: BLE001
            row["scan_error"] = repr(exc)
        with open(lane_rows, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        # incremental serving telemetry
        try:
            size = os.path.getsize(REQUESTS)
            if size > offset:
                with open(REQUESTS) as fh:
                    fh.seek(offset)
                    new = fh.read()
                offset = size
                with open(req_rows, "a") as fh:
                    fh.write(new if new.endswith("\n") else new + "\n")
        except Exception as exc:  # noqa: BLE001
            with open(req_rows, "a") as fh:
                fh.write(json.dumps({"ts": now_utc(), "read_error": repr(exc)}) + "\n")
        if tick % 5 == 0:
            try:
                with open(stall_rows, "a") as fh:
                    fh.write(json.dumps({"ts": now_utc(), "runs": scan_runs()}) + "\n")
            except Exception:  # noqa: BLE001
                pass
        time.sleep(TICK)


if __name__ == "__main__":
    main()