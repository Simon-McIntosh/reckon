"""Render the host-facts decision flow.

Left panel: how ``in_allocation`` is decided, with the branch the declared
mutation replaces marked, because the cgroup line is the whole difference
between the two answers. Right panel: how ``tmp_is_node_local`` is decided,
where the tree's mount predicate is widened by the mount's own filesystem
type.

The figure shows the order the checks are taken in, which is the part a
description has to spell out and a reader cannot see from the values alone.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = Path(__file__).resolve().parent / "host-facts-decision-flow.png"

BOX = {"boxstyle": "round,pad=0.4", "linewidth": 1.2}
EDGE = "#444444"
GUARD = "#1b6ca8"
WIDEN = "#2e7d32"
PLAIN = "#eeeeee"


def box(ax, x, y, text, colour=PLAIN, width=3.2, height=0.62) -> None:
    ax.text(
        x,
        y,
        text,
        ha="center",
        va="center",
        fontsize=8.5,
        bbox={"facecolor": colour, "edgecolor": EDGE, **BOX},
        wrap=True,
    )


def arrow(ax, start, end, label="", offset=0.0) -> None:
    ax.annotate(
        "",
        xy=end,
        xytext=start,
        arrowprops={"arrowstyle": "-|>", "color": EDGE, "linewidth": 1.6},
    )
    if label:
        ax.text(
            (start[0] + end[0]) / 2 + offset,
            (start[1] + end[1]) / 2,
            label,
            ha="center",
            va="center",
            fontsize=8,
            color=EDGE,
        )


def placement_panel(ax) -> None:
    ax.set_title("in_allocation", fontsize=11, fontweight="bold")
    box(ax, 0, 4.6, "read this process's own cgroup line\n(/proc/self/cgroup)")
    box(ax, 0, 3.5, "SLURM_JOB_ID set?", colour=PLAIN)
    box(ax, -1.7, 2.2, "in_allocation\nfalse\nno-slurm-job-id…", colour="#f6d5d5")
    box(ax, 1.7, 2.2, "does the cgroup path name\njob_<id>?", colour=GUARD)
    box(ax, 1.7, 0.9, "in_allocation\ntrue", colour="#cfe6cf")
    box(ax, -1.7, 0.9, "in_allocation\nfalse\nenvironment-inherited…", colour="#f6d5d5")
    arrow(ax, (0, 4.2), (0, 3.85))
    arrow(ax, (-0.9, 3.2), (-1.7, 2.6), "no")
    arrow(ax, (0.9, 3.2), (1.7, 2.6), "yes")
    arrow(ax, (1.7, 1.85), (1.7, 1.25), "yes")
    arrow(ax, (0.6, 2.2), (-1.5, 1.25), "no")
    ax.text(
        0,
        5.95,
        "blue: the check the declared mutation replaces",
        ha="center",
        va="center",
        fontsize=7.8,
        color=GUARD,
    )
    ax.text(
        0,
        5.25,
        "the cgroup is the authority: an inherited\nSLURM_JOB_ID cannot decide this",
        ha="center",
        va="center",
        fontsize=8,
        color="#333333",
    )


def storage_panel(ax) -> None:
    ax.set_title("tmp_is_node_local", fontsize=11, fontweight="bold")
    box(ax, 0, 4.6, "read the mount table\n(/proc/self/mountinfo)")
    box(ax, 0, 3.5, "longest mount point that\ncovers the path wins")
    box(
        ax, -1.8, 2.2, "dispatch\n_path_is_tmpfs or\n_is_node_local_path?", colour=GUARD
    )
    box(ax, 1.8, 2.2, "mount type shared?\n(gpfs/nfs/lustre/…)", colour=WIDEN)
    box(ax, -1.1, 0.9, "tmp_is_node_local\ntrue", colour="#cfe6cf")
    box(ax, 1.8, 0.9, "tmp_is_node_local\nfalse", colour="#f6d5d5")
    arrow(ax, (0, 4.2), (0, 3.85))
    arrow(ax, (-0.7, 3.2), (-1.8, 2.6))
    arrow(ax, (0.7, 3.2), (1.8, 2.6))
    arrow(ax, (-1.8, 1.85), (-1.1, 1.28), "yes")
    arrow(ax, (1.8, 1.85), (-0.5, 1.28), "no")
    arrow(ax, (2.4, 1.85), (2.0, 1.28), "yes")
    ax.text(
        0,
        5.25,
        "the mount's own type widens the tree's predicate,\nso a block-backed local scratch reads node-local",
        ha="center",
        va="center",
        fontsize=8,
        color="#333333",
    )


def main() -> int:
    fig, (left, right) = plt.subplots(1, 2, figsize=(11, 6))
    for ax in (left, right):
        ax.set_xlim(-3.2, 3.2)
        ax.set_ylim(0.4, 6.3)
        ax.axis("off")
    placement_panel(left)
    storage_panel(right)
    fig.suptitle("host_facts(): the order the answers are taken in", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(OUT, dpi=140)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
