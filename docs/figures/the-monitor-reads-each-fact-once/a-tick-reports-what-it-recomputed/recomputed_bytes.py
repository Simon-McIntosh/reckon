#!/usr/bin/env python3
"""Draw what each station pass reported reading, round by round.

    .venv/bin/python recomputed_bytes.py <rounds.json> <out.svg>

``rounds.json`` is the census the node's rounds recorded: three rounds of one
tick and one sweep pass, the first cold, the second unchanged, the third with
one plan rewritten and one 4 KiB stream grown. The figure reports those counts;
apart from summing the bytes of the kinds a pass reported, it computes nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.style.use("data-ink")

POPULATION = "#929292"
SUBJECT = "#2b6cb0"

ROUND_LABELS = ("cold", "unchanged", "one plan rewritten,\none 4 KiB stream grown")


def bytes_by_pass(rounds: list[dict]) -> list[tuple[int, int]]:
    """Per round, how many bytes the tick read and how many the sweep pass read.

    A pass reports each memo kind separately; the round's cost is what those
    kinds add up to. Recomputed and extended bytes are summed together here
    because both are bytes the pass read, and the rounds measure how much that
    is rather than which mechanism paid it.
    """
    out = []
    for entry in rounds:
        counts = entry["counts"]
        tick = sum(row["bytes_read"] for row in counts["tick"].values())
        sweep = sum(row["bytes_read"] for row in counts["sweep"].values())
        out.append((tick, sweep))
    return out


def draw(rounds: list[dict], out: Path) -> None:
    per_round = bytes_by_pass(rounds)
    tick = [row[0] / 1024 for row in per_round]
    sweep = [row[1] / 1024 for row in per_round]

    figure, axis = plt.subplots(figsize=(14, 7.2), layout="constrained")
    positions = list(range(len(ROUND_LABELS)))
    width = 0.36

    axis.bar(
        [p - width / 2 for p in positions],
        tick,
        width,
        color=POPULATION,
        label="tick",
    )
    axis.bar(
        [p + width / 2 for p in positions],
        sweep,
        width,
        color="#bdbdbd",
        label="sweep pass",
    )

    for index, value in enumerate(tick):
        axis.annotate(
            f"{value:,.0f} KiB" if value else "0",
            (index - width / 2, value),
            textcoords="offset points",
            xytext=(0, 4),
            ha="center",
            fontsize=13,
        )
    for index, value in enumerate(sweep):
        label = (
            f"{value:,.0f} KiB"
            if value >= 1
            else (f"{value * 1024:,.0f} B" if value else "0")
        )
        axis.annotate(
            label,
            (index + width / 2, value),
            textcoords="offset points",
            xytext=(0, 4),
            ha="center",
            fontsize=13,
        )

    moved = len(positions) - 1
    axis.annotate(
        "extension of the bytes that arrived",
        (moved - width / 2, tick[moved]),
        textcoords="offset points",
        xytext=(0, 58),
        ha="center",
        fontsize=12,
        color=SUBJECT,
    )
    axis.annotate(
        "one plan re-read",
        (moved + width / 2, sweep[moved]),
        textcoords="offset points",
        xytext=(0, 34),
        ha="center",
        fontsize=12,
    )
    axis.annotate(
        "nothing changed: no bytes read",
        (1, 0),
        textcoords="offset points",
        xytext=(0, 34),
        ha="center",
        fontsize=13,
        color="#4a5568",
    )

    axis.set_xticks(positions)
    axis.set_xticklabels(ROUND_LABELS)
    axis.set_ylabel("bytes read by the pass [KiB]")
    axis.spines[["top", "right"]].set_visible(False)
    axis.legend(frameon=False, loc="upper right")

    figure.savefig(out)
    plt.close(figure)


if __name__ == "__main__":
    draw(json.loads(Path(sys.argv[1]).read_text())["rounds"], Path(sys.argv[2]))
