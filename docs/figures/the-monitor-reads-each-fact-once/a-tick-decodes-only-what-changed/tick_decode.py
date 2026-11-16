#!/usr/bin/env python3
"""Draw the two figures for the tick-decode census.

    tick_decode.py bytes [--out decode-bytes-by-caller.svg]
    tick_decode.py peak  [--out peak-bytes-per-tick.svg]

Reads the two census logs beside this run's manifest -- one arm at base, one at
head -- and draws what the arms measured. The logs are the record; nothing here
recomputes a figure the census did not report.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

RUN = Path(
    "/home/ITER/mcintos/.config/reckon/crew/runs/"
    "r-20261115T142458653959-a-tick-decodes-only-what-changed/census"
)

ROW = re.compile(r"^\s+(\d+)\s+\d+\s+(\S+):\d+\s+\[(.+?)\]")

# Every caller the census attributes, as one of the passes this node reasoned
# about.  A caller that is none of them is reported as everything else rather
# than folded into a pass it is not part of.
PASSES = (
    ("parse_events", "the streams' new bytes"),
    ("_pointer_for_stream", "the fleet's fields, per stream"),
    ("pointer_fields", "pointer fields, per tick"),
    ("recovery_wait", "classifier line scans"),
    ("other", "everything else"),
)
COLOURS = {
    "parse_events": "#2b6cb0",
    "_pointer_for_stream": "#c53030",
    "pointer_fields": "#718096",
    "recovery_wait": "#4a5568",
    "other": "#cbd5e0",
}
HEAD_POINTER_FIELD_CALLERS = {"_pointer_streams", "classify_tick", "tick"}
RECOVERY_WAIT_CALLERS = (
    "_stream_holds_assistant_record",
    "_tool_call_in_line",
    "measure_stream_tokens",
)


def pass_of(caller: str) -> str:
    if caller == "parse_events":
        return "parse_events"
    if caller == "_pointer_for_stream":
        return "_pointer_for_stream"
    if caller in HEAD_POINTER_FIELD_CALLERS:
        return "pointer_fields"
    if caller.startswith(RECOVERY_WAIT_CALLERS):
        return "recovery_wait"
    return "other"


def read_arm(log: Path) -> list[dict]:
    """One record per tick the arm traced."""
    ticks: list[dict] = []
    current: dict | None = None
    reading_decodes = False
    for line in log.read_text().splitlines():
        if line.startswith("=== tick"):
            current = {"tick": int(line.split()[2]), "bytes": {}, "peak": None}
            ticks.append(current)
            reading_decodes = False
            continue
        if current is None:
            continue
        if line.startswith("decode_top20_bytes:"):
            reading_decodes = True
            continue
        if line.startswith("decode_bytes_total:"):
            reading_decodes = False
        if line.startswith("tracemalloc_peak_bytes:"):
            current["peak"] = int(line.split(":", 1)[1])
        found = ROW.match(line) if reading_decodes else None
        if found is None:
            continue
        size = int(found.group(1))
        key = pass_of(found.group(3))
        current["bytes"][key] = current["bytes"].get(key, 0) + size
    return ticks


def draw_bytes(base: list[dict], head: list[dict], out: Path) -> Path:
    arms = (("base 2780ddad", base), ("head 33f28354f7", head))
    rows = [(label, tick) for label, ticks in arms for tick in ticks]
    height = 0.62
    figure, axes = plt.subplots(figsize=(10.5, 0.42 * len(rows) + 1.9))
    axes.invert_yaxis()
    largest = max(sum(tick["bytes"].values()) for _, tick in rows) / 1e6
    for index, (_label, tick) in enumerate(rows):
        left = 0.0
        for key, _ in PASSES:
            size = tick["bytes"].get(key, 0) / 1e6
            if not size:
                continue
            axes.barh(
                index,
                size,
                height,
                left=left,
                color=COLOURS[key],
                edgecolor="white",
                linewidth=0.7,
            )
            if size / largest > 0.06:
                axes.text(
                    left + size / 2,
                    index,
                    f"{size:.0f}",
                    ha="center",
                    va="center",
                    fontsize=7.5,
                    color="white"
                    if key not in {"other", "pointer_fields"}
                    else "#1a202c",
                )
            left += size
        axes.text(
            left + largest * 0.012,
            index,
            f"{left:.0f} MB",
            va="center",
            fontsize=8.5,
            color="#1a202c",
        )
    axes.set_xlim(0, largest * 1.14)
    axes.set_yticks(range(len(rows)))
    axes.set_yticklabels(
        [f"{label}  t{tick['tick']}" for label, tick in rows], fontsize=8.5
    )
    axes.set_xlabel("bytes decoded in one station tick [MB]")
    axes.set_title(
        "What one station tick decodes, by pass\n"
        "the fleet's fields per stream leaves the head arm entirely",
        fontsize=11,
        loc="left",
    )
    handles = [plt.Rectangle((0, 0), 1, 1, color=COLOURS[key]) for key, _ in PASSES]
    axes.legend(
        handles,
        [f"{label}" for _, label in PASSES],
        loc="upper center",
        bbox_to_anchor=(0.5, -0.16),
        ncol=3,
        frameon=False,
        fontsize=8.5,
    )
    for spine in ("top", "right", "left"):
        axes.spines[spine].set_visible(False)
    axes.grid(axis="x", color="#e2e8f0", linewidth=0.8)
    axes.set_axisbelow(True)
    figure.tight_layout()
    figure.savefig(out, format="svg", metadata={"Date": None})
    plt.close(figure)
    return out


def draw_peak(base: list[dict], head: list[dict], out: Path) -> Path:
    figure, axes = plt.subplots(figsize=(9.2, 4.4))
    width = 0.38
    for offset, (label, ticks, colour) in enumerate(
        (
            ("base 2780ddad", base, "#a0aec0"),
            ("head 33f28354f7", head, "#2b6cb0"),
        )
    ):
        xs = [tick["tick"] + (offset - 0.5) * width for tick in ticks]
        ys = [tick["peak"] / 1e6 for tick in ticks]
        axes.bar(xs, ys, width, color=colour, label=label)
        for x, y in zip(xs, ys, strict=True):
            axes.text(x, y + 9, f"{y:.0f}", ha="center", fontsize=8)
    axes.axhline(
        600,
        color="#c53030",
        linewidth=1.4,
        linestyle=(0, (5, 3)),
        label="600 MB per tick",
    )
    axes.text(0.55, 612, "600 MB per tick", color="#c53030", fontsize=9)
    axes.set_ylim(0, 690)
    axes.set_xlabel("tick")
    axes.set_ylabel("peak traced bytes [MB]")
    axes.set_xticks([tick["tick"] for tick in head] or [1])
    axes.set_title(
        "Tracemalloc peak of one station tick\n"
        "every observed tick in both arms is far under the 600 MB line",
        fontsize=11,
        loc="left",
    )
    axes.legend(frameon=False, fontsize=8.5, loc="upper right")
    for spine in ("top", "right"):
        axes.spines[spine].set_visible(False)
    axes.grid(axis="y", color="#e2e8f0", linewidth=0.8)
    axes.set_axisbelow(True)
    figure.tight_layout()
    figure.savefig(out, format="svg", metadata={"Date": None})
    plt.close(figure)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("figure", choices=("bytes", "peak"))
    parser.add_argument("--census", type=Path, default=RUN)
    parser.add_argument("--out", type=Path)
    arguments = parser.parse_args()
    base = read_arm(arguments.census / "decode-census-base.log")
    head = read_arm(arguments.census / "decode-census-head.log")
    if arguments.figure == "bytes":
        out = arguments.out or Path("decode-bytes-by-census-pass.svg")
        print(draw_bytes(base, head, out))
    else:
        out = arguments.out or Path("peak-bytes-per-tick.svg")
        print(draw_peak(base, head, out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
