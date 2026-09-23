"""Draw the figure: an armed lifetime against the wall it runs against.

Each row is one grant. The thin line is the wall clock of a real
``crew follow`` subprocess (the min and max of five runs). The pale bar is the
follower's own ``elapsed_seconds`` at its deadline, which is the part it
controls; the gap left of it is the cost of reaching an arm, and the gap right
of it is one poll pass. The host's 30 minute Monitor cap is marked on the same
log axis.

Every number here was measured on the login node on 2026-09-23; the pre-arm gap
is read off as wall minus elapsed rather than measured on its own.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

WALL = {"3 s grant": (5.53, 6.42), "1 s grant": (2.93, 4.39)}
ELAPSED = {"3 s grant": 3.104, "1 s grant": 1.293}
HOST_CAP = 30 * 60.0
POLL = 0.1


def main() -> None:
    fig, ax = plt.subplots(figsize=(9.5, 3.6))
    rows = list(WALL)
    for row, label in enumerate(rows):
        low, high = WALL[label]
        granted = float(label.split()[0])
        elapsed = ELAPSED[label]
        ax.plot([low, high], [row, row], color="#6b6b6b", lw=1.4)
        ax.plot([low, high], [row, row], "|", color="#6b6b6b")
        ax.barh(row, low - 0.1, left=0.1, height=0.34, facecolor="none", edgecolor="#b9b9b9")
        ax.barh(row, elapsed, left=low, height=0.34, color="#8fbcd4")
        ax.barh(row, POLL, left=low + elapsed, height=0.34, color="#d9a5b3")
        ax.plot([low + elapsed], [row], "v", color="#2f2f2f")
        ax.annotate(
            f"granted {granted:g} s",
            (low + elapsed / 2, row + 0.22),
            ha="center",
            fontsize=8,
        )
    ax.annotate(
        "outline: cost of reaching an arm, derived as wall minus elapsed",
        (0.12, -0.42),
        ha="left",
        fontsize=7.5,
        color="#7a7a7a",
    )
    ax.annotate(
        "pink: one poll pass",
        (0.12, -0.62),
        ha="left",
        fontsize=7.5,
        color="#b06a7d",
    )
    ax.annotate(
        "host ends Monitor at 30 min",
        (HOST_CAP, len(rows) - 0.45),
        xytext=(-6, 8),
        textcoords="offset points",
        ha="right",
        fontsize=8,
        color="#a33",
    )
    ax.set_xscale("log")
    ax.set_xlim(0.1, 4000)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(list(WALL))
    ax.set_xlabel("seconds from process start (log)")
    ax.set_ylim(-0.75, len(rows) - 0.1)
    ax.grid(axis="x", color="#e4e4e4", lw=0.6)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig("arming_wall_breakdown.png", dpi=150)


if __name__ == "__main__":
    main()