"""Draw where the per-plan rows enter the sprint summary pipeline.

Two panels: which report fields each summary reads, and how one pending row is
projected. The point of the figure is that the summary still answers with
counts and now also carries the plan rows, and for the MCP path it reads
them from the plan file when the inventory row is slim.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrow, FancyBboxPatch

BASE = "#f5f6f8"
EDGE = "#3b4252"
ROW = "#dbe7f3"
NEW = "#cfe8d5"
TEXT = "#1f2430"


def main() -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(12.5, 5.0), dpi=150)
    for ax in (left, right):
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")

    left.set_title("Sprint summary: fields the view reads", fontsize=11.5, color=TEXT)

    def box(ax, x, y, w, h, label, *, face=BASE, size=9.5, weight="normal"):
        ax.add_patch(
            FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle="round,pad=0.012,rounding_size=0.02",
                linewidth=1.1,
                edgecolor=EDGE,
                facecolor=face,
            )
        )
        ax.text(
            x + w / 2,
            y + h / 2,
            label,
            ha="center",
            va="center",
            fontsize=size,
            color=TEXT,
            weight=weight,
        )

    def arrow(ax, y0, y1, x=0.5):
        ax.add_patch(
            FancyArrow(
                x,
                y0,
                x,
                y1,
                width=0.006,
                head_width=0.045,
                head_length=0.022,
                length_includes_head=True,
                color=EDGE,
            )
        )

    box(left, 0.06, 0.86, 0.88, 0.11, "roadmap report (build_roadmap)", weight="bold")
    arrow(left, 0.855, 0.79)
    box(
        left,
        0.06,
        0.63,
        0.40,
        0.15,
        "completion / counts\nready · blocked · deferred",
        face=BASE,
    )
    box(
        left,
        0.54,
        0.63,
        0.40,
        0.15,
        "pending_work rows\n(one per pending plan)",
        face=ROW,
    )
    arrow(left, 0.625, 0.56, x=0.26)
    arrow(left, 0.625, 0.56, x=0.74)
    box(
        left,
        0.06,
        0.355,
        0.40,
        0.20,
        "summary, without sprint:\nkeys unchanged, byte for byte",
        face=BASE,
    )
    box(
        left,
        0.54,
        0.355,
        0.40,
        0.20,
        "summary, sprint=X:\n+ pending_plans, one compact\nrow per pending plan",
        face=NEW,
    )
    left.text(
        0.5,
        0.22,
        "the row list is what a coordinator\nhad to parse sprint HTML for",
        ha="center",
        va="center",
        fontsize=8.5,
        color="#5a6270",
        style="italic")
    left.text(
        0.74,
        0.10,
        "shipped plan is terminal → absent",
        ha="center",
        va="center",
        fontsize=8.5,
        color="#5a6270")

    right.set_title("One compact pending row", fontsize=11.5, color=TEXT)
    keys = [
        ("slug", "plan identity"),
        ("status", "lifecycle word"),
        ("impl", "implementation fraction"),
        ("ready", "can start now"),
        ("blocking", "ids that hold it"),
        ("implementable", "section ids still declared"),
    ]
    y = 0.80
    for key, note in keys:
        box(right, 0.06, y, 0.34, 0.105, key, face=ROW, size=9.0, weight="bold")
        right.text(0.44, y + 0.0525, note, ha="left", va="center", fontsize=9, color=TEXT)
        y -= 0.135
    box(
        right,
        0.06,
        0.075,
        0.88,
        0.13,
        "sections read from the plan file when the\ninventory row carries no classification",
        face=NEW,
        size=9.0,
    )

    fig.suptitle(
        "A sprint summary names its pending plans",
        fontsize=13,
        color=TEXT,
        y=0.975)
    fig.tight_layout(rect=(0, 0, 1, 0.945))
    fig.savefig("summary_pending_rows.png", dpi=150, facecolor="white")
    print("wrote summary_pending_rows.png")


if __name__ == "__main__":
    main()