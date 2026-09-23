"""Draw the ticker row's column budget before and after the state cell change.

One row of the fleet ticker is a fixed grid; the reason clause is the only free
text and is truncated to whatever the fixed cells leave. The figure shows the
fixed cells as stacked bars at a 180-column pane on each side of the change, so
the thirteen columns that move from the state region into the reason cell are
visible as a shift rather than read out of a table.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

WIDTH = 180
# The fixed cells left of the state region are identical on both sides; only the
# state region and the reason room differ.
LEADING = 8 + 2 + 13 + 2 + 36  # clock, role, node with their gaps
TRAILING = 10 + 1 + 7 + 11 + 13 + 2  # model, effort, counters, spend, tail gap

BASE = {
    "leading": LEADING,
    "state": 27,  # two ten-column states, the gaps, and the arrow cell
    "trailing": TRAILING,
}
HEAD = {
    "leading": LEADING,
    "state": 14,  # marker cell, destination state, separator
    "trailing": TRAILING,
}

COLOURS = {
    "leading": "#b9c6d6",
    "state": "#e0956a",
    "trailing": "#cfd8c5",
    "reason": "#6b8f6b",
}
LABELS = {
    "leading": "clock · role · node",
    "state": "state region",
    "trailing": "model · effort · counters · spend",
}


def draw(ax, cells: dict[str, int], title: str) -> None:
    x = 0
    reason = WIDTH - cells["leading"] - cells["state"] - cells["trailing"]
    for key in ("leading", "state", "trailing"):
        width = cells[key]
        ax.barh(0, width, left=x, height=0.5, color=COLOURS[key], edgecolor="white")
        ax.text(
            x + width / 2,
            0,
            f"{LABELS[key]}\n{cells[key]}",
            ha="center",
            va="center",
            fontsize=7,
        )
        x += width
    ax.barh(0, reason, left=x, height=0.5, color=COLOURS["reason"], edgecolor="white")
    ax.text(
        x + reason / 2,
        0,
        f"reason clause: {reason}",
        ha="center",
        va="center",
        fontsize=8,
        color="white",
        fontweight="bold",
    )
    ax.set_title(title, fontsize=10)
    ax.set_xlim(0, WIDTH)
    ax.set_ylim(-0.5, 0.5)
    ax.set_yticks([])
    ax.set_xlabel("screen column")
    ax.set_xticks(range(0, WIDTH + 1, 30))


def main() -> None:
    fig, axes = plt.subplots(2, 1, figsize=(9, 3.2), sharex=True)
    draw(axes[0], BASE, "base revision", )
    draw(axes[1], HEAD, "this change")
    for ax in axes:
        for spine in ("top", "right", "left"):
            ax.spines[spine].set_visible(False)
    axes[0].annotate(
        "state region 27 → 14",
        xy=(LEADING + 14, -0.28),
        xytext=(LEADING + 26, -0.62),
        fontsize=8,
        arrowprops={"arrowstyle": "->", "lw": 0.8},
    )
    fig.tight_layout()
    out = Path(__file__).with_name("row_column_budget.png")
    fig.savefig(out, dpi=150)
    print(out)


if __name__ == "__main__":
    main()