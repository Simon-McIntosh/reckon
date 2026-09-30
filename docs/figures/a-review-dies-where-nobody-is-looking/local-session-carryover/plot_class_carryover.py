#!/usr/bin/env python3
"""Draw the session-inheritance class shares of both lanes, either side of the boundary.

Reads the committed censuses rather than the streams, so the figure and the
files a gate reads cannot drift apart: the local lane's
``local-session-carryover.json`` and the codex laposne's
``codex-resume-burn-after.json`` report the same classes over the same
boundary, so one figure can put them side by side as share of dispatches.

Usage:
    <venv>/bin/python plot_class_carryover.py [--out <png>]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
AXIS = "#c3c2b7"

HERE = Path(__file__).resolve().parent
DATA = HERE.parents[2] / "research" / "data"

# Categorical slots 1-3 in fixed order plus a neutral for the unknown bucket;
# identity follows the class, so a class keeps its hue in every bar.
CLASS_ORDER = ["fresh", "same_task", "cross_task", "resumed_prior_unknown"]
CLASS_LABEL = {
    "fresh": "fresh session",
    "same_task": "same-task resume",
    "cross_task": "cross-task resume",
    "resumed_prior_unknown": "prior unknown",
}
CLASS_COLOR = {
    "fresh": "#2a78d6",
    "same_task": "#1baf7a",
    "cross_task": "#eb6834",
    "resumed_prior_unknown": MUTED,
}

GROUPS = [
    ("local-clive", "before"),
    ("local-clive", "after"),
    ("codex", "before"),
    ("codex", "after"),
]


def load(path: Path) -> dict:
    with path.open() as handle:
        return json.load(handle)


def share_rows():
    local = load(DATA / "local-session-carryover.json")
    codex = load(DATA / "codex-resume-burn-after.json")
    rows = []
    for lane, window in GROUPS:
        data = local if lane == "local-clive" else codex
        summary = data[f"{window}_window"]
        counts = summary["class_counts"]
        total = summary["dispatches"]
        rows.append(
            {
                "lane": lane,
                "window": window,
                "total": total,
                "shares": {name: counts.get(name, 0) / total for name in CLASS_ORDER},
                "counts": {name: counts.get(name, 0) for name in CLASS_ORDER},
            }
        )
    return rows


def label_small(axis, x, anchor, text, index):
    """Label a segment too thin to hold its own text, with an elbow leader.

    The leader leaves the bar's edge and rises into the band above the axis,
    where text cannot collide with a neighbouring bar or with the fills.
    """

    y = 104.0 + 5.5 * index
    axis.plot(
        [x + 0.36, x + 0.36, x + 0.5],
        [anchor, y, y],
        color=AXIS,
        linewidth=0.9,
        solid_capstyle="butt",
        zorder=3,
    )
    axis.text(
        x + 0.54,
        y,
        text,
        va="center",
        ha="left",
        fontsize=9,
        color=INK_2,
        zorder=4,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "class-carryover.png")
    args = parser.parse_args(argv)

    rows = share_rows()
    figure, axis = plt.subplots(figsize=(9.2, 5.4), dpi=200)
    figure.patch.set_facecolor(SURFACE)
    axis.set_facecolor(SURFACE)

    positions = [0.0, 1.0, 2.35, 3.35]
    width = 0.72
    for position, row in zip(positions, rows, strict=True):
        bottom = 0.0
        small_index = 0
        for name in CLASS_ORDER:
            share = row["shares"][name]
            if share <= 0:
                continue
            axis.bar(
                position,
                share * 100,
                width,
                bottom=bottom * 100,
                color=CLASS_COLOR[name],
                linewidth=0,
                zorder=2,
            )
            top = bottom + share
            if share >= 0.08:
                axis.text(
                    position,
                    (bottom + top) / 2 * 100,
                    f"{CLASS_LABEL[name]}\n{share * 100:.1f}%",
                    va="center",
                    ha="center",
                    fontsize=9.5,
                    color="#ffffff",
                    zorder=4,
                )
            else:
                label_small(
                    axis,
                    position,
                    (bottom + top) / 2 * 100,
                    f"{CLASS_LABEL[name]} {row['counts'][name]} ({share * 100:.2f}%)",
                    small_index,
                )
                small_index += 1
            bottom = top
        # A surface-coloured rule between the fills, so adjacent segments read
        # as separate quantities rather than one gradient.
        for name in CLASS_ORDER[:-1]:
            boundary = 0.0
            for inner in CLASS_ORDER[: CLASS_ORDER.index(name) + 1]:
                boundary += row["shares"][inner]
            if 0 < boundary * 100 < 100:
                axis.plot(
                    [position - width / 2, position + width / 2],
                    [boundary * 100, boundary * 100],
                    color=SURFACE,
                    linewidth=2.0,
                    solid_capstyle="butt",
                    zorder=3,
                )
        axis.text(
            position,
            -3.4,
            f"{row['lane']} · {row['window']}\n{row['total']:,} dispatches",
            ha="center",
            va="top",
            fontsize=9.5,
            color=INK_2,
        )

    axis.set_xlim(-0.75, 4.15)
    axis.set_ylim(0, 118)
    axis.set_yticks([0, 25, 50, 75, 100])
    axis.set_yticklabels(["0", "25", "50", "75", "100%"], fontsize=9.5, color=INK_2)
    axis.set_ylabel("share of dispatches", fontsize=10, color=INK)
    axis.set_xticks([])
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.spines["left"].set_color(AXIS)
    axis.spines["bottom"].set_visible(False)
    axis.tick_params(axis="y", length=0, pad=6)
    axis.set_title(
        "Session inheritance by lane, before and after reuse was keyed to the task",
        fontsize=11.5,
        color=INK,
        loc="left",
        pad=12,
    )

    figure.subplots_adjust(left=0.075, right=0.995, top=0.86, bottom=0.14)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.out, facecolor=SURFACE)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
