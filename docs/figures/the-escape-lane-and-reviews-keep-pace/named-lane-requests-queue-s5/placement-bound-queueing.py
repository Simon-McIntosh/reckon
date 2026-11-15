"""Draw the roster states a held dispatch passes through.

Every label is emitted as a vector path, so the figure renders with the same
text wherever no font is resolvable — the shape of a glyph never depends on the
renderer's font configuration.

    python placement-bound-queueing.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

SVG_FONT = "path"

POPULATION = "#929292"
NAMED = "#2f6fa8"
INK = "#1a1a1a"
POLITE = "#5f5f5f"

SEAT_W, SEAT_H, SEAT_GAP = 60, 46, 12
FIRST_SEAT_X = 60


def _seats(ax, y, *, filled, outline=None, outline_colour=INK):
    for index in range(8):
        x = FIRST_SEAT_X + index * (SEAT_W + SEAT_GAP)
        face = POPULATION if index < filled else "#ffffff"
        ax.add_patch(
            Rectangle((x, y), SEAT_W, SEAT_H, facecolor=face, edgecolor="none")
        )
        if outline is not None and index == 7:
            ax.add_patch(
                Rectangle(
                    (x, y),
                    SEAT_W,
                    SEAT_H,
                    facecolor="none",
                    edgecolor=outline_colour,
                    linewidth=2,
                )
            )


def _arrow_up(ax, x, y_tail, y_head, colour):
    ax.annotate(
        "",
        xy=(x, y_head),
        xytext=(x, y_tail),
        arrowprops={"arrowstyle": "->", "color": colour, "linewidth": 3},
    )


def main() -> Path:
    plt.rcParams["svg.fonttype"] = SVG_FONT
    fig = plt.figure(figsize=(14, 7.2), dpi=100)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, 1400)
    ax.set_ylim(0, 720)
    ax.axis("off")
    fig.patch.set_facecolor("#ffffff")

    label_kw = {"fontsize": 16, "color": INK, "ha": "left", "va": "baseline"}
    note_kw = {"fontsize": 14, "color": POLITE, "ha": "left", "va": "baseline"}

    # roster at its cap, three requests waiting
    ax.text(60, 650, "roster at its cap", **label_kw)
    _seats(ax, 580, filled=8)
    ax.add_patch(Rectangle((66, 530), 14, 14, facecolor=NAMED, edgecolor="none"))
    ax.text(90, 528, "named lane request", fontsize=14, color=NAMED)
    ax.text(320, 528, "local request a", **note_kw)
    ax.text(500, 528, "local request b", **note_kw)
    ax.text(
        700,
        620,
        "all three wait queued; no run id is printed without a pointer",
        **note_kw,
    )

    # one seat frees and the named request takes it
    ax.text(60, 470, "one seat frees", **label_kw)
    _seats(ax, 400, filled=7)
    ax.add_patch(
        Rectangle(
            (FIRST_SEAT_X + 7 * (SEAT_W + SEAT_GAP), 400),
            SEAT_W,
            SEAT_H,
            facecolor=NAMED,
            edgecolor="none",
        )
    )
    _arrow_up(ax, FIRST_SEAT_X + 7 * (SEAT_W + SEAT_GAP) + SEAT_W / 2, 360, 392, NAMED)
    ax.text(90, 358, "named lane request launches", fontsize=14, color=NAMED)
    ax.text(700, 358, "local requests hold the queue", **note_kw)

    # nothing waits on another lane, so the local lane fills the cap
    ax.text(60, 300, "one seat free, nothing on another lane waiting", **label_kw)
    _seats(ax, 230, filled=8, outline=True)
    ax.text(700, 280, "the local lane takes the last seat: no reserve", **note_kw)
    ax.text(700, 250, "stands while nothing waits on another lane", **note_kw)

    # a named request waits, so the last seat stays reserved
    ax.text(60, 150, "one seat free, a named request waiting", **label_kw)
    _seats(ax, 80, filled=7, outline=True, outline_colour=NAMED)
    ax.add_patch(Rectangle((66, 30), 14, 14, facecolor=NAMED, edgecolor="none"))
    ax.text(90, 28, "named lane request waiting", fontsize=14, color=NAMED)
    ax.text(700, 130, "the last seat stays free for the request that named", **note_kw)
    ax.text(
        700, 100, "another lane; local work queues one seat short of the cap", **note_kw
    )

    target = Path(__file__).with_suffix(".svg")
    fig.savefig(target, format="svg", facecolor="#ffffff")
    plt.close(fig)
    return target


if __name__ == "__main__":
    print(main())
