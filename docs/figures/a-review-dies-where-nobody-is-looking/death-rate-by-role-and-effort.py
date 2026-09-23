"""Plot the crew run death rate per role-by-effort cell, with cell sizes.

Reads docs/research/data/review-death-by-effort.json and draws two panels: the
death rate each cell carries, and the size of the population it is computed
over. A rate without its denominator cannot be read — the small cells here are
one or two runs — so the two are shown together.
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "docs/research/data/review-death-by-effort.json"
OUT = Path(__file__).with_name("death-rate-by-role-and-effort.png")

MIN_CELL = 10


def main() -> int:
    census = json.loads(DATA.read_text(encoding="utf-8"))
    cells = [c for c in census["cells"] if c["size"] >= MIN_CELL]
    cells.sort(key=lambda c: (c["role"], c["effort"] or ""))
    labels = [f"{c['role']}\n{c['effort'] or 'unset'}" for c in cells]
    rates = [100.0 * (c["death_rate"] or 0.0) for c in cells]
    sizes = [c["size"] for c in cells]
    dead = [c["dead"] for c in cells]

    figure, (left, right) = plt.subplots(1, 2, figsize=(11, 4.6))

    colours = ["#b2182b" if c["role"] == "review" else "#2166ac" for c in cells]
    bars = left.bar(labels, rates, color=colours)
    left.set_ylabel("death rate (%)")
    left.set_title("Death rate by role and effort")
    left.tick_params(axis="x", labelsize=8)
    for bar, cell in zip(bars, cells, strict=True):
        left.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.2,
            f"{cell['dead']}/{cell['size']}",
            ha="center",
            va="bottom",
            fontsize=8,
        )
    left.set_ylim(0, max(rates) * 1.25)

    right.bar(labels, sizes, color=colours)
    right.set_ylabel("runs in cell")
    right.set_title(f"Cell population (cells with n >= {MIN_CELL})")
    right.tick_params(axis="x", labelsize=8)
    right.set_yscale("log")
    for index, size in enumerate(sizes):
        right.text(index, size * 1.1, str(size), ha="center", va="bottom", fontsize=8)

    figure.suptitle(
        "Crew run deaths on the locally served lane, by role and effort",
        fontsize=11,
    )
    figure.tight_layout(rect=(0, 0, 1, 0.94))
    figure.savefig(OUT, dpi=140)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())