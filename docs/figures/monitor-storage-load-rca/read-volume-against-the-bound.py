"""Bytes read and CPU spent by one node station, against the cutover bound.

Each arm is a 60 s window on clu-2068 with ``/proc/<pid>/io`` and
``/proc/<pid>/stat`` deltas, the instruments the RCA defines in section 2.
The station ran alone, so every figure is one process.

The bound the cutover must meet is 20 MB/min of read volume and 5 % of one
core for all crew monitor processes together.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

BOUND_MB_PER_MIN = 20.0
BOUND_CPU_PERCENT = 5.0

ARMS: tuple[tuple[str, float, float], ...] = (
    ("5 live runs", 126.273, 42.332),
    ("71 live runs", 1067.146, 91.714),
)

SUBJECT = "#2f6f8f"
NEUTRAL = "#929292"

WIDTH_IN = 14.0
HEIGHT_IN = 7.2
DPI = 100
LABEL_PT = 16
TICK_PT = 14
BAR_PT = 22


def _arm_axis(axis, values, bound, unit, limit) -> None:
    labels = [arm[0] for arm in ARMS]
    positions = np.arange(len(ARMS))
    axis.barh(positions, values, height=0.52, color=SUBJECT, linewidth=0, zorder=2)
    axis.set_yticks(positions)
    axis.set_yticklabels(labels, fontsize=TICK_PT)
    axis.invert_yaxis()
    axis.set_xlim(0, limit)
    axis.set_xlabel(unit, fontsize=LABEL_PT)
    axis.tick_params(axis="x", labelsize=TICK_PT)
    axis.axvline(bound, color=NEUTRAL, linewidth=1.2, linestyle=(0, (1, 2)), zorder=3)
    axis.annotate(
        f"bound {bound:g}",
        xy=(bound, -0.72),
        xytext=(5, 0),
        textcoords="offset points",
        color=NEUTRAL,
        fontsize=TICK_PT,
        ha="left",
        va="center",
    )
    for position, value in zip(positions, values, strict=True):
        axis.annotate(
            f"{value:,.0f}",
            xy=(value, position),
            xytext=(5, 0),
            textcoords="offset points",
            color=SUBJECT,
            fontsize=TICK_PT,
            ha="left",
            va="center",
        )
    axis.set_ylim(len(ARMS) - 0.42, -1.5)
    axis.spines[["top", "right"]].set_visible(False)


def main() -> None:
    plt.style.use("data-ink")
    figure, (left, right) = plt.subplots(
        1, 2, figsize=(WIDTH_IN, HEIGHT_IN), dpi=DPI, sharey=True
    )
    _arm_axis(
        left,
        [arm[1] for arm in ARMS],
        BOUND_MB_PER_MIN,
        "read volume [MB/min]",
        1560.0,
    )
    _arm_axis(
        right,
        [arm[2] for arm in ARMS],
        BOUND_CPU_PERCENT,
        "CPU [% of one core]",
        132.0,
    )
    right.tick_params(axis="y", labelleft=False, left=False)
    for extension in (".svg",):
        figure.savefig(
            f"docs/figures/monitor-storage-load-rca/read-volume-against-the-bound{extension}",
            bbox_inches="tight",
            facecolor="white",
        )
    plt.close(figure)


if __name__ == "__main__":
    main()
