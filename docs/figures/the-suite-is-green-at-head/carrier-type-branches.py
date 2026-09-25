"""How a reexec handover carrier is classified, before and after the change.

The parsed payload is one of three shapes: a mapping (its ``pids`` field is the
carrier), a list or tuple (itself the pid sequence), or any other parsed JSON
type — an integer scalar such as ``123``, or a bare string. The first two are
carriers; the third is not a pid sequence at all. Before the change that third
shape fell into the pid-list branch and was iterated, so a scalar raised
``TypeError`` and refused to start the new followed image. After the change the
type test admits only a list or tuple, and every other shape starts the new
image with an empty registry.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).with_name("carrier-type-branches.png")

GREEN = "#2e7d32"
RED = "#c62828"
GREY = "#455a64"
BOX = "#eceff1"

ROOT = ("json.loads(carrier)\n(withheld if empty, refused if unparsable)", 0.5, 0.87)
BRANCHES = [
    ('isinstance(payload, dict)', "pids = payload['pids']\nruns_carried = payload['runs']", 0.17),
    ("isinstance(payload, (list, tuple))", "pids = payload", 0.50),
    ("any other parsed type\n(e.g. the scalar 123, a string)", "no pid sequence", 0.83),
]


def box(ax, x, y, text, *, face=BOX, edge=GREY, w=0.28, h=0.13, size=9):
    ax.add_patch(
        FancyBboxPatch(
            (x - w / 2, y - h / 2),
            w,
            h,
            boxstyle="round,pad=0.012,rounding_size=0.02",
            linewidth=1.4,
            facecolor=face,
            edgecolor=edge,
        )
    )
    ax.text(x, y, text, ha="center", va="center", fontsize=size, color="#102027")


def arrow(ax, x0, y0, x1, y1, color=GREY):
    ax.add_patch(
        FancyArrowPatch(
            (x0, y0), (x1, y1), arrowstyle="-|>", mutation_scale=13, linewidth=1.4, color=color
        )
    )


fig, ax = plt.subplots(figsize=(11, 6.6))
ax.set_xlim(0, 1)
ax.set_ylim(0, 1.02)
ax.axis("off")

box(ax, ROOT[1], ROOT[2], ROOT[0], size=10)

for label, effect, x in BRANCHES:
    arrow(ax, ROOT[1], ROOT[2] - 0.075, x, 0.68)
    box(ax, x, 0.60, label, w=0.28, h=0.15, size=8.5)
    box(ax, x, 0.43, effect, w=0.28, h=0.13, size=8.5)

# Before / after outcome rows for the third branch.
box(ax, 0.83, 0.245, "", face="#ffebee", edge=RED, w=0.28, h=0.14, size=8.5)
ax.text(0.83, 0.245, "before: TypeError\n'int' object is not iterable", ha="center", va="center",
        fontsize=8.5, color=RED, fontweight="bold")
box(ax, 0.83, 0.085, "after: registry stays empty\nnew image starts clean", face="#e8f5e9", edge=GREEN, w=0.28, h=0.14, size=8.5)

# The two genuine carriers are unchanged in both images.
box(ax, 0.335, 0.245, "unchanged in both images:\npids registered and reaped", face="#e3f2fd", edge="#1565c0", w=0.28, h=0.14, size=8.5)

fig.text(0.5, 0.975, "Reexec carrier classification: only a mapping or a sequence is a carrier",
         ha="center", va="top", fontsize=11, fontweight="bold", color="#102027")

fig.tight_layout()
fig.savefig(OUT, dpi=140)
print(f"wrote {OUT}")