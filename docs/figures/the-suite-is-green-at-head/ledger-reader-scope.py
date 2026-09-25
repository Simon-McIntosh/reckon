"""Two panels for the ledger-reader scope repair.

Left: the read-surface test's own duration, measured at the base revision
against the whole 129-plan checkout and at head against a two-plan synthetic
tree. Right: the guard's two directions on a stand-in live directory -- a peer
session's pointer is not owned, this file's own pointer is.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT = Path(__file__).with_suffix(".png")

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.4))

# ── Left: duration, base vs head ────────────────────────────────────────────
labels = ["base\n129-plan checkout", "head\n2-plan synthetic tree"]
seconds = [155.50, 1.32]
bars = ax1.bar(labels, seconds, color=["#b0413e", "#2e7d32"], width=0.55)
ax1.set_yscale("log")
ax1.set_ylabel("test duration (s, log scale)")
ax1.set_title("read-surface test: duration follows checkout size")
for bar, value in zip(bars, seconds):
    ax1.text(
        bar.get_x() + bar.get_width() / 2,
        value * 1.15,
        f"{value:.2f}s",
        ha="center",
        va="bottom",
        fontsize=10,
    )
ax1.set_ylim(0.5, 600)
ax1.grid(axis="y", which="both", alpha=0.25)
ax1.annotate(
    "118x",
    xy=(1, 1.32),
    xytext=(0.5, 20),
    arrowprops=dict(arrowstyle="->", color="#444"),
    ha="center",
    fontsize=11,
    color="#444",
)

# ── Right: the guard's two directions ───────────────────────────────────────
cases = ["peer pointer\n(other project)", "own pointer\n(this file's run id)"]
owned = [0, 1]
colors = ["#2e7d32", "#b0413e"]
bars = ax2.bar(cases, owned, color=colors, width=0.55)
ax2.set_ylim(0, 1.6)
ax2.set_ylabel("entries the guard owns")
ax2.set_title("guard reports only its own live write")
for bar, value in zip(bars, owned):
    verdict = "ignored" if value == 0 else "reported"
    ax2.text(
        bar.get_x() + bar.get_width() / 2,
        value + 0.05,
        verdict,
        ha="center",
        va="bottom",
        fontsize=10,
    )
ax2.set_yticks([0, 1])
ax2.grid(axis="y", alpha=0.25)

fig.tight_layout()
fig.savefig(OUT, dpi=140)
print(f"wrote {OUT}")