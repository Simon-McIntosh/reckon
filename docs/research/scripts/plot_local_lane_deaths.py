#!/usr/bin/env python3
"""Draw the local lane's mid-turn deaths: per day by role, and rate by role.

Reads the committed measurement rather than the run streams, so the figure and
the file a gate reads cannot drift apart.

Usage:
    <venv>/bin/python plot_local_lane_deaths.py --data <json> --out <png>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

# Categorical slots 1-3 in fixed order; validated as a set against this
# surface. Identity follows the entity, so a role keeps its hue in both panels.
ROLE_COLOR = {
    "review": "#2a78d6",
    "implement": "#eb6834",
    "other": "#1baf7a",
}
ROLE_ORDER = ["review", "implement", "other"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    stated = json.loads(Path(args.data).read_text(encoding="utf-8"))
    before = stated["before"]
    per_day = before["deaths_per_day_by_role"]

    days = sorted(per_day)
    series = {key: [0] * len(days) for key in ROLE_ORDER}
    for index, day in enumerate(days):
        bucket = per_day[day]
        for role, count in bucket.items():
            key = role if role in ("review", "implement") else "other"
            series[key][index] += count

    fig, (left, right) = plt.subplots(
        1,
        2,
        figsize=(13.0, 5.4),
        dpi=200,
        gridspec_kw={"width_ratios": [1.62, 1.0], "wspace": 0.22},
    )
    fig.patch.set_facecolor(SURFACE)
    for panel in (left, right):
        panel.set_facecolor(SURFACE)
        for spine in panel.spines.values():
            spine.set_visible(False)

    # ── left: deaths per day, stacked by role ───────────────────────────────
    positions = range(len(days))
    bottoms = [0] * len(days)
    for key in ROLE_ORDER:
        values = series[key]
        left.bar(
            positions,
            values,
            bottom=bottoms,
            width=0.66,
            label={
                "review": "review",
                "implement": "implement",
                "other": "other roles",
            }[key],
            color=ROLE_COLOR[key],
            edgecolor=SURFACE,
            linewidth=1.4,
            zorder=3,
        )
        bottoms = [base + value for base, value in zip(bottoms, values, strict=True)]

    left.set_xticks(list(positions))
    left.set_xticklabels(
        [day[5:] for day in days], rotation=90, fontsize=7.5, color=MUTED
    )
    left.set_ylabel("mid-turn deaths", fontsize=9, color=INK_2)
    left.tick_params(axis="y", labelsize=8, colors=MUTED, length=0)
    left.tick_params(axis="x", length=0)
    left.set_axisbelow(True)
    left.yaxis.grid(True, color=GRID, linewidth=0.8)
    left.xaxis.grid(False)
    left.spines["bottom"].set_visible(True)
    left.spines["bottom"].set_color(AXIS)
    left.spines["bottom"].set_linewidth(0.8)
    left.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=6))
    left.set_ylim(0, max(bottoms) * 1.22)

    peak = max(range(len(days)), key=lambda i: bottoms[i])
    left.annotate(
        f"{bottoms[peak]} deaths\n{series['review'][peak]} review",
        xy=(peak, bottoms[peak]),
        xytext=(peak, bottoms[peak] + max(bottoms) * 0.10),
        ha="center",
        fontsize=8.5,
        color=INK,
        fontweight="bold",
        linespacing=1.35,
    )
    left.legend(
        loc="upper left",
        frameon=False,
        fontsize=8.5,
        labelcolor=INK_2,
        handlelength=0.9,
        handleheight=0.9,
        borderaxespad=0.1,
        ncols=3,
        columnspacing=1.1,
    )
    left.set_title(
        "Deaths per day, by role",
        fontsize=10.5,
        color=INK,
        loc="left",
        pad=22,
        fontweight="bold",
    )

    # ── right: death rate by role ───────────────────────────────────────────
    cells = before["cells"]
    role_totals: dict[str, list[int]] = {}
    for cell in cells:
        role = cell["role"]
        entry = role_totals.setdefault(role, [0, 0])
        entry[0] += cell["dead"]
        entry[1] += cell["attempts"]
    rows = [
        (role, dead, attempts, dead / attempts)
        for role, (dead, attempts) in role_totals.items()
        if attempts >= 40
    ]
    rows.sort(key=lambda row: row[3])

    height = 0.52
    for index, (role, dead, attempts, rate) in enumerate(rows):
        color = ROLE_COLOR.get(role, ROLE_COLOR["other"])
        right.barh(
            index,
            rate,
            height=height,
            color=color,
            zorder=4,
            edgecolor=SURFACE,
            linewidth=1.4,
        )
        right.text(
            rate + max(row[3] for row in rows) * 0.03,
            index,
            f"{rate * 100:.1f}%   {dead}/{attempts}",
            va="center",
            fontsize=8.5,
            color=INK_2,
        )
        right.text(
            0,
            index + 0.34,
            role,
            va="center",
            ha="left",
            fontsize=9,
            color=INK,
            fontweight="bold" if role == "review" else "normal",
        )

    right.set_yticks([])
    right.set_ylim(-0.7, len(rows) - 0.25)
    right.set_xlim(0, max(row[3] for row in rows) * 1.42)
    right.set_xticks([0, 0.1, 0.2, 0.3])
    right.set_xticklabels(["0%", "10%", "20%", "30%"], fontsize=8, color=MUTED)
    right.tick_params(axis="x", length=0)
    right.set_axisbelow(True)
    right.xaxis.grid(True, color=GRID, linewidth=0.8)
    right.spines["bottom"].set_visible(True)
    right.spines["bottom"].set_color(AXIS)
    right.spines["bottom"].set_linewidth(0.8)
    right.set_xlabel("share of attempts that died mid-turn", fontsize=9, color=INK_2)
    right.set_title(
        "Death rate by role, all efforts",
        fontsize=10.5,
        color=INK,
        loc="left",
        pad=22,
        fontweight="bold",
    )

    window = before["window"]["start"][:10] + " → " + before["window"]["end"][:10]
    lane = stated["lane"]
    headline = before["deaths"]["headline"]
    fig.suptitle(
        f"The local lane kills review mid-turn at {headline['rate'] * 100:.0f}% — "
        f"{headline['count']} of {headline['attempts']} review attempts at {headline['effort']}, "
        f"against {role_totals['implement'][0] / role_totals['implement'][1] * 100:.0f}% for implement",
        fontsize=13.5,
        color=INK,
        x=0.0075,
        y=0.975,
        ha="left",
        fontweight="bold",
    )
    fig.text(
        0.0075,
        0.885,  # under the headline, above both panels
        f"{window} · {lane['backend']} ({lane['model']}, effort {lane['effort_declared']}) · "
        f"{before['population']['attempts_on_local_lane']:,} attempts read from the runs' own streams · "
        "a death is a stream with no result record whose process is gone",
        fontsize=8.8,
        color=INK_2,
        ha="left",
    )
    fig.subplots_adjust(left=0.055, right=0.985, top=0.80, bottom=0.115)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, facecolor=SURFACE)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
