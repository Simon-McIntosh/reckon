"""Render the reap-branch diagram for the worker-death evidence record.

One decision the reaper makes, the emptiness of the stream, and what each
branch writes to the run, so a reader sees which branch keeps the death
without reading the source.

Run from the repository root:

    python docs/figures/worker-death-is-recorded/reap_wait_status.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

HERE = Path(__file__).resolve().parent
BLUE = "#3c5a99"
GREEN = "#2f7d4f"
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
    ax.text(x + 0.016, y + h - 0.048, title, fontsize=9.5, weight="bold", color=INK)
    for index, line in enumerate(lines):
        ax.text(
            x + 0.016,
            y + h - 0.092 - index * 0.04,
            line,
            fontsize=8.2,
            family="monospace",
            color="#33475e",
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
    fig, ax = plt.subplots(figsize=(10.2, 4.4))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(
        0.02,
        0.95,
        "A reaped worker's wait status reaching its run record",
        fontsize=13,
        weight="bold",
        color=INK,
    )

    box(
        ax,
        0.02,
        0.62,
        0.22,
        0.2,
        "reaper waits on its child",
        ["waitstatus_to_exitcode(status)"],
    )
    arrow(ax, (0.24, 0.72), (0.31, 0.72))

    box(ax, 0.31, 0.62, 0.2, 0.2, "stream empty?", ["no model was reached"])

    arrow(ax, (0.51, 0.72), (0.60, 0.72))
    ax.text(0.53, 0.745, "no", fontsize=8.5, color="#5a6b80")
    box(
        ax,
        0.60,
        0.62,
        0.38,
        0.2,
        "record the wait status",
        ["phase untouched: a turn ran, so it is no failure"],
        colour=GREEN,
        face="#eaf6ee",
    )

    arrow(ax, (0.41, 0.62), (0.41, 0.50))
    ax.text(0.425, 0.55, "yes", fontsize=8.5, color="#5a6b80")

    box(
        ax,
        0.31,
        0.30,
        0.2,
        0.2,
        "launch failure",
        ["phase = launch-failed", "launch_failures appended"],
    )
    arrow(ax, (0.51, 0.40), (0.60, 0.40))
    box(
        ax,
        0.60,
        0.30,
        0.38,
        0.2,
        "the same three fields",
        ["exit_code, signal, signal_name", "on the failure record too"],
        colour=GREEN,
        face="#eaf6ee",
    )
    box(
        ax,
        0.60,
        0.04,
        0.38,
        0.18,
        "a placed launch whose payload ran",
        [
            "records nothing: that wait belongs",
            "to the scheduler client, not the worker",
        ],
        colour=AMBER,
        face="#fdf3e6",
    )
    box(
        ax,
        0.02,
        0.08,
        0.24,
        0.42,
        "what the record carries",
        [
            "exit_code: 0..255   exited",
            "",
            "signal: 15          signalled",
            "signal_name: SIGTERM",
        ],
    )

    fig.tight_layout()
    for suffix in ("svg", "png"):
        fig.savefig(HERE / f"reap-wait-status.{suffix}", dpi=160, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    main()
