#!/usr/bin/env python3
"""Draw the shared server's resident set at two worker counts.

    .venv/bin/python resident_set_by_worker_count.py <measure.json> <out.svg>

``measure.json`` is the two-arm measure's own record: the server's resident set
sampled while five workers, then forty, held sessions open against it, with the
process count read at the same time. The figure reports those samples; the only
thing it computes is each arm's median and their difference.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.style.use("data-ink")

FIVE = "#929292"
FORTY = "#2b6cb0"
ALLOWANCE_KB = 500_000


def _megabytes(kilobytes: float) -> float:
    return kilobytes / 1024


def draw(measure: dict, out: Path) -> None:
    small = measure["small"]
    large = measure["large"]
    small_mb = [_megabytes(value) for value in small["samples"]]
    large_mb = [_megabytes(value) for value in large["samples"]]
    delta_mb = _megabytes(measure["delta_kb"])

    figure, axis = plt.subplots(figsize=(14, 7.2), layout="constrained")
    samples = list(range(1, len(small_mb) + 1))
    axis.plot(
        samples,
        small_mb,
        color=FIVE,
        linewidth=2.6,
        marker="o",
        markersize=5,
        label=f"{small['workers']} workers",
    )
    axis.plot(
        samples,
        large_mb,
        color=FORTY,
        linewidth=3.0,
        marker="o",
        markersize=5,
        label=f"{large['workers']} workers",
        zorder=3,
    )

    axis.annotate(
        f"{small['workers']} workers\n{_megabytes(measure['small_median_kb']):.0f} MB",
        (samples[-1], small_mb[-1]),
        textcoords="offset points",
        xytext=(12, -26),
        ha="left",
        fontsize=14,
        color=FIVE,
    )
    axis.annotate(
        f"{large['workers']}\n{_megabytes(measure['large_median_kb']):.0f} MB",
        (samples[-1], large_mb[-1]),
        textcoords="offset points",
        xytext=(12, 8),
        ha="left",
        fontsize=14,
        color=FORTY,
    )
    axis.annotate(
        f"difference {delta_mb:.1f} MB, "
        f"within the {_megabytes(ALLOWANCE_KB):.0f} MB allowance",
        (samples[0], max(large_mb)),
        textcoords="offset points",
        xytext=(0, 34),
        ha="left",
        fontsize=14,
    )

    axis.set_xlim(0.5, len(samples) + 1.5)
    span = max(large_mb) - min(small_mb)
    axis.set_ylim(min(small_mb) - max(span, 1.0), max(large_mb) + max(span, 1.0) * 3)
    axis.set_xticks(samples)
    axis.set_xlabel("sample within the arm's window")
    axis.set_ylabel("resident set [MB]")
    axis.spines[["top", "right"]].set_visible(False)

    figure.savefig(out)
    plt.close(figure)


if __name__ == "__main__":
    draw(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]))
