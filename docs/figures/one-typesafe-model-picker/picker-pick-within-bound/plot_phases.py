"""Per-phase cost of one dispatch-path pick, before and after the cut.

Each row is a phase inside ``pick()``, timed on the same fixture with the
ledger held (records and verdict inputs prebuilt) so the figure shows the
pick's own critical path rather than the dispatch pre-builds that sit outside
its bound. Values are the median of three runs, milliseconds.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# (phase, before, after) milliseconds, median of three runs, ledger held
ROWS = [
    ("candidate snapshot", 2590, 1270),
    ("return-time render", 673, 78),
    ("local-lane load", 6, 6),
]
BOUND_S = 5.0
LABEL = {"candidate snapshot": "candidate snapshot\n(git census, x2)", "return-time render": "return-time render", "local-lane load": "local-lane load"}

phases = [row[0] for row in ROWS]
before = [row[1] / 1000.0 for row in ROWS]
after = [row[2] / 1000.0 for row in ROWS]
pos = range(len(ROWS))
h = 0.36

fig, ax = plt.subplots(figsize=(14.0, 4.8), dpi=100)
bars_b = ax.barh([p + h / 2 for p in pos], before, h, color="#8c8c8c", label="before")
bars_a = ax.barh([p - h / 2 for p in pos], after, h, color="#1f6f9c", label="after")

ax.axvline(BOUND_S, color="#8c8c8c", linestyle=":", linewidth=1.2)
ax.text(BOUND_S + 0.03, len(ROWS) - 0.55, "5 s pick bound", fontsize=20,
        color="#8c8c8c", va="center")

for bars, vals in ((bars_b, before), (bars_a, after)):
    for bar, v in zip(bars, vals):
        ax.text(v + 0.03, bar.get_y() + bar.get_height() / 2,
                f"{v:.2f}", fontsize=20, va="center", color=bar.get_facecolor())

ax.set_yticks(list(pos))
ax.set_yticklabels([LABEL[p] for p in phases], fontsize=20)
ax.set_xlabel("wall time per pick [s]", fontsize=22)
ax.set_xlim(0, 3.0)
ax.tick_params(axis="x", labelsize=20)
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.legend(frameon=False, fontsize=20, loc="lower right")

fig.tight_layout()
out = Path(__file__).with_name("pick-phases-before-after.png")
fig.savefig(out)
print(out)