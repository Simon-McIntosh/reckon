"""Bounds behind the four load-sensitive tests, base arm against head arm.

The base arm is the revision's own test file under load; the head arm is the
same file with the bound placed on the property rather than on wall clock. The
y axis is logarithmic, so a bound a hundred times looser reads as one step.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# (test, knob, base bound seconds, observed max under load, head bound seconds)
ROWS = [
    ("receipt: endless writer\ncannot hang the fold", "elapsed <", 2.0, 10.25, 60.0),
    (
        "receipt: writer ends with\nno receipt keeps fallback",
        "elapsed <",
        5.0,
        16.46,
        60.0,
    ),
    (
        "promotion: no terminal record\nkeeps mtime fallback",
        "elapsed <",
        5.0,
        14.87,
        60.0,
    ),
    ("ledger: every read view\nreturns", "read deadline", 30.0, 36.3, 240.0),
]

labels = [row[0] for row in ROWS]
base = [row[2] for row in ROWS]
observed = [row[3] for row in ROWS]
head = [row[4] for row in ROWS]

positions = range(len(ROWS))
width = 0.36

fig, ax = plt.subplots(figsize=(11.0, 5.6), dpi=150)
left = [p - width / 2 - 0.02 for p in positions]
right = [p + width / 2 + 0.02 for p in positions]

base_bars = ax.bar(left, base, width, color="#c0392b", label="base bound (asserted)")
head_bars = ax.bar(right, head, width, color="#1e8449", label="head bound (asserted)")

for x, _value, obs in zip(left, base, observed, strict=True):
    ax.plot(
        [x - width / 2, x + width / 2],
        [obs, obs],
        color="#111111",
        linewidth=2.0,
        zorder=5,
    )
    ax.annotate(
        f"{obs:g} s observed",
        (x, obs),
        textcoords="offset points",
        xytext=(0, 6),
        ha="center",
        fontsize=8.0,
        color="#111111",
    )

for x, value in zip(right, head, strict=True):
    ax.annotate(
        f"{value:g}",
        (x, value),
        textcoords="offset points",
        xytext=(0, 4),
        ha="center",
        fontsize=8.0,
        color="#1e8449",
    )

ax.axhline(30.0, color="#7f8c8d", linewidth=1.0, linestyle=":")
ax.annotate(
    "30 s production read deadline (ledger base arm)",
    (3.45, 32.0),
    ha="right",
    fontsize=8.0,
    color="#7f8c8d",
)

ax.set_yscale("log")
ax.set_ylim(1.0, 400.0)
ax.set_xticks(list(positions))
ax.set_xticklabels(labels, fontsize=8.5)
ax.set_ylabel("seconds (log scale)")
ax.set_title(
    "Load-sensitive timing bounds: the base bound falls under load, the head bound holds",
    fontsize=11.0,
)
ax.legend(loc="upper left", fontsize=9.0)
ax.grid(axis="y", which="both", color="#dddddd", linewidth=0.6)
ax.set_axisbelow(True)

footnote = (
    "Base arm observed maxima read from the loaded red logs "
    "(assert ... < 2.0 / < 5.0 failures, and the ledger's storage-slow error at 36.3 s). "
    "The endless writer never quiesces, so any finite head bound still proves the settle "
    "ceiling, not the writer, ended the wait."
)
fig.text(0.01, 0.01, footnote, fontsize=7.5, color="#444444", wrap=True)

figure_path = Path(__file__).with_suffix(".png")
fig.tight_layout(rect=(0, 0.05, 1, 1))
fig.savefig(figure_path)
print(figure_path)
