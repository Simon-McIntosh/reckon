"""Which revision turned tests/test_a_live_run_never_reads_dead.py red.

The figure places six revisions along a schematic axis in commit order and marks
each one that was actually executed. Two slots carry no measured outcome and are
drawn hollow, so the figure never implies a measurement nobody took. Numbers
come from the arm logs beside the report; nothing here reads the tree.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
AXIS = "#c3c2b7"
STATUS_GOOD = "#0ca30c"
STATUS_CRITICAL = "#d03b3b"

# (x, short sha, caption, outcome) — outcome is "pass", "fail" or None for a
# revision read for context but not executed.
SLOTS = [
    (0, "50aff503", "the asserting test is written", None),
    (1, "132a7d4b", "the commit before the inversion", "pass"),
    (2, "ddf4509f", "the departure word is inverted", "fail"),
    (3, "af15a34b", "the merge's first parent", "fail"),
    (4, "92c39d90", "the merge under suspicion", None),
    (5, "356928b3", "origin/main head", "fail"),
]

COUNTS = {"pass": "1 passed, 0 failed", "fail": "11 passed, 1 failed"}

fig, ax = plt.subplots(figsize=(11.4, 5.0), dpi=200)
fig.subplots_adjust(top=0.80, bottom=0.10, left=0.03, right=0.97)
fig.patch.set_facecolor(SURFACE)
ax.set_facecolor(SURFACE)

ax.plot([-0.55, 5.55], [0, 0], color=AXIS, linewidth=1.0, zorder=1)

for x, sha, caption, outcome in SLOTS:
    if outcome is None:
        ax.plot(
            x,
            0,
            marker="o",
            markersize=11,
            markerfacecolor=SURFACE,
            markeredgecolor=MUTED,
            markeredgewidth=2.0,
            zorder=3,
        )
        ax.text(
            x, 0.34, "not run", ha="center", va="baseline", color=MUTED, fontsize=8.5
        )
        ax.text(x, 0.62, "—", ha="center", va="baseline", color=MUTED, fontsize=9.5)
    else:
        color = STATUS_GOOD if outcome == "pass" else STATUS_CRITICAL
        ax.plot(
            x,
            0,
            marker="o",
            markersize=18,
            markerfacecolor=color,
            markeredgecolor=SURFACE,
            markeredgewidth=2.0,
            zorder=4,
        )
        ax.text(
            x,
            0,
            "✓" if outcome == "pass" else "✗",
            ha="center",
            va="center",
            color=SURFACE,
            fontsize=10.5,
            fontweight="bold",
            zorder=5,
        )
        ax.text(
            x,
            0.62,
            outcome.upper(),
            ha="center",
            va="baseline",
            color=INK,
            fontsize=9.5,
            fontweight="bold",
        )
        ax.text(
            x,
            0.34,
            COUNTS[outcome],
            ha="center",
            va="baseline",
            color=INK,
            fontsize=9.5,
        )
    ax.text(
        x,
        -0.30,
        sha,
        ha="center",
        va="top",
        color=INK,
        fontsize=9.5,
        family="monospace",
    )
    ax.text(
        x,
        -0.58,
        textwrap.fill(caption, 17),
        ha="center",
        va="top",
        color=INK_SECONDARY,
        fontsize=8.5,
        linespacing=1.4,
    )

ax.annotate(
    "",
    xy=(3.74, 0.86),
    xytext=(4.26, 0.86),
    arrowprops={
        "arrowstyle": "|-|",
        "color": MUTED,
        "linewidth": 1.2,
        "shrinkA": 0,
        "shrinkB": 0,
    },
)
ax.text(
    4.0,
    0.94,
    "reason text only",
    ha="center",
    va="bottom",
    color=INK_SECONDARY,
    fontsize=8.5,
)
ax.text(
    4.0,
    1.16,
    "SUSPECT, CLEARED",
    ha="center",
    va="bottom",
    color=INK,
    fontsize=9.5,
    fontweight="bold",
)

ax.set_title(
    "Where tests/test_a_live_run_never_reads_dead.py turns red",
    color=INK,
    fontsize=12.5,
    pad=36,
    loc="left",
)
ax.text(
    0.0,
    1.015,
    "Six revisions in commit order, schematic spacing; "
    "markers show the runs actually executed",
    transform=ax.transAxes,
    color=INK_SECONDARY,
    fontsize=8.5,
    va="bottom",
)

legend = ax.legend(
    handles=[
        Line2D(
            [],
            [],
            marker="o",
            linestyle="none",
            markersize=10,
            markerfacecolor=STATUS_GOOD,
            markeredgecolor=SURFACE,
            label="✓  the asserting test passes",
        ),
        Line2D(
            [],
            [],
            marker="o",
            linestyle="none",
            markersize=10,
            markerfacecolor=STATUS_CRITICAL,
            markeredgecolor=SURFACE,
            label="✗  the asserting test fails",
        ),
    ],
    loc="lower center",
    bbox_to_anchor=(0.5, -0.02),
    ncol=2,
    frameon=False,
    fontsize=9,
    handletextpad=0.8,
)
for text in legend.get_texts():
    text.set_color(INK)

ax.set_xlim(-0.75, 5.75)
ax.set_ylim(-1.18, 1.55)
ax.axis("off")

out = Path(__file__).with_name("revision_attribution_timeline.png")
fig.savefig(out, facecolor=SURFACE, bbox_inches="tight")
print(f"wrote {out}")
