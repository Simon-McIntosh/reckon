"""Draw when a killed worker's row appears, with and without the death branch.

The kill falls between two watcher snapshots. One reading of a non-terminal
manifest is the worker's own word, held while its process lives; once the
process table says the process is gone that word is stale, and the death
branch reports the death on the very next snapshot. Without it the row waits
out the stall window before anything is said.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

WINDOW = 900.0
KILL_AT = 2.0
SNAPSHOT_AT = 2.4
BLOCKED_COLOUR = "#c0392b"
WORKING_COLOUR = "#2e86c1"
HELD_COLOUR = "#95a5a6"
INK = "#2c3e50"


def main() -> None:
    figure, (with_branch, without_branch) = plt.subplots(
        2,
        1,
        figsize=(9.0, 3.6),
        sharex=True,
        layout="constrained",
    )

    for axis, title in (
        (with_branch, "with the death branch"),
        (without_branch, "without it: the deferral holds the row"),
    ):
        axis.set_yticks([])
        for side in ("top", "right", "left"):
            axis.spines[side].set_visible(False)
        axis.spines["bottom"].set_color("#bdc3c7")
        axis.set_xlim(0, WINDOW)
        axis.set_title(title, loc="left", fontsize=10, color=INK)

    with_branch.axvspan(0, SNAPSHOT_AT, color=WORKING_COLOUR, alpha=0.85)
    with_branch.annotate(
        "baseline: working",
        xy=(0.5, 0.5),
        xytext=(18, 0),
        textcoords="offset points",
        va="center",
        fontsize=9,
        color="white",
    )
    with_branch.axvspan(SNAPSHOT_AT, WINDOW, color=BLOCKED_COLOUR, alpha=0.85)
    with_branch.annotate(
        "blocked: the process is gone, the turn did not end",
        xy=(SNAPSHOT_AT, 0.5),
        xytext=(18, 0),
        textcoords="offset points",
        va="center",
        fontsize=9,
        color="white",
    )

    without_branch.axvspan(0, WINDOW, color=HELD_COLOUR, alpha=0.85)
    without_branch.annotate(
        f"working, no row for {WINDOW - KILL_AT:.0f}s",
        xy=(0.5, 0.5),
        xytext=(18, 0),
        textcoords="offset points",
        va="center",
        fontsize=9,
        color="white",
    )

    with_branch.axvline(KILL_AT, color=INK, linewidth=1.4)
    with_branch.annotate(
        "worker killed (SIGKILL)",
        xy=(KILL_AT, 1.0),
        xytext=(6, -14),
        textcoords="offset points",
        fontsize=8.5,
        color=INK,
    )
    with_branch.annotate(
        "next snapshot",
        xy=(SNAPSHOT_AT, 0.0),
        xytext=(6, 10),
        textcoords="offset points",
        fontsize=8.5,
        color=INK,
    )
    without_branch.axvline(KILL_AT, color=INK, linewidth=1.4)
    without_branch.annotate(
        f"stall window ends: {WINDOW:.0f}s",
        xy=(WINDOW, 1.0),
        xytext=(-8, -14),
        textcoords="offset points",
        ha="right",
        fontsize=8.5,
        color=INK,
    )
    without_branch.set_xlabel("seconds after dispatch", fontsize=9, color=INK)

    out = Path(__file__).with_name("death-row-timeline.png")
    figure.savefig(out, dpi=160, facecolor="white")
    print(out)


if __name__ == "__main__":
    main()
