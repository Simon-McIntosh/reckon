"""Draw the baseline marker cell's token boundary against the transition's.

The row holds one marker cell ahead of the state word on every row, blank on a
transition. The figure draws the state region as a character grid on each side
as a pair of rows: a baseline row and a transition row. With the marker cell
sized to the word alone the marker and the state run together as one token;
with the cell one column wider the marker stands as its own word and the state
still begins on the same column for both row kinds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from reckon.crew import ticker as ticker_module  # noqa: E402

WORD = ticker_module.BASELINE_MARKER
STATE = "working"
BASE_CELL = len(WORD)               # base: the word alone
HEAD_CELL = ticker_module.MARKER    # head: the word and its separator


def region(cell_width: int, marker: str) -> list[str]:
    """The marker cell and the state word as characters, cell padding included."""
    return list(f"{marker:<{cell_width}}" + f"{STATE:<{ticker_module.STATE}}")


def draw(ax, rows, title, sep_col) -> None:
    width = len(rows[0][1])
    for y, (label, chars) in enumerate(rows):
        for x, ch in enumerate(chars):
            face = "#f4c9b0" if sep_col is not None and x == sep_col else "white"
            ax.add_patch(Rectangle((x, -y), 1, 1, facecolor=face,
                                   edgecolor="#c8ccd2", lw=0.6))
            ax.text(x + 0.5, -y + 0.5, ch if ch.strip() else "␣",
                    ha="center", va="center", fontsize=9)
    ax.set_xlim(0, width + 0.2)
    ax.set_ylim(-len(rows) + 1, 1.15)
    ax.set_yticks([-y + 0.5 for y in range(len(rows))])
    ax.set_yticklabels([label for label, _ in rows])
    ax.set_xticks([x + 0.5 for x in range(width)])
    ax.set_xticklabels([str(x) for x in range(width)], fontsize=6)
    ax.set_title(title, fontsize=10)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)


def main() -> None:
    base = [("baseline", region(BASE_CELL, WORD)),
            ("transition", region(BASE_CELL, ""))]
    head = [("baseline", region(HEAD_CELL, WORD)),
            ("transition", region(HEAD_CELL, ""))]
    fig, axes = plt.subplots(2, 1, figsize=(8.4, 3.4))
    draw(axes[0], base, "base revision — marker cell 3: one token `nowworking`", None)
    draw(axes[1], head, "this change — marker cell 4: `now working`, state word shared by both rows", HEAD_CELL - 1)
    fig.tight_layout()
    out = Path(__file__).with_name("baseline_marker_separator.png")
    fig.savefig(out, dpi=150)
    print(out)


if __name__ == "__main__":
    main()