"""Draw the continuity decision: which dispatch may reuse a prior session.

The figure is a decision path, not a table. A dispatch is routed by the task
identity of the run it names, and the same-task branch carries the three reuse
forms that are kept deliberately. The fork is the rule, so it is what the
drawing has to show; the measured cost sits beneath it as the reason the fork
exists.
"""

from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

OUT = Path(__file__).with_name("continuity_decision.png")

FRESH = "#9a5a4a"
REUSE = "#4f7d5a"
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


fig, ax = plt.subplots(figsize=(10.4, 6.6))
ax.set_xlim(0, 10.4)
ax.set_ylim(0, 7.6)
ax.axis("off")

box(
    ax,
    3.4,
    6.7,
    3.6,
    0.62,
    "next piece of work",
    face="#eef1f6",
    edge="#33475b",
    text_color="#22303f",
)

box(
    ax,
    2.7,
    5.35,
    5.0,
    0.92,
    "is there a prior run of the SAME TASK?\n"
    "(project, plan, node id) — or the reviewed run id",
    face=QUESTION,
    edge=QUESTION,
    size=9.0,
)
arrow(ax, (5.2, 6.7), (5.2, 6.27), "")

box(
    ax,
    0.3,
    3.2,
    3.7,
    1.6,
    "FRESH session\n\nevery other dispatch —\na new node, new scope,\na wider file set",
    face=FRESH,
    edge=FRESH,
    size=9.0,
)
arrow(ax, (3.5, 5.35), (2.15, 4.8), "no", dx=-0.28)

box(
    ax,
    5.6,
    3.0,
    4.5,
    1.95,
    "RESUME that run's session\n\n"
    "① crew resume of the same run\n"
    "② crew redispatch of the same run\n"
    "③ a new run on the same task —\n"
    "     a repair of the same node, or a\n"
    "     re-review of the same reviewed run",
    face=REUSE,
    edge=REUSE,
    size=8.6,
)
arrow(ax, (6.9, 5.35), (7.85, 4.95), "yes", dx=0.34)

ax.text(
    0.3,
    2.35,
    "Measured 2026-09-23 over 5,440 runs. A session keyed by member inherited whatever that member "
    "did last:\n312 of 2,089 local runs resumed another task's session and started their first turn at a "
    "median 81,957 tokens\n(max 494,189) against ~60,000 of fresh setup; 1,315 of 3,105 codex runs (42%) "
    "resumed one. A session is\nresumed only on the same task, and never when its run ended too large to "
    "continue (Prompt is too long /\nblocking_limit, or compaction that never completed).",
    fontsize=8.4,
    color="#444444",
    va="top",
)

fig.tight_layout()
fig.savefig(OUT, dpi=160)
print(OUT)