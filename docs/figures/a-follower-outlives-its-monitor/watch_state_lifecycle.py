"""Draw the state word the classifier emits across a delivered run's life.

A delivered run that has not been promoted reads as completed_unpromoted. One
test in the watch-lifetime file also reads the state the run leaves behind when
it is promoted, so the figure marks both ends of that transition and counts the
assertion sites the correction touches. The stored pointer phase is drawn
separately because it is not the classifier's reading: the tests read the
classifier, never the pointer's own stored word.

Run: python docs/figures/a-follower-outlives-its-monitor/watch_state_lifecycle.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

HERE = Path(__file__).resolve().parent

STATE_FACE = "#e8f0fe"
STATE_EDGE = "#3b6fb6"
PHASE_FACE = "#f3f3f3"
PHASE_EDGE = "#9a9a9a"
INK = "#1c1c1c"


def _state(ax, x, y, w, h, label, sub, *, face, edge, size=10.5):
    box = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.02,rounding_size=0.08",
        linewidth=1.6,
        edgecolor=edge,
        facecolor=face,
    )
    ax.add_patch(box)
    ax.text(
        x + w / 2,
        y + h * 0.62,
        label,
        ha="center",
        va="center",
        fontsize=size,
        fontweight="bold",
        color=INK,
    )
    ax.text(
        x + w / 2,
        y + h * 0.26,
        sub,
        ha="center",
        va="center",
        fontsize=8.5,
        color="#4a4a4a",
    )


def _arrow(ax, xy_from, xy_to, label):
    ax.add_patch(
        FancyArrowPatch(
            xy_from,
            xy_to,
            arrowstyle="-|>",
            mutation_scale=16,
            linewidth=1.6,
            color=STATE_EDGE,
        )
    )
    mx = (xy_from[0] + xy_to[0]) / 2
    ax.text(
        mx,
        xy_from[1] + 0.13,
        label,
        ha="center",
        va="bottom",
        fontsize=8,
        color="#4a4a4a",
    )


def main() -> None:
    fig, ax = plt.subplots(figsize=(11.5, 3.9))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 3.6)
    ax.set_aspect("auto")
    ax.axis("off")

    w, h, y = 2.8, 1.05, 1.75
    gap = 1.15
    xs = [0.2, 0.2 + w + gap, 0.2 + 2 * (w + gap)]

    _state(ax, xs[0], y, w, h, "dispatched", "worker running",
           face=STATE_FACE, edge=STATE_EDGE)
    _state(ax, xs[1], y, w, h, "completed unpromoted",
           "delivered, not yet promoted",
           face=STATE_FACE, edge=STATE_EDGE, size=10)
    _state(ax, xs[2], y, w, h, "promoted", "gate closes, work landed",
           face=STATE_FACE, edge=STATE_EDGE)

    _arrow(ax, (xs[0] + w, y + h / 2), (xs[1], y + h / 2), "worker\nexits")
    _arrow(ax, (xs[1] + w, y + h / 2), (xs[2], y + h / 2), "promotes")

    ax.text(0.2, 3.45,
            "The classifier's state word for a delivered run",
            fontsize=13, fontweight="bold", color=INK, ha="left", va="top")

    ax.annotate(
        "4 assertion sites corrected:\n"
        "   cold watch — baseline to_state, and promoted from_state\n"
        "   dispatch admitted — baseline to_state\n"
        "   empty fleet — baseline to_state",
        xy=(xs[1] + w / 2, y - 0.02),
        xytext=(xs[1] + w / 2 - 0.3, 0.05),
        ha="left", va="bottom", fontsize=8.5, color=INK,
        arrowprops=dict(arrowstyle="-", color="#8a8a8a", linewidth=1.0),
    )

    ax.text(
        11.8, 0.55,
        "stored phase: complete\n(not read by these tests)",
        ha="right", va="center", fontsize=8, color="#5a5a5a",
        bbox=dict(boxstyle="round,pad=0.35", facecolor=PHASE_FACE,
                  edgecolor=PHASE_EDGE, linewidth=1.0),
    )

    fig.tight_layout()
    fig.savefig(HERE / "watch-state-lifecycle.png", dpi=160)
    print(HERE / "watch-state-lifecycle.png")


if __name__ == "__main__":
    main()