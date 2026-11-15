"""Draw which source answers a ledger reader, and what each one answers.

Every label is emitted as a vector path, so the figure renders with the same
text wherever no font is resolvable — a renderer without fonts loses an SVG
text element entirely, which is how an earlier hand-authored version of this
drawing reached a reader as empty boxes.

    python source-precedence.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Rectangle

SVG_FONT = "path"

POPULATION = "#929292"
FEATURE = "#2f6fa8"
INK = "#1a1a1a"
POLITE = "#5f5f5f"
FAILED = "#b23c3c"

LABEL = 16
NOTE = 14


def _box(ax, x, y, w, h, colour=INK, width=1.2):
    ax.add_patch(
        Rectangle((x, y), w, h, facecolor="#ffffff", edgecolor=colour, linewidth=width)
    )


def _arrow(ax, points, colour, width):
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        ax.annotate(
            "",
            xy=(x1, y1),
            xytext=(x0, y0),
            arrowprops={"arrowstyle": "->", "color": colour, "linewidth": width},
        )


def main() -> Path:
    plt.rcParams["svg.fonttype"] = SVG_FONT
    fig = plt.figure(figsize=(14, 7.2), dpi=100)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, 1400)
    ax.set_ylim(0, 720)
    ax.axis("off")
    fig.patch.set_facecolor("#ffffff")

    label = {"fontsize": LABEL, "color": INK, "ha": "left", "va": "baseline"}
    note = {"fontsize": NOTE, "color": POLITE, "ha": "left", "va": "baseline"}
    name = {"fontsize": LABEL, "color": INK, "ha": "left", "va": "baseline"}

    # the readers, and the one gate they share
    _box(ax, 40, 596, 420, 100)
    ax.text(60, 668, "ledger readers", fontsize=LABEL, color=INK, fontweight="bold")
    ax.text(60, 640, "load · runs · read_records · run_ids", **note)
    ax.text(60, 616, "open_run_index rows", **note)
    _arrow(ax, [(460, 646), (596, 646)], INK, 1.2)

    _box(ax, 600, 596, 300, 100)
    ax.text(620, 668, "any *.json in runs/ ?", fontsize=LABEL, color=INK, fontweight="bold")
    ax.text(620, 640, "run_directory_holds_records", **note)

    # a document answers for its own run id; the store answers the rest
    _arrow(ax, [(750, 596), (750, 554), (520, 554), (520, 516)], POPULATION, 2.6)
    _arrow(ax, [(750, 596), (750, 554), (1040, 554), (1040, 516)], FEATURE, 3.0)
    ax.text(512, 566, "yes", fontsize=NOTE, color=POLITE, ha="right", va="baseline")
    ax.text(1048, 566, "no", fontsize=NOTE, color=FEATURE, va="baseline")

    _box(ax, 300, 396, 440, 120, colour=POPULATION, width=2.6)
    ax.text(320, 488, "per-run documents + aggregate", fontsize=LABEL, color=INK, fontweight="bold")
    ax.text(320, 458, "a document answers for its own run id", **note)
    ax.text(320, 430, "and stays the fuller record", **note)

    _box(ax, 820, 396, 480, 120, colour=FEATURE, width=3.0)
    ax.text(840, 488, "run store", fontsize=LABEL, color=FEATURE, fontweight="bold")
    ax.text(840, 458, "crew/run_store.db, its retired rows attached", **note)
    ax.text(840, 430, "answers every run id no document names", **note)

    ax.text(
        40,
        370,
        "Both sources answer one population: one surviving document no longer hides the store's rows.",
        **note,
    )

    _box(ax, 40, 218, 1220, 116, colour="#cccccc")
    ax.text(60, 314, "run_ids reads the store's id columns, so membership costs no payload decode.", **note)
    ax.text(60, 288, "the store read is one project's rows, on a connection that cannot write.", **note)
    ax.text(60, 262, "open_run_index counts an aggregate, a run document, or stored ids before reporting a project empty.", **note)
    ax.text(60, 236, "load_aggregate stays the writer's view: folded store rows there would be written back into the aggregate.", **note)

    ax.text(40, 190, "three arms over one population of reader tests", **name)

    arms = [
        (FAILED, 158, 52, 70, "read reverted at b1f4c666e8: 2 failed, 19 passed"),
        (POLITE, 118, 52, 70, "base 2bf8e6947d: 58 passed over the two test files present there"),
        (FEATURE, 78, 52, 70, "head 9572b14547: 79 passed over the same two files plus the new one"),
    ]
    for colour, y, dot_x, text_x, text in arms:
        ax.add_patch(Circle((dot_x, y + 4), 4, facecolor=colour, edgecolor="none"))
        ax.text(text_x, y, text, **note)

    return fig.savefig(Path(__file__).with_suffix(".svg"), format="svg", facecolor="#ffffff")


if __name__ == "__main__":
    print(main())