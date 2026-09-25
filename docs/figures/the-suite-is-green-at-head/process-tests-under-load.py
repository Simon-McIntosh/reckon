"""Draw the failure window and the measured result for the process-backed cases.

Left panel: the ordering that made the reload cases fail on their own trigger.
Right panel: each owned id's state alone and under a loaded ``-n 8`` run.

Run: python process-tests-under-load.py   (writes process-tests-under-load.svg)
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = Path(__file__).with_suffix(".svg")

# ---------------------------------------------------------------------------
# Left: the two orderings of one follower arming, drawn on one time axis.
# ---------------------------------------------------------------------------
fig, (left, right) = plt.subplots(1, 2, figsize=(14.5, 5.4))
fig.suptitle(
    "Process-backed cases under a loaded parallel run",
    fontsize=13,
    fontweight="bold",
)

arm, registered, baseline, change = 0.0, 0.7, 1.9, 2.2
rows = [
    ("change before\nthe baseline\n(the defect)", change, False),
    ("change after\nthe baseline\n(the repair)", baseline + 0.3, True),
]
for index, (_label, _ignored, seen) in enumerate(rows):
    y = index
    left.axhline(y, color="#d0d5dd", linewidth=1.4, zorder=1)
    left.barh(y, registered - arm, left=arm, height=0.22, color="#b9c7dd", zorder=2)
    left.barh(
        y,
        baseline - registered,
        left=registered,
        height=0.22,
        color="#f0c86a",
        zorder=2,
    )
    left.scatter([baseline], [y], marker="|", s=420, color="#444", zorder=3)
    marker_x = change if not seen else baseline + 0.3
    left.scatter(
        [marker_x],
        [y],
        marker="v",
        s=140,
        color="#b3261e" if not seen else "#1b7f3b",
        zorder=4,
    )
    left.annotate(
        "captured as the baseline:\nno reload ever seen"
        if not seen
        else "seen as a change:\nreload fires",
        xy=(marker_x, y),
        xytext=(marker_x + 0.08, y - 0.30),
        fontsize=9,
        color="#b3261e" if not seen else "#1b7f3b",
    )
left.annotate(
    "registration\nrecord written", xy=(registered, 1.42), fontsize=9, color="#333"
)
left.annotate("baseline fixed", xy=(baseline - 0.95, 1.62), fontsize=9, color="#333")
left.set_yticks(range(len(rows)), [label for label, _x, _seen in rows])
left.set_xlim(arm - 0.15, 3.6)
left.set_ylim(-0.6, 1.9)
left.set_xlabel("seconds after the case arms the follower")
left.set_title(
    "The source change must land after the follower fixes its baseline",
    fontsize=11,
)
for spine in ("top", "right", "left"):
    left.spines[spine].set_visible(False)
left.legend(
    handles=[
        plt.Rectangle((0, 0), 1, 1, color="#b9c7dd"),
        plt.Rectangle((0, 0), 1, 1, color="#f0c86a"),
    ],
    labels=["entering the stream loop", "baseline being fixed (the window)"],
    fontsize=8,
    loc="lower right",
)

# ---------------------------------------------------------------------------
# Right: the six owned ids, alone and under a concurrent full-suite load.
# ---------------------------------------------------------------------------
IDS = [
    "reloaded follower still leaves\non the original owner",
    "reload under a live subreaper\nkeeps the dead owner",
    "reload does not restart\nthe lifetime",
    "concurrent dispatches arm\nexactly one producer",
    "dispatch restarts a producer\nafter its predecessor dies",
    "shared-fixture dispatch\narms no producer",
]
# (base alone, base under load, head alone, head under load runs that passed of 3)
CELLS = [
    (0, 0, 1, 3),
    (0, 0, 1, 3),
    (0, 0, 1, 3),
    (1, 0, 1, 3),
    (1, 1, 1, 3),
    (1, 1, 1, 3),
]
right.imshow(
    [[1] * 5 for _ in IDS],
    aspect="auto",
    cmap=matplotlib.colors.ListedColormap(["#ffffff"]),
    extent=(-0.5, 4.5, len(IDS) - 0.5, -0.5),
)
for row_index, (base_alone, base_load, head_alone, head_runs) in enumerate(CELLS):
    values = [
        "pass" if base_alone else "FAIL",
        "pass" if base_load else "FAIL",
        "pass" if head_alone else "FAIL",
        f"{head_runs}/3",
    ]
    colours = [
        "#dff3e3" if base_alone else "#f8d7da",
        "#dff3e3" if base_load else "#f8d7da",
        "#dff3e3" if head_alone else "#f8d7da",
        "#dff3e3" if head_runs == 3 else "#fff4cc",
    ]
    for column, (text, colour) in enumerate(zip(values, colours, strict=True)):
        right.add_patch(
            plt.Rectangle(
                (column - 0.5, row_index - 0.5),
                1,
                1,
                facecolor=colour,
                edgecolor="#c9ced6",
                linewidth=1,
            )
        )
        right.text(
            column,
            row_index,
            text,
            ha="center",
            va="center",
            fontsize=8.5,
            color="#333",
        )
right.set_xticks(
    range(4), ["base\nalone", "base\nloaded", "head\nalone", "head\nloaded"]
)
right.set_yticks(range(len(IDS)), IDS, fontsize=8.5)
right.set_title(
    "Six owned ids: alone, and under a concurrent -n 8 full-suite run", fontsize=11
)
right.set_xlim(-0.5, 3.5)
for spine in right.spines.values():
    spine.set_visible(False)
right.tick_params(length=0)

fig.tight_layout(rect=(0, 0, 1, 0.94))
fig.savefig(OUT, format="svg")
print(f"wrote {OUT}")
