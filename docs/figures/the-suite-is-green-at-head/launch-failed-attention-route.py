"""Where a launch-failed run is named, and the edge that was missing.

The recovery classifier emits `launch-failed`, marks it actionable, and the
ticker's action set carries it, so the fleet's blocked bucket counts it. The
watch vocabulary did not name it, so a coordinator reading the watch surface
had no route to the run. The figure shows the four vocabularies in order and
marks the repaired edge into the watch set.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

BOXES = [
    ("recovery.RECOVERY_VERBS\n→ 'launch-failed': 'resume'", True),
    ("recovery.ACTIONABLE_RECOVERY_CLASSIFICATIONS\n'launch-failed'", True),
    ("ticker.NEEDS_ACTION\n'launch-failed'", True),
    ("recovery.FLEET_BLOCKED_STATES\n(NEEDS_ACTION − WAITING_STATES)", True),
    ("runs.WATCH_ATTENTION_STATES\nmissing at base → added at head", False),
]

fig, ax = plt.subplots(figsize=(11, 2.6))
ax.set_xlim(0, len(BOXES))
ax.set_ylim(0, 1)
ax.axis("off")

w, h, y = 0.86, 0.44, 0.30
centres = []
for i, (label, carried) in enumerate(BOXES):
    x = i + 0.07
    fix = i == len(BOXES) - 1
    face = "#e8f4ea" if not fix else "#fdecea"
    edge = "#2e7d32" if not fix else "#c0392b"
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.06",
            linewidth=1.4, edgecolor=edge, facecolor=face,
        )
    )
    ax.text(x + w / 2, y + h / 2, label, ha="center", va="center", fontsize=8.2)
    centres.append(x + w / 2)

for a, b in zip(centres, centres[1:]):
    fixed = b == centres[-1]
    ax.add_patch(
        FancyArrowPatch(
            (a + w / 2, y + h / 2), (b - w / 2, y + h / 2),
            arrowstyle="-|>", mutation_scale=13,
            linewidth=2.0, color="#c0392b" if fixed else "#455a64",
            linestyle="-" if fixed else "-",
        )
    )

ax.text(
    (centres[-2] + centres[-1]) / 2, y + h + 0.06,
    "the missing route — repaired", ha="center", fontsize=8.6, color="#c0392b",
)
ax.text(
    0.5, y - 0.12,
    "A state the fleet counts as blocked but the watch vocabulary does not name "
    "has no route from the watch surface.",
    ha="left", fontsize=8.4, color="#37474f",
)

fig.tight_layout()
fig.savefig(
    "docs/figures/the-suite-is-green-at-head/launch-failed-attention-route.png",
    dpi=160,
)