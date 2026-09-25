"""Draw the crew rule the sprint skill now teaches: liveness is read, not stored.

A sprint is live when at least one live run pointer in its project serves a plan
whose `plan-sprint` is that sprint and that pointer is classified working,
starting or dispatched; a blocked or unpromoted pointer is held, not live. The
derivation runs at read time from the same classification the live crew view
uses, and is never written to a resource.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

LIVE = "#27ae60"
HELD = "#95a5a6"
INK = "#2c3e50"
MUTED = "#7f8c8d"


def box(ax, x, y, w, h, text, face, ink="white", size=8.5, lw=0.8):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.06",
            linewidth=lw,
            edgecolor=INK,
            facecolor=face,
        )
    )
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size, color=ink)


def main() -> None:
    fig, ax = plt.subplots(figsize=(9.6, 4.4), layout="constrained")
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    ax.set_title(
        "the crew rule: liveness is read, never stored",
        loc="left",
        fontsize=10.5,
        color=INK,
    )
    ax.text(0.1, 8.75, "a live run pointer's own classification decides", fontsize=8.5, color=MUTED)

    rows = [
        (7.1, "coord run · working", "plan-sprint S23", "S23 · live", LIVE),
        (5.5, "worker run · working", "plan-sprint S21", "S21 · live", LIVE),
        (3.9, "worker run · blocked", "plan-sprint S12", "S12 · held, not live", HELD),
    ]
    for y, pointer, plan, badge, colour in rows:
        box(ax, 0.1, y, 3.2, 1.0, pointer, colour)
        ax.text(3.5, y + 0.5, "→", fontsize=12, color=INK)
        ax.text(3.85, y + 0.5, plan, fontsize=8.5, color=INK, va="center")
        box(ax, 7.1, y + 0.07, 2.8, 0.86, badge, "white", ink=INK, size=8.5, lw=3.2)

    ax.text(
        0.1,
        2.3,
        "Several sprints may be active, and several may be live, at once.\n"
        "active_sprint_id follows the most recently active live sprint; it is a\n"
        "compatibility field, not evidence that anyone is working.",
        fontsize=8.5,
        color=MUTED,
        va="top",
    )

    out = Path(__file__).with_name("liveness-derivation.png")
    fig.savefig(out, dpi=160, facecolor="white")
    print(out)


if __name__ == "__main__":
    main()