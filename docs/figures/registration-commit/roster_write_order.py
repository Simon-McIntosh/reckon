"""Figure: the two roster writes a dispatch makes, and why both must commit.

A dispatch provisions its own roster member when it is given none. Before it
registers, it reaps idle session rows, so the two writes land in the same
checkout one after the other. A registration commits the row it writes, and
refuses when the roster carries content that is not its own — so a reap left
uncommitted is refused by the registration that follows it and the dispatch
provisions no member at all. The left panel draws that order; the right panel
shows the gate delta.

Regenerate with the project interpreter:

    <repo>/.venv/bin/python docs/figures/registration-commit/roster_write_order.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle

HERE = Path(__file__).resolve().parent

INK = "#1f2933"
ACCENT = "#1f6d5c"
WARN = "#a33a2c"


def _sequence(ax: plt.Axes) -> None:
    ax.set_axis_off()
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    columns = [
        (1.7, "dispatch"),
        (5.0, "roster write\n(ledger / routing)"),
        (8.3, "repository"),
    ]
    for x, label in columns:
        ax.add_patch(
            Rectangle(
                (x - 1.15, 9.05),
                2.3,
                0.62,
                fill=True,
                facecolor="#eef2f5",
                edgecolor=INK,
                linewidth=0.9,
            )
        )
        ax.text(x, 9.36, label, ha="center", va="center", fontsize=8.5, color=INK)
        ax.plot(
            [x, x],
            [
                0.6,
                9.05,
            ],
            color=INK,
            linewidth=0.8,
            linestyle=(0, (3, 3)),
        )

    def step(y: float, source: int, sink: int, label: str, *, commit: bool) -> None:
        x0 = columns[source][0]
        x1 = columns[sink][0]
        color = ACCENT if commit else WARN
        ax.add_patch(
            FancyArrowPatch(
                (x0, y),
                (x1, y),
                arrowstyle="-|>",
                mutation_scale=9,
                color=color,
                linewidth=1.3,
            )
        )
        ax.text(
            (x0 + x1) / 2,
            y + 0.22,
            label,
            ha="center",
            va="bottom",
            fontsize=7.8,
            color=color,
        )

    step(8.30, 0, 1, "reap idle session rows", commit=False)
    step(7.75, 1, 2, 'commit "chore(roster): retire <id>"', commit=True)
    step(6.70, 0, 1, "register the session member", commit=False)
    step(6.15, 1, 2, 'commit "chore(roster): register <id>"', commit=True)

    ax.add_patch(
        Rectangle(
            (0.45, 4.55),
            9.1,
            1.15,
            fill=True,
            facecolor="#fbf3f1",
            edgecolor=WARN,
            linewidth=0.9,
        )
    )
    ax.text(
        0.65,
        5.42,
        "Hazard if the reap were left uncommitted:",
        fontsize=8.2,
        color=WARN,
        va="top",
    )
    ax.text(
        0.65,
        5.05,
        "the registration refuses on the dirty roster and provisions no member",
        fontsize=8.2,
        color=WARN,
        va="top",
    )

    ax.text(
        0.15,
        9.9,
        "A. Order of the two writes",
        fontsize=10,
        color=INK,
        fontweight="bold",
        va="top",
    )


def _delta(ax: plt.Axes) -> None:
    labels = ["base 7dfae503", "head 6a0bb500"]
    mine = [7, 0]
    pre = [3, 3]
    x = [0, 1]
    ax.bar(x, mine, width=0.5, color=WARN, label="this node's falsifiers")
    ax.bar(
        x,
        pre,
        width=0.5,
        bottom=mine,
        color="#c9c9c9",
        label="pre-existing (test_crew_members)",
    )
    for xi, (m, p) in enumerate(zip(mine, pre, strict=True)):
        ax.text(xi, m + p + 0.25, str(m + p), ha="center", fontsize=9, color=INK)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylabel("failing tests", fontsize=9)
    ax.set_ylim(0, 12)
    ax.set_title("B. Four-file gate, before and after", fontsize=10, color=INK)
    ax.legend(fontsize=7.6, frameon=False, loc="upper right")
    ax.spines[["top", "right"]].set_visible(False)


def main() -> None:
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(11, 5.4), gridspec_kw={"width_ratios": [1.55, 1]}
    )
    _sequence(left)
    _delta(right)
    fig.tight_layout()
    fig.savefig(HERE / "roster_write_order.png", dpi=170, facecolor="white")
    print("wrote", HERE / "roster_write_order.png")


if __name__ == "__main__":
    main()
