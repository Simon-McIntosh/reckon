"""Draw the control admission's decision flow and the four replayed runs.

The left panel is the decision the gate makes: two facts read from the control
log, then the arm that decides the third. The right panel is each replayed run's
position — its control's failing id is red in the run's baseline arm by design
and green in its head arm, which is the whole reason the baseline comparison
refused it and the head comparison admits it.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).with_name("arm_decision.png")

RUNS = [
    ("lane-reading", "control id: payload carries lane counts", True, False),
    ("receipt-rollback", "control id: rolled-back row names re-promotion", True, False),
    ("directory-phase", "control id: assistant record reads working", True, False),
    ("orientation-stub", "control id: live stub reads its work", True, False),
]

EDGE_WIDTH = 1.1


def _box(ax, x, y, w, h, text, face, edge="#33415c", size=9) -> None:
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.02",
            linewidth=EDGE_WIDTH,
            edgecolor=edge,
            facecolor=face,
        )
    )
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size)


def _arrow(ax, xy_from, xy_to) -> None:
    ax.add_patch(
        FancyArrowPatch(
            xy_from,
            xy_to,
            arrowstyle="->",
            mutation_scale=12,
            linewidth=1.1,
            color="#33415c",
        )
    )


def flow(ax) -> None:
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    ax.set_title("The gate's decision", fontsize=11, loc="left", pad=16)

    _box(
        ax,
        0.4,
        8.6,
        9.2,
        1.1,
        "control log: the dots the run left behind\nexit record, failing test ids",
        "#e8eef7",
    )
    _box(ax, 0.4, 7.0, 4.3, 1.1, "EXIT record read?", "#e8eef7")
    _box(ax, 5.3, 7.0, 4.3, 1.1, "at least one failing id?", "#e8eef7")
    _box(
        ax,
        0.4,
        5.2,
        4.3,
        1.1,
        "no EXIT= line or EXIT=0\n-> refused",
        "#fbe3e3",
        edge="#a4423a",
    )
    _box(ax, 5.3, 5.2, 4.3, 1.1, "none named\n-> refused", "#fbe3e3", edge="#a4423a")
    _box(ax, 1.6, 3.6, 6.8, 1.1, "is a readable head arm recorded?", "#e8eef7")
    _box(
        ax,
        0.4,
        1.7,
        4.3,
        1.4,
        "yes: compare against after_suite\nthe id must be green in the head arm",
        "#dff0e2",
        edge="#2f6b3a",
    )
    _box(
        ax,
        5.3,
        1.7,
        4.3,
        1.4,
        "no: the baseline comparison decides\n(it must not be read as an empty arm)",
        "#fdf0d8",
        edge="#8a6a1f",
    )
    _box(
        ax,
        2.0,
        0.2,
        6.0,
        1.0,
        "no id the deciding arm passes -> refused\notherwise -> matched",
        "#e8eef7",
    )

    _arrow(ax, (5.0, 8.6), (5.0, 8.1))
    _arrow(ax, (2.55, 7.0), (2.55, 6.3))
    _arrow(ax, (7.45, 7.0), (7.45, 6.3))
    _arrow(ax, (5.0, 5.2), (5.0, 4.7))
    _arrow(ax, (5.0, 3.6), (5.0, 3.1))


def runs(ax) -> None:
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 6)
    ax.axis("off")
    ax.set_title("The four replayed runs", fontsize=11, loc="left", pad=16)
    y = 5.1
    ax.text(3.2, y, "baseline arm", ha="center", fontsize=9, weight="bold")
    ax.text(5.9, y, "head arm", ha="center", fontsize=9, weight="bold")
    ax.text(8.6, y, "admission", ha="center", fontsize=9, weight="bold")
    for name, label, base_red, head_red in RUNS:
        y -= 1.05
        ax.text(0.1, y, name, fontsize=9, va="center")
        _box(
            ax,
            2.0,
            y - 0.32,
            2.4,
            0.64,
            "red" if base_red else "green",
            "#fbe3e3" if base_red else "#dff0e2",
            edge="#a4423a" if base_red else "#2f6b3a",
            size=8,
        )
        _box(
            ax,
            4.7,
            y - 0.32,
            2.4,
            0.64,
            "red" if head_red else "green",
            "#fbe3e3" if head_red else "#dff0e2",
            edge="#a4423a" if head_red else "#2f6b3a",
            size=8,
        )
        _box(
            ax,
            7.4,
            y - 0.32,
            2.4,
            0.64,
            "matched (was waived)",
            "#dff0e2",
            edge="#2f6b3a",
            size=8,
        )
        ax.text(0.1, y - 0.42, label, fontsize=6.5, va="center", color="#555")
    ax.text(
        0.1,
        0.25,
        "the baseline arm is the suite over the unfixed tree, so every case the node wrote first is red there by design",
        fontsize=7,
        color="#555",
    )


fig, (left, right) = plt.subplots(
    1, 2, figsize=(13.5, 6.2), gridspec_kw={"width_ratios": [1, 1.05]}
)
flow(left)
runs(right)
fig.tight_layout()
fig.savefig(OUT, dpi=140)
print(OUT)
