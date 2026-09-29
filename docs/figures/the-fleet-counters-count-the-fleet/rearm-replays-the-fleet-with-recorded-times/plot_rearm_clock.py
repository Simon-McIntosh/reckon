"""Render the re-arm clock figure: base burst vs head recorded-time list."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT = Path(__file__).with_name("rearm-clock-base-vs-head.png")

ARM = "06:42:22"
RUNS = [
    ("r-cache-replay", "04:10:05"),
    ("r-measured-map", "05:52:31"),
    ("r-gate-audit", "06:31:44"),
]

fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)

base_clock = [ARM] * len(RUNS)
base_y = list(range(len(RUNS)))

ax = axes[0]
ax.barh(base_y, [1] * len(RUNS), color="#c0392b")
for y, (name, _) in zip(base_y, RUNS):
    ax.text(0.02, y, f"{ARM}   {name}", va="center", ha="left", color="white", fontsize=9)
ax.set_title("base — every replayed row at the attach second")
ax.set_yticks([])
ax.set_xlim(0, 1)
ax.set_xticks([])

ax = axes[1]
head_clock = [clock for _, clock in RUNS]
ax.barh(base_y, [1] * len(RUNS), color="#2471a3")
for y, (name, clock) in zip(base_y, RUNS):
    ax.text(0.02, y, f"{clock}   {name}", va="center", ha="left", color="white", fontsize=9)
ax.set_title("head — one row per run at its recorded state time")
ax.set_yticks([])
ax.set_xlim(0, 1)
ax.set_xticks([])

fig.suptitle("Re-arm renders N live runs: a burst under one clock, or a fleet timeline")
fig.tight_layout()
fig.savefig(OUT, dpi=140)
print(f"wrote {OUT}")