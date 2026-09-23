"""The figure behind §4: how a review's lane is composed from its owning run.

Each column is one configuration, each row one lane. A lane is filled when the
composition may compose the review onto it, hatched when it is withheld, and
blank when it is not configured. The owning run's lane leads when it is present,
the locally served lane follows, and the configured rest fill the tail - and
never a lane named in review_excluded_backends, which is why the exclusion is
drawn as a rule across the fallback rather than as a preference.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

LANES = ["clive", "alpha", "beta", "barren"]
CONFIGS = [
    ("no exclusion,\nownership recorded", ["clive", "alpha", "beta", "barren"], []),
    ("barren excluded", ["clive", "alpha", "beta"], ["barren"]),
    ("alpha, beta excluded", ["clive"], ["alpha", "beta"]),
    (
        "all excluded but clive,\nclive already dropped it",
        [],
        ["alpha", "beta", "barren"],
    ),
]

fig, axes = plt.subplots(
    1,
    len(CONFIGS),
    figsize=(
        13,
        4.2,
    ),
    sharey=True,
)
for ax, (title, allowed, excluded) in zip(axes, CONFIGS, strict=True):
    for row, lane in enumerate(LANES):
        y = len(LANES) - 1 - row
        if lane in allowed:
            ax.barh(y, 1.0, color="#3b7dd8", edgecolor="#1b3f70")
        elif lane in excluded:
            ax.barh(y, 1.0, color="none", edgecolor="#b03030", hatch="///")
        else:
            ax.barh(y, 1.0, color="#eeeeee", edgecolor="#cccccc")
    ax.set_title(title, fontsize=9)
    ax.set_yticks(range(len(LANES)))
    ax.set_yticklabels(list(reversed(LANES)), fontsize=9)
    ax.set_xticks([])
    ax.set_xlim(0, 1.35)
    ax.text(1.02, len(LANES) - 1, "eligible", fontsize=8, color="#1b3f70", va="center")
    ax.text(1.02, len(LANES) - 2, "withheld", fontsize=8, color="#b03030", va="center")
    for spine in ax.spines.values():
        spine.set_visible(False)

axes[0].set_ylabel("configured lane", fontsize=9)
axes[0].text(
    1.02, len(LANES) - 3, "not configured", fontsize=8, color="#777777", va="center"
)
fig.suptitle(
    "Review lane composition: owning lane first, exclusions never reached",
    fontsize=11,
)
fig.tight_layout(rect=(0, 0, 1, 0.94))
out = Path(__file__).with_suffix(".png")
fig.savefig(out, dpi=150)
print("wrote", out)
