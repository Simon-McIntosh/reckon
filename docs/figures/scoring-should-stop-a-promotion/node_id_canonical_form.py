"""Two gate-log spellings of one test reducing to one id, and the cases that stay apart.

The measured numbers on the right are the phantom count the comparison produced
before the reduction and the zero it produces after, with the negative control
that reverts the reduction.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE_SPELLING = "FAILED tests/test_gate.py::test_alpha"
HEAD_SPELLING = (
    "FAILED ../../home/ITER/mcintos/Code/.reckon-worktrees/\n"
    "       reckon-abc123/added/tests/test_gate.py::test_alpha"
)
NEW_SPELLING = (
    "FAILED /home/ITER/mcintos/Code/.reckon-worktrees/\n"
    "       reckon-abc123/added/tests/test_gate.py::test_beta"
)
BASE_ID = "tests/test_gate.py::test_alpha"
NEW_ID = "tests/test_gate.py::test_beta"

ROWS = (
    ("base arm, run at the repository root", BASE_SPELLING, BASE_ID, "#1b2a4a"),
    ("head arm, run from another directory", HEAD_SPELLING, BASE_ID, "#1b2a4a"),
    ("head arm, a test the base never ran", NEW_SPELLING, NEW_ID, "#8a3b12"),
)

fig, ax = plt.subplots(figsize=(13.5, 5.6))
ax.set_axis_off()
ax.text(
    0.0,
    1.0,
    "One test under two working directories reduces to one id",
    fontsize=13,
    weight="bold",
    color="#1b2a4a",
    ha="left",
    va="bottom",
)

left_x, left_w = 0.0, 0.46
mid_x = 0.60
top = 0.80
row_h = 0.15

for index, (label, spelling, canonical, colour) in enumerate(ROWS):
    y = top - index * (row_h + 0.075)
    ax.text(
        left_x,
        y + row_h + 0.022,
        label,
        fontsize=10.5,
        weight="bold",
        color="#444",
        ha="left",
    )
    ax.add_patch(
        plt.Rectangle(
            (left_x, y),
            left_w,
            row_h,
            fill=True,
            facecolor="#f4f6fa",
            edgecolor=colour,
            linewidth=1.1,
        )
    )
    ax.text(
        left_x + 0.012,
        y + row_h / 2,
        spelling,
        ha="left",
        va="center",
        fontsize=9.2,
        family="monospace",
        color=colour,
    )
    ax.annotate(
        "",
        xy=(mid_x - 0.015, y + row_h / 2),
        xytext=(left_x + left_w + 0.015, y + row_h / 2),
        arrowprops={"arrowstyle": "-|>", "color": "#3c5a99", "linewidth": 1.4},
    )
    ax.add_patch(
        plt.Rectangle(
            (mid_x, y),
            0.40,
            row_h,
            fill=True,
            facecolor="#eef3fb",
            edgecolor="#3c5a99",
            linewidth=1.2,
        )
    )
    ax.text(
        mid_x + 0.20,
        y + row_h / 2,
        canonical,
        ha="center",
        va="center",
        fontsize=10.5,
        family="monospace",
        color="#1b2a4a",
    )

ax.text(
    mid_x + 0.20,
    top + row_h + 0.022,
    "compared as",
    fontsize=10.5,
    weight="bold",
    color="#444",
    ha="center",
)

ax.text(
    left_x,
    0.16,
    "rows 1 and 2 are one id: the run added nothing, so the count is 0 and the total\n"
    "is left uncapped. Row 3 is a second id, so the count is 1 and the total is capped.",
    fontsize=10.5,
    color="#2f6b3f",
    ha="left",
    va="top",
)
ax.text(
    0.66,
    0.16,
    "measured: count 1 before, 0 after\n"
    "negative control, reduction reverted: 8 of 9 cases fail",
    fontsize=10.5,
    color="#8a3b12",
    ha="left",
    va="top",
)

fig.savefig("node_id_canonical_form.png", dpi=170, bbox_inches="tight")
print("wrote node_id_canonical_form.png")
