"""Render the reader-side split of a live pointer for the evidence record.

One live pointer, one classification, and the two rows it can become, so a
reader sees that a run still in flight and a run interrupted are told apart by
the surfaces rather than counted together.

Run from the repository root:

    python docs/figures/worker-death-is-recorded/interrupted_run_readers.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

HERE = Path(__file__).resolve().parent
BLUE = "#3c5a99"
AMBER = "#a86b1f"
INK = "#12203a"


def box(ax, x, y, w, h, title, lines, colour=BLUE, face="#f7f9fc"):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.012,rounding_size=0.02",
            linewidth=1.8,
            edgecolor=colour,
            facecolor=face,
        )
    )
    ax.text(x + 0.016, y + h - 0.05, title, fontsize=9.5, weight="bold", color=INK)
    for index, line in enumerate(lines):
        ax.text(
            x + 0.016,
            y + h - 0.096 - index * 0.042,
            line,
            fontsize=8.0,
            family="monospace",
            color="#33475f",
        )


def arrow(ax, start, end):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=12,
            linewidth=1.6,
            color="#8698b3",
        )
    )


def main() -> None:
    fig, ax = plt.subplots(figsize=(11.0, 4.8))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(
        0.02,
        0.95,
        "One fleet read, one judgement, two rows",
        fontsize=13.5,
        weight="bold",
        color=INK,
    )

    box(
        ax,
        0.02,
        0.58,  # the shared input sits at the left edge
        0.24,
        0.24,
        "live pointers",
        [
            "list_live(project)",
            "",
            "run-live   process alive",
            "run-killed wait_status.signal=15",
        ],
    )
    arrow(ax, (0.26, 0.70), (0.33, 0.70))
    box(
        ax,
        0.33,
        0.58,
        0.28,
        0.24,
        "classify_pointer (one judgement)",
        [
            "the authority `recover` also uses",
            "",
            "run-live   -> running",
            "run-killed -> interrupted",
        ],
    )

    arrow(ax, (0.61, 0.76), (0.68, 0.80))
    arrow(ax, (0.61, 0.64), (0.68, 0.60))

    box(
        ax,
        0.68,
        0.70,
        0.30,
        0.22,
        "interrupted",
        [
            "in_flight_by_plan excludes it",
            "interrupted_by_plan groups it",
            "row carries reason + next_action",
        ],
        colour=AMBER,
        face="#fdf3e6",
    )
    box(
        ax,
        0.68,
        0.36,
        0.30,
        0.22,
        "in flight",
        [
            "in_flight_by_plan groups it",
            "counted as work in progress",
            "roadmap pending row + sprint row",
        ],
        colour=BLUE,
        face="#f4f7fc",
    )

    box(
        ax,
        0.02,
        0.06,
        0.94,
        0.20,
        "what each surface reports",
        [
            "sprint_state_view: 'in_flight' list (runs waited on) | 'interrupted' list, present even when empty",
            "build_roadmap: pending row and sprint row each carry in_flight and interrupted apart",
            "an interrupted run is a decision (resume where a session survived, else redispatch) — never patience",
        ],
    )

    fig.tight_layout()
    for suffix in ("svg", "png"):
        fig.savefig(HERE / f"interrupted-run-readers.{suffix}", dpi=160, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    main()