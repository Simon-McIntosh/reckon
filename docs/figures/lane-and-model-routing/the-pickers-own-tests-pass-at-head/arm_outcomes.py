"""The picker population's outcome in each arm, and what the ten owned failures cost.

Two arms run the same forty-eight-file population: the base revision, and this
head. The bars carry the outcome split, so the ten picker tests this node owns
are separable from the one bound that belongs to the module-size work and from
the tests that passed in both arms. The figure is about the ten, so the ten take
the only colour; everything else is neutral.
"""

from pathlib import Path

import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent

PASSING = "#929292"
OUT_OF_SCOPE = "#3a3a3a"
OWNED = "#c0392b"

ARMS = [
    ("base 4d09842e", 368, 10, 1),
    ("head df6f0048", 379, 0, 1),
]
NOTE_X = 396

fig, ax = plt.subplots(figsize=(14.0, 2.8))

labels = [name for name, *_ in ARMS]
passing = [p for _, p, *_ in ARMS]
owned = [o for _, _, o, _ in ARMS]
other = [s for _, _, _, s in ARMS]

y = list(range(len(ARMS)))
ax.barh(y, passing, color=PASSING, height=0.5, label="passing")
ax.barh(
    y, owned, left=passing, color=OWNED, height=0.5, label="this node's picker tests"
)
ax.barh(
    y,
    other,
    left=[p + o for p, o in zip(passing, owned, strict=True)],
    color=OUT_OF_SCOPE,
    height=0.5,
    label="out of scope: the module-window bound",
)

for i, (_, p, o, _) in enumerate(ARMS):
    ax.text(p / 2, i, str(p), ha="center", va="center", color="white", fontsize=14)
    if o:
        ax.text(
            p + o / 2, i, str(o), ha="center", va="center", color="white", fontsize=14
        )

ax.text(
    NOTE_X, 0, "10 picker tests failing here", color=OWNED, va="center", fontsize=14
)
ax.text(
    NOTE_X,
    1,
    "1 failing test, out of this node's scope",
    color=OUT_OF_SCOPE,
    va="center",
    fontsize=14,
)

ax.set_yticks(y, labels, fontsize=14)
ax.set_xlabel("tests in the picker population", fontsize=16)
ax.tick_params(labelsize=14)
ax.set_xlim(0, 560)
ax.spines[["top", "right"]].set_visible(False)
ax.legend(
    frameon=False, fontsize=14, loc="upper center", bbox_to_anchor=(0.5, 1.32), ncols=3
)

fig.tight_layout()
out = HERE / "arm_outcomes.svg"
fig.savefig(out, format="svg")
print(out)
