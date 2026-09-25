"""Draw the row's one measure cell on the real row, and the columns it recovered.

The top panel prints the row the renderer produces at the 180-column default
from an event that carries a generation rate, a model-seconds figure, a token
count and a cost — every fact the row used to spend columns on. The character
positions come from that rendered string, so the boxes sit where the row
actually puts its cells rather than where a diagram says it should.

The bottom panel carries the pair the negative control measured: the column the
clause begins at with the single cell, and the column it begins at once the
generation-rate cell is restored ahead of it. The five columns between them are
what the rewritten layout test fails on.
"""

from __future__ import annotations

import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from reckon.crew import ticker as ticker_module

WIDTH = 180
ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# The fixture: a blocked row whose record carries everything the row stopped
# rendering, so the panel shows they are absent from a row that holds them.
EVENT = {
    "observed_at": "2026-09-24T12:00:00+00:00",
    "run_id": "r-measure-cell",
    "node": "n-measure-cell",
    "role": "review",
    "from_state": "working",
    "to_state": "blocked",
    "working": 3,
    "blocked": 1,
    "unpromoted": 0,
    "model": "dsv4.1-flash",
    "effort": "xhigh",
    "spend_wall_seconds": 3421.0,
    "spend_model_seconds": 3133.5,
    "spend_charged_tokens": 1_500_000,
    "spend_generation_rate": 38.2,
    "spend_notional_cost_usd": 0.12,
    "detail": "the gate refused and no receipt was written",
}

# Measured on the negative-control log's own output at this node's head: the
# column the clause begins at with the row as it stands, and with the
# generation-rate cell restored in Ticker.render ahead of it.
CLAUSE_COLUMN = {
    "elapsed cell alone": 114,
    "rate cell restored": 119,
}

OUTPUT = Path(__file__).with_suffix(".png")


def plain(line: str) -> str:
    return ESCAPES.sub("", line)


def counters_block(event: dict) -> str:
    """The fleet-counter text this event renders, composed as the renderer does."""
    letters = ticker_module.STAT_LETTER
    return "·".join(
        f"{int(event[label]):>2}{letters[label]}" for label in ticker_module._CELLS
    )


def main() -> None:
    grid = ticker_module.Ticker(
        width=WIDTH, theme="light", color=False, model_aliases=()
    )
    row = plain(grid.render(EVENT))
    assert len(row) == WIDTH, len(row)

    clause = EVENT["detail"]
    clause_at = row.index(clause)
    cell_at = clause_at - ticker_module.GAP - ticker_module.WALL
    block = counters_block(EVENT)
    counters_at = row.index(block)
    assert counters_at + len(block) + ticker_module.SPEND_GAP == cell_at, (
        counters_at,
        len(block),
        cell_at,
    )

    figure = plt.figure(figsize=(13.5, 5.6))
    upper = figure.add_axes((0.03, 0.4, 0.94, 0.48))
    lower = figure.add_axes((0.13, 0.08, 0.85, 0.2))

    upper.set_xlim(0, WIDTH)
    upper.set_ylim(0, 3.6)
    upper.axis("off")
    upper.set_title(
        f"The row at {WIDTH} columns: one measure cell, and the clause begins at "
        f"column {clause_at}",
        fontsize=11,
        loc="left",
    )

    for column in range(0, WIDTH, 10):
        upper.plot([column, column], [2.78, 2.9], color="#9a9a9a", linewidth=0.6)
    for column in range(0, WIDTH, 20):
        upper.text(column, 2.98, str(column), fontsize=6, ha="center", color="#5a5a5a")

    for index, character in enumerate(row):
        if character != " ":
            upper.text(
                index + 0.5,
                2.55,
                character,
                fontsize=7.4,
                ha="center",
                va="center",
                family="monospace",
            )

    def box(start: int, width: int, colour: str, label: str, label_y: float) -> None:
        upper.add_patch(
            Rectangle(
                (start, 2.32),
                width,
                0.46,
                fill=False,
                edgecolor=colour,
                linewidth=1.2,
            )
        )
        upper.annotate(
            label,
            xy=(start + width / 2, 2.32),
            xytext=(start + width / 2, label_y),
            fontsize=8,
            ha="center",
            color=colour,
            arrowprops={"arrowstyle": "-", "color": colour, "linewidth": 0.8},
        )

    box(clause_at, len(clause), "#1f6f3f", "the clause: the margin's own text", 1.62)
    box(
        cell_at,
        ticker_module.WALL,
        "#1f4e9c",
        "one measure cell —\nelapsed time alone (57m)",
        1.02,
    )
    box(counters_at, len(block), "#8a5a00", "the fleet counters", 0.5)

    removed_at = cell_at + ticker_module.WALL + ticker_module.GAP
    upper.add_patch(
        Rectangle(
            (removed_at, 2.32),
            5,
            0.46,
            facecolor="#cccccc",
            edgecolor="#666666",
            hatch="///",
            linewidth=0.6,
        )
    )
    upper.annotate(
        "the rate cell's five columns\nnow carry the clause's first five characters",
        xy=(removed_at + 2.5, 2.32),
        xytext=(removed_at + 16, 1.9),
        fontsize=7.5,
        ha="center",
        color="#444444",
        arrowprops={"arrowstyle": "-", "color": "#666666", "linewidth": 0.8},
    )

    names = list(CLAUSE_COLUMN)
    values = [CLAUSE_COLUMN[name] for name in names]
    rooms = [WIDTH - value for value in values]
    lower.set_xlim(0, WIDTH + 45)
    lower.set_ylim(-0.7, len(names) - 0.3)
    lower.set_yticks(range(len(names)))
    lower.set_yticklabels(names, fontsize=9)
    lower.barh(
        range(len(names)),
        rooms,
        left=values,
        color="#dfe6f2",
        edgecolor="#1f4e9c",
        height=0.5,
        label="the clause",
    )
    lower.barh(
        range(len(names)),
        values,
        color="#cccccc",
        edgecolor="#666666",
        height=0.5,
        label="fixed cells and the measure block",
    )
    for index, (value, room) in enumerate(zip(values, rooms, strict=True)):
        lower.text(
            value + room + 1,
            index,
            f"clause begins at {value}, room {room}",
            fontsize=8.5,
            va="center",
            color="#1f4e9c",
        )
    lower.set_xlabel("screen column", fontsize=9)
    lower.set_title(
        "The column the clause begins at, at this node's head and under the "
        "negative control's restored rate cell",
        fontsize=10,
        loc="left",
    )
    lower.legend(fontsize=8, loc="lower left", frameon=False)

    figure.savefig(OUTPUT, dpi=160)
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
