"""Read volume and CPU of the fleet's monitor processes against the cutover bound.

The two windows are the captures committed beside this script, taken on the
fleet node with the RCA's section 2 instruments: per-process
``/proc/<pid>/io`` rchar deltas and utime+stime deltas from
``/proc/<pid>/stat``. Processes are selected by adjacent argv tokens (crew
station, crew watch, crew follow). The station is the subject; the producers
and the followers are the old cost it was measured against.

Every number drawn is read from the captures, so the figure cannot state a
figure the logs do not carry. Read volume is drawn on a log axis because the
bound and the measurement are three decades apart; CPU is linear. The save
refuses a figure whose text runs off the canvas or overlaps another label.
"""

from __future__ import annotations

import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

BOUND_MB_PER_MIN = 20.0
BOUND_CPU_PERCENT = 5.0

CAPTURES = (
    ("window 1", Path(__file__).resolve().parent / "captures" / "window-1.txt"),
    ("window 2", Path(__file__).resolve().parent / "captures" / "window-2.txt"),
)
KIND_ORDER = ("station", "watch", "follow")
KIND_LABELS = {
    "station": "station",
    "watch": "producers (crew watch)",
    "follow": "followers (crew follow)",
}

KIND = re.compile(
    r"^BY_KIND (?P<kind>\w+) count=\d+ rchar_MB_per_min=(?P<rchar>[\d.]+)"
    r" read_bytes_MB=[\d.]+ cpu_pct=(?P<cpu>[\d.]+)"
)

SUBJECT = "#2f6f8f"
SUBJECT_LIGHT = "#7fa9bf"
NEUTRAL = "#929292"

WIDTH_IN = 14.0
HEIGHT_IN = 7.2
DPI = 100
LABEL_PT = 16
TICK_PT = 14


def read_capture(path: Path) -> dict[str, float]:
    """Per-kind read volume and CPU of one capture, in its own units."""
    figures: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := KIND.match(line):
            figures[f"{match['kind']}_mb"] = float(match["rchar"])
            figures[f"{match['kind']}_cpu"] = float(match["cpu"])
    missing = [kind for kind in KIND_ORDER if f"{kind}_mb" not in figures]
    if missing:
        raise SystemExit(f"{path} carries no figure for {missing}")
    return figures


def _panel(axis, first, second, bound, unit, log, limit) -> None:
    labels = [KIND_LABELS[kind] for kind in KIND_ORDER]
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
    marks = [
        (position - height / 2, value, SUBJECT) for position, value in zip(positions, first, strict=True)
    ] + [
        (position + height / 2, value, SUBJECT_LIGHT) for position, value in zip(positions, second, strict=True)
    ]
    for position, value, colour in marks:
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
    windows = [(label, read_capture(path)) for label, path in CAPTURES]
    plt.style.use("data-ink")
    figure, (left, right) = plt.subplots(1, 2, figsize=(WIDTH_IN, HEIGHT_IN), dpi=DPI)
    _panel(
        left,
        [windows[0][1][f"{kind}_mb"] for kind in KIND_ORDER],
        [windows[1][1][f"{kind}_mb"] for kind in KIND_ORDER],
        BOUND_MB_PER_MIN,
        "read volume [MB/min, log scale]",
        True,
        (1.0, 400000.0),
    )
    _panel(
        right,
        [windows[0][1][f"{kind}_cpu"] for kind in KIND_ORDER],
        [windows[1][1][f"{kind}_cpu"] for kind in KIND_ORDER],
        BOUND_CPU_PERCENT,
        "CPU [% of one core]",
        False,
        (0.0, 185.0),
    )
    right.tick_params(axis="y", labelleft=False, left=False)
    figure.legend(
        handles=[
            Patch(color=SUBJECT, label=windows[0][0]),
            Patch(color=SUBJECT_LIGHT, label=windows[1][0]),
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