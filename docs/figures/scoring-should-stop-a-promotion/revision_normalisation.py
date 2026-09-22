"""One fact, five store spellings, and the fork that keeps a recorded
absence distinguishable from an unread field."""

from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SPELLINGS = (
    "reviewed_head_sha",
    "reviewed_commit",
    "commits_read",
    "reviewed_base_sha",
    "reviewed_base",
)

fig, ax = plt.subplots(figsize=(13.5, 6.2))
ax.set_axis_off()

left_x, box_w, box_h = 0.02, 0.26, 0.105
top = 0.86
centres = []
for index, name in enumerate(SPELLINGS):
    y = top - index * 0.16
    ax.add_patch(
        plt.Rectangle((left_x, y), box_w, box_h, fill=True,
                      facecolor="#eef3fb", edgecolor="#3c5a99", linewidth=1.1)
    )
    ax.text(left_x + box_w / 2, y + box_h / 2, name, ha="center", va="center",
            fontsize=11, family="monospace", color="#1b2a4a")
    centres.append((left_x + box_w, y + box_h / 2))

ax.text(left_x + box_w / 2, top + 0.09,
        "the store's five spellings (312 records)",
        ha="center", va="center", fontsize=11, weight="bold", color="#1b2a4a")

mid_x, mid_w = 0.40, 0.24
ax.add_patch(plt.Rectangle((mid_x, 0.42), mid_w, 0.30, fill=True,
                           facecolor="#dff1e3", edgecolor="#2f7d4f", linewidth=1.4))
ax.text(mid_x + mid_w / 2, 0.63, "reviewed_revision", ha="center", va="center",
        fontsize=13, weight="bold", family="monospace", color="#1d4d32")
ax.text(mid_x + mid_w / 2, 0.53,
        "the one key a guard reads", ha="center", va="center",
        fontsize=10, color="#1d4d32")
ax.text(mid_x + mid_w / 2, 0.455,
        "emitted REVISION: line wins", ha="center", va="center",
        fontsize=9, style="italic", color="#4d6b58")

for x_end, y in centres:
    ax.annotate("", xy=(mid_x, 0.5 + (y - 0.5) * 0.35), xytext=(x_end, y),
                arrowprops=dict(arrowstyle="-|>", color="#7d8ba4", linewidth=1.0,
                                shrinkA=1, shrinkB=3))

ax.text(mid_x - 0.045, 0.80, "first spelling carried\nwins (presence, never\ntruthiness)",
        ha="center", va="center", fontsize=9.5, color="#3c5a99")

ax.add_patch(plt.Rectangle((0.72, 0.60), 0.26, 0.20, fill=True,
                           facecolor="#fdf3e0", edgecolor="#b07d2b", linewidth=1.1))
ax.text(0.85, 0.725, "spelling carried, value empty",
        ha="center", va="center", fontsize=10, weight="bold", color="#6b4a12")
ax.text(0.85, 0.655, "key present, value None\n= a recorded absence",
        ha="center", va="center", fontsize=10, color="#6b4a12")

ax.add_patch(plt.Rectangle((0.72, 0.30), 0.26, 0.20, fill=True,
                           facecolor="#fbe9e9", edgecolor="#a63b3b", linewidth=1.1))
ax.text(0.85, 0.425, "no spelling carried",
        ha="center", va="center", fontsize=10, weight="bold", color="#6d2020")
ax.text(0.85, 0.355, "key absent\n= an unread field, age unknown",
        ha="center", va="center", fontsize=10, color="#6d2020")

ax.annotate("", xy=(0.72, 0.70), xytext=(mid_x + mid_w, 0.62),
            arrowprops=dict(arrowstyle="-|>", color="#b07d2b", linewidth=1.2))
ax.annotate("", xy=(0.72, 0.40), xytext=(mid_x + mid_w, 0.50),
            arrowprops=dict(arrowstyle="-|>", color="#a63b3b", linewidth=1.2))

ax.set_xlim(0, 1)
ax.set_ylim(0.2, 1.02)
fig.tight_layout()
out = pathlib.Path(__file__).with_name("revision_normalisation.png")
fig.savefig(out, dpi=170, facecolor="white")
print(out)
