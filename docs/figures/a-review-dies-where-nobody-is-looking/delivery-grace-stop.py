"""Draw when the watcher ends a review that has already delivered.

Four reviews on one time axis. Each has a stream that keeps growing to the
right; the marker where its record is stored and its manifest reads complete is
the same instant the grace is measured from. Only the review whose stream
outlives that grace by more than five minutes is stopped.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

GRACE = 300.0
HERE = Path(__file__).resolve().parent

DELIVERED = "#2f7d4f"
STOPPED = "#a4262c"
UNKNOWN = "#8a8f98"
INK = "#1f2933"
PAPER = "#ffffff"

# label, stream length after delivery (s), delivered?, stopped by this rule?
ROWS = [
    ("delivered, stream runs 10 min", 600.0, True, True),
    ("delivered, stream runs 1 min", 60.0, True, False),
    ("delivered, stream runs 30 s", 30.0, True, False),
    ("no stored record, stream runs 3 d (drawn capped)", 660.0, False, False),
]
DISPLAY_MAX = 660.0


def draw() -> None:
    fig, ax = plt.subplots(figsize=(9.0, 4.4), dpi=180)
    fig.patch.set_facecolor(PAPER)
    ax.set_facecolor(PAPER)

    for index, (label, span, delivered, stopped) in enumerate(ROWS):
        y = len(ROWS) - index
        bar = min(span, DISPLAY_MAX)
        ax.barh(
            y,
            bar,
            height=0.34,
            color=DELIVERED if delivered else UNKNOWN,
            alpha=0.30,
            edgecolor="none",
        )
        if delivered:
            ax.add_line(
                Line2D([0, 0], [y - 0.24, y + 0.24], color=DELIVERED, linewidth=2.4)
            )
            ax.add_line(
                Line2D(
                    [GRACE, GRACE],
                    [y - 0.24, y + 0.24],
                    color=INK,
                    linewidth=1.6,
                    linestyle=(0, (4, 1, 1, 1, 1, 1)),
                )
            )
            if stopped:
                stop_at = span
                ax.plot([stop_at], [y], marker="X", markersize=11, color=STOPPED)
                ax.annotate(
                    "SIGTERM to this run's pid",
                    xy=(stop_at, y),
                    xytext=(stop_at - 6, y + 0.42),
                    ha="right",
                    va="bottom",
                    fontsize=8.4,
                    color=STOPPED,
                )
        ax.text(-14, y, label, ha="right", va="center", fontsize=9.2, color=INK)

    ax.text(
        DISPLAY_MAX,
        len(ROWS) + 1.0,
        "stream keeps growing",
        ha="right",
        va="bottom",
        fontsize=8.6,
        color=UNKNOWN,
    )
    ax.set_xlim(-330, DISPLAY_MAX + 20)
    ax.set_ylim(0.4, len(ROWS) + 1.6)
    ax.set_yticks([])
    ax.set_xticks([0, GRACE, 600], labels=["0", "grace\n300 s", "600 s"])
    ax.set_xlabel(
        "seconds after the review delivers (record stored + manifest complete)",
        fontsize=9.0,
        color=INK,
    )
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(UNKNOWN)
    ax.tick_params(colors=INK, labelsize=8.6)
    fig.tight_layout()
    out = HERE / "delivery-grace-stop.png"
    fig.savefig(out, facecolor=PAPER)
    print(out)


if __name__ == "__main__":
    draw()
