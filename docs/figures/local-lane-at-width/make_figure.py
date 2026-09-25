#!/usr/bin/env python3
"""Reduce the local-lane ramp samples into a JSON summary and a time-series figure.

Reads the lane samples, the serving-telemetry samples and the stall scans written
by the sampler under the run's reports directory, and emits ``ramp.json`` (headline
metrics plus the reduced per-tick series) and ``ramp.svg`` beside this script.

Usage:
    python3 make_figure.py [reports_dir] [outdir]
"""

import json
import os
import sys
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

DEFAULT_REPORTS = (
    "/home/ITER/mcintos/.config/reckon/crew/reports/reckon/s21-coord/local-lane-at-width"
)
DEFAULT_OUT = os.path.dirname(os.path.abspath(__file__))
STALL_SECONDS = 900
WINDOW = 35


def parse_ts(text):
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def num(value):
    return value if isinstance(value, (int, float)) else None


def reduce_series(lanes, reqs):
    reqs = sorted(
        [r for r in reqs if parse_ts(r.get("timestamp"))],
        key=lambda r: r["timestamp"],
    )
    series = []
    for row in lanes:
        tick = parse_ts(row.get("ts"))
        ceiling = num(row.get("concurrent_requests"))
        if ceiling is None:
            ceiling = num(row.get("concurrent_requests_instant"))
        window = [
            r
            for r in reqs
            if tick.timestamp() - WINDOW
            <= parse_ts(r["timestamp"]).timestamp()
            <= tick.timestamp() + 1
        ]
        ttfts = [
            t
            for t in (num(r.get("time_to_first_token_s")) for r in window)
            if t is not None and t > 0
        ]
        prefill = []
        outrates = []
        comp_tokens = 0.0
        for r in window:
            ttft = num(r.get("time_to_first_token_s"))
            prompt = num(r.get("prompt_tokens"))
            if ttft is not None and ttft > 0 and prompt:
                prefill.append(prompt / ttft)
            dur = num(r.get("duration_s"))
            comp = num(r.get("completion_tokens"))
            if dur is not None and dur > 0 and comp:
                outrates.append(comp / dur)
                comp_tokens += comp
        series.append(
            {
                "ts": row.get("ts"),
                "running": num(row.get("running")),
                "ceiling": ceiling,
                "waiting": num(row.get("waiting")),
                "kv_occupancy": num(row.get("kv_occupancy")),
                "prefix_hit_rate": num(row.get("prefix_hit_rate")),
                "gate_in_flight": num(row.get("gate_in_flight")),
                "local_alive_runs": num(row.get("local_alive_runs")),
                "n_requests": len(window),
                "ttft_mean_s": round(sum(ttfts) / len(ttfts), 3) if ttfts else None,
                "ttft_max_s": round(max(ttfts), 3) if ttfts else None,
                "prefill_tok_s_mean": round(sum(prefill) / len(prefill))
                if prefill
                else None,
                "out_tok_s_per_stream": round(sum(outrates) / len(outrates), 2)
                if outrates
                else None,
                "out_tok_s_aggregate": round(comp_tokens / WINDOW, 1) if window else None,
            }
        )
    return series


def collect_stalls(stalls):
    events = []
    seen = set()
    for scan in stalls:
        for run in scan.get("runs", []):
            silence = run.get("stream_silence_s") or 0
            if not run.get("process_alive") or silence <= STALL_SECONDS:
                continue
            key = (run.get("run_id"), run.get("node"))
            if key in seen:
                continue
            seen.add(key)
            events.append(
                {
                    "ts": scan.get("ts"),
                    "run_id": run.get("run_id"),
                    "node": run.get("node"),
                    "silence_s": silence,
                    "phase": run.get("phase"),
                }
            )
    return events


def summarize(series, stalls):
    def col(name):
        return [s[name] for s in series if s[name] is not None]

    running = col("running")
    ceilings = col("ceiling")
    waitings = col("waiting")
    kvs = col("kv_occupancy")
    hits = col("prefix_hit_rate")
    ttfts = col("ttft_mean_s")
    gates = col("gate_in_flight")
    outs = col("out_tok_s_per_stream")
    aggs = col("out_tok_s_aggregate")
    peak_running = max(running) if running else None
    peak_row = next((s for s in series if s["running"] == peak_running), None)
    control = next(
        (
            s
            for s in series
            if s["running"] is not None
            and s["local_alive_runs"] is not None
            and s["running"] == s["local_alive_runs"]
        ),
        None,
    )
    return {
        "sample_count": len(series),
        "first_sample": series[0]["ts"] if series else None,
        "last_sample": series[-1]["ts"] if series else None,
        "peak_running": peak_running,
        "peak_running_ts": peak_row["ts"] if peak_row else None,
        "running_reached_20": bool(peak_running is not None and peak_running >= 20),
        "running_reached_30": bool(peak_running is not None and peak_running >= 30),
        "peak_gate_in_flight": max(gates) if gates else None,
        "ceiling_min": min(ceilings) if ceilings else None,
        "ceiling_max": max(ceilings) if ceilings else None,
        "ceiling_last": ceilings[-1] if ceilings else None,
        "waiting_max": max(waitings) if waitings else None,
        "waiting_any_positive": bool(waitings and max(waitings) > 0),
        "kv_occupancy_max": max(kvs) if kvs else None,
        "prefix_hit_rate_min": min(hits) if hits else None,
        "prefix_hit_rate_mean": round(sum(hits) / len(hits), 4) if hits else None,
        "ttft_mean_s_first": ttfts[0] if ttfts else None,
        "ttft_mean_s_last": ttfts[-1] if ttfts else None,
        "ttft_mean_s_max": max(ttfts) if ttfts else None,
        "out_tok_s_per_stream_mean": round(sum(outs) / len(outs), 2) if outs else None,
        "out_tok_s_per_stream_min": min(outs) if outs else None,
        "out_tok_s_per_stream_at_peak": peak_row.get("out_tok_s_per_stream")
        if peak_row
        else None,
        "out_tok_s_aggregate_mean": round(sum(aggs) / len(aggs), 1) if aggs else None,
        "out_tok_s_aggregate_at_peak": peak_row.get("out_tok_s_aggregate")
        if peak_row
        else None,
        "stall_events": collect_stalls(stalls),
        "positive_control": control,
    }


def draw(series, summary, outdir):
    times = [parse_ts(s["ts"]) for s in series]
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 2]}
    )
    runc, ceic, waitc = "#1f6f8a", "#b3541e", "#7a4b8f"
    kvc, ttftc = "#1f6f8a", "#b3541e"

    ax1.plot(times, [s["running"] for s in series], color=runc, lw=2, label="running")
    ax1.step(
        times,
        [s["ceiling"] for s in series],
        where="post", color=ceic, lw=1.6, ls="--",
        label="ceiling concurrent_requests",
    )
    if any((s["waiting"] or 0) > 0 for s in series):
        ax1.plot(
            times, [s["waiting"] for s in series], color=waitc, lw=1.4, label="waiting"
        )
    ax1.axhline(30, color="#555555", lw=0.8, ls=":", label="width 30")
    peak = summary.get("peak_running")
    if peak is not None:
        ax1.annotate(
            "peak running %d" % peak,
            xy=(parse_ts(summary["peak_running_ts"]), peak),
            xytext=(8, 8),
            textcoords="offset points",
            fontsize=9,
            color=runc,
        )
    ax1.set_ylabel("concurrent requests")
    ax1.set_title("Local lane under a coordinator concurrency ramp, 2026-09-25 UTC")
    ax1.legend(loc="upper left", fontsize=9, framealpha=0.9)
    ax1.grid(alpha=0.25)

    ax2.plot(times, [s["kv_occupancy"] for s in series], color=kvc, lw=2, label="kv_occupancy")
    ax2.set_ylabel("KV occupancy / fraction")
    ax2.set_ylim(0.0, 1.0)
    ax2b = ax2.twinx()
    ax2b.plot(
        times,
        [s["ttft_mean_s"] for s in series],
        color=ttftc, lw=2, marker="o", ms=2.5,
        label="mean TTFT (s)",
    )
    ax2b.set_ylabel("mean TTFT (s)")
    ax2.set_xlabel("wall time (UTC)")
    ax2.grid(alpha=0.25)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    handles = ax2.get_lines() + ax2b.get_lines()
    ax2.legend(handles, [h.get_label() for h in handles], loc="upper left", fontsize=9, framealpha=0.9)

    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "ramp.svg"))
    fig.savefig(os.path.join(outdir, "ramp.png"), dpi=150)
    plt.close(fig)


def main():
    reports = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPORTS
    outdir = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    os.makedirs(outdir, exist_ok=True)
    lanes = [r for r in load_jsonl(os.path.join(reports, "lane.jsonl")) if parse_ts(r.get("ts"))]
    lanes.sort(key=lambda r: r["ts"])
    reqs = load_jsonl(os.path.join(reports, "requests_sample.jsonl"))
    stalls = load_jsonl(os.path.join(reports, "stalls.jsonl"))
    series = reduce_series(lanes, reqs)
    summary = summarize(series, stalls)
    with open(os.path.join(outdir, "ramp.json"), "w") as handle:
        json.dump({"summary": summary, "series": series}, handle, indent=2)
    draw(series, summary, outdir)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()