"""Failing-id count per cause group, base 1ae39477 against the repaired head.

One horizontal bar pair per cause group. The base bar is the group's count of
FAILED ids from the base arm's own log; the head bar is the group's count at
the repaired head, which is zero for every group.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

GROUPS = [
    ("cited the base sha as its own work", 5),
    ("crew view vocabulary grew", 1),
    ("member guard needs a named member", 1),
    ("review gate preempts the ledger read", 1),
    ("two live runs shared one landing fragment", 1),
    ("aggregate ledger absent at dispatch", 2),
]

BASE = "#b5482f"
HEAD = "#2f8f5b"
OUT = Path(__file__).with_suffix(".png")

labels = [name for name, _ in GROUPS]
base_counts = [count for _, count in GROUPS]
head_counts = [0 for _ in GROUPS]
y = range(len(GROUPS))
height = 0.38

fig, ax = plt.subplots(figsize=(9.6, 4.6))
ax.barh([pos + height / 2 for pos in y], base_counts, height, color=BASE,
        label="base 1ae39477 (FAILED)")
ax.barh([pos - height / 2 for pos in y], head_counts, height, color=HEAD,
        label="repaired head (FAILED)")
ax.set_yticks(list(y))
ax.set_yticklabels(labels, fontsize=9)
ax.invert_yaxis()
ax.set_xlabel("failing node ids in tests/test_ledger.py")
ax.set_xlim(0, 6)
ax.set_title(
    "tests/test_ledger.py: 11 failing ids at base 1ae39477, 0 at the repaired head",
    fontsize=11,
)
for pos, count in zip(y, base_counts):
    ax.text(count + 0.08, pos + height / 2, str(count), va="center", fontsize=9,
            color=BASE)
for pos, count in zip(y, head_counts):
    ax.text(count + 0.08, pos - height / 2, str(count), va="center", fontsize=9,
            color=HEAD)
ax.legend(loc="lower right", fontsize=9)
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(OUT, dpi=150)
print(f"wrote {OUT}")