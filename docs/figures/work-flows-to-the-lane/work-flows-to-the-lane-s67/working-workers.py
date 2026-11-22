"""Draw the working-worker trace through the measured window.

    python working-workers.py <window-start> <window-end> <out.svg>

The spans come from the lane census module, so the trace is computed from the
same ledger reading the mean in the record is computed over rather than from a
second instrument. The SVG carries the record's own figure directory.

The save gate is the one figure guard the repository already runs — labels
outside the canvas, labels over each other, labels over a plotted line — taken
from the figure that owns it rather than written again here, so a repair to that
guard reaches this figure too.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

MEASURED = "#1f5fa8"  # the subject: workers actually working
LIMIT = "#52514e"  # the bound and its label
MEAN = "#0b0b0b"  # the measured summary, near-black dashed

CANVAS_IN = (14.0, 7.2)

GUARD = (
    Path(__file__).resolve().parents[4]
    / "docs/figures/a-project-reads-as-one-picture"
    / "work-in-flight-sits-on-its-section/section_row_readers.py"
)


def load_guard(path=GUARD):
    """Import the repository's figure save gate from the module that owns it."""
    spec = importlib.util.spec_from_file_location("figure_save_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def place_mean_label(ax, fig, mean, x, series, *, size=14) -> None:
    """Put the summary's name on its own line, lifted clear of any plotted data."""
    points = ax.transData.transform([(px, py) for px, py in series])
    for lift in (0, 6, 12, 18, 24, 30):
        label = ax.annotate(f" mean {mean} ", (x, mean), xycoords="data",
                            textcoords="offset points", xytext=(0, lift),
                            color=MEAN, fontsize=size, va="bottom", ha="left")
        fig.canvas.draw()
        box = label.get_window_extent(fig.canvas.get_renderer())
        on_data = any(box.x0 < px < box.x1 and box.y0 < py < box.y1
                      for px, py in points)
        if not on_data:
            return
        label.remove()
    raise SystemExit("no clear place for the mean label")


def main(argv: list[str]) -> int:
    start = dt.datetime.fromisoformat(argv[1])
    end = dt.datetime.fromisoformat(argv[2])
    out = Path(argv[3])

    sys.path.insert(0, str(out.parents[4] / "scripts"))
    import lane_window_census as census

    runs, _counts = census.load_runs()
    spans = census.worker_spans(runs)
    report = census.census(start, end, runs, spans, {}, [])
    mean = report["working_workers"]["mean"]

    minutes = int((end - start).total_seconds() // 60)
    counts = []
    for minute in range(minutes + 1):
        when = start + dt.timedelta(minutes=minute)
        counts.append(sum(1 for first, last, _run in spans if first <= when < last))
    xs = [minute / 60.0 for minute in range(minutes + 1)]

    plt.style.use("data-ink")
    fig, ax = plt.subplots(figsize=CANVAS_IN)
    fig.subplots_adjust(left=0.07, right=0.975, bottom=0.145, top=0.9)
    ax.step(xs, counts, where="post", color=MEASURED, linewidth=3.0, zorder=3)
    peak = max(counts) if counts else 0
    ax.set_ylim(0, max(14, peak + 2))
    ax.axhline(12, color=LIMIT, linewidth=1.2, linestyle=(0, (1.5, 2.5)), zorder=1)
    ax.axhline(mean, color=MEAN, linewidth=2.4, linestyle=(0, (5, 3)), zorder=2)
    ax.set_xlim(0, minutes / 60.0)
    ax.set_xticks(range(0, int(minutes / 60.0) + 1))
    ax.set_xlabel("hours into the window")
    ax.set_ylabel("working workers")
    ax.text(minutes / 60.0, 12, " bound 12", color=LIMIT, fontsize=14,
            va="bottom", ha="right")
    place_mean_label(ax, fig, mean, 0.04, list(zip(xs, counts)))
    gate = load_guard()
    gate.DATA_LINES[:] = [list(zip(xs, counts))]
    gate.DATA_MARKERS[:] = []
    gate._check(fig, ax)
    fig.savefig(out, format="svg")
    print(f"{out} mean={mean} peak={max(counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))