"""Draw when a concluded turn's row appears, and which reading a dead pointer gets.

Top: a worker's turn ends with a result record and the process exits; the
delivered reader reports it on the next snapshot, while the base revision holds
the row behind the stall window and then names the quiet time instead of the
end. Bottom: the arms the reader tries, in order, and the reading and verb each
one composes, with the declared mutation shown removing the middle arm so a
result tail falls through to the death reading.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

WINDOW = 900.0
ROW_AT = 2.0
INK = "#2c3e50"
ENDED = "#1f6fb2"
STALL = "#7f8c8d"
PRECEDENCE = "#8e44ad"
DEATH = "#c0392b"
MUTANT = "#b9770e"
OUT = Path(__file__).with_suffix(".png")


def _timeline(axis) -> None:
    axis.set_xlim(-120, 1330)
    axis.set_ylim(-0.9, 2.0)
    axis.axis("off")
    axis.set_title("when the row appears", fontsize=10, color=INK, loc="left")

    axis.axvline(0, color=INK, linewidth=1.3)
    axis.text(
        14,
        1.86,
        "the turn ends: the stream's last record is\n"
        "result, and the manifest still reads in-progress",
        fontsize=9,
        color=INK,
        va="top",
    )

    axis.axvline(WINDOW, color=INK, linewidth=1.1, linestyle="--")
    axis.text(
        WINDOW + 16,
        -0.82,
        "the stall window (900 s)",
        fontsize=9,
        color=INK,
    )

    axis.annotate(
        "",
        xy=(ROW_AT + 10, 1.05),
        xytext=(0, 1.05),
        arrowprops=dict(arrowstyle="-|>", color=ENDED, linewidth=2.4),
    )
    axis.plot([ROW_AT + 10], [1.05], marker="o", color=ENDED, markersize=7)
    axis.text(
        ROW_AT + 24,
        1.05,
        "the delivered reader: the row, on the very next snapshot\n"
        "“turn ended: … the run is resumable rather than stalled”",
        fontsize=9,
        color=ENDED,
        va="center",
    )

    axis.text(
        14,
        0.42,
        "the base revision: nothing is said, and the process reading alone\n"
        "waits out the window",
        fontsize=9,
        color=STALL,
        va="center",
    )
    axis.plot([901], [0.0], marker="o", color=STALL, markersize=7)
    axis.text(
        925,
        0.0,
        "at 901 s: “stream quiet for 901s, process gone” —\n"
        "the quiet time named, not the end",
        fontsize=9,
        color=STALL,
        va="center",
    )
    axis.annotate(
        "",
        xy=(WINDOW, 0.0),
        xytext=(0, 0.0),
        arrowprops=dict(arrowstyle="-|>", color=STALL, linewidth=1.6, linestyle=":"),
    )


def _arm(axis, y, text, colour) -> None:
    axis.text(
        0.10,
        y,
        text,
        fontsize=9.5,
        color=INK,
        va="center",
        bbox=dict(
            boxstyle="round,pad=0.5",
            edgecolor=colour,
            facecolor="white",
            linewidth=1.6,
        ),
    )
    axis.annotate(
        "",
        xy=(0.085, y),
        xytext=(0.030, y),
        arrowprops=dict(arrowstyle="-|>", color=colour, linewidth=1.6),
    )


def _readings(axis) -> None:
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.axis("off")
    axis.set_title(
        "which reading a dead pointer gets, and in what order the arms are tried",
        fontsize=10,
        color=INK,
        loc="left",
    )

    axis.text(
        0.5,
        0.955,
        "a pointer whose process is gone and whose manifest is not terminal",
        fontsize=10,
        color=INK,
        ha="center",
        va="center",
        bbox=dict(boxstyle="round,pad=0.45", edgecolor=INK, facecolor="#f4f6f7"),
    )
    axis.annotate(
        "",
        xy=(0.030, 0.20),
        xytext=(0.030, 0.90),
        arrowprops=dict(arrowstyle="-", color=INK, linewidth=1.4),
    )

    _arm(
        axis,
        0.72,
        "the manifest's word is outside the vocabulary  →  "
        "unreadable · repair   (§5 outranks the end)",
        PRECEDENCE,
    )
    _arm(
        axis,
        0.46,
        "the stream's last record is result  →  "
        "turn ended · resume   (§8, the arm this node adds)",
        ENDED,
    )
    _arm(
        axis,
        0.20,
        "any other last record  →  "
        "interrupted · redispatch   (§2, the death)",
        DEATH,
    )

    axis.text(
        0.10,
        0.045,
        "the declared mutation deletes the turn-ended arm:\n"
        "a result tail then falls to the death reading",
        fontsize=8.5,
        color=MUTANT,
        va="center",
        bbox=dict(
            boxstyle="round,pad=0.45",
            edgecolor=MUTANT,
            facecolor="white",
            linewidth=1.4,
            linestyle="--",
        ),
    )
    axis.annotate(
        "",
        xy=(0.10, 0.20),
        xytext=(0.10, 0.085),
        arrowprops=dict(arrowstyle="-|>", color=MUTANT, linewidth=1.4, linestyle="--"),
    )


def main() -> None:
    figure = plt.figure(figsize=(10.5, 6.4), layout="constrained")
    grid = figure.add_gridspec(2, 1, height_ratios=[0.8, 1.2])
    _timeline(figure.add_subplot(grid[0]))
    _readings(figure.add_subplot(grid[1]))
    figure.savefig(OUT, dpi=200)
    print(OUT)


if __name__ == "__main__":
    main()