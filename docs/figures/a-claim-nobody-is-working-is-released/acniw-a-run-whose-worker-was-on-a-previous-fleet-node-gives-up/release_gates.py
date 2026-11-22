"""Draw the gates a promoted live pointer walks before its claim is released.

Text is emitted as glyph outlines (``svg.fonttype = "path"``) so the figure
reads the same in every renderer: a rasteriser with no font stack of its own
would otherwise draw the boxes and arrows and drop every label.
"""

import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

WIDTH_CM = 31.1
HEIGHT_CM = 16.0
GATE = "#f7f7f7"
GATE_EDGE = "#6b6b6b"
HELD = "#ffffff"
HELD_EDGE = "#929292"
INK = "#222222"
MUTED = "#555555"
GREEN = "#2e8b57"
GREEN_INK = "#1f6b43"
RETIRED = "#e9f3ec"

GATE_SIZE = 14.0
SIDE_SIZE = 12.0


def _box(ax, x, y, width, height, lines, *, fill, edge, ink, size=GATE_SIZE):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            width,
            height,
            boxstyle="round,pad=0,rounding_size=8",
            linewidth=1.3,
            facecolor=fill,
            edgecolor=edge,
            mutation_aspect=1,
        )
    )
    ax.text(
        x + width / 2,
        y + height / 2,
        "\n".join(lines),
        ha="center",
        va="center",
        fontsize=size,
        color=ink,
        linespacing=1.5,
    )


def _arrow(ax, start, end, *, color=MUTED, width=1.3):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=13,
            linewidth=width,
            color=color,
            shrinkA=0,
            shrinkB=0,
        )
    )


def _elbow(ax, points, *, color=GREEN, width=1.3):
    xs, ys = zip(*points, strict=True)
    ax.plot(xs, ys, color=color, linewidth=width, solid_capstyle="butt", zorder=1)
    _arrow(ax, points[-2], points[-1], color=color, width=width)


def _declare_canvas(path: Path) -> None:
    """State the canvas in centimetres so the figure drops onto a slide at size."""
    text = path.read_text(encoding="utf-8")
    for attribute, value in (("width", WIDTH_CM), ("height", HEIGHT_CM)):
        text = re.sub(
            rf'(<svg[^>]*\s{attribute}=")[^"]*(")',
            rf"\g<1>{value}cm\2",
            text,
            count=1,
        )
    path.write_text(text, encoding="utf-8")


def draw(destination: Path) -> None:
    plt.style.use("data-ink")
    # Glyph outlines rather than font references: a rasteriser without the
    # family installed would otherwise draw empty boxes.
    plt.rcParams["svg.fonttype"] = "path"
    figure = plt.figure(figsize=(WIDTH_CM / 2.54, HEIGHT_CM / 2.54))
    ax = figure.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, 1400)
    ax.set_ylim(720, 0)
    ax.set_axis_off()

    _box(
        ax,
        430,
        30,
        540,
        60,
        ["a live pointer with a promotion mark", "or a promoted ledger row"],
        fill="#f2f2f2",
        edge=GATE_EDGE,
        ink=INK,
    )

    gates = [
        (
            130,
            ["workspace on disk, or an", "unfinished landing journal?"],
            ["held: the landing paths", "decide the work"],
        ),
        (
            240,
            ["launched on a node", "the fleet has left?"],
            None,
        ),
        (
            350,
            ["worker alive here, or", "not proved dead?"],
            ["held: working, or", "unreadable"],
        ),
        (
            460,
            ["assistant turn inside the silence", "window of its newest stream?"],
            ["held: a turn is in", "progress"],
        ),
    ]

    for index, (top, question, held) in enumerate(gates):
        _box(ax, 430, top, 540, 70, question, fill=GATE, edge=GATE_EDGE, ink=INK)
        if held is not None:
            _box(
                ax,
                1030,
                top,
                340,
                70,
                held,
                fill=HELD,
                edge=HELD_EDGE,
                ink=MUTED,
                size=SIDE_SIZE,
            )
            _arrow(ax, (970, top + 35), (1024, top + 35))
            ax.text(
                998,
                top + 22,
                "yes",
                ha="center",
                va="center",
                fontsize=SIDE_SIZE,
                color=MUTED,
            )
        if index < len(gates) - 1:
            _arrow(ax, (700, top + 70), (700, top + 106))
        if index in (1, 2):
            ax.text(
                712,
                top + 84,
                "no",
                ha="left",
                va="center",
                fontsize=SIDE_SIZE,
                color=MUTED,
            )

    # A run launched on a node the fleet has left never asks this host's table,
    # so the local process gate is bypassed rather than failed.
    _elbow(ax, [(430, 275), (370, 275), (370, 495), (424, 495)])
    ax.text(400, 258, "yes", ha="right", va="center", fontsize=SIDE_SIZE, color=GREEN)
    ax.text(
        200,
        394,
        "its pid answers\nonly there, so this\nhost's table is\nnever asked",
        ha="center",
        va="center",
        fontsize=SIDE_SIZE,
        color=GREEN,
        linespacing=1.45,
    )

    _arrow(ax, (700, 530), (700, 576))
    _box(
        ax,
        360,
        580,
        680,
        100,
        [
            "retired: the claim is released and the work is untouched",
            "worktree, commits and run records stay on disk,",
            "and a later pass finds no pointer to restore",
        ],
        fill=RETIRED,
        edge=GREEN,
        ink=GREEN_INK,
        size=13.0,
    )

    figure.savefig(
        destination,
        format="svg",
        metadata={"Date": None},
        bbox_inches=None,
        pad_inches=0,
    )
    plt.close(figure)
    _declare_canvas(destination)


if __name__ == "__main__":
    draw(Path(__file__).with_name("release-gates.svg"))
