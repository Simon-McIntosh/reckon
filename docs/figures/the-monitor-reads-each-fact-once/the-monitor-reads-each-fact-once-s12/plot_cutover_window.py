"""Read volume and CPU of the fleet's monitor processes against the cutover bound.

Two 60 s windows on the fleet node, taken with the RCA's section 2 instruments:
per-process ``/proc/<pid>/io`` rchar deltas and utime+stime deltas from
``/proc/<pid>/stat``. Processes are selected by adjacent argv tokens (crew
station, crew watch, crew follow). The station is the subject; the producers
and the followers are the old cost it was to replace.

Read volume is drawn on a log axis because the bound and the measurement are
three decades apart; CPU is linear. The save refuses a figure whose text runs
off the canvas or overlaps another label.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

BOUND_MB_PER_MIN = 20.0
BOUND_CPU_PERCENT = 5.0

# (kind, window-1 MB/min, window-1 CPU %, window-2 MB/min, window-2 CPU %)
KINDS: tuple[tuple[str, float, float, float, float], ...] = (
    ("station", 11644.6, 90.25, 13621.5, 104.58),
    ("producers (crew watch)", 10702.2, 142.76, 6468.9, 89.92),
    ("followers (crew follow)", 299.3, 28.98, 799.0, 13.85),
)

SUBJECT = "#2f6f8f"
SUBJECT_LIGHT = "#7fa9bf"
NEUTRAL = "#929292"

WIDTH_IN = 14.0
HEIGHT_IN = 7.2
DPI = 100
LABEL_PT = 16
TICK_PT = 14


def _panel(axis, first, second, bound, unit, log, limit) -> None:
    labels = [kind[0] for kind in KINDS]
    positions = np.arange(len(labels), dtype=float)
    height = 0.34
    axis.barh(positions - height / 2, first, height=height, color=SUBJECT, linewidth=0, zorder=2)
    axis.barh(positions + height / 2, second, height=height, color=SUBJECT_LIGHT, linewidth=0, zorder=2)
    axis.set_yticks(positions)
    axis.set_yticklabels(labels, fontsize=TICK_PT)
    if log:
        axis.set_xscale("log")
    axis.set_xlim(limit)
    axis.invert_yaxis()
    axis.set_xlabel(unit, fontsize=LABEL_PT)
    axis.tick_params(axis="x", labelsize=TICK_PT)
    axis.axvline(bound, color=NEUTRAL, linewidth=1.2, linestyle=(0, (1, 2)), zorder=3)
    axis.annotate(
        f"bound {bound:g}",
        xy=(bound, -0.5),
        xytext=(4, 0),
        textcoords="offset points",
        color=NEUTRAL,
        fontsize=TICK_PT,
        ha="left",
        va="center",
    )
    for position, value, colour in (
        *[(p - height / 2, v, SUBJECT) for p, v in zip(positions, first, strict=True)],
        *[(p + height / 2, v, SUBJECT_LIGHT) for p, v in zip(positions, second, strict=True)],
    ):
        axis.annotate(
            f"{value:,.0f} MB/min" if log else f"{value:.1f} %",
            xy=(value, position),
            xytext=(5, 0),
            textcoords="offset points",
            color=colour,
            fontsize=TICK_PT,
            ha="left",
            va="center",
        )
    axis.set_ylim(len(labels) - 0.4, -0.55)
    axis.spines[["top", "right"]].set_visible(False)


def _visible_ticks(axis) -> list:
    low, high = sorted(axis.get_xlim())
    x_ticks = [
        label
        for value, label in zip(axis.get_xticks(), axis.get_xticklabels(), strict=False)
        if low <= value <= high
    ]
    low, high = sorted(axis.get_ylim())
    y_ticks = [
        label
        for value, label in zip(axis.get_yticks(), axis.get_yticklabels(), strict=False)
        if low <= value <= high
    ]
    return x_ticks + y_ticks


def _check_labels(figure) -> None:
    """Refuse text that leaves the canvas or overlaps another label."""
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    # The saved figure uses the tight bounding box, which is what a reader sees.
    canvas = figure.get_tightbbox(renderer).transformed(figure.dpi_scale_trans)
    boxes = []
    for axis in figure.axes:
        for text in (axis.xaxis.label, axis.yaxis.label, *_visible_ticks(axis), *axis.texts):
            if not text.get_text().replace(" ", ""):
                continue
            box = text.get_window_extent(renderer=renderer)
            if box.width == 0 or box.height == 0:
                continue
            boxes.append((text.get_text(), box))
    problems = []
    for name, box in boxes:
        if (
            box.x0 < canvas.x0 - 1
            or box.x1 > canvas.x1 + 1
            or box.y0 < canvas.y0 - 1
            or box.y1 > canvas.y1 + 1
        ):
            problems.append(f"offscreen: {name!r}")
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if boxes[i][1].overlaps(boxes[j][1]):
                problems.append(f"overlap: {boxes[i][0]!r} with {boxes[j][0]!r}")
    if problems:
        raise SystemExit("figure check failed:\n" + "\n".join(problems))


def main() -> None:
    plt.style.use("data-ink")
    figure, (left, right) = plt.subplots(1, 2, figsize=(WIDTH_IN, HEIGHT_IN), dpi=DPI)
    _panel(
        left,
        [kind[1] for kind in KINDS],
        [kind[3] for kind in KINDS],
        BOUND_MB_PER_MIN,
        "read volume [MB/min, log scale]",
        True,
        (1.0, 400000.0),
    )
    _panel(
        right,
        [kind[2] for kind in KINDS],
        [kind[4] for kind in KINDS],
        BOUND_CPU_PERCENT,
        "CPU [% of one core]",
        False,
        (0.0, 185.0),
    )
    right.tick_params(axis="y", labelleft=False, left=False)
    figure.legend(
        handles=[
            Patch(color=SUBJECT, label="window 1"),
            Patch(color=SUBJECT_LIGHT, label="window 2"),
        ],
        loc="upper center",
        ncol=2,
        frameon=False,
        fontsize=TICK_PT,
        bbox_to_anchor=(0.5, 1.02),
    )
    _check_labels(figure)
    figure.savefig(
        "docs/figures/the-monitor-reads-each-fact-once/the-monitor-reads-each-fact-once-s12/cutover-window-against-the-bound.svg",
        bbox_inches="tight",
        facecolor="white",
    )
    plt.close(figure)


if __name__ == "__main__":
    main()