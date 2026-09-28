"""Draw a review sweep's enumeration order and where stopping early cuts it.

The continuation claim is about ORDER, not about counts: the refused run is the
one the sweep reaches first, so a sweep that stops at the first refusal never
reaches the run behind it and reports an empty dispatch list. Drawing the two
panels side by side is what makes that visible - the same three pointers, the
same refusal, and one break in the middle of the walk.

Run: python docs/figures/scoring-should-stop-a-promotion/review_refusal_continuation.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

HERE = Path(__file__).resolve().parent

INK = "#1c1c1c"
MUTED = "#4a4a4a"
NEUTRAL_FACE = "#f3f3f3"
NEUTRAL_EDGE = "#9a9a9a"
REFUSED_FACE = "#fdecea"
REFUSED_EDGE = "#b03a2e"
DISPATCHED_FACE = "#e8f0fe"
DISPATCHED_EDGE = "#3b6fb6"
UNREACHED_FACE = "#fbfbfb"
UNREACHED_EDGE = "#c8c8c8"

ROWS = (
    (
        "r-claiming-elsewhere",
        "live; holds the claim on r-one's review record",
        NEUTRAL_FACE,
        NEUTRAL_EDGE,
    ),
    ("r-one", "scoring; its review dispatch is refused", REFUSED_FACE, REFUSED_EDGE),
    ("r-two", "scoring; its review dispatch is issued", DISPATCHED_FACE, DISPATCHED_EDGE),
)

BOX_X, BOX_W, BOX_H = 0.35, 7.0, 0.95
ROW_Y = (4.55, 3.05, 1.55)


def _row(ax, y, label, sub, face, edge, *, dashed=False):
    ax.add_patch(
        FancyBboxPatch(
            (BOX_X, y),
            BOX_W,
            BOX_H,
            boxstyle="round,pad=0.02,rounding_size=0.07",
            linewidth=1.5,
            edgecolor=edge,
            facecolor=face,
            linestyle="--" if dashed else "-",
        )
    )
    ax.text(
        BOX_X + 0.22,
        y + BOX_H * 0.66,
        label,
        ha="left",
        va="center",
        fontsize=10,
        fontweight="bold",
        color=INK if not dashed else MUTED,
    )
    ax.text(
        BOX_X + 0.22,
        y + BOX_H * 0.27,
        sub,
        ha="left",
        va="center",
        fontsize=8.2,
        color=MUTED,
    )


def _arrow(ax, start, end, text, *, color="#6a6a6a", style="-", offset=0.12):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=11,
            linewidth=1.3,
            color=color,
            linestyle=style,
            shrinkA=0,
            shrinkB=0,
        )
    )
    if text:
        ax.text(
            start[0] - offset,
            (start[1] + end[1]) / 2,
            text,
            ha="right",
            va="center",
            fontsize=8,
            color=MUTED,
        )


def _panel(ax, title, note):
    ax.set_xlim(0, 11.4)
    ax.set_ylim(0.6, 6.35)
    ax.axis("off")
    ax.text(0.35, 6.15, title, fontsize=11.5, fontweight="bold", color=INK, va="top")
    ax.text(0.35, 5.72, note, fontsize=8.6, color=MUTED, va="top")


def main() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 4.6))

    left = axes[0]
    _panel(
        left,
        "The sweep as it runs",
        "pointers in enumeration order: run id ascending",
    )
    for y, row in zip(ROW_Y, ROWS, strict=True):
        _row(left, y, *row)
    _arrow(left, (BOX_X + BOX_W * 0.5, ROW_Y[0]), (BOX_X + BOX_W * 0.5, ROW_Y[1] + BOX_H), "skip")
    _arrow(left, (BOX_X + BOX_W * 0.5, ROW_Y[1]), (BOX_X + BOX_W * 0.5, ROW_Y[2] + BOX_H), "keep going")
    left.text(
        BOX_X + BOX_W + 0.25,
        ROW_Y[1] + BOX_H / 2,
        "recorded\nrefused",
        ha="left",
        va="center",
        fontsize=8.4,
        color=REFUSED_EDGE,
        fontweight="bold",
    )
    left.text(
        BOX_X + BOX_W + 0.25,
        ROW_Y[2] + BOX_H / 2,
        "review run\nin flight",
        ha="left",
        va="center",
        fontsize=8.4,
        color=DISPATCHED_EDGE,
        fontweight="bold",
    )
    left.text(
        BOX_X,
        0.85,
        "report: dispatched = [review of r-two]      refused = [r-one]",
        ha="left",
        va="center",
        fontsize=8.6,
        color=INK,
        family="monospace",
    )

    right = axes[1]
    _panel(
        right,
        "Stopping at the first refusal",
        "the same walk, cut where the refusal lands",
    )
    _row(right, ROW_Y[0], *ROWS[0])
    _row(right, ROW_Y[1], *ROWS[1])
    _row(right, ROW_Y[2], ROWS[2][0], "never reached", UNREACHED_FACE, UNREACHED_EDGE, dashed=True)
    _arrow(right, (BOX_X + BOX_W * 0.5, ROW_Y[0]), (BOX_X + BOX_W * 0.5, ROW_Y[1] + BOX_H), "skip")
    cut = ROW_Y[1] - 0.14
    right.plot(
        [BOX_X - 0.15, BOX_X + BOX_W + 0.1],
        [cut, cut],
        color=REFUSED_EDGE,
        linewidth=1.6,
        linestyle=(0, (4, 3)),
    )
    right.text(
        BOX_X + BOX_W + 0.25,
        cut,
        "break after the\nfirst refusal",
        ha="left",
        va="center",
        fontsize=8.4,
        color=REFUSED_EDGE,
        fontweight="bold",
    )
    right.text(
        BOX_X,
        0.85,
        "report: dispatched = []      refused = [r-one]",
        ha="left",
        va="center",
        fontsize=8.6,
        color=INK,
        family="monospace",
    )

    fig.tight_layout()
    target = HERE / "review_refusal_continuation.png"
    fig.savefig(target, dpi=160)
    print(target)


if __name__ == "__main__":
    main()