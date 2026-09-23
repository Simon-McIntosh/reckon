"""Draw the two questions a promoted-revision sweep asks, and its four outcomes.

The figure is a decision path, not a table: a row either fails ancestry or
passes through to the marker question, and the marker question itself forks on
whether the passing answer is presence or absence. The fork is the reason the
removal assertion exists, so it is what the drawing has to show.
"""

from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

OUT = Path(__file__).with_name("landed_sweep_decision.png")

REPORT = "#c65d3b"
PASS = "#4f7d5a"
QUESTION = "#3f5d8a"


def box(ax, x, y, w, h, text, *, face, edge, text_color="white", size=9.5):
    ax.add_patch(
        mpatches.FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.02,rounding_size=0.04",
            linewidth=1.4,
            facecolor=face,
            edgecolor=edge,
        )
    )
    ax.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        color=text_color,
        fontsize=size,
    )


def arrow(ax, start, end, label, *, color="#555555", dx=0.0):
    ax.annotate(
        "",
        xy=end,
        xytext=start,
        arrowprops={"arrowstyle": "-|>", "color": color, "linewidth": 1.4},
    )
    if label:
        ax.text(
            (start[0] + end[0]) / 2 + dx,
            (start[1] + end[1]) / 2,
            label,
            ha="center",
            va="center",
            fontsize=8.5,
            color=color,
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.0},
        )


fig, ax = plt.subplots(figsize=(9.6, 6.4))
ax.set_xlim(0, 10)
ax.set_ylim(0, 7.2)
ax.axis("off")

box(
    ax,
    3.0,
    6.3,
    4.0,
    0.68,
    "promoted row: promoted_revision",
    face="#eef1f6",
    edge="#33475b",
    text_color="#22303f",
)

ax.text(
    0.15,
    6.64,
    "each promoted run",
    fontsize=8.5,
    color="#666666",
    va="center",
)

box(
    ax,
    2.9,
    5.0,
    4.2,
    0.72,
    "Q1  is promoted_revision an ancestor\nof the target head?",
    face=QUESTION,
    edge=QUESTION,
)
arrow(ax, (5.0, 6.3), (5.0, 5.72), "")

box(
    ax,
    0.35,
    3.35,
    3.5,
    0.86,
    "REPORT\nnot-an-ancestor\n(never merged)",
    face=REPORT,
    edge=REPORT,
)
arrow(ax, (2.9, 5.36), (2.1, 4.21), "no")

box(
    ax,
    4.5,
    3.35,
    5.15,
    0.86,
    "Q2  marker the change introduced present\n— or removed marker absent?",
    face=QUESTION,
    edge=QUESTION,
)
arrow(ax, (5.9, 5.0), (6.6, 4.21), "yes", dx=0.42)

box(
    ax,
    2.1,
    1.6,
    2.4,
    0.9,
    "REPORT\nmarker-absent\n(merged then dropped)",
    face=REPORT,
    edge=REPORT,
)
arrow(ax, (5.2, 3.35), (3.3, 2.5), "content gone", dx=-0.15)

box(
    ax,
    5.0,
    1.6,
    2.4,
    0.9,
    "REPORT\nremoved-marker-still-present\n(deletion dropped)",
    face=REPORT,
    edge=REPORT,
)
arrow(ax, (7.0, 3.35), (6.2, 2.5), "deletion undone", dx=0.35)

box(
    ax,
    7.8,
    1.6,
    2.0,
    0.9,
    "no report\n(landed intact)",
    face=PASS,
    edge=PASS,
)
arrow(ax, (8.7, 3.35), (8.8, 2.5), "intact", dx=-0.1)

ax.text(
    0.15,
    0.6,
    "Two questions, neither subsuming the other: a run can pass ancestry and still "
    "have lost its content,\nand a dropped deletion is silent to a presence-only check.",
    fontsize=8.5,
    color="#444444",
    va="center",
)

fig.tight_layout()
fig.savefig(OUT, dpi=160)
print(OUT)