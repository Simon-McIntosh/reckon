"""Figure: the session teardown that answers for a leaked watch producer.

Draws the two stages a session end runs through — the record-driven reaper, then
the scan that reads every live producer's own configuration home — and the two
outcomes. The dashed gap is the case the reaper cannot see and the scan exists
for: a producer that never registered a seat record.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

OUT = "docs/figures/the-suite-is-green-at-head/watch-producer-session-check.png"


def draw_box(ax, x, y, w, h, text, face, edge="#334155", size=9.5, weight="normal"):
    box = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.10,rounding_size=0.10",
        linewidth=1.4,
        edgecolor=edge,
        facecolor=face,
    )
    ax.add_patch(box)
    ax.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        color="#0f172a",
        fontsize=size,
        fontweight=weight,
        va="center",
    )


def arrow(ax, xy_from, xy_to, color="#334155", dashed=False):
    ax.annotate(
        "",
        xy=xy_to,
        xytext=xy_from,
        arrowprops={
            "arrowstyle": "-|>",
            "color": color,
            "linewidth": 1.6,
            "linestyle": "--" if dashed else "-",
            "shrinkA": 2,
            "shrinkB": 2,
        },
    )


fig, ax = plt.subplots(figsize=(12.4, 6.6))
ax.set_xlim(0, 12.4)
ax.set_ylim(0, 6.6)
ax.axis("off")

ax.text(
    0.3,
    6.15,
    "No watch producer outlives the test session",
    fontsize=13.5,
    fontweight="bold",
    color="#0f172a",
)
ax.text(
    0.3,
    5.78,
    "arming is detached, so a producer a test starts is nobody's child: it is ended by a check at session end, not by the test's own teardown",
    fontsize=9.5,
    color="#475569",
)

draw_box(
    ax,
    0.3,
    3.95,
    3.55,
    1.15,
    "stage 1 — reaper\nreads the seat records\nunder the base temp and\nSIGTERMs each pid",
    "#dbeafe",
    weight="bold",
)
draw_box(ax, 4.45, 3.95, 3.0, 1.15, "bounded grace\nwait until those\npids leave /proc", "#e2e8f0")
draw_box(
    ax,
    8.05,
    3.95,
    4.05,
    1.15,
    "stage 2 — scan /proc\nfor every live crew watch\nwhose RECKON_HOME\na test created",
    "#dbeafe",
    weight="bold",
)
arrow(ax, (3.90, 4.52), (4.45, 4.52))
arrow(ax, (7.50, 4.52), (8.05, 4.52))

draw_box(
    ax,
    0.3,
    2.15,
    3.55,
    0.98,
    "a producer that never\nwrote a record is invisible\nto the reaper",
    "#fee2e2",
    edge="#b91c1c",
)
arrow(ax, (2.07, 3.95), (2.07, 3.13), color="#b91c1c", dashed=True)

draw_box(
    ax,
    2.4,
    0.75,
    3.4,
    1.05,
    "no survivor\nsession passes",
    "#dcfce7",
    edge="#15803d",
    weight="bold",
)
draw_box(
    ax,
    8.05,
    0.75,
    4.05,
    1.05,
    "survivors\nsession FAILS, naming\npid and RECKON_HOME",
    "#fee2e2",
    edge="#b91c1c",
    weight="bold",
)
arrow(ax, (10.07, 3.95), (10.07, 1.80), color="#b91c1c")
ax.text(10.2, 2.7, "survivor", fontsize=9, color="#b91c1c")
arrow(ax, (8.05, 1.27), (5.80, 1.27), color="#475569")
ax.text(6.5, 1.38, "none", fontsize=9, color="#15803d")

ax.text(
    0.3,
    0.25,
    "measured at 2a72458c: the check fails the session on a deliberately leaked producer (pid and RECKON_HOME reported);\nwith the check removed, the same leak goes unreported (exit 0, producer still alive)",
    fontsize=9,
    color="#475569",
)

fig.tight_layout()
fig.savefig(OUT, dpi=170)
print("wrote", OUT)