"""Draw the model cell's width against the aliases the config declares.

The row sizes the model cell from the longest alias the resolved flight config
declares, and cuts any other model id to that width. The figure draws the model
cell, the tight gap and the effort cell as a character grid, once per alias, on
each side of the change.

At the base revision the model cell is a fixed ten columns, so the
twelve-character alias dsv4.1-flash overflows it and its effort cell lands two
columns right of the others — the grid shifts. Sized from the longest declared
alias (twelve here), every effort cell lands on one column, and a model id
wider than the cell is cut with an ellipsis rather than allowed to overflow.
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from reckon.crew import ticker as ticker_module

ALIASES = ("dsv4.1-flash", "astra6", "sonnet 5")
UNCONFIGURED = "gpt-5.6-sol-ultra-xy"  # declared by no backend
ROWS = (*ALIASES, UNCONFIGURED)
EFFORT = "medium"
BASE_CELL = 10  # the fixed ten-column model cell at the base revision
HEAD_CELL = max(len(a) for a in ALIASES)  # the longest declared alias
PALETTE = ("#f4c9b0", "#b9d9f0", "#c8e6c9", "#e2d6f0")


def row(alias: str, cell: int, *, cut: bool) -> tuple[str, int]:
    """The model cell + gap + effort cell as characters, and the effort start."""
    shown = ticker_module.elide(alias, cell) if cut else alias
    model = f"{shown:<{cell}}"
    tail = " " * ticker_module.PAIR_GAP + f"{EFFORT:<{ticker_module.EFFORT}}"
    return list(model + tail), len(model) + ticker_module.PAIR_GAP


def draw(ax, title: str, cell: int, *, cut: bool) -> None:
    rows = [row(alias, cell, cut=cut) for alias in ROWS]
    width = max(len(chars) for chars, _ in rows)
    starts = [start for _, start in rows]
    for y, (chars, start) in enumerate(rows):
        for x, ch in enumerate(chars):
            face = PALETTE[y % len(PALETTE)] if x == start else "white"
            ax.add_patch(
                Rectangle((x, -y), 1, 1, facecolor=face, edgecolor="#c8ccd2", lw=0.6)
            )
            ax.text(
                x + 0.5,
                -y + 0.5,
                ch if ch.strip() else "␣",
                ha="center",
                va="center",
                fontsize=9,
            )
    ax.set_xlim(0, width + 0.2)
    ax.set_ylim(-len(rows) + 1, 1.15)
    ax.set_yticks([-y + 0.5 for y in range(len(rows))])
    ax.set_yticklabels(ROWS, fontsize=8, fontfamily="monospace")
    ax.set_xticks([x + 0.5 for x in range(width)])
    ax.set_xticklabels([str(x) for x in range(width)], fontsize=6)
    ax.set_title(title, fontsize=10)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)

    distinct = len(set(starts))
    noun = "column" if distinct == 1 else "columns"
    ax.set_xlabel(
        f"effort cell begins on {distinct} different {noun}: "
        + ", ".join(str(s) for s in starts),
        fontsize=8,
    )


def main() -> None:
    assert len(UNCONFIGURED) > HEAD_CELL, "the unconfigured id must exceed the cell"
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 3.4))
    draw(
        axes[0],
        f"base revision — fixed {BASE_CELL}-column cell",
        BASE_CELL,
        cut=False,
    )
    draw(
        axes[1],
        f"this change — cell {HEAD_CELL} from the longest declared alias",
        HEAD_CELL,
        cut=True,
    )
    fig.tight_layout()
    out = Path(__file__).with_name("model_cell_alias_width.png")
    fig.savefig(out, dpi=150)
    print(out)


if __name__ == "__main__":  # pragma: no cover
    main()
