"""Rows the recorded hour holds, and the session each row is addressed to.

The hour is the event log of one fleet node between 17:00 and 18:00 on
2026-10-23. A row naming a run is addressed to the session that owns it; a row
naming no run is a budget lift granted or ended and is addressed to every pane,
which is why two sessions carry seven rows each from the lift traffic alone.

Every row here was carried by the station, so the bar is both the recorded
count and the carried count.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

CARRIED: tuple[tuple[str, int], ...] = (
    ("a337e0e6", 27),
    ("efd0ee8c", 22),
    ("d768318d", 15),
    ("6f111d4a", 11),
    ("530468fb", 9),
    ("4f554c2d", 7),
    ("3338dfd2", 7),
    ("404513fc", 2),
)

SUBJECT = "#2f6f8f"
WIDTH_IN = 14.0
HEIGHT_IN = 7.2
DPI = 100
LABEL_PT = 16
TICK_PT = 14


def main() -> None:
    plt.style.use("data-ink")
    figure, axis = plt.subplots(figsize=(WIDTH_IN, HEIGHT_IN), dpi=DPI)
    labels = [row[0] for row in CARRIED]
    values = [row[1] for row in CARRIED]
    positions = np.arange(len(CARRIED))
    axis.barh(positions, values, height=0.58, color=SUBJECT, linewidth=0, zorder=2)
    axis.set_yticks(positions)
    axis.set_yticklabels(labels, fontsize=TICK_PT)
    axis.invert_yaxis()
    axis.set_xlim(0, 31)
    axis.set_ylim(len(CARRIED) - 0.42, -0.9)
    axis.set_xlabel("rows carried [count]", fontsize=LABEL_PT)
    axis.tick_params(axis="x", labelsize=TICK_PT)
    axis.spines[["top", "right"]].set_visible(False)
    for position, value in zip(positions, values, strict=True):
        axis.annotate(
            f"{value}",
            xy=(value, position),
            xytext=(6, 0),
            textcoords="offset points",
            color=SUBJECT,
            fontsize=TICK_PT,
            ha="left",
            va="center",
        )
    for extension in (".svg", ".png"):
        figure.savefig(
            "docs/figures/the-monitor-reads-each-fact-once/"
            "the-station-carries-every-row-at-cutover/rows-by-session-of-addressing"
            f"{extension}",
            bbox_inches="tight",
            facecolor="white",
        )
    plt.close(figure)


if __name__ == "__main__":
    main()
