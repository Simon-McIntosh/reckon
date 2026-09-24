"""Render the nested-launch decision flow.

The order the shim takes its checks in is the part a reader cannot see from the
values alone: the in-allocation gate comes first, the discharge and the
explicit-single-step form are tested only once the process is known to be
inside the job, and every path that is not a refusal execs the real binary with
the argv unchanged. The figure marks the gate the declared mutation removes.

A decision flow rather than a table: the branches are what matters, and the two
exec arms are distinguishable only by where they sit in the order.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = Path(__file__).resolve().parent / "nested-launch-decision-flow.png"

BOX = {"boxstyle": "round,pad=0.45", "linewidth": 1.2}
EDGE = "#444444"
GUARD = "#1b6ca8"
EXEC = "#2e7d32"
REFUSE = "#b03030"
QUESTION = "#eeeeee"
TEXT = "#222222"


def box(ax, x, y, text, *, color, edge=EDGE):
    ax.text(
        x,
        y,
        text,
        ha="center",
        va="center",
        fontsize=9,
        color=TEXT,
        bbox={**BOX, "facecolor": color, "edgecolor": edge},
    )


def arrow(ax, start, end, label=None, *, color=EDGE, dx=0.0):
    ax.annotate(
        "",
        xy=end,
        xytext=start,
        arrowprops={"arrowstyle": "-|>", "color": color, "linewidth": 1.2},
    )
    if label:
        ax.text(
            (start[0] + end[0]) / 2 + dx,
            (start[1] + end[1]) / 2,
            label,
            ha="center",
            va="center",
            fontsize=8,
            color=color,
        )


def main() -> int:
    fig, ax = plt.subplots(figsize=(11.0, 4.6))
    ax.set_xlim(0, 11)
    ax.set_ylim(0, 4.6)
    ax.axis("off")

    box(ax, 1.35, 2.9, "host_facts()\nin_allocation?", color=GUARD)
    box(ax, 3.9, 2.9, "RECKON_ALLOW_\nNESTED_LAUNCH set?", color=QUESTION)
    box(ax, 6.6, 2.9, "srun with --overlap\nand --jobid / -n 1?", color=QUESTION)
    box(ax, 9.2, 3.9, "exec the real binary\nargv unchanged", color=EXEC)
    box(ax, 9.2, 1.5, "refuse, exit 97\nname job, node, override", color=REFUSE)

    arrow(ax, (1.35, 2.55), (1.35, 1.0))
    box(ax, 1.35, 0.75, "outside the job", color="#f4f4f4", edge=EDGE)
    arrow(ax, (1.35, 0.5), (8.6, 0.5))
    arrow(ax, (8.6, 0.5), (9.2, 1.25), label="exec unchanged", dx=0.75)

    arrow(ax, (1.85, 2.9), (3.45, 2.9), "inside", dx=0.02)
    arrow(ax, (4.35, 2.9), (6.15, 2.9), "no", dx=0.02)
    arrow(ax, (7.05, 2.9), (8.85, 2.9), "no", dx=0.02)
    arrow(ax, (3.9, 3.25), (8.9, 3.9), "yes", dx=0.0)
    arrow(ax, (6.6, 3.25), (8.9, 3.85), "yes", dx=0.0)
    arrow(ax, (9.2, 2.6), (9.2, 1.85))

    ax.text(
        5.5,
        4.45,
        "the shim never launches anything itself; it only decides and forwards",
        ha="center",
        va="center",
        fontsize=9,
        color=TEXT,
        style="italic",
    )
    ax.text(
        1.35,
        3.35,
        "the declared mutation removes this gate",
        ha="center",
        va="center",
        fontsize=8,
        color=GUARD,
    )

    fig.tight_layout()
    fig.savefig(OUT, dpi=150)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
