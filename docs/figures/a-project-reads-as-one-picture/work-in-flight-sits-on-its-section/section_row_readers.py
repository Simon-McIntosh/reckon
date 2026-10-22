"""Draw the recorded run state beside the two readers that disagree over it.

Left panel: the watcher events one interrupted run of the power_convertor mount
recorded between dispatch and the measurement, as a step line, with the two
answers a reader could take at the measurement instant marked on it — waiting
from the stored event, interrupted from the classifier asked the pointer now.

Right panel: why the two answers differ. Both call sites reach the same
classifier; the roadmap-side one asks a recorded event first, and that event was
written fifteen minutes before the measurement, so an earlier answer to the same
question outranks a fresh one.

The save refuses to write a figure whose labels leave the canvas, collide, or
sit on a plotted line.

Run: python docs/figures/a-project-reads-as-one-picture/
     work-in-flight-sits-on-its-section/section_row_readers.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from matplotlib.text import Text

plt.style.use("data-ink")

RECORDED = "#8a8a8a"  # the stored event, and the state it carries
FRESH = "#2f6fb3"  # the classifier asked the pointer now
STALE = "#b0564f"  # the answer that disagrees
INK = "#222222"


def _at(*parts: int) -> datetime:
    """A UTC instant on the run's timeline, so the axis arithmetic is explicit."""
    return datetime(*parts, tzinfo=UTC)


DISPATCH = _at(2026, 10, 21, 23, 45, 15)
TRANSITIONS = (
    (_at(2026, 10, 21, 23, 45, 15), "dispatched"),
    (_at(2026, 10, 21, 23, 45, 34), "working"),
    (_at(2026, 10, 22, 2, 6, 1), "completed_unpromoted"),
    (_at(2026, 10, 22, 2, 8, 45), "working"),
    (_at(2026, 10, 22, 2, 54, 21), "waiting"),
)
MEASURED = _at(2026, 10, 22, 3, 9, 54)

STATES = ("dispatched", "working", "completed_unpromoted", "waiting", "interrupted")

FIG_W, FIG_H = 14.0, 6.6

# Plotted polylines in data coordinates, so a label over one can be refused.
DATA_LINES: list[tuple] = []


def _hours(moment: datetime) -> float:
    return (moment - DISPATCH).total_seconds() / 3600.0


def _samples(vertices, per_segment: int = 24):
    points = []
    for (x0, y0), (x1, y1) in pairwise(vertices):
        for step in range(per_segment + 1):
            fraction = step / per_segment
            points.append((x0 + (x1 - x0) * fraction, y0 + (y1 - y0) * fraction))
    return points


def _check(fig, axis) -> None:
    """Refuse to write unless every label is on the canvas, clear of the others,
    and clear of the plotted lines."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    canvas = fig.bbox
    boxes = []
    for text in fig.findobj(Text):
        if not text.get_visible() or not text.get_text().strip():
            continue
        box = text.get_window_extent(renderer=renderer)
        if (
            box.x0 < canvas.x0 - 1
            or box.y0 < canvas.y0 - 1
            or box.x1 > canvas.x1 + 1
            or box.y1 > canvas.y1 + 1
        ):
            raise SystemExit(f"label outside the canvas: {text.get_text()!r}")
        boxes.append((box, text.get_text()))
    for index, (first, first_text) in enumerate(boxes):
        for second, second_text in boxes[index + 1 :]:
            if first.overlaps(second):
                raise SystemExit(f"labels overlap: {first_text!r} / {second_text!r}")

    transform = axis.transData.transform
    for vertices in DATA_LINES:
        for x, y in _samples(vertices):
            point = transform((x, y))
            for box, text in boxes:
                if box.contains(*point):
                    raise SystemExit(f"label sits on the plotted line: {text!r}")
    for x, y in DATA_MARKERS:
        point = transform((x, y))
        for box, text in boxes:
            if box.contains(*point):
                raise SystemExit(f"label covers a plotted point: {text!r}")


DATA_MARKERS: list[tuple[float, float]] = []


def _box(axis, x, y, w, h, text, color, *, dashed=False):
    axis.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.02,rounding_size=0.04",
            linewidth=1.6,
            linestyle="--" if dashed else "-",
            edgecolor=color,
            facecolor="white",
        )
    )
    axis.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        fontsize=14,
        color=color,
        linespacing=1.4,
    )


def _arrow(axis, start, end, color=RECORDED):
    axis.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=15,
            linewidth=1.4,
            color=color,
            shrinkA=2,
            shrinkB=2,
        )
    )


def _timeline(axis) -> None:
    recorded = {name: index for index, name in enumerate(STATES)}
    top = len(STATES) - 0.05
    axis.set_xlim(_hours(DISPATCH) - 0.25, 5.6)
    axis.set_ylim(-0.85, top)
    axis.set_yticks([recorded[name] for name in STATES])
    axis.set_yticklabels(STATES, fontsize=14)

    vertices = [(_hours(TRANSITIONS[0][0]), recorded[TRANSITIONS[0][1]])]
    for moment, state in TRANSITIONS[1:]:
        vertices.append((_hours(moment), vertices[-1][1]))
        vertices.append((_hours(moment), recorded[state]))
    DATA_LINES.append(tuple(vertices))
    axis.plot(
        [x for x, _ in vertices],
        [y for _, y in vertices],
        color=RECORDED,
        linewidth=2.6,
        zorder=3,
    )

    carry = (
        (_hours(TRANSITIONS[-1][0]), recorded["waiting"]),
        (_hours(MEASURED), recorded["waiting"]),
    )
    DATA_LINES.append(carry)
    axis.plot(
        [x for x, _ in carry],
        [y for _, y in carry],
        color=RECORDED,
        linewidth=2.4,
        linestyle="--",
        zorder=2,
    )

    axis.text(
        3.55,
        recorded["waiting"] + 0.35,
        "record read: waiting",
        fontsize=14,
        color=RECORDED,
        ha="left",
        va="center",
    )

    marker = (_hours(MEASURED), recorded["interrupted"])
    DATA_MARKERS.append(marker)
    axis.plot(
        [marker[0]],
        [marker[1]],
        marker="o",
        markersize=9,
        color=FRESH,
        zorder=5,
    )
    axis.text(
        4.05,
        recorded["interrupted"] + 0.72,
        "fresh read: interrupted",
        fontsize=14,
        color=FRESH,
        ha="right",
        va="center",
    )

    reference = ((_hours(MEASURED), -0.85), (_hours(MEASURED), 4.35))
    DATA_LINES.append(reference)
    axis.plot(
        [x for x, _ in reference],
        [y for _, y in reference],
        color=RECORDED,
        linewidth=1.2,
        linestyle=":",
        zorder=1,
    )
    axis.text(
        1.8,
        recorded["waiting"] + 0.95,
        "the record is 15 min older\nthan the pointer's own liveness",
        fontsize=14,
        color=STALE,
        ha="center",
        va="center",
        linespacing=1.5,
    )

    ticks = [0, 1, 2, 3, 4, 5]
    axis.set_xticks(ticks)
    axis.set_xticklabels(
        [(DISPATCH + timedelta(hours=hour)).strftime("%H:%M") for hour in ticks],
        fontsize=14,
    )
    axis.set_xlabel("time, 2026-10-21/22 UTC", fontsize=16)
    for side in ("top", "right"):
        axis.spines[side].set_visible(False)


def _readers(axis) -> None:
    axis.set_xlim(0, 10)
    axis.set_ylim(0, 10)
    axis.axis("off")
    axis.text(
        0.0,
        9.7,
        "(b) one classifier, asked at two ages",
        ha="left",
        va="center",
        fontsize=16,
        color=INK,
    )

    _box(axis, 0.05, 8.3, 4.35, 1.1, "roadmap\nsection row", INK)
    _box(axis, 5.2, 8.3, 4.35, 1.1, "live pointer\nview", INK)

    _box(axis, 0.05, 6.2, 4.35, 1.2, "the recorded event\nleads", RECORDED)
    _box(axis, 5.2, 6.2, 4.35, 1.2, "classify_pointer\nthe pointer, now", FRESH)

    _box(
        axis,
        0.05,
        3.9,
        4.35,
        1.2,
        "latest watcher\n02:54:21Z  waiting",
        RECORDED,
        dashed=True,
    )
    axis.text(
        2.22,
        3.42,
        "answer: waiting",
        ha="center",
        va="center",
        fontsize=14,
        color=RECORDED,
    )
    axis.text(
        7.37,
        3.42,
        "answer: interrupted",
        ha="center",
        va="center",
        fontsize=14,
        color=FRESH,
    )

    _arrow(axis, (2.22, 8.3), (2.22, 7.4))
    _arrow(axis, (7.37, 8.3), (7.37, 7.4), FRESH)
    _arrow(axis, (2.22, 6.2), (2.22, 5.1), RECORDED)
    _arrow(axis, (7.37, 6.2), (7.37, 3.78), FRESH)

    axis.text(
        5.0,
        1.7,
        "One classifier answers both. The stored\n"
        "event is an earlier answer to the same\n"
        "question, and nothing ages the record.",
        ha="center",
        va="center",
        fontsize=14,
        color=INK,
        linespacing=1.5,
    )


def main() -> None:
    DATA_LINES.clear()
    DATA_MARKERS.clear()
    figure, (timeline_axis, readers_axis) = plt.subplots(
        1, 2, figsize=(FIG_W, FIG_H), gridspec_kw={"width_ratios": [1.3, 1]}
    )
    _timeline(timeline_axis)
    _readers(readers_axis)
    figure.subplots_adjust(left=0.195, right=0.99, top=0.90, bottom=0.15, wspace=0.10)
    _check(figure, timeline_axis)
    out = Path(__file__).with_suffix(".svg")
    figure.savefig(out, format="svg", dpi=100)
    plt.close(figure)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
