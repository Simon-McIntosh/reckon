"""Two launch paths: where an OSError from the worker spawn ends up.

The left column is the dispatch before the change: the launch *plan* is wrapped
so its errors become a refusal, but the *spawn* beneath it is not, so an OSError
from the supervisor fork or a launcher reaches the caller as a traceback with no
remedy. The right column is after: the spawn sits inside the same wrap, so the
same OSError is rendered as a launch refusal and the unwind still runs.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).with_name("launcher-oserror-refusal.png")

GREEN = "#2e7d32"
RED = "#c62828"
GREY = "#455a64"
BOX = "#eceff1"


def box(ax, x, y, w, h, text, *, edge, fc=BOX, fs=9.5):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.02,rounding_size=0.04",
            linewidth=1.6,
            edgecolor=edge,
            facecolor=fc,
        )
    )
    ax.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        fontsize=fs,
        color="#263238",
    )


def arrow(ax, start, end, *, color, style="-|>", label=None, dy=0.0):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle=style,
            mutation_scale=14,
            linewidth=1.6,
            color=color,
            connectionstyle=None,
            shrinkA=1,
            shrinkB=1,
        )
    )
    if label:
        mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2 + dy
        ax.text(mx, my, label, ha="center", va="center", fontsize=8.4, color=color)


fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.6))
for ax, title in zip(
    axes,
    ("At base -- OSError escapes", "At head -- OSError refused as D22 refusal"),
    strict=True,
):
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title(title, fontsize=11.5, color="#1a237e", pad=8)

base, head = axes

# -- base ------------------------------------------------------------------
box(
    base,
    0.08,
    0.78,
    0.84,
    0.13,
    "resolve launch plan\n[wrapped: BackendError | flight | OSError]",
    edge=GREEN,
)
box(
    base,
    0.08,
    0.52,
    0.84,
    0.13,
    "start worker\n(unwrapped: supervisor fork / launcher)",
    edge=RED,
)
box(
    base,
    0.08,
    0.16,
    0.84,
    0.15,
    "OSError reaches the caller\ntraceback, no remedy, no watch row",
    edge=RED,
    fc="#ffebee",
)
arrow(base, (0.5, 0.78), (0.5, 0.65), color=GREY)
arrow(
    base, (0.5, 0.52), (0.5, 0.31), color=RED, label="only the plan is wrapped", dy=0.03
)

# -- head ------------------------------------------------------------------
box(
    head,
    0.08,
    0.78,
    0.84,
    0.13,
    "resolve launch plan\n[wrapped: BackendError | flight | OSError]",
    edge=GREEN,
)
box(
    head,
    0.08,
    0.52,
    0.84,
    0.13,
    "start worker\n[wrapped: supervisor fork / launcher OSError]",
    edge=GREEN,
)
box(
    head,
    0.08,
    0.16,
    0.84,
    0.15,
    "CrewError refusal\nResolve with `reckon crew dispatch` ...",
    edge=GREEN,
    fc="#e8f5e9",
)
arrow(head, (0.5, 0.78), (0.5, 0.65), color=GREY)
arrow(
    head,
    (0.5, 0.52),
    (0.5, 0.31),
    color=GREEN,
    label="same wrap, now covering the spawn",
    dy=0.03,
)

fig.suptitle(
    "A dispatch's worker-launch OSError: where it lands before and after:",
    fontsize=12.5,
    color="#1a237e",
    y=0.99,
)
fig.text(
    0.5,
    0.02,
    "The unwind (unlink pointer, remove run directory, remove worktree) runs on "
    "either path, so a refusal leaves no trace.",
    ha="center",
    fontsize=8.6,
    color=GREY,
)
fig.tight_layout(rect=(0, 0.04, 1, 0.95))
fig.savefig(OUT, dpi=150)
print(OUT)
